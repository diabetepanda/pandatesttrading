"""Portfolio-level Round-2 6-strategy backtester & strict IS/OOS screener (S1-S3, L1-L3)."""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series
from src.strategies.six_strategies import (
    SignalAction,
    allocate_subaccount_position,
    build_symbol_base_cache,
    decide_l3_dual_momentum_step,
    decide_strategy_step,
    precompute_series,
)


@dataclass(frozen=True)
class BacktestTrade:
    """Single closed simulated trade in strategy historical backtest."""

    symbol: str
    strategy_id: str
    entry_ts_utc: str
    exit_ts_utc: str
    direction: str  # "long" or "short"
    entry_price: float
    exit_price: float
    r_multiple: float
    net_pnl_usd: float
    cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def compute_metrics(
    trades: list[BacktestTrade],
    pool_capital: float,
    equity_curve_points: list[float] | None = None,
) -> dict[str, Any]:
    """Compute trade count, win rate, expectancy, PF, min equity, and bounded max drawdown %."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "expectancy_r": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "min_equity_usd": round(pool_capital, 2),
            "ending_equity_usd": round(pool_capital, 2),
            "total_return_pct": 0.0,
            "total_pnl_usd": 0.0,
        }

    ordered = sorted(trades, key=lambda t: (t.exit_ts_utc, t.entry_ts_utc, t.symbol))
    wins = [t for t in ordered if t.net_pnl_usd > 0]
    losses = [t for t in ordered if t.net_pnl_usd <= 0]
    gross_win = sum(t.net_pnl_usd for t in wins)
    gross_loss = abs(sum(t.net_pnl_usd for t in losses))
    if gross_loss > 1e-9:
        pf = gross_win / gross_loss
    else:
        pf = 99.0 if gross_win > 0 else 0.0

    if equity_curve_points and len(equity_curve_points) > 0:
        curve = equity_curve_points
    else:
        curve = [pool_capital]
        eq = pool_capital
        for t in ordered:
            eq = max(0.01, eq + t.net_pnl_usd)
            curve.append(eq)

    peak = pool_capital
    max_dd_pct = 0.0
    min_equity = pool_capital
    for eq in curve:
        if eq > peak:
            peak = eq
        if eq < min_equity:
            min_equity = eq
        dd = max(0.0, (peak - eq) / max(1e-9, peak) * 100.0)
        if dd > max_dd_pct:
            max_dd_pct = dd

    max_dd_pct = min(99.99, max_dd_pct) if min_equity > 0 else 100.0
    total_pnl = sum(t.net_pnl_usd for t in ordered)
    avg_r = sum(t.r_multiple for t in ordered) / n
    avg_usd = total_pnl / n
    ending_equity = curve[-1]

    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "expectancy_r": round(avg_r, 4),
        "expectancy_usd": round(avg_usd, 2),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "min_equity_usd": round(min_equity, 2),
        "ending_equity_usd": round(ending_equity, 2),
        "total_return_pct": round((ending_equity - pool_capital) / max(1e-9, pool_capital) * 100.0, 2),
        "total_pnl_usd": round(total_pnl, 2),
    }


def _calc_trade_costs(
    direction: str,
    raw_entry: float,
    raw_exit: float,
    qty: float,
    entry_dt_str: str,
    exit_dt_str: str,
    fee_rate: float,
    overnight_bps: float,
) -> tuple[float, float, float]:
    """Compute net PnL USD and total costs USD including fees & overnight carry."""
    entry_notional = raw_entry * qty
    exit_notional = raw_exit * qty
    entry_fee = entry_notional * fee_rate
    exit_fee = exit_notional * fee_rate
    days = max(0.0, (_parse_dt(exit_dt_str) - _parse_dt(entry_dt_str)).total_seconds() / 86400.0)
    overnight_usd = entry_notional * (overnight_bps / 10000.0) * days
    gross = (raw_exit - raw_entry) * qty if direction == "long" else (raw_entry - raw_exit) * qty
    total_cost = entry_fee + exit_fee + overnight_usd
    return gross - total_cost, total_cost, entry_fee


def _conservative_entry_candidate_indices(
    strategy_id: str,
    series: dict[str, Any],
    params: dict[str, Any],
    allow_short: bool,
    start_bar_idx: int,
    end_bar_idx: int,
) -> list[int]:
    """Return superset of closed bar indices that can emit an entry (all verified via decide_strategy_step)."""
    closes = series["closes"]
    atr = series["atr"]
    out: list[int] = []

    if strategy_id == "L4":
        l4_sma = series["l4_sma"]
        l4_atr = series["l4_atr"]
        l4_hi = series["l4_hi_close"]
        l4_wend = series["l4_week_end"]
        for i in range(start_bar_idx, end_bar_idx):
            if not l4_wend[i]:
                continue
            sv = l4_sma[i]
            av = l4_atr[i]
            hc = l4_hi[i]
            if sv is None or av is None or av <= 0 or hc is None:
                continue
            if closes[i] > sv:
                out.append(i)
        return out

    if strategy_id == "S2":
        bo_p = int(params.get("breakout_bars", 20))
        tr_p = int(params.get("trail_bars", 8))
        bhi_s = series["s2_bo_hi"]
        blo_s = series["s2_bo_lo"]
        eff_start = max(start_bar_idx, max(bo_p, tr_p))
        for i in range(eff_start, end_bar_idx):
            av = atr[i]
            bhi = bhi_s[i]
            blo = blo_s[i]
            if av is None or av <= 0 or bhi is None or blo is None:
                continue
            c = closes[i]
            if c > bhi or (allow_short and c < blo):
                out.append(i)
        return out

    if strategy_id == "S3":
        sma_t = series["sma_trend"]
        rsi = series["rsi"]
        rsi_l = float(params.get("rsi_long_thresh", 10.0))
        rsi_s = float(params.get("rsi_short_thresh", 90.0))
        eff_allow_short = bool(params.get("allow_short", allow_short)) and allow_short
        for i in range(start_bar_idx, end_bar_idx):
            av = atr[i]
            st = sma_t[i]
            rv = rsi[i]
            if av is None or av <= 0 or st is None or rv is None:
                continue
            c = closes[i]
            if (c > st and rv < rsi_l) or (eff_allow_short and c < st and rv > rsi_s):
                out.append(i)
        return out

    if strategy_id in ("L1", "L2"):
        entry_p = int(params.get("entry_period", 20))
        exit_p = int(params.get("exit_period", 10))
        ehi = series["don_entry_hi"]
        elo = series["don_entry_lo"]
        eff_start = max(start_bar_idx, max(entry_p, exit_p))
        for i in range(eff_start, end_bar_idx):
            av = atr[i]
            hi_v = ehi[i]
            lo_v = elo[i]
            if av is None or av <= 0 or hi_v is None or lo_v is None:
                continue
            c = closes[i]
            if c > hi_v or (allow_short and c < lo_v):
                out.append(i)
        return out

    return list(range(start_bar_idx, end_bar_idx))


def simulate_portfolio_strategy(
    bars_by_symbol: dict[str, list[OHLCVBar]],
    strategy_id: str,
    params: dict[str, Any],
    instruments_by_sym: dict[str, dict[str, Any]],
    config: dict[str, Any],
    interval: str,
    allow_short: bool = True,
    min_signal_ts: str | None = None,
    base_caches: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[BacktestTrade], list[float], list[tuple[str, str, str, str, float | None]]]:
    """Chronological portfolio simulation with shared sub-account risk limits and concurrent caps."""
    strat_cfg = config.get("strategies", {}).get(strategy_id, {})
    bt_cfg = config.get("backtest", {})
    risk_cfg = config.get("risk", {})
    sub_cap = float(strat_cfg.get("capital_usd", 10000.0))
    atr_period = int(bt_cfg.get("atr_period", 14))
    slippage_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    slip = slippage_bps / 10000.0
    max_concurrent = int(bt_cfg.get("max_concurrent_positions", 5))
    eff_allow_short = bool(params.get("allow_short", allow_short)) if strategy_id == "S3" else allow_short

    eval_syms = sorted(sym for sym, bars in bars_by_symbol.items() if len(bars) >= 30)
    if not eval_syms:
        return [], [sub_cap], []

    all_ts_set: set[str] = set()
    for sym in eval_syms:
        for b in bars_by_symbol[sym]:
            all_ts_set.add(b.ts_utc)
    all_ts = sorted(all_ts_set)
    ts_to_t = {ts: t for t, ts in enumerate(all_ts)}

    sym_bidx_by_t: dict[str, dict[int, int]] = {}
    sym_t_by_bidx: dict[str, list[int]] = {}
    series_by_sym: dict[str, dict[str, Any]] = {}
    inst_params: dict[str, tuple[float, float, float]] = {}

    entry_triggers_by_t: dict[int, list[tuple[str, int]]] = defaultdict(list)

    for sym in eval_syms:
        bars = bars_by_symbol[sym]
        inst = instruments_by_sym[sym]
        inst_params[sym] = (
            float(inst.get("assumed_spread_bps", 2.0)),
            float(inst.get("fee_rate", 0.0005)),
            float(inst.get("overnight_cost_daily_bps", 0.0)),
        )
        t_list = [ts_to_t[b.ts_utc] for b in bars]
        sym_t_by_bidx[sym] = t_list
        sym_bidx_by_t[sym] = {t: idx for idx, t in enumerate(t_list)}

        bc = base_caches.get(sym) if base_caches is not None else None
        ser = precompute_series(
            strategy_id=strategy_id,
            bars=bars,
            params=params,
            market=inst.get("market", "crypto"),
            interval=interval,
            atr_period=atr_period,
            allow_short=eff_allow_short,
            base_cache=bc,
        )
        series_by_sym[sym] = ser

        start_bidx = 1
        if min_signal_ts is not None:
            for idx, b in enumerate(bars):
                if b.ts_utc >= min_signal_ts:
                    start_bidx = max(1, idx)
                    break
            else:
                start_bidx = len(bars)

        if start_bidx < len(bars) - 1:
            cand_indices = _conservative_entry_candidate_indices(
                strategy_id=strategy_id,
                series=ser,
                params=params,
                allow_short=allow_short,
                start_bar_idx=start_bidx,
                end_bar_idx=len(bars) - 1,
            )
            for bidx in cand_indices:
                entry_triggers_by_t[t_list[bidx]].append((sym, bidx))

    current_equity = sub_cap
    equity_curve: list[float] = [sub_cap]
    trades: list[BacktestTrade] = []
    trace: list[tuple[str, str, str, str, float | None]] = []

    open_pos: dict[str, dict[str, Any]] = {}
    pending_exits: dict[int, list[tuple[str, int]]] = defaultdict(list)
    pending_entries: dict[int, list[tuple[str, int, SignalAction]]] = defaultdict(list)

    for t_idx in range(len(all_ts)):
        if not open_pos and t_idx not in pending_exits and t_idx not in pending_entries and t_idx not in entry_triggers_by_t:
            continue

        # 1) Pending exits at bar open
        if t_idx in pending_exits:
            for sym, fill_bidx in pending_exits.pop(t_idx):
                if sym not in open_pos:
                    continue
                pos = open_pos.pop(sym)
                bar = bars_by_symbol[sym][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                raw_exit = (
                    bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                    if pos["dir"] == "long"
                    else bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                )
                raw_net, cost_usd, _ = _calc_trade_costs(
                    pos["dir"],
                    pos["entry_px"],
                    raw_exit,
                    pos["qty"],
                    pos["entry_ts"],
                    bar.ts_utc,
                    fee_rate,
                    overnight_bps,
                )
                net_pnl = max(-(current_equity - 0.01), raw_net)
                current_equity = max(0.01, current_equity + net_pnl)
                equity_curve.append(current_equity)
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id=strategy_id,
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        direction=pos["dir"],
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )

        # 2) Pending entries at bar open using shared allocate_subaccount_position
        if t_idx in pending_entries:
            corr_groups_cfg = config.get("correlation_groups", {})
            for sym, fill_bidx, act in pending_entries.pop(t_idx):
                if sym in open_pos:
                    continue
                inst_entry = instruments_by_sym[sym]
                corr_group = str(inst_entry.get("correlation_group") or "")
                max_grp = corr_groups_cfg.get(corr_group, {}).get("max_positions") if corr_group else None
                if max_grp is not None:
                    cur_grp_cnt = sum(
                        1 for osym in open_pos
                        if str(instruments_by_sym.get(osym, {}).get("correlation_group") or "") == corr_group
                    )
                    if cur_grp_cnt >= int(max_grp):
                        continue
                bar = bars_by_symbol[sym][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                direction = act.direction
                raw_entry = (
                    bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                    if direction == "long"
                    else bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                )
                if raw_entry <= 0:
                    continue
                open_risk_sum = sum(p["risk_usd"] for p in open_pos.values())
                open_notional_sum = sum(p["notional_usd"] for p in open_pos.values())
                alloc = allocate_subaccount_position(
                    strategy_id=strategy_id,
                    subaccount_equity_usd=current_equity,
                    open_positions_count=len(open_pos),
                    open_total_risk_usd=open_risk_sum,
                    open_total_notional_usd=open_notional_sum,
                    entry_price=raw_entry,
                    stop_loss=act.stop_loss,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    config=config,
                    risk_multiplier=act.risk_multiplier,
                )
                if not alloc.approved:
                    continue
                open_pos[sym] = {
                    "dir": direction,
                    "entry_px": raw_entry,
                    "entry_ts": bar.ts_utc,
                    "entry_bidx": fill_bidx,
                    "qty": alloc.qty,
                    "notional_usd": alloc.notional_usd,
                    "risk_usd": alloc.risk_usd,
                    "sl_px": act.stop_loss,
                    "tp_px": act.take_profit,
                }

        # 3) Intra-bar SL/TP & close-bar exit check on currently open positions
        exited_this_bar: set[str] = set()
        for sym in list(open_pos.keys()):
            bidx = sym_bidx_by_t[sym].get(t_idx)
            if bidx is None:
                continue
            bars = bars_by_symbol[sym]
            bar = bars[bidx]
            pos = open_pos[sym]
            spread_bps, fee_rate, overnight_bps = inst_params[sym]
            pos_dir = pos["dir"]
            sl_px = pos["sl_px"]
            tp_px = pos["tp_px"]

            exited = False
            raw_exit = 0.0
            if pos_dir == "long":
                if sl_px is not None:
                    open_bid = bar.open * (1.0 - spread_bps / 20000.0)
                    if open_bid <= sl_px:
                        raw_exit = open_bid * (1.0 - slip)
                        exited = True
                    elif bar.low * (1.0 - spread_bps / 20000.0) <= sl_px:
                        raw_exit = sl_px * (1.0 - slip)
                        exited = True
                if not exited and tp_px is not None:
                    if bar.high * (1.0 - spread_bps / 20000.0) >= tp_px:
                        raw_exit = tp_px * (1.0 - slip)
                        exited = True
            else:
                if sl_px is not None:
                    open_ask = bar.open * (1.0 + spread_bps / 20000.0)
                    if open_ask >= sl_px:
                        raw_exit = open_ask * (1.0 + slip)
                        exited = True
                    elif bar.high * (1.0 + spread_bps / 20000.0) >= sl_px:
                        raw_exit = sl_px * (1.0 + slip)
                        exited = True
                if not exited and tp_px is not None:
                    if bar.low * (1.0 + spread_bps / 20000.0) <= tp_px:
                        raw_exit = tp_px * (1.0 + slip)
                        exited = True

            if exited:
                open_pos.pop(sym)
                exited_this_bar.add(sym)
                raw_net, cost_usd, _ = _calc_trade_costs(
                    pos_dir,
                    pos["entry_px"],
                    raw_exit,
                    pos["qty"],
                    pos["entry_ts"],
                    bar.ts_utc,
                    fee_rate,
                    overnight_bps,
                )
                net_pnl = max(-(current_equity - 0.01), raw_net)
                current_equity = max(0.01, current_equity + net_pnl)
                equity_curve.append(current_equity)
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id=strategy_id,
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        direction=pos_dir,
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )
                continue

            if bidx < len(bars) - 1:
                bars_held = max(1, bidx - pos["entry_bidx"] + 1)
                act = decide_strategy_step(
                    strategy_id=strategy_id,
                    series=series_by_sym[sym],
                    idx=bidx,
                    position_dir=pos_dir,
                    params=params,
                    allow_short=allow_short,
                    bars_held=bars_held,
                )
                if act.action == "exit":
                    sl_round = round(act.stop_loss, 6) if act.stop_loss is not None else None
                    trace.append((sym, bar.ts_utc, act.action, act.direction, sl_round))
                    next_t = sym_t_by_bidx[sym][bidx + 1]
                    pending_exits[next_t].append((sym, bidx + 1))
                elif strategy_id == "L4":
                    ser_l4 = series_by_sym[sym]
                    hi_c = ser_l4["l4_hi_close"][bidx]
                    atr_v = ser_l4["l4_atr"][bidx]
                    k_mult = float(params.get("atr_mult", 3.0))
                    if hi_c is not None and atr_v is not None and atr_v > 0:
                        cand_sl = hi_c - k_mult * atr_v
                        if cand_sl > pos["sl_px"]:
                            pos["sl_px"] = cand_sl

        # 4) Candidate entries at close of t_idx
        if t_idx in entry_triggers_by_t and len(open_pos) < max_concurrent:
            for sym, bidx in entry_triggers_by_t[t_idx]:
                if sym in open_pos or sym in exited_this_bar:
                    continue
                bars = bars_by_symbol[sym]
                if bidx >= len(bars) - 1:
                    continue
                act = decide_strategy_step(
                    strategy_id=strategy_id,
                    series=series_by_sym[sym],
                    idx=bidx,
                    position_dir="",
                    params=params,
                    allow_short=allow_short,
                    bars_held=0,
                )
                if act.action in ("enter_long", "enter_short"):
                    sl_round = round(act.stop_loss, 6) if act.stop_loss is not None else None
                    trace.append((sym, bars[bidx].ts_utc, act.action, act.direction, sl_round))
                    next_t = sym_t_by_bidx[sym][bidx + 1]
                    pending_entries[next_t].append((sym, bidx + 1, act))

    return trades, equity_curve, trace


def simulate_single_symbol(
    bars: list[OHLCVBar],
    strategy_id: str,
    params: dict[str, Any],
    instrument: dict[str, Any],
    config: dict[str, Any],
    interval: str,
    allow_short: bool = True,
    start_idx: int = 0,
) -> tuple[list[BacktestTrade], list[tuple[str, str, str, float | None]]]:
    """Single-symbol wrapper sharing exact decide_strategy_step and allocate_subaccount_position."""
    if len(bars) < 30:
        return [], []
    sym = instrument["symbol"]
    min_ts = bars[start_idx].ts_utc if 0 < start_idx < len(bars) else None
    trades, _, full_trace = simulate_portfolio_strategy(
        bars_by_symbol={sym: bars},
        strategy_id=strategy_id,
        params=params,
        instruments_by_sym={sym: instrument},
        config=config,
        interval=interval,
        allow_short=allow_short,
        min_signal_ts=min_ts,
    )
    trace = [(ts, act, d, sl) for _, ts, act, d, sl in full_trace]
    return trades, trace


def simulate_l3_dual_momentum(
    bars_by_symbol: dict[str, list[OHLCVBar]],
    instruments_by_sym: dict[str, dict[str, Any]],
    params: dict[str, Any],
    config: dict[str, Any],
    start_date: str | None = None,
) -> tuple[list[BacktestTrade], list[float], list[tuple[str, str, str, str]]]:
    """Weekly rebalanced L3 Dual Momentum simulation across crypto & index asset classes."""
    lookback = int(params.get("lookback_days", 60))
    top_k_crypto = int(params.get("top_k_crypto", 2))
    top_k_index = int(params.get("top_k_index", 1))
    rebalance_days = int(params.get("rebalance_days", 7))
    sl_mult = float(params.get("sl_atr_mult", 2.5))
    vol_target = float(params.get("vol_target_pct", 0.015))

    bt_cfg = config.get("backtest", {})
    risk_cfg = config.get("risk", {})
    strat_cfg = config.get("strategies", {}).get("L3", {})
    atr_period = int(bt_cfg.get("atr_period", 14))
    sub_cap = float(strat_cfg.get("capital_usd", 23333.34))
    slip_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    slip = slip_bps / 10000.0

    date_bars: dict[str, dict[str, int]] = {}
    atr_lookup: dict[str, list[float | None]] = {}
    markets_by_sym: dict[str, str] = {}
    all_dates_set: set[str] = set()

    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        mkt = instruments_by_sym[sym]["market"]
        markets_by_sym[sym] = mkt
        atr_raw = compute_atr_series(bars, period=atr_period, market=mkt, interval="1d")
        atr_s: list[float | None] = [None] * len(bars)
        for k, val in enumerate(atr_raw):
            if atr_period + k < len(atr_s):
                atr_s[atr_period + k] = val
        atr_lookup[sym] = atr_s
        sym_map: dict[str, int] = {}
        for idx, b in enumerate(bars):
            d = b.ts_utc[:10]
            sym_map[d] = idx
            all_dates_set.add(d)
        date_bars[sym] = sym_map

    sorted_dates = sorted(all_dates_set)
    if len(sorted_dates) < lookback + 10:
        return [], [sub_cap], []

    start_pos = 0
    if start_date:
        for idx, d in enumerate(sorted_dates):
            if d >= start_date:
                start_pos = idx
                break

    current_equity = sub_cap
    equity_curve: list[float] = [sub_cap]
    trades: list[BacktestTrade] = []
    trace: list[tuple[str, str, str, str]] = []
    held: dict[str, dict[str, Any]] = {}

    for d_idx in range(max(1, start_pos), len(sorted_dates) - 1):
        d = sorted_dates[d_idx]
        next_d = sorted_dates[d_idx + 1]

        # Intra-day protective SL check
        for sym in list(held.keys()):
            b_idx = date_bars.get(sym, {}).get(d)
            if b_idx is None:
                continue
            bar = bars_by_symbol[sym][b_idx]
            pos = held[sym]
            spread_bps = pos["spread_bps"]
            open_bid = bar.open * (1.0 - spread_bps / 20000.0)
            sl_hit = open_bid <= pos["sl_px"] or bar.low * (1.0 - spread_bps / 20000.0) <= pos["sl_px"]
            if sl_hit:
                raw_exit = (open_bid * (1.0 - slip)) if open_bid <= pos["sl_px"] else (pos["sl_px"] * (1.0 - slip))
                raw_net, cost_usd, _ = _calc_trade_costs(
                    "long",
                    pos["entry_px"],
                    raw_exit,
                    pos["qty"],
                    pos["entry_ts"],
                    bar.ts_utc,
                    pos["fee_rate"],
                    pos["overnight_bps"],
                )
                net_pnl = max(-(current_equity - 0.01), raw_net)
                current_equity = max(0.01, current_equity + net_pnl)
                equity_curve.append(current_equity)
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id="L3",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        direction="long",
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )
                del held[sym]

        if (d_idx - start_pos) % rebalance_days != 0:
            continue

        up_to_date: dict[str, list[OHLCVBar]] = {}
        pre_atr: dict[str, float | None] = {}
        for sym, sym_map in date_bars.items():
            b_idx = sym_map.get(d)
            if b_idx is not None:
                up_to_date[sym] = bars_by_symbol[sym][: b_idx + 1]
                pre_atr[sym] = atr_lookup[sym][b_idx]

        actions = decide_l3_dual_momentum_step(
            bars_up_to_date=up_to_date,
            markets_by_sym=markets_by_sym,
            held_symbols=set(held.keys()),
            lookback_days=lookback,
            top_k_crypto=top_k_crypto,
            top_k_index=top_k_index,
            sl_atr_mult=sl_mult,
            atr_period=atr_period,
            vol_target_pct=vol_target,
            precomputed_atr_by_sym=pre_atr,
        )

        ordered_items = sorted(actions.items(), key=lambda kv: (0 if kv[1].action == "exit" else 1, kv[0]))
        for sym, act in ordered_items:
            nxt_idx = date_bars.get(sym, {}).get(next_d)
            if nxt_idx is None:
                continue
            nxt_bar = bars_by_symbol[sym][nxt_idx]
            inst = instruments_by_sym[sym]
            spread_bps = float(inst.get("assumed_spread_bps", 3.0))
            fee_rate = float(inst.get("fee_rate", 0.0005))
            overnight_bps = float(inst.get("overnight_cost_daily_bps", 2.0))

            if act.action == "exit" and sym in held:
                trace.append((d, sym, "exit", "long"))
                pos = held.pop(sym)
                raw_exit = nxt_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                raw_net, cost_usd, _ = _calc_trade_costs(
                    "long",
                    pos["entry_px"],
                    raw_exit,
                    pos["qty"],
                    pos["entry_ts"],
                    nxt_bar.ts_utc,
                    fee_rate,
                    overnight_bps,
                )
                net_pnl = max(-(current_equity - 0.01), raw_net)
                current_equity = max(0.01, current_equity + net_pnl)
                equity_curve.append(current_equity)
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id="L3",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=nxt_bar.ts_utc,
                        direction="long",
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )
            elif act.action == "enter_long" and sym not in held:
                trace.append((d, sym, "enter_long", "long"))
                raw_entry = nxt_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                if raw_entry <= 0:
                    continue
                open_risk_sum = sum(p["risk_usd"] for p in held.values())
                open_notional_sum = sum(p["notional_usd"] for p in held.values())
                alloc = allocate_subaccount_position(
                    strategy_id="L3",
                    subaccount_equity_usd=current_equity,
                    open_positions_count=len(held),
                    open_total_risk_usd=open_risk_sum,
                    open_total_notional_usd=open_notional_sum,
                    entry_price=raw_entry,
                    stop_loss=act.stop_loss or raw_entry * 0.9,
                    fee_rate=fee_rate,
                    slippage_bps=slip_bps,
                    config=config,
                    risk_multiplier=act.risk_multiplier,
                )
                if not alloc.approved:
                    continue
                held[sym] = {
                    "entry_px": raw_entry,
                    "entry_ts": nxt_bar.ts_utc,
                    "qty": alloc.qty,
                    "notional_usd": alloc.notional_usd,
                    "risk_usd": alloc.risk_usd,
                    "sl_px": act.stop_loss or raw_entry * 0.9,
                    "fee_rate": fee_rate,
                    "overnight_bps": overnight_bps,
                    "spread_bps": spread_bps,
                }

    return trades, equity_curve, trace


def _build_param_grid(strategy_id: str, strat_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    cand = strat_cfg.get("candidates", {})
    if strategy_id == "L4":
        smas = cand.get("sma_periods", [100, 200])
        mults = cand.get("atr_mults", [3.0, 5.0])
        atr_p = int(cand.get("atr_period", 20))
        high_lb = int(cand.get("high_lookback", 60))
        return [
            {
                "sma_period": int(n),
                "atr_mult": float(k),
                "atr_period": atr_p,
                "high_lookback": high_lb,
            }
            for n, k in itertools.product(smas, mults)
        ]

    if strategy_id == "S2":
        bos = cand.get("breakout_bars", [12, 20, 30])
        trs = cand.get("trail_bars", [6, 10])
        sls = cand.get("sl_atr_mults", [2.0, 2.5])
        max_h = int(cand.get("max_hold_bars", 30))
        return [
            {
                "breakout_bars": int(bo),
                "trail_bars": int(tr),
                "sl_atr_mult": float(slm),
                "max_hold_bars": max_h,
            }
            for bo, tr, slm in itertools.product(bos, trs, sls)
            if tr < bo
        ]

    if strategy_id == "S3":
        t_ma = int(cand.get("trend_ma_period", 200))
        rsis = cand.get("rsi_periods", [2, 3])
        l_ths = cand.get("rsi_long_thresholds", [10.0, 15.0])
        e_mas = cand.get("exit_ma_periods", [5, 8])
        short_modes = cand.get("allow_short_modes", [False, True])
        slm = float(cand.get("sl_atr_mult", 2.5))
        max_h = int(cand.get("max_hold_bars", 10))
        return [
            {
                "trend_ma_period": t_ma,
                "rsi_period": int(rp),
                "rsi_long_thresh": float(lt),
                "rsi_short_thresh": 100.0 - float(lt),
                "exit_ma_period": int(em),
                "allow_short": bool(sm),
                "sl_atr_mult": slm,
                "max_hold_bars": max_h,
            }
            for rp, lt, em, sm in itertools.product(rsis, l_ths, e_mas, short_modes)
        ]

    if strategy_id in ("L1", "L2"):
        ents = cand.get("entry_periods", [20, 55])
        exts = cand.get("exit_periods", [10, 20])
        sls = cand.get("sl_atr_mults", [2.5, 3.0])
        vols = cand.get("vol_target_pcts", [0.015])
        return [
            {
                "entry_period": int(en),
                "exit_period": int(ex),
                "sl_atr_mult": float(slm),
                "vol_target_pct": float(vt),
            }
            for en, ex, slm, vt in itertools.product(ents, exts, sls, vols)
            if ex < en
        ]

    if strategy_id == "L3":
        lbs = cand.get("lookback_days", [20, 60, 120])
        kc_list = cand.get("top_k_crypto_list", [2, 3])
        ki_list = cand.get("top_k_index_list", [1, 2])
        reb = int(cand.get("rebalance_days", 7))
        sls = cand.get("sl_atr_mults", [2.5, 3.0])
        return [
            {
                "lookback_days": int(lb),
                "top_k_crypto": int(kc),
                "top_k_index": int(ki),
                "rebalance_days": reb,
                "sl_atr_mult": float(slm),
                "vol_target_pct": 0.015,
            }
            for lb, kc, ki, slm in itertools.product(lbs, kc_list, ki_list, sls)
        ]

    raise ValueError(f"Unknown strategy_id: {strategy_id}")


def _history_span_years(bars: list[OHLCVBar]) -> float:
    if len(bars) < 2:
        return 0.0
    dt0 = _parse_dt(bars[0].ts_utc)
    dt1 = _parse_dt(bars[-1].ts_utc)
    return round((dt1 - dt0).total_seconds() / (86400.0 * 365.25), 4)


def _score_is_metrics(m: dict[str, Any], min_trades: int) -> float:
    if m["trades"] == 0:
        return -1e9
    sample_bonus = 100.0 if m["trades"] >= min_trades else float(m["trades"])
    return sample_bonus + m["expectancy_r"] * 10.0 + min(m["profit_factor"], 5.0) - m["max_drawdown_pct"] * 0.1


def screen_single_strategy(
    strategy_id: str,
    bars_by_interval: dict[str, dict[str, list[OHLCVBar]]],
    config: dict[str, Any],
    backup_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Portfolio-level 70/30 IS/OOS screening for one of S1-S3, L1-L3."""
    bt_cfg = config.get("backtest", {})
    strat_cfg = config.get("strategies", {})[strategy_id]
    interval = str(strat_cfg["interval"])
    symbol_whitelist = set(strat_cfg["symbols"]) if "symbols" in strat_cfg else None
    allowed_markets = set(strat_cfg.get("markets", ["crypto", "forex", "commodity", "index"]))
    allow_short = bool(strat_cfg.get("allow_short", True))
    sub_cap = float(strat_cfg.get("capital_usd", 10000.0))
    atr_period = int(bt_cfg.get("atr_period", 14))
    min_years = float(bt_cfg.get("min_history_years", 5.0))
    is_ratio = float(bt_cfg.get("in_sample_ratio", 0.7))
    min_oos_trades = int(bt_cfg.get("min_oos_trades", 30))
    max_oos_dd = float(bt_cfg.get("max_oos_drawdown_pct", 25.0))
    min_oos_pf = float(bt_cfg.get("min_oos_profit_factor", 1.3))

    if symbol_whitelist is not None:
        enabled_insts = [
            inst for inst in config.get("instruments", []) if inst.get("enabled", True) and inst["symbol"] in symbol_whitelist
        ]
    else:
        enabled_insts = [
            inst
            for inst in config.get("instruments", [])
            if inst.get("enabled", True) and inst.get("market") in allowed_markets
        ]

    inst_by_sym = {i["symbol"]: i for i in enabled_insts}
    raw_bars_map = bars_by_interval.get(interval, {})

    is_intraday = interval in ("15m", "1h", "4h")
    all_available: dict[str, list[OHLCVBar]] = {}
    years_by_sym: dict[str, float] = {}
    five_yr_syms: list[str] = []
    short_history_crypto: list[str] = []
    excluded_no_free_history: list[str] = []
    short_history_daily: list[str] = []
    active_backup_notes: list[str] = []

    for inst in enabled_insts:
        sym = inst["symbol"]
        mkt = inst["market"]
        bars = sorted(raw_bars_map.get(sym, []), key=lambda b: b.ts_utc)
        yrs = _history_span_years(bars) if bars else 0.0

        if is_intraday and mkt != "crypto":
            if yrs >= min_years and bars:
                all_available[sym] = bars
                years_by_sym[sym] = yrs
                five_yr_syms.append(sym)
                if backup_notes and sym in backup_notes:
                    active_backup_notes.append(backup_notes[sym])
            else:
                excluded_no_free_history.append(sym)
            continue

        if not bars or len(bars) < 30:
            if mkt == "crypto":
                short_history_crypto.append(sym)
            else:
                short_history_daily.append(sym)
            continue

        all_available[sym] = bars
        years_by_sym[sym] = yrs
        if yrs >= min_years:
            five_yr_syms.append(sym)
        elif mkt == "crypto":
            short_history_crypto.append(sym)
        else:
            short_history_daily.append(sym)

    eval_syms = sorted(all_available.keys()) if is_intraday else sorted(five_yr_syms)

    notes: list[str] = []
    if active_backup_notes:
        notes.extend(sorted(set(active_backup_notes)))
    if excluded_no_free_history:
        sample_ex = ", ".join(excluded_no_free_history[:5]) + (" 等" if len(excluded_no_free_history) > 5 else "")
        notes.append(f"資料不足，觀察中（{sample_ex} 共 {len(excluded_no_free_history)} 檔無 5 年分時歷史資料，不拿 1 個月資料湊數）")
    if is_intraday and short_history_crypto:
        sample_tags = ", ".join(f"{s}({years_by_sym.get(s, 0.0):.1f}y)" for s in sorted(short_history_crypto)[:5])
        more = " 等" if len(short_history_crypto) > 5 else ""
        notes.append(f"未滿 5 年加密貨幣 {len(short_history_crypto)} 檔（{sample_tags}{more}）使用全部可得資料")
    elif not is_intraday and (short_history_crypto or short_history_daily):
        cnt_excl = len(short_history_crypto) + len(short_history_daily)
        notes.append(f"日線未滿 5 年標的 {cnt_excl} 檔已排除")
    data_note = "；".join(notes)

    if not eval_syms:
        max_yrs = max(years_by_sym.values(), default=0.0)
        return {
            "strategy_id": strategy_id,
            "name": strat_cfg.get("name", strategy_id),
            "pool": strat_cfg.get("pool", "long_term"),
            "capital_usd": round(sub_cap, 2),
            "interval": interval,
            "status": "insufficient_history",
            "verification_label": "資料不足，觀察中",
            "passed": False,
            "applicable_instruments": len(enabled_insts),
            "eligible_instruments": 0,
            "excluded_no_free_history_count": len(excluded_no_free_history),
            "excluded_insufficient_count": len(excluded_no_free_history) + len(short_history_crypto) + len(short_history_daily),
            "short_history_crypto_count": len(short_history_crypto),
            "history_years": round(max_yrs, 4),
            "data_note": data_note or f"無可用 {interval} 歷史資料（資料不足，觀察中）",
            "chosen_params": None,
            "in_sample": compute_metrics([], sub_cap),
            "out_of_sample": compute_metrics([], sub_cap),
            "reason": f"資料不足，觀察中（達 {min_years:.0f} 年之標的 0 個，最長 {max_yrs:.2f} 年）",
        }

    all_timeline = sorted({b.ts_utc for sym in eval_syms for b in all_available[sym]})
    split_t_idx = max(1, min(len(all_timeline) - 1, int(len(all_timeline) * is_ratio)))
    cutoff_ts = all_timeline[split_t_idx]

    is_slices: dict[str, list[OHLCVBar]] = {
        sym: [b for b in all_available[sym] if b.ts_utc < cutoff_ts]
        for sym in eval_syms
    }
    full_eval_bars: dict[str, list[OHLCVBar]] = {sym: all_available[sym] for sym in eval_syms}

    grid = _build_param_grid(strategy_id, strat_cfg)
    best_score = -1e18
    best_params = grid[0]
    best_is_metrics = compute_metrics([], sub_cap)

    if strategy_id == "L3":
        for params in grid:
            is_trades, is_eq, _ = simulate_l3_dual_momentum(is_slices, inst_by_sym, params, config, start_date=None)
            m = compute_metrics(is_trades, sub_cap, equity_curve_points=is_eq)
            sc = _score_is_metrics(m, min_oos_trades)
            if sc > best_score:
                best_score = sc
                best_params = params
                best_is_metrics = m

        oos_start_d = cutoff_ts[:10]
        oos_trades, oos_eq, _ = simulate_l3_dual_momentum(
            full_eval_bars, inst_by_sym, best_params, config, start_date=oos_start_d
        )
        oos_metrics = compute_metrics(oos_trades, sub_cap, equity_curve_points=oos_eq)
    else:
        is_caches = {
            sym: build_symbol_base_cache(
                is_slices[sym],
                market=inst_by_sym[sym]["market"],
                interval=interval,
                atr_period=atr_period,
            )
            for sym in eval_syms
            if len(is_slices[sym]) >= 30
        }
        for params in grid:
            is_trades_all, is_eq, _ = simulate_portfolio_strategy(
                bars_by_symbol=is_slices,
                strategy_id=strategy_id,
                params=params,
                instruments_by_sym=inst_by_sym,
                config=config,
                interval=interval,
                allow_short=allow_short,
                min_signal_ts=None,
                base_caches=is_caches,
            )
            m = compute_metrics(is_trades_all, sub_cap, equity_curve_points=is_eq)
            sc = _score_is_metrics(m, min_oos_trades)
            if sc > best_score:
                best_score = sc
                best_params = params
                best_is_metrics = m

        is_caches.clear()
        oos_trades_all, oos_eq, _ = simulate_portfolio_strategy(
            bars_by_symbol=full_eval_bars,
            strategy_id=strategy_id,
            params=best_params,
            instruments_by_sym=inst_by_sym,
            config=config,
            interval=interval,
            allow_short=allow_short,
            min_signal_ts=cutoff_ts,
        )
        oos_metrics = compute_metrics(oos_trades_all, sub_cap, equity_curve_points=oos_eq)

    rep_years = round(max(years_by_sym[s] for s in eval_syms), 4)
    has_five_years = rep_years >= min_years

    fail_reasons: list[str] = []
    if not has_five_years:
        fail_reasons.append(f"歷史資料不足 {min_years:.0f} 年（可用 {rep_years:.2f} 年）")
    if oos_metrics["expectancy_r"] <= 0:
        fail_reasons.append(f"樣本外期望值 {oos_metrics['expectancy_r']:.3f}R <= 0")
    if oos_metrics["trades"] < min_oos_trades:
        fail_reasons.append(f"樣本外交易數 {oos_metrics['trades']} < {min_oos_trades}")
    if oos_metrics["profit_factor"] <= min_oos_pf:
        fail_reasons.append(f"樣本外獲利因子 {oos_metrics['profit_factor']:.2f} <= {min_oos_pf}")
    if oos_metrics["max_drawdown_pct"] >= max_oos_dd:
        fail_reasons.append(f"樣本外最大回撤 {oos_metrics['max_drawdown_pct']:.2f}% >= {max_oos_dd:.0f}%")

    passed = len(fail_reasons) == 0
    status = "ok" if has_five_years else "insufficient_history"
    verification_label = "已驗證" if passed else ("資料不足，觀察中" if not has_five_years else "觀察中（未通過驗證）")
    reason = "符合所有樣本外與歷史年數門檻" if passed else "；".join(fail_reasons)

    return {
        "strategy_id": strategy_id,
        "name": strat_cfg.get("name", strategy_id),
        "pool": strat_cfg.get("pool", "long_term"),
        "capital_usd": round(sub_cap, 2),
        "interval": interval,
        "status": status,
        "verification_label": verification_label,
        "passed": passed,
        "applicable_instruments": len(enabled_insts),
        "eligible_instruments": len(eval_syms),
        "excluded_no_free_history_count": len(excluded_no_free_history),
        "excluded_insufficient_count": (
            len(excluded_no_free_history) if is_intraday else (len(short_history_crypto) + len(short_history_daily))
        ),
        "short_history_crypto_count": len(short_history_crypto),
        "history_years": rep_years,
        "data_note": data_note,
        "chosen_params": best_params,
        "in_sample": best_is_metrics,
        "out_of_sample": oos_metrics,
        "reason": reason,
    }


def screen_all_six_strategies(
    bars_by_interval: dict[str, dict[str, list[OHLCVBar]]],
    config: dict[str, Any],
    backup_notes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Run portfolio screening sequentially for S2, S3, L1, L2, L3, L4."""
    order = ["S2", "S3", "L1", "L2", "L3", "L4"]
    strats_cfg = config.get("strategies", {})
    return [screen_single_strategy(sid, bars_by_interval, config, backup_notes=backup_notes) for sid in order if sid in strats_cfg]

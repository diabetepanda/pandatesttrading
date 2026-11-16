"""Portfolio-level 6-strategy backtester & strict IS/OOS screener (S1-S3, L1-L3)."""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series
from src.strategies.six_strategies import (
    SignalAction,
    decide_l2_rotation_step,
    decide_strategy_bar,
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


def compute_metrics(trades: list[BacktestTrade], pool_capital: float) -> dict[str, Any]:
    """Compute trade count, win rate, expectancy, profit factor, and max drawdown %."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "expectancy_r": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
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

    equity = pool_capital
    peak = pool_capital
    max_dd_pct = 0.0
    for t in ordered:
        equity += t.net_pnl_usd
        if equity > peak:
            peak = equity
        dd = (peak - equity) / max(1e-9, peak) * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    total_pnl = sum(t.net_pnl_usd for t in ordered)
    avg_r = sum(t.r_multiple for t in ordered) / n
    avg_usd = total_pnl / n

    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "expectancy_r": round(avg_r, 4),
        "expectancy_usd": round(avg_usd, 2),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "total_return_pct": round(total_pnl / max(1e-9, pool_capital) * 100.0, 2),
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
    """Simulate a single symbol using the exact shared decide_strategy_bar logic.

    Returns (closed_trades, signal_trace) where signal_trace logs every non-hold
    SignalAction emitted on closed bars so parity tests can compare 1-for-1 with live calls.
    """
    if len(bars) < 30:
        return [], []

    bt_cfg = config.get("backtest", {})
    risk_cfg = config.get("risk", {})
    strat_cfg = config.get("strategies", {}).get(strategy_id, {})
    pool_name = strat_cfg.get("pool", "short_term" if strategy_id.startswith("S") else "long_term")
    atr_period = int(bt_cfg.get("atr_period", 14))
    risk_usd = float(risk_cfg.get("max_risk_per_trade_usd", {}).get(pool_name, 250.0))
    sub_cap = float(strat_cfg.get("capital_usd", 10000.0))
    pool_cap = float(config.get("account", {}).get("pools", {}).get(pool_name, {}).get("capital_usd", 30000.0))
    max_notional = min(
        sub_cap,
        float(config.get("account", {}).get("pools", {}).get(pool_name, {}).get("target_notional_usd", 4000.0)),
    )

    spread_bps = float(instrument.get("assumed_spread_bps", 2.0))
    fee_rate = float(instrument.get("fee_rate", 0.0005))
    slippage_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    overnight_bps = float(instrument.get("overnight_cost_daily_bps", 0.0))
    slip = slippage_bps / 10000.0
    market = instrument.get("market", "crypto")

    series = precompute_series(
        strategy_id=strategy_id,
        bars=bars,
        params=params,
        market=market,
        interval=interval,
        atr_period=atr_period,
    )

    trades: list[BacktestTrade] = []
    trace: list[tuple[str, str, str, float | None]] = []

    in_pos = False
    pos_dir = ""
    entry_px = 0.0
    entry_ts = ""
    qty = 0.0
    sl_px: float | None = None
    tp_px: float | None = None

    effective_start = max(1, start_idx)
    for i in range(effective_start, len(bars) - 1):
        bar = bars[i]
        next_bar = bars[i + 1]

        # 1) Check intra-bar protective stop loss or fixed target on current closed bar
        if in_pos:
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
                net_pnl, cost_usd, _ = _calc_trade_costs(
                    pos_dir, entry_px, raw_exit, qty, entry_ts, bar.ts_utc, fee_rate, overnight_bps
                )
                trades.append(
                    BacktestTrade(
                        symbol=instrument["symbol"],
                        strategy_id=strategy_id,
                        entry_ts_utc=entry_ts,
                        exit_ts_utc=bar.ts_utc,
                        direction=pos_dir,
                        entry_price=round(entry_px, 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, risk_usd), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )
                in_pos = False
                pos_dir = ""
                continue

        # 2) Run the exact shared strategy bar decision at closed bar i
        act: SignalAction = decide_strategy_bar(
            strategy_id=strategy_id,
            series=series,
            idx=i,
            position_dir=pos_dir,
            params=params,
            allow_short=allow_short,
        )
        if act.action != "hold":
            sl_round = round(act.stop_loss, 6) if act.stop_loss is not None else None
            trace.append((bar.ts_utc, act.action, act.direction, sl_round))

        if in_pos and act.action == "exit":
            raw_exit = (
                next_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                if pos_dir == "long"
                else next_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
            )
            net_pnl, cost_usd, _ = _calc_trade_costs(
                pos_dir, entry_px, raw_exit, qty, entry_ts, next_bar.ts_utc, fee_rate, overnight_bps
            )
            trades.append(
                BacktestTrade(
                    symbol=instrument["symbol"],
                    strategy_id=strategy_id,
                    entry_ts_utc=entry_ts,
                    exit_ts_utc=next_bar.ts_utc,
                    direction=pos_dir,
                    entry_price=round(entry_px, 6),
                    exit_price=round(raw_exit, 6),
                    r_multiple=round(net_pnl / max(1e-9, risk_usd), 4),
                    net_pnl_usd=round(net_pnl, 4),
                    cost_usd=round(cost_usd, 4),
                )
            )
            in_pos = False
            pos_dir = ""

        elif not in_pos and act.action in ("enter_long", "enter_short"):
            direction = act.direction
            raw_entry = (
                next_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                if direction == "long"
                else next_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
            )
            if raw_entry <= 0:
                continue
            atr_val = series["atr"][i] or (raw_entry * 0.02)
            sl_dist = abs(raw_entry - act.stop_loss) if act.stop_loss is not None else (2.0 * atr_val)
            sl_dist_pct = max(0.002, sl_dist / raw_entry)
            cost_pct = 2.0 * (fee_rate + slippage_bps / 10000.0)
            notional = min(max_notional, risk_usd / (sl_dist_pct + cost_pct))
            if notional <= 0:
                continue

            qty = notional / raw_entry
            entry_px = raw_entry
            entry_ts = next_bar.ts_utc
            pos_dir = direction
            sl_px = act.stop_loss
            tp_px = act.take_profit
            in_pos = True

    return trades, trace


def simulate_l2_portfolio(
    bars_by_symbol: dict[str, list[OHLCVBar]],
    instruments_by_sym: dict[str, dict[str, Any]],
    params: dict[str, Any],
    config: dict[str, Any],
    start_date: str | None = None,
) -> tuple[list[BacktestTrade], list[tuple[str, str, str, str]]]:
    """Cross-sectional simulation for L2 weekly crypto momentum rotation."""
    lookback = int(params.get("lookback_days", 28))
    top_k = int(params.get("top_k", 3))
    rebalance_days = int(params.get("rebalance_days", 7))
    sl_mult = float(params.get("sl_atr_mult", 2.5))

    bt_cfg = config.get("backtest", {})
    risk_cfg = config.get("risk", {})
    strat_cfg = config.get("strategies", {}).get("L2", {})
    atr_period = int(bt_cfg.get("atr_period", 14))
    risk_usd = float(risk_cfg.get("max_risk_per_trade_usd", {}).get("long_term", 500.0))
    sub_cap = float(strat_cfg.get("capital_usd", 23333.33))
    per_pos_cap = sub_cap / max(1, top_k)
    slip_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    slip = slip_bps / 10000.0

    # Index bars by YYYY-MM-DD and precompute causal ATR lookup
    date_bars: dict[str, dict[str, int]] = {}
    atr_lookup: dict[str, list[float | None]] = {}
    all_dates_set: set[str] = set()
    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        atr_raw = compute_atr_series(bars, period=atr_period, market="crypto", interval="1d")
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
        return [], []

    start_pos = 0
    if start_date:
        for idx, d in enumerate(sorted_dates):
            if d >= start_date:
                start_pos = idx
                break

    trades: list[BacktestTrade] = []
    trace: list[tuple[str, str, str, str]] = []
    held: dict[str, dict[str, Any]] = {}  # sym -> {entry_px, entry_ts, qty, sl_px, fee_rate, overnight_bps, spread_bps}

    for d_idx in range(max(1, start_pos), len(sorted_dates) - 1):
        d = sorted_dates[d_idx]
        next_d = sorted_dates[d_idx + 1]

        # Check intra-day SL first for held coins
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
                net_pnl, cost_usd, _ = _calc_trade_costs(
                    "long", pos["entry_px"], raw_exit, pos["qty"], pos["entry_ts"], bar.ts_utc, pos["fee_rate"], pos["overnight_bps"]
                )
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id="L2",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        direction="long",
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, risk_usd), 4),
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

        actions = decide_l2_rotation_step(
            closes_up_to_date=up_to_date,
            held_symbols=set(held.keys()),
            lookback_days=lookback,
            top_k=top_k,
            sl_atr_mult=sl_mult,
            atr_period=atr_period,
            precomputed_atr_by_sym=pre_atr,
        )

        for sym, act in actions.items():
            nxt_idx = date_bars.get(sym, {}).get(next_d)
            if nxt_idx is None:
                continue
            nxt_bar = bars_by_symbol[sym][nxt_idx]
            inst = instruments_by_sym[sym]
            spread_bps = float(inst.get("assumed_spread_bps", 5.0))
            fee_rate = float(inst.get("fee_rate", 0.0005))
            overnight_bps = float(inst.get("overnight_cost_daily_bps", 3.0))

            if act.action == "exit" and sym in held:
                trace.append((d, sym, "exit", "long"))
                pos = held.pop(sym)
                raw_exit = nxt_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                net_pnl, cost_usd, _ = _calc_trade_costs(
                    "long", pos["entry_px"], raw_exit, pos["qty"], pos["entry_ts"], nxt_bar.ts_utc, fee_rate, overnight_bps
                )
                trades.append(
                    BacktestTrade(
                        symbol=sym,
                        strategy_id="L2",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=nxt_bar.ts_utc,
                        direction="long",
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        r_multiple=round(net_pnl / max(1e-9, risk_usd), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                    )
                )
            elif act.action == "enter_long" and sym not in held:
                trace.append((d, sym, "enter_long", "long"))
                raw_entry = nxt_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                if raw_entry <= 0:
                    continue
                sl_dist_pct = max(0.002, abs(raw_entry - (act.stop_loss or raw_entry * 0.95)) / raw_entry)
                cost_pct = 2.0 * (fee_rate + slippage_bps_val(risk_cfg) / 10000.0)
                notional = min(per_pos_cap, risk_usd / (sl_dist_pct + cost_pct))
                if notional <= 0:
                    continue
                held[sym] = {
                    "entry_px": raw_entry,
                    "entry_ts": nxt_bar.ts_utc,
                    "qty": notional / raw_entry,
                    "sl_px": act.stop_loss or raw_entry * 0.9,
                    "fee_rate": fee_rate,
                    "overnight_bps": overnight_bps,
                    "spread_bps": spread_bps,
                }

    return trades, trace


def slippage_bps_val(risk_cfg: dict[str, Any]) -> float:
    return float(risk_cfg.get("default_slippage_bps", 1.0))


def _build_param_grid(strategy_id: str, strat_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    cand = strat_cfg.get("candidates", {})
    if strategy_id == "S1":
        t_mas = cand.get("trend_ma_periods", [200])
        e_mas = cand.get("exit_ma_periods", [5])
        rsi_p = int(cand.get("rsi_period", 2))
        l_ths = cand.get("rsi_long_thresholds", [10.0, 15.0])
        s_ths = cand.get("rsi_short_thresholds", [90.0, 85.0])
        sls = cand.get("sl_atr_mults", [2.0, 2.5])
        grid = []
        for t_ma, e_ma, l_th, s_th, slm in itertools.product(t_mas, e_mas, l_ths, s_ths, sls):
            if (l_th == 10.0 and s_th == 90.0) or (l_th == 15.0 and s_th == 85.0):
                grid.append(
                    {
                        "trend_ma_period": int(t_ma),
                        "exit_ma_period": int(e_ma),
                        "rsi_period": rsi_p,
                        "rsi_long_thresh": float(l_th),
                        "rsi_short_thresh": float(s_th),
                        "sl_atr_mult": float(slm),
                    }
                )
        return grid

    if strategy_id == "S2":
        bb_p = int(cand.get("bb_period", 20))
        bb_std = float(cand.get("bb_std", 2.0))
        lbs = cand.get("squeeze_lookbacks", [48, 96])
        pcts = cand.get("squeeze_percentiles", [20.0, 30.0])
        sls = cand.get("sl_atr_mults", [1.2, 1.5])
        return [
            {
                "bb_period": bb_p,
                "bb_std": bb_std,
                "squeeze_lookback": int(lb),
                "squeeze_percentile": float(p),
                "sl_atr_mult": float(slm),
            }
            for lb, p, slm in itertools.product(lbs, pcts, sls)
        ]

    if strategy_id == "S3":
        r_bars = cand.get("range_bars", [32, 48])
        touches = int(cand.get("min_edge_touches", 2))
        edges = cand.get("edge_zone_pcts", [15.0, 20.0])
        rsi_p = int(cand.get("rsi_period", 14))
        os_lvls = cand.get("rsi_oversold_levels", [30.0, 35.0])
        ob_lvls = cand.get("rsi_overbought_levels", [70.0, 65.0])
        bufs = cand.get("sl_atr_buffers", [0.5])
        grid = []
        for rb, ep, os_v, ob_v, buf in itertools.product(r_bars, edges, os_lvls, ob_lvls, bufs):
            if (os_v == 30.0 and ob_v == 70.0) or (os_v == 35.0 and ob_v == 65.0):
                grid.append(
                    {
                        "range_bars": int(rb),
                        "min_edge_touches": touches,
                        "edge_zone_pct": float(ep),
                        "rsi_period": rsi_p,
                        "rsi_oversold": float(os_v),
                        "rsi_overbought": float(ob_v),
                        "sl_atr_buffer": float(buf),
                    }
                )
        return grid

    if strategy_id == "L1":
        ents = cand.get("entry_periods", [20, 55])
        exts = cand.get("exit_periods", [10, 20])
        sls = cand.get("sl_atr_mults", [2.0, 2.5, 3.0])
        return [
            {"entry_period": int(en), "exit_period": int(ex), "sl_atr_mult": float(slm)}
            for en, ex, slm in itertools.product(ents, exts, sls)
            if ex < en
        ]

    if strategy_id == "L2":
        lbs = cand.get("lookback_days", [14, 28, 56])
        ks = cand.get("top_k_list", [3, 5])
        reb = int(cand.get("rebalance_days", 7))
        sls = cand.get("sl_atr_mults", [2.5, 3.0])
        return [
            {"lookback_days": int(lb), "top_k": int(k), "rebalance_days": reb, "sl_atr_mult": float(slm)}
            for lb, k, slm in itertools.product(lbs, ks, sls)
        ]

    if strategy_id == "L3":
        mas = cand.get("ma_periods", [150, 200])
        sls = cand.get("sl_atr_mults", [2.5, 3.0])
        return [{"ma_period": int(ma), "sl_atr_mult": float(slm)} for ma, slm in itertools.product(mas, sls)]

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
) -> dict[str, Any]:
    """Portfolio-level 70/30 IS/OOS screening for one of S1-S3, L1-L3."""
    bt_cfg = config.get("backtest", {})
    strat_cfg = config.get("strategies", {})[strategy_id]
    interval = str(strat_cfg["interval"])
    allowed_markets = set(strat_cfg.get("markets", ["crypto", "forex", "commodity"]))
    allow_short = bool(strat_cfg.get("allow_short", True))
    sub_cap = float(strat_cfg.get("capital_usd", 10000.0))
    min_years = float(bt_cfg.get("min_history_years", 5.0))
    is_ratio = float(bt_cfg.get("in_sample_ratio", 0.7))
    min_oos_trades = int(bt_cfg.get("min_oos_trades", 30))
    max_oos_dd = float(bt_cfg.get("max_oos_drawdown_pct", 25.0))
    min_oos_pf = float(bt_cfg.get("min_oos_profit_factor", 1.3))

    enabled_insts = [
        inst
        for inst in config.get("instruments", [])
        if inst.get("enabled", True) and inst.get("market") in allowed_markets
    ]
    inst_by_sym = {i["symbol"]: i for i in enabled_insts}
    raw_bars_map = bars_by_interval.get(interval, {})

    # Separate instruments meeting vs below 5-year bar span
    all_available: dict[str, list[OHLCVBar]] = {}
    years_by_sym: dict[str, float] = {}
    five_yr_syms: list[str] = []
    short_history_syms: list[str] = []

    for inst in enabled_insts:
        sym = inst["symbol"]
        bars = sorted(raw_bars_map.get(sym, []), key=lambda b: b.ts_utc)
        if not bars:
            continue
        yrs = _history_span_years(bars)
        all_available[sym] = bars
        years_by_sym[sym] = yrs
        if yrs >= min_years:
            five_yr_syms.append(sym)
        else:
            short_history_syms.append(sym)

    # For long-term (L1/L2/L3), evaluate portfolio over instruments with >= 5 years history.
    # For short-term (S1/S2/S3), if no symbols reach 5 years, evaluate available intraday history honestly
    # to report real years and OOS trade count, while strictly failing the 5-year threshold.
    is_long_term = interval == "1d"
    eval_syms = five_yr_syms if (five_yr_syms or is_long_term) else sorted(all_available.keys())

    if not eval_syms:
        max_yrs = max(years_by_sym.values(), default=0.0)
        return {
            "strategy_id": strategy_id,
            "name": strat_cfg.get("name", strategy_id),
            "pool": strat_cfg.get("pool", "long_term"),
            "capital_usd": sub_cap,
            "interval": interval,
            "status": "insufficient_history",
            "passed": False,
            "applicable_instruments": len(enabled_insts),
            "eligible_instruments": 0,
            "excluded_insufficient_count": len(short_history_syms),
            "history_years": round(max_yrs, 4),
            "chosen_params": None,
            "in_sample": compute_metrics([], sub_cap),
            "out_of_sample": compute_metrics([], sub_cap),
            "reason": f"達 {min_years:.0f} 年歷史標準之標的為 0 個（最長僅 {max_yrs:.2f} 年），依規格不硬算",
        }

    # Build strict 70/30 IS/OOS splits per instrument (IS contains zero OOS bars)
    is_slices: dict[str, list[OHLCVBar]] = {}
    split_indices: dict[str, int] = {}
    for sym in eval_syms:
        bars = all_available[sym]
        split_idx = max(1, int(len(bars) * is_ratio))
        split_indices[sym] = split_idx
        is_slices[sym] = bars[:split_idx]

    grid = _build_param_grid(strategy_id, strat_cfg)
    best_score = -1e18
    best_params = grid[0]
    best_is_metrics = compute_metrics([], sub_cap)

    if strategy_id == "L2":
        all_is_dates = sorted({b.ts_utc[:10] for bars in is_slices.values() for b in bars})
        for params in grid:
            is_trades, _ = simulate_l2_portfolio(is_slices, inst_by_sym, params, config, start_date=None)
            m = compute_metrics(is_trades, sub_cap)
            sc = _score_is_metrics(m, min_oos_trades)
            if sc > best_score:
                best_score = sc
                best_params = params
                best_is_metrics = m

        # OOS on full series starting strictly at split date (first OOS date)
        full_eval_bars = {sym: all_available[sym] for sym in eval_syms}
        oos_dates = sorted({b.ts_utc[:10] for sym in eval_syms for b in full_eval_bars[sym][split_indices[sym] :]})
        oos_start_d = oos_dates[0] if oos_dates else None
        oos_trades, _ = simulate_l2_portfolio(full_eval_bars, inst_by_sym, best_params, config, start_date=oos_start_d)
        oos_metrics = compute_metrics(oos_trades, sub_cap)
    else:
        for params in grid:
            is_trades_all: list[BacktestTrade] = []
            for sym in eval_syms:
                tr_list, _ = simulate_single_symbol(
                    bars=is_slices[sym],
                    strategy_id=strategy_id,
                    params=params,
                    instrument=inst_by_sym[sym],
                    config=config,
                    interval=interval,
                    allow_short=allow_short,
                    start_idx=0,
                )
                is_trades_all.extend(tr_list)
            m = compute_metrics(is_trades_all, sub_cap)
            sc = _score_is_metrics(m, min_oos_trades)
            if sc > best_score:
                best_score = sc
                best_params = params
                best_is_metrics = m

        # Evaluate best_params ONCE on out-of-sample (indices >= split_idx)
        oos_trades_all: list[BacktestTrade] = []
        for sym in eval_syms:
            tr_list, _ = simulate_single_symbol(
                bars=all_available[sym],
                strategy_id=strategy_id,
                params=best_params,
                instrument=inst_by_sym[sym],
                config=config,
                interval=interval,
                allow_short=allow_short,
                start_idx=split_indices[sym],
            )
            oos_trades_all.extend(tr_list)
        oos_metrics = compute_metrics(oos_trades_all, sub_cap)

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
    reason = "符合所有樣本外與歷史年數門檻" if passed else "；".join(fail_reasons)

    return {
        "strategy_id": strategy_id,
        "name": strat_cfg.get("name", strategy_id),
        "pool": strat_cfg.get("pool", "long_term"),
        "capital_usd": round(sub_cap, 2),
        "interval": interval,
        "status": status,
        "passed": passed,
        "applicable_instruments": len(enabled_insts),
        "eligible_instruments": len(eval_syms),
        "excluded_insufficient_count": len(short_history_syms),
        "history_years": rep_years,
        "chosen_params": best_params,
        "in_sample": best_is_metrics,
        "out_of_sample": oos_metrics,
        "reason": reason,
    }


def screen_all_six_strategies(
    bars_by_interval: dict[str, dict[str, list[OHLCVBar]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run portfolio screening sequentially for S1, S2, S3, L1, L2, L3."""
    order = ["S1", "S2", "S3", "L1", "L2", "L3"]
    strats_cfg = config.get("strategies", {})
    return [screen_single_strategy(sid, bars_by_interval, config) for sid in order if sid in strats_cfg]

"""Strategy B historical backtester and strict IS/OOS screener (SPEC Section 12.1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series


@dataclass(frozen=True)
class BacktestTrade:
    """Single closed simulated trade in Strategy B historical backtest."""

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


def _ema_series(closes: list[float], period: int) -> list[float]:
    if not closes:
        return []
    k = 2.0 / (period + 1.0)
    out = [closes[0]]
    for val in closes[1:]:
        out.append(val * k + out[-1] * (1.0 - k))
    return out


def compute_metrics(trades: list[BacktestTrade], pool_capital: float = 80000.0) -> dict[str, Any]:
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

    wins = [t for t in trades if t.net_pnl_usd > 0]
    losses = [t for t in trades if t.net_pnl_usd <= 0]
    gross_win = sum(t.net_pnl_usd for t in wins)
    gross_loss = abs(sum(t.net_pnl_usd for t in losses))
    if gross_loss > 1e-9:
        pf = gross_win / gross_loss
    else:
        pf = 99.0 if gross_win > 0 else 0.0

    equity = pool_capital
    peak = pool_capital
    max_dd_pct = 0.0
    for t in trades:
        equity += t.net_pnl_usd
        if equity > peak:
            peak = equity
        dd = (peak - equity) / max(1e-9, peak) * 100.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    total_pnl = sum(t.net_pnl_usd for t in trades)
    avg_r = sum(t.r_multiple for t in trades) / n
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


def _simulate_candidate(
    bars: list[OHLCVBar],
    strategy_name: str,
    params: dict[str, Any],
    instrument: dict[str, Any],
    config: dict[str, Any],
    start_trade_idx: int = 0,
) -> list[BacktestTrade]:
    """Run deterministic daily simulation with fees, spread, slippage, and overnight costs."""
    if len(bars) < 60:
        return []

    sb_cfg = config.get("strategy_b", {})
    risk_cfg = config.get("risk", {})
    acct_cfg = config.get("account", {}).get("pools", {}).get("long_term", {})

    atr_period = int(sb_cfg.get("atr_period", 14))
    risk_usd = float(risk_cfg.get("max_risk_per_trade_usd", {}).get("long_term", 500.0))
    max_notional = float(sb_cfg.get("default_notional_usd", acct_cfg.get("target_notional_usd", 8000.0)))

    spread_bps = float(instrument.get("assumed_spread_bps", 2.0))
    fee_rate = float(instrument.get("fee_rate", 0.0005))
    slippage_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    overnight_bps = float(instrument.get("overnight_cost_daily_bps", 0.0))
    slip = slippage_bps / 10000.0

    market = instrument.get("market", "crypto")
    atr_raw = compute_atr_series(bars, period=atr_period, market=market, interval="1d")
    # Align ATR series to bar index i (atr_raw[0] corresponds to bar index `atr_period`)
    atr_by_idx: dict[int, float] = {atr_period + k: val for k, val in enumerate(atr_raw)}

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    sl_mult = float(params["sl_atr_mult"])

    # Precompute indicator arrays strictly up to each bar index i
    ema_fast: list[float] = []
    ema_slow: list[float] = []
    weekly_ema: list[float] = []
    if strategy_name == "ema_cross":
        ema_fast = _ema_series(closes, int(params["fast"]))
        ema_slow = _ema_series(closes, int(params["slow"]))
    elif strategy_name == "weekly_trend_atr":
        weekly_ema = _ema_series(closes, int(params["weekly_ema"]) * 5)

    warmup = max(atr_period + 5, int(params.get("period", params.get("slow", params.get("weekly_ema", 20) * 5))) + 1)
    effective_start = max(warmup, start_trade_idx)

    trades: list[BacktestTrade] = []
    in_pos = False
    pos_dir = "long"
    entry_px = 0.0
    entry_dt_str = ""
    qty = 0.0
    stop_loss = 0.0
    entry_fee = 0.0

    for i in range(effective_start, len(bars) - 1):
        bar = bars[i]
        next_bar = bars[i + 1]
        atr_val = atr_by_idx.get(i)
        if atr_val is None or atr_val <= 0:
            continue

        if in_pos:
            # 1) Gap open beyond trailing stop
            if pos_dir == "long":
                open_bid = bar.open * (1.0 - spread_bps / 20000.0)
                if open_bid <= stop_loss:
                    exit_px = open_bid * (1.0 - slip)
                    in_pos = False
                elif bar.low * (1.0 - spread_bps / 20000.0) <= stop_loss:
                    exit_px = stop_loss * (1.0 - slip)
                    in_pos = False
            else:
                open_ask = bar.open * (1.0 + spread_bps / 20000.0)
                if open_ask >= stop_loss:
                    exit_px = open_ask * (1.0 + slip)
                    in_pos = False
                elif bar.high * (1.0 + spread_bps / 20000.0) >= stop_loss:
                    exit_px = stop_loss * (1.0 + slip)
                    in_pos = False

            # 2) Trend reversal exit on closed bar -> fill next bar open
            if in_pos:
                rev = False
                if strategy_name == "donchian":
                    exit_p = max(10, int(params["period"]) // 2)
                    if pos_dir == "long" and bar.close < min(lows[i - exit_p : i]):
                        rev = True
                    elif pos_dir == "short" and bar.close > max(highs[i - exit_p : i]):
                        rev = True
                elif strategy_name == "ema_cross":
                    if pos_dir == "long" and ema_fast[i] < ema_slow[i]:
                        rev = True
                    elif pos_dir == "short" and ema_fast[i] > ema_slow[i]:
                        rev = True
                elif strategy_name == "weekly_trend_atr":
                    if pos_dir == "long" and bar.close < weekly_ema[i]:
                        rev = True
                    elif pos_dir == "short" and bar.close > weekly_ema[i]:
                        rev = True

                if rev:
                    if pos_dir == "long":
                        exit_px = next_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                    else:
                        exit_px = next_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                    in_pos = False
                    exit_dt_str = next_bar.ts_utc
                else:
                    exit_dt_str = bar.ts_utc
            else:
                exit_dt_str = bar.ts_utc

            if not in_pos:
                hold_days = max(0.0, (_parse_dt(exit_dt_str) - _parse_dt(entry_dt_str)).total_seconds() / 86400.0)
                exit_fee = (exit_px * qty) * fee_rate
                overnight_cost = (entry_px * qty) * (overnight_bps / 10000.0) * hold_days
                gross = (exit_px - entry_px) * qty if pos_dir == "long" else (entry_px - exit_px) * qty
                total_cost = entry_fee + exit_fee + overnight_cost
                net_pnl = gross - total_cost
                trades.append(
                    BacktestTrade(
                        entry_ts_utc=entry_dt_str,
                        exit_ts_utc=exit_dt_str,
                        direction=pos_dir,
                        entry_price=round(entry_px, 6),
                        exit_price=round(exit_px, 6),
                        r_multiple=round(net_pnl / max(1e-9, risk_usd), 4),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(total_cost, 4),
                    )
                )
                continue

            # Update ATR trailing stop favorably only
            if pos_dir == "long":
                stop_loss = max(stop_loss, bar.high - sl_mult * atr_val)
            else:
                stop_loss = min(stop_loss, bar.low + sl_mult * atr_val)
            continue

        # Entry check strictly on closed bar i -> execute at next_bar.open
        sig_dir: str | None = None
        if strategy_name == "donchian":
            p = int(params["period"])
            if bar.close > max(highs[i - p : i]):
                sig_dir = "long"
            elif bar.close < min(lows[i - p : i]):
                sig_dir = "short"
        elif strategy_name == "ema_cross":
            bull = ema_fast[i] > ema_slow[i]
            bear = ema_fast[i] < ema_slow[i]
            crossed_up = ema_fast[i - 1] <= ema_slow[i - 1] and bull
            crossed_dn = ema_fast[i - 1] >= ema_slow[i - 1] and bear
            touched_fast_down = any(lows[k] <= ema_fast[k] for k in range(max(0, i - 5), i))
            touched_fast_up = any(highs[k] >= ema_fast[k] for k in range(max(0, i - 5), i))
            if bull and (crossed_up or (touched_fast_down and bar.close > ema_fast[i] and bars[i - 1].close <= ema_fast[i - 1])):
                sig_dir = "long"
            elif bear and (crossed_dn or (touched_fast_up and bar.close < ema_fast[i] and bars[i - 1].close >= ema_fast[i - 1])):
                sig_dir = "short"
        elif strategy_name == "weekly_trend_atr":
            p_break = 20
            if bar.close > weekly_ema[i] and bar.close > max(highs[i - p_break : i]):
                sig_dir = "long"
            elif bar.close < weekly_ema[i] and bar.close < min(lows[i - p_break : i]):
                sig_dir = "short"

        if sig_dir is not None:
            if sig_dir == "long":
                raw_open = next_bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                sl_init = raw_open - sl_mult * atr_val
            else:
                raw_open = next_bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                sl_init = raw_open + sl_mult * atr_val

            stop_dist_pct = abs(raw_open - sl_init) / max(1e-9, raw_open)
            if stop_dist_pct <= 0:
                continue
            notional = min(max_notional, risk_usd / stop_dist_pct)
            qty = notional / raw_open
            entry_fee = notional * fee_rate
            entry_px = raw_open
            entry_dt_str = next_bar.ts_utc
            pos_dir = sig_dir
            stop_loss = sl_init
            in_pos = True

    return trades


def _build_candidate_grid(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    cand_cfg = config.get("strategy_b", {}).get("candidates", {})
    donchian_periods = cand_cfg.get("donchian_periods", [20, 55, 100])
    ema_pairs = cand_cfg.get("ema_fast_slow", [[20, 100], [50, 200]])
    weekly_emas = cand_cfg.get("weekly_ema_periods", [10, 20])
    sl_mults = cand_cfg.get("sl_atr_mults", [2.0, 2.5, 3.0])

    grid: list[tuple[str, dict[str, Any]]] = []
    for p in donchian_periods:
        for slm in sl_mults:
            grid.append(("donchian", {"period": int(p), "sl_atr_mult": float(slm)}))
    for pair in ema_pairs:
        for slm in sl_mults:
            grid.append(("ema_cross", {"fast": int(pair[0]), "slow": int(pair[1]), "sl_atr_mult": float(slm)}))
    for w_ema in weekly_emas:
        for slm in sl_mults:
            grid.append(("weekly_trend_atr", {"weekly_ema": int(w_ema), "sl_atr_mult": float(slm)}))
    return grid


def _select_best_in_sample(
    is_bars: list[OHLCVBar],
    instrument: dict[str, Any],
    config: dict[str, Any],
    pool_capital: float,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Select optimal Strategy B candidate using strictly in-sample bars ONLY."""
    grid = _build_candidate_grid(config)
    best_score = -1e18
    best_strat = grid[0][0]
    best_params = grid[0][1]
    best_metrics = compute_metrics([], pool_capital)

    for strat_name, params in grid:
        trades = _simulate_candidate(is_bars, strat_name, params, instrument, config, start_trade_idx=0)
        m = compute_metrics(trades, pool_capital=pool_capital)
        # Prefer candidates with meaningful trade sample (>= 30, or >= 15) and positive expectancy
        sample_bonus = 100.0 if m["trades"] >= 30 else (20.0 if m["trades"] >= 15 else 0.0)
        score = sample_bonus + m["expectancy_r"] * 10.0 + min(m["profit_factor"], 5.0) - m["max_drawdown_pct"] * 0.1
        if m["trades"] == 0:
            score = -1e9
        if score > best_score:
            best_score = score
            best_strat = strat_name
            best_params = params
            best_metrics = m

    return best_strat, best_params, best_metrics


def screen_instrument(
    instrument: dict[str, Any],
    bars_1d: list[OHLCVBar],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Screen one instrument using strict 70/30 IS/OOS split and Walk-Forward check."""
    symbol = instrument["symbol"]
    market = instrument.get("market", "")
    sb_cfg = config.get("strategy_b", {})
    pool_capital = float(
        config.get("account", {}).get("pools", {}).get("long_term", {}).get("capital_usd", 80000.0)
    )

    sorted_bars = sorted(bars_1d, key=lambda b: b.ts_utc)
    if len(sorted_bars) < 2:
        return {
            "symbol": symbol,
            "market": market,
            "status": "insufficient_history",
            "history_years": 0.0,
            "bar_count": len(sorted_bars),
            "passed": False,
            "chosen_strategy": None,
            "chosen_params": None,
            "in_sample": None,
            "out_of_sample": None,
            "walk_forward": None,
            "reason": "無日線歷史資料",
        }

    first_dt = _parse_dt(sorted_bars[0].ts_utc)
    last_dt = _parse_dt(sorted_bars[-1].ts_utc)
    history_years = round((last_dt - first_dt).total_seconds() / (86400.0 * 365.25), 2)
    min_years = float(sb_cfg.get("min_history_years", 5.0))

    if history_years < min_years:
        return {
            "symbol": symbol,
            "market": market,
            "status": "insufficient_history",
            "history_years": history_years,
            "bar_count": len(sorted_bars),
            "start_date_utc": sorted_bars[0].ts_utc[:10],
            "end_date_utc": sorted_bars[-1].ts_utc[:10],
            "passed": False,
            "chosen_strategy": None,
            "chosen_params": None,
            "in_sample": None,
            "out_of_sample": None,
            "walk_forward": None,
            "reason": f"資料年數不足：僅 {history_years:.2f} 年（少於 {min_years:.0f} 年要求），不硬算篩選",
        }

    is_ratio = float(sb_cfg.get("in_sample_ratio", 0.7))
    split_idx = int(len(sorted_bars) * is_ratio)
    is_bars = sorted_bars[:split_idx]

    # Step 1: Select parameters strictly on IS bars ONLY (OOS bars are NEVER passed here)
    chosen_strat, chosen_params, is_metrics = _select_best_in_sample(
        is_bars, instrument, config, pool_capital=pool_capital
    )

    # Step 2: Validate ONCE on OOS bars (with IS tail used purely as indicator warmup, trades >= split_idx)
    oos_trades = _simulate_candidate(
        sorted_bars, chosen_strat, chosen_params, instrument, config, start_trade_idx=split_idx
    )
    oos_metrics = compute_metrics(oos_trades, pool_capital=pool_capital)

    # Step 3: Walk-Forward check across rolling windows
    wf_windows = int(sb_cfg.get("walk_forward_windows", 3))
    wf_positive = 0
    wf_folds: list[dict[str, Any]] = []
    if wf_windows > 0 and len(sorted_bars) >= 600:
        fold_size = len(sorted_bars) // (wf_windows + 2)
        for w in range(wf_windows):
            train_end = fold_size * (w + 2)
            test_end = min(len(sorted_bars), train_end + fold_size)
            if test_end - train_end < 60:
                continue
            w_strat, w_params, _ = _select_best_in_sample(
                sorted_bars[:train_end], instrument, config, pool_capital=pool_capital
            )
            w_trades = _simulate_candidate(
                sorted_bars[:test_end], w_strat, w_params, instrument, config, start_trade_idx=train_end
            )
            w_m = compute_metrics(w_trades, pool_capital=pool_capital)
            if w_m["expectancy_r"] > 0 and w_m["trades"] > 0:
                wf_positive += 1
            wf_folds.append({"fold": w + 1, "strategy": w_strat, "trades": w_m["trades"], "expectancy_r": w_m["expectancy_r"]})

    # Step 4: Strict OOS pass criteria (never relaxed)
    min_oos_trades = int(sb_cfg.get("min_oos_trades", 30))
    max_oos_dd = float(sb_cfg.get("max_oos_drawdown_pct", 25.0))
    min_oos_pf = float(sb_cfg.get("min_oos_profit_factor", 1.3))

    fail_reasons: list[str] = []
    if oos_metrics["expectancy_r"] <= 0:
        fail_reasons.append(f"樣本外期望值 {oos_metrics['expectancy_r']:.3f}R <= 0")
    if oos_metrics["trades"] < min_oos_trades:
        fail_reasons.append(f"樣本外交易數 {oos_metrics['trades']} < {min_oos_trades}")
    if oos_metrics["max_drawdown_pct"] >= max_oos_dd:
        fail_reasons.append(f"樣本外最大回撤 {oos_metrics['max_drawdown_pct']:.2f}% >= {max_oos_dd}%")
    if oos_metrics["profit_factor"] <= min_oos_pf:
        fail_reasons.append(f"樣本外獲利因子 {oos_metrics['profit_factor']:.2f} <= {min_oos_pf}")

    passed = len(fail_reasons) == 0
    return {
        "symbol": symbol,
        "market": market,
        "status": "ok",
        "history_years": history_years,
        "bar_count": len(sorted_bars),
        "start_date_utc": sorted_bars[0].ts_utc[:10],
        "end_date_utc": sorted_bars[-1].ts_utc[:10],
        "split_date_utc": sorted_bars[split_idx].ts_utc[:10],
        "passed": passed,
        "chosen_strategy": chosen_strat,
        "chosen_params": chosen_params,
        "in_sample": is_metrics,
        "out_of_sample": oos_metrics,
        "walk_forward": {
            "windows_evaluated": len(wf_folds),
            "positive_windows": wf_positive,
            "folds": wf_folds,
        },
        "reason": "通過全部樣本外檢定" if passed else "；".join(fail_reasons),
    }

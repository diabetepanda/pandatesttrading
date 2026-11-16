"""Unified 6-strategy definitions (S1-S3 short-term, L1-L3 long-term).

Single source of truth: both historical backtesting and live scanning execute the
exact same step-by-step decision functions and sub-account sizing rules on closed bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series


@dataclass(frozen=True)
class SignalAction:
    """Deterministic action returned by a strategy on closed bar index `idx`."""

    action: str  # "enter_long", "enter_short", "exit", "hold"
    direction: str = ""  # "long", "short", or ""
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class SubaccountAllocation:
    """Shared sub-account risk & concurrent position allocation result (backtest == live)."""

    approved: bool
    risk_usd: float
    notional_usd: float
    qty: float
    reason: str


def allocate_subaccount_position(
    strategy_id: str,
    subaccount_equity_usd: float,
    open_positions_count: int,
    open_total_risk_usd: float,
    open_total_notional_usd: float,
    entry_price: float,
    stop_loss: float | None,
    fee_rate: float,
    slippage_bps: float,
    config: dict[str, Any],
    risk_multiplier: float = 1.0,
) -> SubaccountAllocation:
    """Compute sub-account position size and enforce concurrent risk/position limits.

    Called identically by both the portfolio backtester and RiskManager live sizing.
    """
    if subaccount_equity_usd <= 1e-6:
        return SubaccountAllocation(False, 0.0, 0.0, 0.0, f"{strategy_id} 子帳戶資金不足（<= 0）")
    if entry_price <= 0:
        return SubaccountAllocation(False, 0.0, 0.0, 0.0, "進場價格無效（<= 0）")

    bt_cfg = config.get("backtest", {})
    max_concurrent = int(bt_cfg.get("max_concurrent_positions", 5))
    if open_positions_count >= max_concurrent:
        return SubaccountAllocation(
            False,
            0.0,
            0.0,
            0.0,
            f"{strategy_id} 同時持倉檔數已達上限 ({open_positions_count}/{max_concurrent})",
        )

    risk_pct = float(bt_cfg.get("risk_per_trade_pct", 0.75)) / 100.0
    max_total_risk_pct = min(0.06, float(bt_cfg.get("max_total_open_risk_pct", 5.0)) / 100.0)
    max_pos_notional_pct = float(bt_cfg.get("max_position_notional_pct", 19.0)) / 100.0
    max_total_notional_pct = min(0.98, max_concurrent * max_pos_notional_pct)

    max_total_risk_usd = subaccount_equity_usd * max_total_risk_pct
    remaining_risk_usd = max(0.0, max_total_risk_usd - max(0.0, open_total_risk_usd))
    if remaining_risk_usd <= 1e-6:
        return SubaccountAllocation(
            False,
            0.0,
            0.0,
            0.0,
            f"{strategy_id} 同時持倉總風險已達子帳戶 {max_total_risk_pct * 100:.1f}% 上限",
        )

    desired_risk_usd = subaccount_equity_usd * risk_pct * max(0.0, risk_multiplier)
    risk_budget_usd = min(desired_risk_usd, remaining_risk_usd)
    if risk_budget_usd <= 1e-6:
        return SubaccountAllocation(False, 0.0, 0.0, 0.0, f"{strategy_id} 風險預算為 0")

    sl_dist = abs(entry_price - stop_loss) if stop_loss is not None else (entry_price * 0.05)
    sl_dist_pct = max(0.002, sl_dist / entry_price)
    cost_pct = 2.0 * (max(0.0, fee_rate) + max(0.0, slippage_bps) / 10000.0)
    eff_stop_pct = sl_dist_pct + cost_pct

    raw_notional = risk_budget_usd / eff_stop_pct
    max_single_notional = subaccount_equity_usd * max_pos_notional_pct
    max_total_notional = subaccount_equity_usd * max_total_notional_pct
    remaining_notional = max(0.0, max_total_notional - max(0.0, open_total_notional_usd))

    notional = min(raw_notional, max_single_notional, remaining_notional)
    if notional <= 1e-6:
        return SubaccountAllocation(
            False,
            0.0,
            0.0,
            0.0,
            f"{strategy_id} 子帳戶可用名目額度不足",
        )

    actual_risk_usd = notional * eff_stop_pct
    if open_total_risk_usd + actual_risk_usd > max_total_risk_usd + 1e-4:
        capped_risk = max(0.0, max_total_risk_usd - open_total_risk_usd)
        notional = min(notional, capped_risk / eff_stop_pct)
        actual_risk_usd = notional * eff_stop_pct
        if notional <= 1e-6:
            return SubaccountAllocation(
                False,
                0.0,
                0.0,
                0.0,
                f"{strategy_id} 同時持倉總風險已達子帳戶 {max_total_risk_pct * 100:.1f}% 上限",
            )

    qty = notional / entry_price
    return SubaccountAllocation(
        approved=True,
        risk_usd=actual_risk_usd,
        notional_usd=notional,
        qty=qty,
        reason="ok",
    )


def _sma_series_np(closes_arr: np.ndarray, period: int) -> list[float | None]:
    n = len(closes_arr)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    win = sliding_window_view(closes_arr, period)
    vals = win.mean(axis=1).tolist()
    for k, v in enumerate(vals):
        out[period - 1 + k] = float(v)
    return out


def _rsi_series(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = (100.0 if avg_gain > 0 else 50.0) if avg_loss <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    inv_p = 1.0 / period
    p_m1 = period - 1
    for g_idx in range(period, len(gains)):
        avg_gain = (avg_gain * p_m1 + gains[g_idx]) * inv_p
        avg_loss = (avg_loss * p_m1 + losses[g_idx]) * inv_p
        b_idx = g_idx + 1
        if avg_loss <= 1e-12:
            out[b_idx] = 100.0 if avg_gain > 0 else 50.0
        else:
            out[b_idx] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _atr_aligned(bars: list[OHLCVBar], period: int, market: str, interval: str) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    raw = compute_atr_series(bars, period=period, market=market, interval=interval)
    for k, val in enumerate(raw):
        idx = period + k
        if idx < len(out):
            out[idx] = val
    return out


def _rolling_extrema(arr: np.ndarray, window: int) -> tuple[list[float | None], list[float | None]]:
    """Compute strictly prior `[idx - window : idx]` rolling max and min."""
    n = len(arr)
    hi_out: list[float | None] = [None] * n
    lo_out: list[float | None] = [None] * n
    if window <= 0 or n <= window:
        return hi_out, lo_out
    w = sliding_window_view(arr[:-1], window)
    maxs = w.max(axis=1).tolist()
    mins = w.min(axis=1).tolist()
    for k, (mx, mn) in enumerate(zip(maxs, mins)):
        hi_out[window + k] = float(mx)
        lo_out[window + k] = float(mn)
    return hi_out, lo_out


def _bb_series_np(
    closes_arr: np.ndarray,
    period: int,
    num_std: float,
    lookback: int,
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    n = len(closes_arr)
    mid_s: list[float | None] = [None] * n
    up_s: list[float | None] = [None] * n
    lo_s: list[float | None] = [None] * n
    bw_pct_s: list[float | None] = [None] * n
    if n < period or period <= 0:
        return mid_s, up_s, lo_s, bw_pct_s

    w = sliding_window_view(closes_arr, period)
    sma = w.mean(axis=1)
    std = w.std(axis=1, ddof=0)
    valid = sma > 1e-12
    up = sma + num_std * std
    lo = sma - num_std * std
    bw = np.full(len(sma), np.nan, dtype=np.float64)
    bw[valid] = (up[valid] - lo[valid]) / sma[valid]

    for k in range(len(sma)):
        if valid[k]:
            idx = period - 1 + k
            mid_s[idx] = float(sma[k])
            up_s[idx] = float(up[k])
            lo_s[idx] = float(lo[k])

    # Bandwidth percentile over trailing lookback (vectorized when window >= lookback + 1)
    win_len = lookback + 1
    if len(bw) >= win_len:
        wbw = sliding_window_view(bw, win_len)
        hist = wbw[:, :-1]
        cur = wbw[:, -1:]
        finite_hist = np.isfinite(hist)
        counts = finite_hist.sum(axis=1)
        below = np.where(finite_hist & (hist < cur), 1, 0).sum(axis=1)
        cur_ok = np.isfinite(cur[:, 0]) & (counts > 0)
        for j in range(len(wbw)):
            if cur_ok[j]:
                idx = period - 1 + lookback + j
                bw_pct_s[idx] = float(below[j] / counts[j] * 100.0)

    # Initial ramp indices [period .. period + lookback - 1]
    upper_ramp = min(len(bw), lookback)
    for k in range(1, upper_ramp):
        cur_bw = bw[k]
        if not np.isfinite(cur_bw):
            continue
        hist_slice = bw[:k]
        finite_vals = hist_slice[np.isfinite(hist_slice)]
        if len(finite_vals) > 0:
            bw_pct_s[period - 1 + k] = float(np.sum(finite_vals < cur_bw) / len(finite_vals) * 100.0)

    return mid_s, up_s, lo_s, bw_pct_s


def _s3_box_series_np(
    highs_arr: np.ndarray,
    lows_arr: np.ndarray,
    rng_bars: int,
    edge_pct: float,
) -> tuple[list[float | None], list[float | None], list[int], list[int]]:
    n = len(highs_arr)
    box_hi_s: list[float | None] = [None] * n
    box_lo_s: list[float | None] = [None] * n
    up_touches_s: list[int] = [0] * n
    lo_touches_s: list[int] = [0] * n
    if n <= rng_bars or rng_bars <= 0:
        return box_hi_s, box_lo_s, up_touches_s, lo_touches_s

    wh = sliding_window_view(highs_arr[:-1], rng_bars)
    wl = sliding_window_view(lows_arr[:-1], rng_bars)
    bhi = wh.max(axis=1)
    blo = wl.min(axis=1)
    hgt = bhi - blo
    up_thresh = bhi - edge_pct * hgt
    lo_thresh = blo + edge_pct * hgt
    ut = (wh >= up_thresh[:, None]).sum(axis=1).tolist()
    lt = (wl <= lo_thresh[:, None]).sum(axis=1).tolist()
    bhi_l = bhi.tolist()
    blo_l = blo.tolist()

    for k in range(len(bhi_l)):
        idx = rng_bars + k
        box_hi_s[idx] = float(bhi_l[k])
        box_lo_s[idx] = float(blo_l[k])
        up_touches_s[idx] = int(ut[k])
        lo_touches_s[idx] = int(lt[k])

    return box_hi_s, box_lo_s, up_touches_s, lo_touches_s


def build_symbol_base_cache(
    bars: list[OHLCVBar],
    market: str = "crypto",
    interval: str = "1d",
    atr_period: int = 14,
) -> dict[str, Any]:
    """Build reusable raw numpy arrays & ATR cache per symbol across parameter grid search."""
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    return {
        "bars": bars,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "closes_arr": np.asarray(closes, dtype=np.float64),
        "highs_arr": np.asarray(highs, dtype=np.float64),
        "lows_arr": np.asarray(lows, dtype=np.float64),
        "atr": _atr_aligned(bars, period=atr_period, market=market, interval=interval),
        "sma_cache": {},
        "rsi_cache": {},
        "bb_cache": {},
        "extrema_cache": {},
        "s3_cache": {},
    }


def precompute_series(
    strategy_id: str,
    bars: list[OHLCVBar],
    params: dict[str, Any],
    market: str = "crypto",
    interval: str = "1d",
    atr_period: int = 14,
    base_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Precompute strictly causal indicator arrays for one symbol."""
    base = base_cache or build_symbol_base_cache(bars, market=market, interval=interval, atr_period=atr_period)
    closes_arr: np.ndarray = base["closes_arr"]
    highs_arr: np.ndarray = base["highs_arr"]
    lows_arr: np.ndarray = base["lows_arr"]

    data: dict[str, Any] = {
        "bars": base["bars"],
        "closes": base["closes"],
        "highs": base["highs"],
        "lows": base["lows"],
        "atr": base["atr"],
    }

    if strategy_id == "S1":
        trend_p = int(params.get("trend_ma_period", 200))
        exit_p = int(params.get("exit_ma_period", 5))
        rsi_p = int(params.get("rsi_period", 2))
        if trend_p not in base["sma_cache"]:
            base["sma_cache"][trend_p] = _sma_series_np(closes_arr, trend_p)
        if exit_p not in base["sma_cache"]:
            base["sma_cache"][exit_p] = _sma_series_np(closes_arr, exit_p)
        if rsi_p not in base["rsi_cache"]:
            base["rsi_cache"][rsi_p] = _rsi_series(base["closes"], rsi_p)
        data["sma_trend"] = base["sma_cache"][trend_p]
        data["sma_exit"] = base["sma_cache"][exit_p]
        data["rsi"] = base["rsi_cache"][rsi_p]

    elif strategy_id == "S2":
        bb_p = int(params.get("bb_period", 20))
        bb_std = float(params.get("bb_std", 2.0))
        sq_lb = int(params.get("squeeze_lookback", 48))
        bb_key = (bb_p, bb_std, sq_lb)
        if bb_key not in base["bb_cache"]:
            base["bb_cache"][bb_key] = _bb_series_np(closes_arr, bb_p, bb_std, sq_lb)
        mid_s, up_s, lo_s, bw_pct_s = base["bb_cache"][bb_key]
        if ("high", bb_p) not in base["extrema_cache"]:
            bhi, _ = _rolling_extrema(highs_arr, bb_p)
            _, blo = _rolling_extrema(lows_arr, bb_p)
            base["extrema_cache"][("high", bb_p)] = bhi
            base["extrema_cache"][("low", bb_p)] = blo
        data["bb_mid"] = mid_s
        data["bb_upper"] = up_s
        data["bb_lower"] = lo_s
        data["bb_bw_pct"] = bw_pct_s
        data["s2_box_hi"] = base["extrema_cache"][("high", bb_p)]
        data["s2_box_lo"] = base["extrema_cache"][("low", bb_p)]

    elif strategy_id == "S3":
        rsi_p = int(params.get("rsi_period", 14))
        rng_bars = int(params.get("range_bars", 48))
        edge_pct = float(params.get("edge_zone_pct", 15.0)) / 100.0
        if rsi_p not in base["rsi_cache"]:
            base["rsi_cache"][rsi_p] = _rsi_series(base["closes"], rsi_p)
        s3_key = (rng_bars, edge_pct)
        if s3_key not in base["s3_cache"]:
            base["s3_cache"][s3_key] = _s3_box_series_np(highs_arr, lows_arr, rng_bars, edge_pct)
        box_hi_s, box_lo_s, ut_s, lt_s = base["s3_cache"][s3_key]
        data["rsi"] = base["rsi_cache"][rsi_p]
        data["s3_box_hi"] = box_hi_s
        data["s3_box_lo"] = box_lo_s
        data["s3_up_touches"] = ut_s
        data["s3_lo_touches"] = lt_s

    elif strategy_id == "L1":
        entry_p = int(params.get("entry_period", 20))
        exit_p = int(params.get("exit_period", 10))
        for p in (entry_p, exit_p):
            if ("high", p) not in base["extrema_cache"]:
                bhi, _ = _rolling_extrema(highs_arr, p)
                _, blo = _rolling_extrema(lows_arr, p)
                base["extrema_cache"][("high", p)] = bhi
                base["extrema_cache"][("low", p)] = blo
        data["l1_entry_hi"] = base["extrema_cache"][("high", entry_p)]
        data["l1_entry_lo"] = base["extrema_cache"][("low", entry_p)]
        data["l1_exit_hi"] = base["extrema_cache"][("high", exit_p)]
        data["l1_exit_lo"] = base["extrema_cache"][("low", exit_p)]

    elif strategy_id == "L3":
        ma_p = int(params.get("ma_period", 200))
        if ma_p not in base["sma_cache"]:
            base["sma_cache"][ma_p] = _sma_series_np(closes_arr, ma_p)
        data["sma_trend"] = base["sma_cache"][ma_p]

    return data


def decide_strategy_step(
    strategy_id: str,
    series: dict[str, Any],
    idx: int,
    position_dir: str,
    params: dict[str, Any],
    allow_short: bool = True,
) -> SignalAction:
    """Single shared step decision function used identically by backtest and live execution."""
    closes = series["closes"]
    atr_val = series["atr"][idx]
    close = closes[idx]

    if atr_val is None or atr_val <= 0:
        return SignalAction(action="hold", reason="ATR 尚未就緒")

    if strategy_id == "S1":
        sma_t = series["sma_trend"][idx]
        sma_e = series["sma_exit"][idx]
        rsi_v = series["rsi"][idx]
        if sma_t is None or sma_e is None or rsi_v is None:
            return SignalAction(action="hold", reason="S1 均線或 RSI(2) 暖機中")
        if position_dir == "long":
            if close > sma_e:
                return SignalAction(
                    action="exit",
                    direction="long",
                    reason=f"S1 收盤 {close:g} 站上 {int(params.get('exit_ma_period', 5))} 期均線出場",
                )
            return SignalAction(action="hold")
        if position_dir == "short":
            if close < sma_e:
                return SignalAction(
                    action="exit",
                    direction="short",
                    reason=f"S1 收盤 {close:g} 跌破 {int(params.get('exit_ma_period', 5))} 期均線出場",
                )
            return SignalAction(action="hold")

        rsi_l = float(params.get("rsi_long_thresh", 10.0))
        rsi_s = float(params.get("rsi_short_thresh", 90.0))
        sl_mult = float(params.get("sl_atr_mult", 2.0))
        if close > sma_t and rsi_v < rsi_l:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"S1 價格於長均線之上且 RSI(2)={rsi_v:.1f} < {rsi_l}",
            )
        if allow_short and close < sma_t and rsi_v > rsi_s:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"S1 價格於長均線之下且 RSI(2)={rsi_v:.1f} > {rsi_s}",
            )
        return SignalAction(action="hold", reason="S1 未觸及 RSI(2) 回檔條件")

    if strategy_id == "S2":
        bb_p = int(params.get("bb_period", 20))
        sq_pct = float(params.get("squeeze_percentile", 25.0))
        sl_mult = min(1.5, float(params.get("sl_atr_mult", 1.5)))
        mid_v = series["bb_mid"][idx]
        up_v = series["bb_upper"][idx]
        lo_v = series["bb_lower"][idx]
        if idx < bb_p + 2 or mid_v is None or up_v is None or lo_v is None:
            return SignalAction(action="hold", reason="S2 布林帶指標暖機中")

        if position_dir == "long":
            if close < mid_v:
                return SignalAction(action="exit", direction="long", reason="S2 收盤跌破布林中軌出場")
            return SignalAction(action="hold")
        if position_dir == "short":
            if close > mid_v:
                return SignalAction(action="exit", direction="short", reason="S2 收盤站上布林中軌出場")
            return SignalAction(action="hold")

        prev_bw_pct = series["bb_bw_pct"][idx - 1]
        if prev_bw_pct is None or prev_bw_pct > sq_pct:
            return SignalAction(action="hold", reason="S2 布林帶寬度尚未進入低波動收斂")

        box_hi = series["s2_box_hi"][idx]
        box_lo = series["s2_box_lo"][idx]
        if box_hi is None or box_lo is None:
            return SignalAction(action="hold", reason="S2 區間邊界暖機中")

        if close > box_hi and close > up_v:
            sl_px = max(box_lo, close - sl_mult * atr_val)
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=sl_px,
                reason=f"S2 低波動收斂 ({prev_bw_pct:.1f}%) 後向上突破區間上緣",
            )
        if allow_short and close < box_lo and close < lo_v:
            sl_px = min(box_hi, close + sl_mult * atr_val)
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=sl_px,
                reason=f"S2 低波動收斂 ({prev_bw_pct:.1f}%) 後向下跌破區間下緣",
            )
        return SignalAction(action="hold", reason="S2 收斂等待突破")

    if strategy_id == "S3":
        rng_bars = int(params.get("range_bars", 48))
        min_touches = int(params.get("min_edge_touches", 2))
        edge_pct = float(params.get("edge_zone_pct", 15.0)) / 100.0
        rsi_os = float(params.get("rsi_oversold", 35.0))
        rsi_ob = float(params.get("rsi_overbought", 65.0))
        sl_buf = float(params.get("sl_atr_buffer", 0.5))
        rsi_v = series["rsi"][idx]
        box_hi = series["s3_box_hi"][idx]
        box_lo = series["s3_box_lo"][idx]
        if idx < rng_bars or rsi_v is None or box_hi is None or box_lo is None:
            return SignalAction(action="hold", reason="S3 箱型視窗或 RSI 暖機中")

        height = box_hi - box_lo
        if height <= 0:
            return SignalAction(action="hold", reason="S3 區間高度無效")

        if position_dir == "long":
            if close >= box_hi - 0.10 * height:
                return SignalAction(action="exit", direction="long", reason="S3 抵達箱型上緣停利出場")
            return SignalAction(action="hold")
        if position_dir == "short":
            if close <= box_lo + 0.10 * height:
                return SignalAction(action="exit", direction="short", reason="S3 抵達箱型下緣停利出場")
            return SignalAction(action="hold")

        up_touches = series["s3_up_touches"][idx]
        lo_touches = series["s3_lo_touches"][idx]
        if up_touches < min_touches or lo_touches < min_touches:
            return SignalAction(action="hold", reason=f"S3 非有效箱型（上下緣觸碰 {up_touches}/{lo_touches} 次）")

        up_thresh = box_hi - edge_pct * height
        lo_thresh = box_lo + edge_pct * height
        if box_lo <= close <= lo_thresh and rsi_v < rsi_os:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=box_lo - sl_buf * atr_val,
                take_profit=box_hi - 0.10 * height,
                reason=f"S3 靠近箱底且 RSI={rsi_v:.1f} < {rsi_os} 做多",
            )
        if allow_short and up_thresh <= close <= box_hi and rsi_v > rsi_ob:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=box_hi + sl_buf * atr_val,
                take_profit=box_lo + 0.10 * height,
                reason=f"S3 靠近箱頂且 RSI={rsi_v:.1f} > {rsi_ob} 做空",
            )
        return SignalAction(action="hold", reason="S3 箱內未達邊緣與超買超賣條件")

    if strategy_id == "L1":
        entry_p = int(params.get("entry_period", 20))
        exit_p = int(params.get("exit_period", 10))
        sl_mult = float(params.get("sl_atr_mult", 2.5))
        entry_hi = series["l1_entry_hi"][idx]
        entry_lo = series["l1_entry_lo"][idx]
        exit_hi = series["l1_exit_hi"][idx]
        exit_lo = series["l1_exit_lo"][idx]
        if idx < max(entry_p, exit_p) or entry_hi is None or exit_lo is None:
            return SignalAction(action="hold", reason="L1 唐奇安通道暖機中")

        if position_dir == "long":
            if close < exit_lo:
                return SignalAction(
                    action="exit",
                    direction="long",
                    reason=f"L1 跌破 {exit_p} 日反向低點 {exit_lo:g} 出場",
                )
            return SignalAction(action="hold")
        if position_dir == "short":
            if exit_hi is not None and close > exit_hi:
                return SignalAction(
                    action="exit",
                    direction="short",
                    reason=f"L1 站上 {exit_p} 日反向高點 {exit_hi:g} 出場",
                )
            return SignalAction(action="hold")

        if close > entry_hi:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"L1 收盤 {close:g} 突破 {entry_p} 日唐奇安上緣 {entry_hi:g}",
            )
        if allow_short and entry_lo is not None and close < entry_lo:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"L1 收盤 {close:g} 跌破 {entry_p} 日唐奇安下緣 {entry_lo:g}",
            )
        return SignalAction(action="hold", reason="L1 未突破唐奇安通道")

    if strategy_id == "L3":
        ma_p = int(params.get("ma_period", 200))
        sl_mult = float(params.get("sl_atr_mult", 2.5))
        sma_v = series["sma_trend"][idx]
        if sma_v is None:
            return SignalAction(action="hold", reason=f"L3 {ma_p} 日均線暖機中")

        if position_dir == "long":
            if close < sma_v:
                return SignalAction(
                    action="exit",
                    direction="long",
                    reason=f"L3 收盤跌破 {ma_p} 日均線 {sma_v:g} 出場",
                )
            return SignalAction(action="hold")
        if position_dir == "short":
            if close > sma_v:
                return SignalAction(
                    action="exit",
                    direction="short",
                    reason=f"L3 收盤站上 {ma_p} 日均線 {sma_v:g} 出場",
                )
            return SignalAction(action="hold")

        # Require clean crossover across SMA (or first warm-up bar) to prevent endless post-SL re-entry chop
        prev_sma = series["sma_trend"][idx - 1] if idx > 0 else None
        prev_close = closes[idx - 1] if idx > 0 else None
        if close > sma_v and (prev_sma is None or (prev_close is not None and prev_close <= prev_sma)):
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"L3 收盤 {close:g} 站上 {ma_p} 日均線 {sma_v:g} 做多",
            )
        if allow_short and close < sma_v and (prev_sma is None or (prev_close is not None and prev_close >= prev_sma)):
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"L3 收盤 {close:g} 跌破 {ma_p} 日均線 {sma_v:g} 做空",
            )
        return SignalAction(action="hold")

    raise ValueError(f"Unsupported single-symbol strategy_id: {strategy_id}")


def decide_l2_rotation_step(
    closes_up_to_date: dict[str, list[OHLCVBar]],
    held_symbols: set[str],
    lookback_days: int,
    top_k: int,
    sl_atr_mult: float,
    atr_period: int = 14,
    precomputed_atr_by_sym: dict[str, float | None] | None = None,
) -> dict[str, SignalAction]:
    """Shared cross-sectional L2 weekly momentum rotation step (used identically by backtest & live)."""
    ranked: list[tuple[float, str, float, float]] = []
    for sym, bars in sorted(closes_up_to_date.items()):
        if len(bars) <= lookback_days or len(bars) < atr_period + 2:
            continue
        cur_close = bars[-1].close
        past_close = bars[-1 - lookback_days].close
        if past_close <= 0:
            continue
        if precomputed_atr_by_sym is not None and sym in precomputed_atr_by_sym:
            atr_val = precomputed_atr_by_sym[sym]
        else:
            atr_series = compute_atr_series(bars, period=atr_period, market="crypto", interval="1d")
            atr_val = atr_series[-1] if atr_series else None
        if atr_val is None or atr_val <= 0:
            continue
        ret = (cur_close / past_close) - 1.0
        ranked.append((ret, sym, cur_close, atr_val))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = {sym for ret, sym, _, _ in ranked[:top_k] if ret > 0}

    actions: dict[str, SignalAction] = {}
    for sym in sorted(held_symbols):
        if sym not in selected:
            actions[sym] = SignalAction(
                action="exit",
                direction="long",
                reason=f"L2 每週輪動調出（跌出前 {top_k} 強或轉負）",
            )
    for ret, sym, cur_close, atr_val in ranked[:top_k]:
        if ret <= 0 or sym in held_symbols:
            continue
        actions[sym] = SignalAction(
            action="enter_long",
            direction="long",
            stop_loss=cur_close - sl_atr_mult * atr_val,
            reason=f"L2 過去 {lookback_days} 日報酬 +{ret * 100:.1f}% 入選前 {top_k} 強",
        )
    return actions


def evaluate_live_bar(
    strategy_id: str,
    closed_bars: list[OHLCVBar],
    position_dir: str,
    params: dict[str, Any],
    market: str = "crypto",
    interval: str = "1d",
    allow_short: bool = True,
    atr_period: int = 14,
) -> SignalAction:
    """Live evaluation helper wrapping the exact same precompute + decide_strategy_step."""
    if not closed_bars:
        return SignalAction(action="hold", reason="無已收盤 K 線")
    series = precompute_series(
        strategy_id=strategy_id,
        bars=closed_bars,
        params=params,
        market=market,
        interval=interval,
        atr_period=atr_period,
    )
    return decide_strategy_step(
        strategy_id=strategy_id,
        series=series,
        idx=len(closed_bars) - 1,
        position_dir=position_dir,
        params=params,
        allow_short=allow_short,
    )

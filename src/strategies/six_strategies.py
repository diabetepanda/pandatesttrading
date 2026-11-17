"""Unified Round-2 6-strategy engine (S1-S3 short-term, L1-L3 long-term).

Single source of truth: both historical portfolio backtesting and live scheduled
execution invoke the exact same step decision and sub-account sizing functions
on strictly closed OHLCV bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series
from src.signals.models import _fmt_num


@dataclass(frozen=True)
class SignalAction:
    """Deterministic action returned by a strategy on closed bar index `idx`."""

    action: str  # "enter_long", "enter_short", "exit", "hold"
    direction: str = ""  # "long", "short", or ""
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_multiplier: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class SubaccountAllocation:
    """Shared sub-account risk & concurrent position allocation result (backtest == live)."""

    approved: bool
    risk_usd: float
    notional_usd: float
    qty: float
    reason: str


def compute_volatility_risk_multiplier(
    atr_val: float | None,
    close_price: float,
    params: dict[str, Any],
) -> float:
    """Scale risk down for higher-volatility instruments when vol_target_pct is configured."""
    vol_target = float(params.get("vol_target_pct", 0.0) or 0.0)
    if vol_target <= 0.0 or atr_val is None or atr_val <= 0.0 or close_price <= 0.0:
        return 1.0
    atr_pct = atr_val / close_price
    return min(1.0, max(0.20, vol_target / max(1e-6, atr_pct)))


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

    risk_pct = float(bt_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
    max_total_risk_pct = float(bt_cfg.get("max_total_open_risk_pct", 10.0)) / 100.0
    max_pos_notional_pct = float(bt_cfg.get("max_position_notional_pct", 500.0)) / 100.0
    max_total_notional_mult = float(
        bt_cfg.get("max_total_notional_multiple", max_concurrent * max_pos_notional_pct)
    )

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
    max_total_notional = subaccount_equity_usd * max_total_notional_mult
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


def _rolling_max_inclusive(arr: np.ndarray, window: int) -> list[float | None]:
    """Compute inclusive `[idx - window + 1 : idx + 1]` rolling max."""
    n = len(arr)
    out: list[float | None] = [None] * n
    if window <= 0 or n < window:
        return out
    w = sliding_window_view(arr, window)
    maxs = w.max(axis=1).tolist()
    for k, mx in enumerate(maxs):
        out[window - 1 + k] = float(mx)
    return out


def _is_l4_week_end(bars: list[OHLCVBar], idx: int, market: str) -> bool:
    """Return True if bars[idx] is the final closed daily bar of its ISO week (Sun crypto / Fri non-crypto)."""
    dt = datetime.strptime(bars[idx].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if market == "crypto":
        if idx + 1 < len(bars):
            dt_next = datetime.strptime(bars[idx + 1].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return dt.isocalendar()[:2] != dt_next.isocalendar()[:2]
        return dt.weekday() == 6
    if dt.weekday() == 4:
        return True
    if dt.weekday() < 4 and idx + 1 < len(bars):
        dt_next = datetime.strptime(bars[idx + 1].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.isocalendar()[:2] != dt_next.isocalendar()[:2]
    return False


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
        "atr_cache": {},
        "max_close_cache": {},
        "sma_cache": {},
        "rsi_cache": {},
        "extrema_cache": {},
    }


def precompute_series(
    strategy_id: str,
    bars: list[OHLCVBar],
    params: dict[str, Any],
    market: str = "crypto",
    interval: str = "1d",
    atr_period: int = 14,
    allow_short: bool = True,
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

    if strategy_id == "S2":
        bo_p = int(params.get("breakout_bars", 20))
        tr_p = int(params.get("trail_bars", 8))
        for p in (bo_p, tr_p):
            if ("high", p) not in base["extrema_cache"]:
                bhi, _ = _rolling_extrema(highs_arr, p)
                _, blo = _rolling_extrema(lows_arr, p)
                base["extrema_cache"][("high", p)] = bhi
                base["extrema_cache"][("low", p)] = blo
        data["s2_bo_hi"] = base["extrema_cache"][("high", bo_p)]
        data["s2_bo_lo"] = base["extrema_cache"][("low", bo_p)]
        data["s2_tr_hi"] = base["extrema_cache"][("high", tr_p)]
        data["s2_tr_lo"] = base["extrema_cache"][("low", tr_p)]

    elif strategy_id == "S3":
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

    elif strategy_id in ("L1", "L2"):
        entry_p = int(params.get("entry_period", 20))
        exit_p = int(params.get("exit_period", 10))
        for p in (entry_p, exit_p):
            if ("high", p) not in base["extrema_cache"]:
                bhi, _ = _rolling_extrema(highs_arr, p)
                _, blo = _rolling_extrema(lows_arr, p)
                base["extrema_cache"][("high", p)] = bhi
                base["extrema_cache"][("low", p)] = blo
        data["don_entry_hi"] = base["extrema_cache"][("high", entry_p)]
        data["don_entry_lo"] = base["extrema_cache"][("low", entry_p)]
        data["don_exit_hi"] = base["extrema_cache"][("high", exit_p)]
        data["don_exit_lo"] = base["extrema_cache"][("low", exit_p)]

    elif strategy_id == "L4":
        sma_p = int(params.get("sma_period", 100))
        atr_p = int(params.get("atr_period", 20))
        high_lb = int(params.get("high_lookback", 60))
        if sma_p not in base["sma_cache"]:
            base["sma_cache"][sma_p] = _sma_series_np(closes_arr, sma_p)
        if atr_p not in base["atr_cache"]:
            base["atr_cache"][atr_p] = _atr_aligned(base["bars"], period=atr_p, market=market, interval=interval)
        if high_lb not in base["max_close_cache"]:
            base["max_close_cache"][high_lb] = _rolling_max_inclusive(closes_arr, high_lb)
        if "l4_week_end" not in base:
            base["l4_week_end"] = [_is_l4_week_end(base["bars"], i, market) for i in range(len(base["bars"]))]
        data["l4_sma"] = base["sma_cache"][sma_p]
        data["l4_atr"] = base["atr_cache"][atr_p]
        data["l4_hi_close"] = base["max_close_cache"][high_lb]
        data["l4_week_end"] = base["l4_week_end"]

    return data


def decide_strategy_step(
    strategy_id: str,
    series: dict[str, Any],
    idx: int,
    position_dir: str,
    params: dict[str, Any],
    allow_short: bool = True,
    bars_held: int = 0,
) -> SignalAction:
    """Single shared step decision function used identically by backtest and live execution."""
    closes = series["closes"]
    close = closes[idx]

    if strategy_id == "L4":
        sma_p = int(params.get("sma_period", 100))
        k_mult = float(params.get("atr_mult", 3.0))
        sma_v = series["l4_sma"][idx]
        atr_v = series["l4_atr"][idx]
        hi_c = series["l4_hi_close"][idx]
        is_wend = bool(series["l4_week_end"][idx])
        if sma_v is None or atr_v is None or atr_v <= 0 or hi_c is None:
            return SignalAction(action="hold", reason="L4 週線趨勢指標暖機中")

        trailing_cand = hi_c - k_mult * atr_v
        if position_dir == "long":
            if is_wend and close < sma_v:
                return SignalAction(
                    action="exit",
                    direction="long",
                    stop_loss=trailing_cand,
                    reason=f"L4 週末日線收盤 {close:g} 跌破 {sma_p} 日均線 {sma_v:g} 出場",
                )
            return SignalAction(
                action="hold",
                stop_loss=trailing_cand,
                reason="L4 持有中（每日更新移動停損）",
            )

        if not is_wend:
            return SignalAction(action="hold", reason="L4 非週末收盤，等待週線結算判斷")
        if close > sma_v:
            sl_px = close - k_mult * atr_v
            if sl_px <= 0:
                return SignalAction(action="hold", reason="L4 初始止損價 <= 0，略過進場")
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=sl_px,
                reason=f"L4 週末日線收盤 {close:g} 站上 {sma_p} 日均線 {sma_v:g}",
            )
        return SignalAction(
            action="hold",
            reason=f"L4 週末日線收盤 {close:g} 未高於 {sma_p} 日均線 {sma_v:g}",
        )

    atr_val = series["atr"][idx]
    if atr_val is None or atr_val <= 0:
        return SignalAction(action="hold", reason="ATR 尚未就緒")

    if strategy_id == "S2":
        # Strategy S2: 4h Channel Breakout + ATR SL + Trailing Exit + Multi-day Hold
        bo_p = int(params.get("breakout_bars", 20))
        tr_p = int(params.get("trail_bars", 8))
        sl_mult = float(params.get("sl_atr_mult", 2.0))
        max_hold = int(params.get("max_hold_bars", 30))

        bo_hi = series["s2_bo_hi"][idx]
        bo_lo = series["s2_bo_lo"][idx]
        tr_hi = series["s2_tr_hi"][idx]
        tr_lo = series["s2_tr_lo"][idx]
        if idx < max(bo_p, tr_p) or bo_hi is None or bo_lo is None or tr_hi is None or tr_lo is None:
            return SignalAction(action="hold", reason="S2 4H 突破通道暖機中")

        if position_dir == "long":
            if bars_held >= max_hold:
                return SignalAction(action="exit", direction="long", reason=f"S2 持倉達上限 {max_hold} 根 4H 時間止損")
            if close < tr_lo:
                return SignalAction(action="exit", direction="long", reason=f"S2 跌破近 {tr_p} 根 4H 移動停利低點 {tr_lo:g}")
            return SignalAction(action="hold")
        if position_dir == "short":
            if bars_held >= max_hold:
                return SignalAction(action="exit", direction="short", reason=f"S2 持倉達上限 {max_hold} 根 4H 時間止損")
            if close > tr_hi:
                return SignalAction(action="exit", direction="short", reason=f"S2 站上近 {tr_p} 根 4H 移動停利高點 {tr_hi:g}")
            return SignalAction(action="hold")

        if close > bo_hi:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"S2 4H 收盤 {close:g} 突破近 {bo_p} 根高點 {bo_hi:g}",
            )
        if allow_short and close < bo_lo:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"S2 4H 收盤 {close:g} 跌破近 {bo_p} 根低點 {bo_lo:g}",
            )
        return SignalAction(action="hold", reason="S2 未突破 4H 通道")

    if strategy_id == "S3":
        # Strategy S3: Daily Mean-Reversion Pullback above 200 SMA, max hold 10 days
        sma_t = series["sma_trend"][idx]
        sma_e = series["sma_exit"][idx]
        rsi_v = series["rsi"][idx]
        max_hold = int(params.get("max_hold_bars", 10))
        if sma_t is None or sma_e is None or rsi_v is None:
            return SignalAction(action="hold", reason="S3 日線 200 均線或短期 RSI 暖機中")

        if position_dir == "long":
            if bars_held >= max_hold:
                return SignalAction(action="exit", direction="long", reason=f"S3 持倉達最長 {max_hold} 天出場")
            if close > sma_e:
                return SignalAction(
                    action="exit",
                    direction="long",
                    reason=f"S3 收盤 {close:g} 站回 {int(params.get('exit_ma_period', 5))} 日均線出場",
                )
            return SignalAction(action="hold")
        if position_dir == "short":
            if bars_held >= max_hold:
                return SignalAction(action="exit", direction="short", reason=f"S3 持倉達最長 {max_hold} 天出場")
            if close < sma_e:
                return SignalAction(
                    action="exit",
                    direction="short",
                    reason=f"S3 收盤 {close:g} 跌回 {int(params.get('exit_ma_period', 5))} 日均線出場",
                )
            return SignalAction(action="hold")

        rsi_p = int(params.get("rsi_period", 2))
        rsi_l = float(params.get("rsi_long_thresh", 10.0))
        rsi_s = float(params.get("rsi_short_thresh", 90.0))
        sl_mult = float(params.get("sl_atr_mult", 2.5))
        eff_allow_short = bool(params.get("allow_short", allow_short)) and allow_short

        if close > sma_t and rsi_v < rsi_l:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"S3 日線在 {int(params.get('trend_ma_period', 200))} 均線上且 RSI({rsi_p})={rsi_v:.1f} < {rsi_l}",
            )
        if eff_allow_short and close < sma_t and rsi_v > rsi_s:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"S3 日線在 {int(params.get('trend_ma_period', 200))} 均線下且 RSI({rsi_p})={rsi_v:.1f} > {rsi_s}",
            )
        return SignalAction(action="hold", reason="S3 未達日線超買／超賣條件")

    if strategy_id in ("L1", "L2"):
        # L1 & L2: Donchian Channel Breakout + Volatility-Adjusted Risk Sizing
        entry_p = int(params.get("entry_period", 20))
        exit_p = int(params.get("exit_period", 10))
        sl_mult = float(params.get("sl_atr_mult", 2.5))
        entry_hi = series["don_entry_hi"][idx]
        entry_lo = series["don_entry_lo"][idx]
        exit_hi = series["don_exit_hi"][idx]
        exit_lo = series["don_exit_lo"][idx]
        if idx < max(entry_p, exit_p) or entry_hi is None or exit_lo is None:
            return SignalAction(action="hold", reason=f"{strategy_id} 唐奇安通道暖機中")

        if position_dir == "long":
            if close < exit_lo:
                return SignalAction(
                    action="exit",
                    direction="long",
                    reason=f"{strategy_id} 跌破 {exit_p} 日反向低點 {exit_lo:g} 出場",
                )
            return SignalAction(action="hold")
        if position_dir == "short":
            if exit_hi is not None and close > exit_hi:
                return SignalAction(
                    action="exit",
                    direction="short",
                    reason=f"{strategy_id} 站上 {exit_p} 日反向高點 {exit_hi:g} 出場",
                )
            return SignalAction(action="hold")

        risk_mult = compute_volatility_risk_multiplier(atr_val, close, params)
        if close > entry_hi:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                risk_multiplier=risk_mult,
                reason=f"{strategy_id} 日線突破 {entry_p} 日高點 {entry_hi:g}（波動係數 {risk_mult:.2f}）",
            )
        if allow_short and entry_lo is not None and close < entry_lo:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                risk_multiplier=risk_mult,
                reason=f"{strategy_id} 日線跌破 {entry_p} 日低點 {entry_lo:g}（波動係數 {risk_mult:.2f}）",
            )
        return SignalAction(action="hold", reason=f"{strategy_id} 未突破 Donchian 通道")

    raise ValueError(f"Unsupported single-symbol strategy_id: {strategy_id}")


def decide_l3_dual_momentum_step(
    bars_up_to_date: dict[str, list[OHLCVBar]],
    markets_by_sym: dict[str, str],
    held_symbols: set[str],
    lookback_days: int,
    top_k_crypto: int,
    top_k_index: int,
    sl_atr_mult: float,
    atr_period: int = 14,
    vol_target_pct: float = 0.015,
    precomputed_atr_by_sym: dict[str, float | None] | None = None,
) -> dict[str, SignalAction]:
    """Shared L3 Dual Momentum weekly rebalancing decision (absolute > 0 AND relative top-K per asset class)."""
    grouped: dict[str, list[tuple[float, str, float, float]]] = {}

    for sym, bars in sorted(bars_up_to_date.items()):
        if len(bars) <= lookback_days or len(bars) < atr_period + 2:
            continue
        cur_close = bars[-1].close
        past_close = bars[-1 - lookback_days].close
        if past_close <= 0:
            continue
        mkt = markets_by_sym.get(sym, "crypto")
        if precomputed_atr_by_sym is not None and sym in precomputed_atr_by_sym:
            atr_val = precomputed_atr_by_sym[sym]
        else:
            atr_series = compute_atr_series(bars, period=atr_period, market=mkt, interval="1d")
            atr_val = atr_series[-1] if atr_series else None
        if atr_val is None or atr_val <= 0:
            continue
        ret = (cur_close / past_close) - 1.0
        grouped.setdefault(mkt, []).append((ret, sym, cur_close, atr_val))

    selected_meta: dict[str, tuple[float, float, float]] = {}
    for mkt, items in grouped.items():
        items.sort(key=lambda x: (-x[0], x[1]))
        top_k = top_k_index if mkt == "index" else top_k_crypto
        for ret, sym, cur_close, atr_val in items[: max(0, top_k)]:
            if ret > 0.0:
                selected_meta[sym] = (ret, cur_close, atr_val)

    actions: dict[str, SignalAction] = {}
    for sym in sorted(held_symbols):
        if sym not in selected_meta:
            actions[sym] = SignalAction(
                action="exit",
                direction="long",
                reason="L3 每週雙動能調出（未入選同類前 K 名或過去報酬轉負）",
            )

    for sym, (ret, cur_close, atr_val) in sorted(selected_meta.items()):
        if sym in held_symbols:
            continue
        risk_mult = compute_volatility_risk_multiplier(atr_val, cur_close, {"vol_target_pct": vol_target_pct})
        mkt = markets_by_sym.get(sym, "crypto")
        top_k = top_k_index if mkt == "index" else top_k_crypto
        actions[sym] = SignalAction(
            action="enter_long",
            direction="long",
            stop_loss=cur_close - sl_atr_mult * atr_val,
            risk_multiplier=risk_mult,
            reason=f"L3 雙動能通過：過去 {lookback_days} 日報酬 +{ret * 100:.1f}% > 0 且為 {mkt} 前 {top_k} 強",
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
    bars_held: int = 0,
) -> SignalAction:
    """Live evaluation helper wrapping the exact same precompute + decide_strategy_step."""
    if not closed_bars:
        return SignalAction(action="hold", reason="無已收盤 K 線")
    eff_allow_short = bool(params.get("allow_short", allow_short)) if strategy_id == "S3" else allow_short
    series = precompute_series(
        strategy_id=strategy_id,
        bars=closed_bars,
        params=params,
        market=market,
        interval=interval,
        atr_period=atr_period,
        allow_short=eff_allow_short,
    )
    return decide_strategy_step(
        strategy_id=strategy_id,
        series=series,
        idx=len(closed_bars) - 1,
        position_dir=position_dir,
        params=params,
        allow_short=allow_short,
        bars_held=bars_held,
    )


def _fmt_price_str(val: float) -> str:
    return _fmt_num(val)


def compute_strategy_exit_info(
    strategy_id: str,
    closed_bars: list[OHLCVBar],
    direction: str,
    params: dict[str, Any],
    market: str = "crypto",
    interval: str = "1d",
    atr_period: int = 14,
) -> tuple[str, float | None]:
    """Return plain-Chinese exit rule sentence and live trailing exit line price (for S2, L1, L2, L4)."""
    if strategy_id == "S3":
        exit_ma_p = int(params.get("exit_ma_period", 5))
        max_hold = int(params.get("max_hold_bars", 10))
        verb = "站回" if direction == "long" else "跌回"
        return f"止損，日線收盤{verb} {exit_ma_p} 日均線，或最多持有 {max_hold} 天時間出場", None

    if strategy_id == "L3":
        lookback = int(params.get("lookback_days", 60))
        top_k = int(params.get("top_k_index", 1) if market == "index" else params.get("top_k_crypto", 2))
        return f"止損，或每週雙動能調出（過去 {lookback} 天報酬轉負或跌出同類前 {top_k} 名）", None

    trailing_px: float | None = None
    if closed_bars:
        series = precompute_series(
            strategy_id=strategy_id,
            bars=closed_bars,
            params=params,
            market=market,
            interval=interval,
            atr_period=atr_period,
            allow_short=True,
        )
        idx = len(closed_bars) - 1
        if strategy_id == "S2":
            raw = series["s2_tr_lo"][idx] if direction == "long" else series["s2_tr_hi"][idx]
            trailing_px = float(raw) if raw is not None else None
        elif strategy_id in ("L1", "L2"):
            raw = series["don_exit_lo"][idx] if direction == "long" else series["don_exit_hi"][idx]
            trailing_px = float(raw) if raw is not None else None
        elif strategy_id == "L4":
            hi_c = series["l4_hi_close"][idx]
            atr_v = series["l4_atr"][idx]
            k_mult = float(params.get("atr_mult", 3.0))
            trailing_px = float(hi_c - k_mult * atr_v) if (hi_c is not None and atr_v is not None and atr_v > 0) else None

    if strategy_id == "S2":
        trail_p = int(params.get("trail_bars", 8))
        max_hold = int(params.get("max_hold_bars", 30))
        side_txt = "低點" if direction == "long" else "高點"
        px_txt = f" {_fmt_price_str(trailing_px)}" if trailing_px is not None else ""
        return f"止損，4H 收盤跌破近 {trail_p} 根移動出場線{px_txt}，或最多持有 {max_hold} 根 4H K 線時間出場" if direction == "long" else (
            f"止損，4H 收盤站上近 {trail_p} 根移動出場線{px_txt}，或最多持有 {max_hold} 根 4H K 線時間出場"
        ), trailing_px

    if strategy_id in ("L1", "L2"):
        exit_p = int(params.get("exit_period", 10))
        px_txt = f" {_fmt_price_str(trailing_px)}" if trailing_px is not None else ""
        verb = "跌破" if direction == "long" else "站上"
        side_txt = "低點" if direction == "long" else "高點"
        return f"止損，或日線{verb} {exit_p} 日反向通道移動出場線{px_txt}", trailing_px

    if strategy_id == "L4":
        sma_p = int(params.get("sma_period", 100))
        k_mult = float(params.get("atr_mult", 3.0))
        return f"移動停損（近 60 日最高收盤 − {k_mult:g}×ATR）或週收盤跌破 {sma_p} 日均線", trailing_px

    return "依策略規則出場或觸及止損", None

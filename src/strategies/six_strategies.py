"""Unified 6-strategy definitions (S1-S3 short-term, L1-L3 long-term).

Single source of truth: both historical backtesting and live scanning execute the
exact same bar-by-bar decision functions on strictly closed OHLCV bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series, compute_rsi


@dataclass(frozen=True)
class SignalAction:
    """Deterministic action returned by a strategy on closed bar index `idx`."""

    action: str  # "enter_long", "enter_short", "exit", "hold"
    direction: str = ""  # "long", "short", or ""
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""


def _sma_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def _rsi_series(bars: list[OHLCVBar], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    # Wilder RSI matching compute_rsi step-by-step causally
    closes = [b.close for b in bars]
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = (100.0 if avg_gain > 0 else 50.0) if avg_loss <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for g_idx in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[g_idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[g_idx]) / period
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


def _bb_series(
    closes: list[float],
    period: int,
    num_std: float,
    lookback: int,
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    n = len(closes)
    mid_s: list[float | None] = [None] * n
    up_s: list[float | None] = [None] * n
    lo_s: list[float | None] = [None] * n
    bw_pct_s: list[float | None] = [None] * n
    bws: list[float | None] = [None] * n

    for i in range(period - 1, n):
        win = closes[i - period + 1 : i + 1]
        sma = sum(win) / period
        if sma <= 1e-12:
            continue
        var = sum((x - sma) ** 2 for x in win) / period
        std = math.sqrt(var)
        up = sma + num_std * std
        lo = sma - num_std * std
        bw = (up - lo) / sma
        mid_s[i] = sma
        up_s[i] = up
        lo_s[i] = lo
        bws[i] = bw

        hist = [bws[j] for j in range(max(period - 1, i - lookback), i) if bws[j] is not None]
        if hist:
            below = sum(1 for x in hist if x is not None and x < bw)
            bw_pct_s[i] = below / len(hist) * 100.0

    return mid_s, up_s, lo_s, bw_pct_s


def precompute_series(
    strategy_id: str,
    bars: list[OHLCVBar],
    params: dict[str, Any],
    market: str = "crypto",
    interval: str = "1d",
    atr_period: int = 14,
) -> dict[str, Any]:
    """Precompute strictly causal indicator arrays for one symbol."""
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    atr_s = _atr_aligned(bars, period=atr_period, market=market, interval=interval)

    data: dict[str, Any] = {
        "bars": bars,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "atr": atr_s,
    }

    if strategy_id == "S1":
        trend_p = int(params.get("trend_ma_period", 200))
        exit_p = int(params.get("exit_ma_period", 5))
        rsi_p = int(params.get("rsi_period", 2))
        data["sma_trend"] = _sma_series(closes, trend_p)
        data["sma_exit"] = _sma_series(closes, exit_p)
        data["rsi"] = _rsi_series(bars, rsi_p)
    elif strategy_id == "S2":
        bb_p = int(params.get("bb_period", 20))
        bb_std = float(params.get("bb_std", 2.0))
        sq_lb = int(params.get("squeeze_lookback", 48))
        mid_s, up_s, lo_s, bw_pct_s = _bb_series(closes, bb_p, bb_std, sq_lb)
        data["bb_mid"] = mid_s
        data["bb_upper"] = up_s
        data["bb_lower"] = lo_s
        data["bb_bw_pct"] = bw_pct_s
    elif strategy_id == "S3":
        rsi_p = int(params.get("rsi_period", 14))
        data["rsi"] = _rsi_series(bars, rsi_p)
    elif strategy_id == "L3":
        ma_p = int(params.get("ma_period", 200))
        data["sma_trend"] = _sma_series(closes, ma_p)

    return data


def decide_strategy_bar(
    strategy_id: str,
    series: dict[str, Any],
    idx: int,
    position_dir: str,
    params: dict[str, Any],
    allow_short: bool = True,
) -> SignalAction:
    """Single shared step decision function used identically by backtest and live execution."""
    closes = series["closes"]
    highs = series["highs"]
    lows = series["lows"]
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
                return SignalAction(action="exit", direction="long", reason=f"S1 收盤 {close:g} 站上 {int(params.get('exit_ma_period', 5))} 期均線出場")
            return SignalAction(action="hold")
        if position_dir == "short":
            if close < sma_e:
                return SignalAction(action="exit", direction="short", reason=f"S1 收盤 {close:g} 跌破 {int(params.get('exit_ma_period', 5))} 期均線出場")
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

        box_hi = max(highs[idx - bb_p : idx])
        box_lo = min(lows[idx - bb_p : idx])
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
        if idx < rng_bars or rsi_v is None:
            return SignalAction(action="hold", reason="S3 箱型視窗或 RSI 暖機中")

        win_hi = highs[idx - rng_bars : idx]
        win_lo = lows[idx - rng_bars : idx]
        box_hi = max(win_hi)
        box_lo = min(win_lo)
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

        up_thresh = box_hi - edge_pct * height
        lo_thresh = box_lo + edge_pct * height
        up_touches = sum(1 for h in win_hi if h >= up_thresh)
        lo_touches = sum(1 for l in win_lo if l <= lo_thresh)
        if up_touches < min_touches or lo_touches < min_touches:
            return SignalAction(action="hold", reason=f"S3 非有效箱型（上下緣觸碰 {up_touches}/{lo_touches} 次）")

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
        if idx < max(entry_p, exit_p):
            return SignalAction(action="hold", reason="L1 唐奇安通道暖機中")

        if position_dir == "long":
            exit_lo = min(lows[idx - exit_p : idx])
            if close < exit_lo:
                return SignalAction(action="exit", direction="long", reason=f"L1 跌破 {exit_p} 日反向低點 {exit_lo:g} 出場")
            return SignalAction(action="hold")
        if position_dir == "short":
            exit_hi = max(highs[idx - exit_p : idx])
            if close > exit_hi:
                return SignalAction(action="exit", direction="short", reason=f"L1 站上 {exit_p} 日反向高點 {exit_hi:g} 出場")
            return SignalAction(action="hold")

        entry_hi = max(highs[idx - entry_p : idx])
        entry_lo = min(lows[idx - entry_p : idx])
        if close > entry_hi:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"L1 收盤 {close:g} 突破 {entry_p} 日唐奇安上緣 {entry_hi:g}",
            )
        if allow_short and close < entry_lo:
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
                return SignalAction(action="exit", direction="long", reason=f"L3 收盤跌破 {ma_p} 日均線 {sma_v:g} 出場")
            return SignalAction(action="hold")
        if position_dir == "short":
            if close > sma_v:
                return SignalAction(action="exit", direction="short", reason=f"L3 收盤站上 {ma_p} 日均線 {sma_v:g} 出場")
            return SignalAction(action="hold")

        if close > sma_v:
            return SignalAction(
                action="enter_long",
                direction="long",
                stop_loss=close - sl_mult * atr_val,
                reason=f"L3 收盤 {close:g} 在 {ma_p} 日均線 {sma_v:g} 之上做多",
            )
        if allow_short and close < sma_v:
            return SignalAction(
                action="enter_short",
                direction="short",
                stop_loss=close + sl_mult * atr_val,
                reason=f"L3 收盤 {close:g} 在 {ma_p} 日均線 {sma_v:g} 之下做空",
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
    """Live evaluation helper wrapping the exact same precompute + decide_strategy_bar."""
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
    return decide_strategy_bar(
        strategy_id=strategy_id,
        series=series,
        idx=len(closed_bars) - 1,
        position_dir=position_dir,
        params=params,
        allow_short=allow_short,
    )

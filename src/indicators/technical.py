"""Technical indicators computed strictly on closed bars without closure-gap distortion."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.data.models import OHLCVBar
from src.data.quality import _is_expected_break, _parse_iso_utc


def _floor_1h(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def resample_15m_to_1h(
    bars_15m: list[OHLCVBar],
    now_utc: datetime | None = None,
) -> list[OHLCVBar]:
    """Resample closed 15m bars into fully closed 1h bars without lookahead."""
    if not bars_15m:
        return []
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    buckets: dict[datetime, list[tuple[datetime, OHLCVBar]]] = defaultdict(list)

    for bar in bars_15m:
        dt = _parse_iso_utc(bar.ts_utc)
        if dt + timedelta(minutes=15) > now:
            continue
        bucket_start = _floor_1h(dt)
        if bucket_start + timedelta(hours=1) > now:
            continue
        buckets[bucket_start].append((dt, bar))

    out: list[OHLCVBar] = []
    for bucket_start in sorted(buckets.keys()):
        items = sorted(buckets[bucket_start], key=lambda x: x[0])
        out.append(
            OHLCVBar(
                ts_utc=bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=items[0][1].open,
                high=max(b.high for _, b in items),
                low=min(b.low for _, b in items),
                close=items[-1][1].close,
                volume=sum(b.volume for _, b in items),
            )
        )
    return out


def compute_atr_series(
    bars: list[OHLCVBar],
    period: int = 14,
    market: str = "crypto",
    interval: str = "1h",
) -> list[float]:
    """Compute Wilder ATR series, ignoring expected weekend/settlement gaps."""
    if len(bars) < period + 1:
        return []
    timestamps = [_parse_iso_utc(b.ts_utc) for b in bars] if market != "crypto" else None
    trs: list[float] = []
    if timestamps is None:
        prev_close = bars[0].close
        for i in range(1, len(bars)):
            curr = bars[i]
            hi = curr.high
            lo = curr.low
            tr1 = hi - lo
            tr2 = abs(hi - prev_close)
            tr3 = abs(lo - prev_close)
            trs.append(tr1 if tr1 >= tr2 and tr1 >= tr3 else (tr2 if tr2 >= tr3 else tr3))
            prev_close = curr.close
    else:
        for i in range(1, len(bars)):
            curr = bars[i]
            prev = bars[i - 1]
            if _is_expected_break(timestamps[i - 1], timestamps[i], market, interval):
                tr = max(0.0, curr.high - curr.low)
            else:
                tr = max(
                    curr.high - curr.low,
                    abs(curr.high - prev.close),
                    abs(curr.low - prev.close),
                )
            trs.append(tr)

    if len(trs) < period:
        return []

    atr = sum(trs[:period]) / period
    series = [atr]
    inv_p = 1.0 / period
    p_minus = period - 1
    for tr in trs[period:]:
        atr = (atr * p_minus + tr) * inv_p
        series.append(atr)
    return series


def compute_atr(
    bars: list[OHLCVBar],
    period: int = 14,
    market: str = "crypto",
    interval: str = "1h",
) -> float | None:
    """Return latest Wilder ATR value."""
    series = compute_atr_series(bars, period=period, market=market, interval=interval)
    return series[-1] if series else None


def compute_atr_ratio(
    bars: list[OHLCVBar],
    atr_period: int = 14,
    baseline_bars: int = 480,
    market: str = "crypto",
    interval: str = "1h",
) -> float | None:
    """Return latest ATR divided by mean ATR over trailing baseline_bars."""
    series = compute_atr_series(bars, period=atr_period, market=market, interval=interval)
    if not series:
        return None
    window = series[-baseline_bars:]
    mean_atr = sum(window) / len(window)
    if mean_atr <= 1e-12:
        return None
    return series[-1] / mean_atr


def compute_adx(
    bars: list[OHLCVBar],
    period: int = 14,
    market: str = "crypto",
    interval: str = "1h",
) -> float | None:
    """Compute Wilder ADX(14), ignoring expected weekend/settlement gaps."""
    if len(bars) < period * 2 + 1:
        return None
    timestamps = [_parse_iso_utc(b.ts_utc) for b in bars]
    trs: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []

    for i in range(1, len(bars)):
        curr = bars[i]
        prev = bars[i - 1]
        if _is_expected_break(timestamps[i - 1], timestamps[i], market, interval):
            trs.append(max(1e-12, curr.high - curr.low))
            plus_dms.append(0.0)
            minus_dms.append(0.0)
            continue

        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        up_move = curr.high - prev.high
        down_move = prev.low - curr.low
        p_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        m_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        trs.append(max(1e-12, tr))
        plus_dms.append(p_dm)
        minus_dms.append(m_dm)

    if len(trs) < period * 2:
        return None

    sm_tr = sum(trs[:period])
    sm_plus = sum(plus_dms[:period])
    sm_minus = sum(minus_dms[:period])

    dxs: list[float] = []
    for idx in range(period, len(trs)):
        sm_tr = sm_tr - (sm_tr / period) + trs[idx]
        sm_plus = sm_plus - (sm_plus / period) + plus_dms[idx]
        sm_minus = sm_minus - (sm_minus / period) + minus_dms[idx]
        p_di = 100.0 * (sm_plus / max(1e-12, sm_tr))
        m_di = 100.0 * (sm_minus / max(1e-12, sm_tr))
        di_sum = p_di + m_di
        dx = 100.0 * abs(p_di - m_di) / di_sum if di_sum > 1e-12 else 0.0
        dxs.append(dx)

    if len(dxs) < period:
        return None

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def compute_rsi(bars: list[OHLCVBar], period: int = 14) -> float | None:
    """Compute Wilder RSI(14) on bar close prices."""
    if len(bars) < period + 1:
        return None
    closes = [b.close for b in bars]
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_bollinger_bandwidth_percentile(
    bars: list[OHLCVBar],
    period: int = 20,
    num_std: float = 2.0,
    lookback: int = 720,
) -> float | None:
    """Compute current Bollinger Bandwidth percentile over trailing lookback bars."""
    if len(bars) < period + 5:
        return None
    closes = [b.close for b in bars]
    bws: list[float] = []
    for i in range(period - 1, len(closes)):
        win = closes[i - period + 1 : i + 1]
        sma = sum(win) / period
        if sma <= 1e-12:
            continue
        var = sum((x - sma) ** 2 for x in win) / period
        std = math.sqrt(var)
        bw = (2.0 * num_std * std) / sma
        bws.append(bw)

    if len(bws) < 2:
        return None
    window = bws[-lookback:]
    current = window[-1]
    prior = window[:-1]
    below_or_eq = sum(1 for x in prior if x <= current)
    return round(below_or_eq / len(prior) * 100.0, 2)

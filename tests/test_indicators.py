"""Unit tests for technical indicators without lookahead or closure-gap distortion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.data.models import OHLCVBar
from src.indicators.technical import (
    compute_adx,
    compute_atr,
    compute_atr_ratio,
    compute_bollinger_bandwidth_percentile,
    compute_rsi,
    resample_15m_to_1h,
)


def _bars_15m(start: datetime, count: int, base: float = 100.0, step: float = 0.0, spread: float = 0.5) -> list[OHLCVBar]:
    out = []
    for i in range(count):
        dt = start + timedelta(minutes=15 * i)
        c = base + i * step
        out.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=c - step * 0.5,
                high=c + spread,
                low=c - spread,
                close=c,
                volume=100.0,
            )
        )
    return out


def test_resample_15m_to_1h_excludes_unclosed_bucket_no_lookahead() -> None:
    now = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)
    start = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    # 12 bars: 02:00..02:45 (closed 1H), 03:00..03:45 (closed 1H), 04:00..04:45 (04:00 1H closes at 05:00 > 04:30 -> excluded)
    bars = _bars_15m(start, 12, base=100.0, step=1.0)
    h1 = resample_15m_to_1h(bars, now_utc=now)
    assert len(h1) == 2
    assert h1[0].ts_utc == "2026-11-16T02:00:00Z"
    assert h1[1].ts_utc == "2026-11-16T03:00:00Z"


def test_atr_and_adx_ignore_expected_weekend_gap() -> None:
    # Friday 1H bars around 1.0800 (range 0.0020), then weekend gap to Sunday 22:00 UTC at 1.1200 (0.0400 gap!)
    fri_start = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
    fri_bars = [
        OHLCVBar(
            ts_utc=(fri_start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=1.0800,
            high=1.0810,
            low=1.0790,
            close=1.0800,
            volume=10.0,
        )
        for i in range(20)  # 02:00 .. 21:00 UTC Friday
    ]
    sun_start = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)
    sun_bars = [
        OHLCVBar(
            ts_utc=(sun_start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=1.1200,
            high=1.1210,
            low=1.1190,
            close=1.1200,
            volume=10.0,
        )
        for i in range(15)
    ]
    combined = fri_bars + sun_bars
    atr = compute_atr(combined, period=14, market="forex", interval="1h")
    adx = compute_adx(combined, period=14, market="forex", interval="1h")
    assert atr is not None
    # True bar range is 0.0020; without weekend gap protection ATR would jump > 0.0040
    assert atr < 0.0025
    assert adx is not None
    assert adx < 20.0


def test_rsi_14_and_bollinger_bandwidth_percentile() -> None:
    start = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    # Downtrend -> RSI < 35
    down_bars = _bars_15m(start, 30, base=200.0, step=-1.0, spread=0.2)
    rsi_down = compute_rsi(down_bars, period=14)
    assert rsi_down is not None and rsi_down < 35.0

    # Uptrend -> RSI > 65
    up_bars = _bars_15m(start, 30, base=100.0, step=1.0, spread=0.2)
    rsi_up = compute_rsi(up_bars, period=14)
    assert rsi_up is not None and rsi_up > 65.0

    # Wide volatility first, then very tight compression -> BB bandwidth percentile < 30%
    wide = [
        OHLCVBar(
            ts_utc=(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=108.0 if i % 2 == 0 else 101.0,
            low=99.0 if i % 2 == 0 else 92.0,
            close=106.0 if i % 2 == 0 else 94.0,
            volume=100.0,
        )
        for i in range(60)
    ]
    tight = [
        OHLCVBar(
            ts_utc=(start + timedelta(hours=60 + i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=100.3,
            low=99.7,
            close=100.1 if i % 2 == 0 else 99.9,
            volume=50.0,
        )
        for i in range(30)
    ]
    pct = compute_bollinger_bandwidth_percentile(wide + tight, period=20, num_std=2.0, lookback=100)
    assert pct is not None and pct < 30.0

    atr_ratio = compute_atr_ratio(wide + tight, atr_period=14, baseline_bars=60)
    assert atr_ratio is not None and atr_ratio < 1.2

"""Unit tests for data quality and continuity checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.data.models import FetchResult, OHLCVBar
from src.data.quality import check_continuity, is_market_open, is_weekend_closure


def _make_bars(start_dt: datetime, count: int, step_minutes: int, base_price: float = 100.0) -> list[OHLCVBar]:
    bars = []
    for i in range(count):
        dt = start_dt + timedelta(minutes=i * step_minutes)
        p = base_price + (i % 3) * 0.5
        bars.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=p,
                high=p + 1.0,
                low=p - 1.0,
                close=p + 0.2,
                volume=100.0,
            )
        )
    return bars


QUALITY_CFG = {
    "max_missing_ratio": 0.10,
    "max_delay_minutes": {"1m": 15, "15m": 45, "1d": 2880},
    "abnormal_gap_atr_mult": 5.0,
}


def test_failed_fetch_returns_failed_status_no_fake_data() -> None:
    res = FetchResult(symbol="BTCUSDT", interval="15m", ok=False, source_used=None, bars=[], error="timeout")
    rep = check_continuity(res, market="crypto", quality_config=QUALITY_CFG)
    assert rep.status == "failed"
    assert rep.bar_count == 0
    assert rep.latest_close is None
    assert "資料來源失敗" in rep.reason


def test_crypto_continuous_bars_ok() -> None:
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(minutes=15 * 20)
    bars = _make_bars(start, 20, 15)
    res = FetchResult(symbol="BTCUSDT", interval="15m", ok=True, source_used="binance_vision", bars=bars)
    rep = check_continuity(res, market="crypto", quality_config=QUALITY_CFG, now_utc=now)
    assert rep.status == "ok"
    assert rep.missing_ratio == 0.0
    assert rep.abnormal_gap_count == 0


def test_crypto_missing_bars_marked_unstable() -> None:
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(minutes=15 * 20)
    bars = _make_bars(start, 20, 15)
    # Remove 5 middle bars -> missing ratio > 10%
    gapped = bars[:5] + bars[10:]
    res = FetchResult(symbol="BTCUSDT", interval="15m", ok=True, source_used="binance_vision", bars=gapped)
    rep = check_continuity(res, market="crypto", quality_config=QUALITY_CFG, now_utc=now)
    assert rep.status == "unstable"
    assert rep.missing_ratio > 0.10


def test_crypto_stale_bars_marked_stale() -> None:
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(hours=4)
    bars = _make_bars(start, 10, 15)
    res = FetchResult(symbol="BTCUSDT", interval="15m", ok=True, source_used="binance_vision", bars=bars)
    rep = check_continuity(res, market="crypto", quality_config=QUALITY_CFG, now_utc=now)
    assert rep.status == "stale"
    assert rep.delay_minutes > 45


def test_forex_weekend_closure_not_counted_as_missing_or_stale() -> None:
    # Friday 21:00 UTC to Sunday 22:00 UTC + Monday 00:00 UTC
    fri_start = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
    fri_bars = _make_bars(fri_start, 8, 15, base_price=1.08)  # 20:00 .. 21:45
    sun_start = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)
    sun_bars = _make_bars(sun_start, 8, 15, base_price=1.08)  # 22:00 .. 23:45

    res = FetchResult(symbol="EURUSD", interval="15m", ok=True, source_used="yfinance", bars=fri_bars + sun_bars)
    # Check during Saturday (market closed) -> not stale!
    sat_now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    assert is_weekend_closure(sat_now) is True
    assert is_market_open("forex", sat_now) is False

    rep = check_continuity(res, market="forex", quality_config=QUALITY_CFG, now_utc=datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc))
    assert rep.status == "ok"
    assert rep.missing_ratio == 0.0


def test_forex_intraday_gap_from_fri_2145_to_sun_2300_is_weekend() -> None:
    fri_start = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
    fri_bars = _make_bars(fri_start, 8, 15, base_price=150.0)  # ends 21:45 UTC
    sun_start = datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc)
    sun_bars = _make_bars(sun_start, 8, 15, base_price=150.0)  # starts 23:00 UTC
    res = FetchResult(symbol="USDJPY", interval="15m", ok=True, source_used="yfinance", bars=fri_bars + sun_bars)
    rep = check_continuity(res, market="forex", quality_config=QUALITY_CFG, now_utc=datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc))
    assert rep.status == "ok"
    assert rep.missing_ratio == 0.0


def test_forex_daily_friday_bar_not_stale_on_monday_morning() -> None:
    # Friday 2026-11-13 daily bar closes at 2026-11-14T00:00:00Z.
    # Checked on Monday 2026-11-16T04:32:00Z -> weekend (48h) subtracted -> effective delay < 7h -> ok!
    bars = [
        OHLCVBar("2026-11-11T00:00:00Z", 1.08, 1.09, 1.07, 1.085, 0.0),
        OHLCVBar("2026-11-12T00:00:00Z", 1.085, 1.09, 1.08, 1.082, 0.0),
        OHLCVBar("2026-11-13T00:00:00Z", 1.082, 1.088, 1.08, 1.084, 0.0),
    ]
    res = FetchResult(symbol="EURUSD", interval="1d", ok=True, source_used="yfinance", bars=bars)
    now_mon = datetime(2026, 11, 16, 4, 32, tzinfo=timezone.utc)
    rep = check_continuity(res, market="forex", quality_config=QUALITY_CFG, now_utc=now_mon)
    assert rep.status == "ok"
    assert rep.delay_minutes < 500

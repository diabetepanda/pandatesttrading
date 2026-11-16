"""Unit tests for 1m -> 15m resampling without lookahead."""

from __future__ import annotations

from datetime import datetime, timezone

from src.data.models import OHLCVBar
from src.data.resample import resample_1m_to_15m


def test_resample_excludes_unclosed_15m_bucket_no_lookahead() -> None:
    now = datetime(2026, 11, 16, 5, 10, tzinfo=timezone.utc)
    bars_1m = [
        # Closed 04:45 bucket (04:45..04:59)
        OHLCVBar("2026-11-16T04:45:00Z", 100.0, 102.0, 99.0, 101.0, 10.0),
        OHLCVBar("2026-11-16T04:50:00Z", 101.0, 105.0, 100.0, 104.0, 15.0),
        OHLCVBar("2026-11-16T04:59:00Z", 104.0, 104.5, 98.0, 103.0, 5.0),
        # In-progress 05:00 bucket (closes at 05:15 > now=05:10) -> MUST be excluded
        OHLCVBar("2026-11-16T05:00:00Z", 103.0, 110.0, 102.0, 109.0, 20.0),
        OHLCVBar("2026-11-16T05:05:00Z", 109.0, 112.0, 108.0, 111.0, 25.0),
    ]
    res = resample_1m_to_15m(bars_1m, now_utc=now)
    assert len(res) == 1
    assert res[0].ts_utc == "2026-11-16T04:45:00Z"
    assert res[0].open == 100.0
    assert res[0].high == 105.0
    assert res[0].low == 98.0
    assert res[0].close == 103.0
    assert res[0].volume == 30.0


def test_resample_excludes_unclosed_1m_bar_even_at_exact_boundary() -> None:
    # At 05:15:00Z, the 05:00..05:14 bucket IS closed, but a 05:15:00Z 1m bar is NOT closed
    now = datetime(2026, 11, 16, 5, 15, tzinfo=timezone.utc)
    bars_1m = [
        OHLCVBar("2026-11-16T05:00:00Z", 10.0, 12.0, 9.0, 11.0, 1.0),
        OHLCVBar("2026-11-16T05:14:00Z", 11.0, 13.0, 10.0, 12.5, 2.0),
        OHLCVBar("2026-11-16T05:15:00Z", 12.5, 99.0, 1.0, 50.0, 999.0),
    ]
    res = resample_1m_to_15m(bars_1m, now_utc=now)
    assert len(res) == 1
    assert res[0].ts_utc == "2026-11-16T05:00:00Z"
    assert res[0].close == 12.5
    assert res[0].volume == 3.0


def test_resample_empty_returns_empty_no_fake_bars() -> None:
    assert resample_1m_to_15m([], now_utc=datetime(2026, 11, 16, 5, 15, tzinfo=timezone.utc)) == []

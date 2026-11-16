"""Resampling utilities for closed OHLCV bars (no lookahead)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.data.models import OHLCVBar


def _parse_iso_utc(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _floor_15m(dt: datetime) -> datetime:
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def resample_1m_to_15m(
    bars_1m: list[OHLCVBar],
    now_utc: datetime | None = None,
) -> list[OHLCVBar]:
    """Resample closed 1m bars into closed 15m bars without lookahead.

    Only 1m bars that are closed by now_utc (open_ts + 60s <= now_utc) and belong
    to a 15m bucket that is fully closed by now_utc (bucket_start + 15m <= now_utc)
    are included.
    """
    if not bars_1m:
        return []

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    buckets: dict[datetime, list[tuple[datetime, OHLCVBar]]] = defaultdict(list)

    for bar in bars_1m:
        dt = _parse_iso_utc(bar.ts_utc)
        # Ensure the 1m bar itself is closed
        if dt + timedelta(minutes=1) > now:
            continue
        bucket_start = _floor_15m(dt)
        # Ensure the 15m bucket is completely closed (no partial/unclosed 15m bar)
        if bucket_start + timedelta(minutes=15) > now:
            continue
        buckets[bucket_start].append((dt, bar))

    resampled: list[OHLCVBar] = []
    for bucket_start in sorted(buckets.keys()):
        items = sorted(buckets[bucket_start], key=lambda x: x[0])
        first_bar = items[0][1]
        last_bar = items[-1][1]
        resampled.append(
            OHLCVBar(
                ts_utc=bucket_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=first_bar.open,
                high=max(b.high for _, b in items),
                low=min(b.low for _, b in items),
                close=last_bar.close,
                volume=sum(b.volume for _, b in items),
            )
        )

    return resampled

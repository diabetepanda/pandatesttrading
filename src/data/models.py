"""Data models for OHLCV bars, adapter results, and data health."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class OHLCVBar:
    """Single closed or live OHLCV bar in UTC."""

    ts_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class AttemptRecord:
    """Result of a single adapter fetch attempt."""

    adapter: str
    ok: bool
    bar_count: int = 0
    latency_ms: float = 0.0
    error: str = ""
    latest_ts_utc: str | None = None
    latest_close: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchResult:
    """Aggregated result from FallbackDataFetcher."""

    symbol: str
    interval: str
    ok: bool
    source_used: str | None
    bars: list[OHLCVBar] = field(default_factory=list)
    error: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass
class QualityReport:
    """Continuity and quality assessment for an instrument/interval."""

    symbol: str
    interval: str
    status: str  # "ok", "unstable", "stale", "failed"
    source_used: str | None
    bar_count: int
    latest_ts_utc: str | None
    latest_close: float | None
    delay_minutes: float | None
    missing_ratio: float
    abnormal_gap_count: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

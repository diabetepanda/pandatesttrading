"""Unit tests for the Stage 1 scan job."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.adapters import DataAdapter, FallbackDataFetcher
from src.data.models import OHLCVBar
from src.jobs.scan import run_scan


class DummyAdapter(DataAdapter):
    name = "dummy"

    def fetch_bars(self, source_symbol: str, interval: str, limit: int = 200, now_utc=None):
        now = now_utc or datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        step = {"1m": 1, "15m": 15, "1d": 1440}[interval]
        bars = []
        for i in range(5):
            dt = now - timedelta(minutes=step * (5 - i))
            bars.append(
                OHLCVBar(
                    ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    open=100.0 + i,
                    high=101.0 + i,
                    low=99.0 + i,
                    close=100.5 + i,
                    volume=10.0,
                )
            )
        return bars


def test_run_scan_outputs_required_files(tmp_path: Path) -> None:
    dummy = DummyAdapter()
    adapters = {
        k: dummy
        for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")
    }
    fetcher = FallbackDataFetcher(adapters=adapters)
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    summary = run_scan(out_dir=tmp_path, fetcher=fetcher, probe_all_adapters=True, now_utc=now)
    assert summary["status_counts"]["ok"] > 0

    for fname in (
        "instruments.json",
        "data_health.json",
        "control_status.json",
        "state.json",
        "run_log.json",
        "data_probe_report.json",
    ):
        assert (tmp_path / fname).exists(), f"Missing {fname}"

    probe = json.loads((tmp_path / "data_probe_report.json").read_text(encoding="utf-8"))
    assert len(probe["results"]) > 0

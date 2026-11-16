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
    # Use Saturday 12:00 UTC so crypto passes low-liquidity window and reaches derivatives check
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

    summary = run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0},
        probe_all_adapters=True,
        now_utc=now,
    )
    assert summary["status_counts"]["ok"] > 0

    for fname in (
        "instruments.json",
        "data_health.json",
        "control_status.json",
        "state.json",
        "run_log.json",
        "data_probe_report.json",
        "watch_today.json",
        "signals_index.json",
        "liquidity_heatmap.json",
        "new_signals_to_commit.json",
        "trades.json",
        "orders.json",
        "ledgers_state.json",
    ):
        assert (tmp_path / fname).exists(), f"Missing {fname}"

    probe = json.loads((tmp_path / "data_probe_report.json").read_text(encoding="utf-8"))
    assert len(probe["results"]) > 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == 3
    assert state["runs_last_24h"] == 1

    # Second scan 15 minutes later restores state and increments 24h count
    run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0},
        probe_all_adapters=False,
        now_utc=now + timedelta(minutes=15),
    )
    state2 = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state2["runs_last_24h"] == 2


def test_run_scan_excludes_unstable_1m_instruments_in_watch_today(tmp_path: Path) -> None:
    class UnstableForAudAdapter(DummyAdapter):
        def fetch_bars(self, source_symbol: str, interval: str, limit: int = 200, now_utc=None):
            bars = super().fetch_bars(source_symbol, interval, limit=limit, now_utc=now_utc)
            if interval == "1m" and source_symbol in ("AUDUSD=X", "NZDUSD=X", "NG=F"):
                # Drop every other minute -> ~50% missing -> unstable!
                now = now_utc or datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
                return [
                    OHLCVBar(
                        ts_utc=(now - timedelta(minutes=i * 2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        open=1.0,
                        high=1.1,
                        low=0.9,
                        close=1.0,
                        volume=10.0,
                    )
                    for i in range(10, 0, -1)
                ]
            return bars

    adapter = UnstableForAudAdapter()
    adapters = {
        k: adapter
        for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")
    }
    fetcher = FallbackDataFetcher(adapters=adapters)
    # Use Monday 12:00 UTC (markets open)
    now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0},
        probe_all_adapters=False,
        now_utc=now,
    )

    watch = json.loads((tmp_path / "watch_today.json").read_text(encoding="utf-8"))
    by_sym = {item["symbol"]: item for item in watch["items"]}
    for sym in ("AUDUSD", "NZDUSD", "NATGAS"):
        assert sym in by_sym
        assert "資料不穩" in by_sym[sym]["reason"]
    # Even when BTCUSDT is blocked early by non-low-liquidity session, derivatives must still be recorded
    btc_item = by_sym["BTCUSDT"]
    assert "非低流動性時段" in btc_item["reason"]
    assert btc_item["derivatives"]["status"] == "ok"
    assert btc_item["derivatives"]["funding_rate"] == 0.0001
    assert btc_item["derivatives"]["oi_change"] == 1.0


def test_run_scan_logs_insufficient_derivatives_data_when_missing(tmp_path: Path) -> None:
    dummy = DummyAdapter()
    adapters = {
        k: dummy
        for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")
    }
    fetcher = FallbackDataFetcher(adapters=adapters)
    # Saturday 12:00 UTC -> crypto is in low-liquidity weekend candidate window
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"source": None, "funding_rate": None, "oi_change": None, "status": "抓取失敗"},
        probe_all_adapters=False,
        now_utc=now,
    )
    watch = json.loads((tmp_path / "watch_today.json").read_text(encoding="utf-8"))
    by_sym = {item["symbol"]: item for item in watch["items"]}
    assert "資料不足" in by_sym["BTCUSDT"]["reason"]
    assert by_sym["BTCUSDT"]["derivatives"]["status"] == "抓取失敗"

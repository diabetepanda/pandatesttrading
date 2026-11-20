"""Unit tests for the Stage 1 scan job."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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

    # Plant obsolete data-branch files to verify scan removes them while preserving probe reports
    (tmp_path / "orders.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trade_stages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "backtests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backtests" / "latest.json").write_text("{}", encoding="utf-8")

    summary = run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0},
        probe_all_adapters=True,
        now_utc=now,
    )
    assert summary["status_counts"]["ok"] > 0
    assert not (tmp_path / "orders.json").exists()
    assert not (tmp_path / "trade_stages.json").exists()
    assert not (tmp_path / "backtests" / "latest.json").exists()
    assert (tmp_path / "data_probe_report.json").exists()
    assert "step_timings_sec" in summary
    assert "fetch_and_quality_sec" in summary["step_timings_sec"]

    for fname in (
        "instruments.json",
        "data_health.json",
        "control_status.json",
        "state.json",
        "run_log.json",
        "data_probe_report.json",
        "watch_today.json",
        "signals_index.json",
        "new_signals_to_commit.json",
        "trades.json",
        "ledgers_state.json",
    ):
        assert (tmp_path / fname).exists(), f"Missing {fname}"

    probe = json.loads((tmp_path / "data_probe_report.json").read_text(encoding="utf-8"))
    assert len(probe["results"]) > 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == 3
    assert state["runs_last_24h"] == 1
    run_log = json.loads((tmp_path / "run_log.json").read_text(encoding="utf-8"))
    assert len(run_log) >= 1 and "step_timings_sec" in run_log[0]
    watch_doc = json.loads((tmp_path / "watch_today.json").read_text(encoding="utf-8"))
    btc_watch = next(it for it in watch_doc["items"] if it["symbol"] == "BTCUSDT")
    assert "S2" in btc_watch["strategies"] and "S1" not in btc_watch["strategies"]

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
            if interval == "1m" and source_symbol in ("CL=F", "NG=F", "HG=F"):
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
    for sym in ("WTI", "NATGAS", "COPPER"):
        assert sym in by_sym
        assert "資料不穩" in by_sym[sym]["reason"]
    # Derivatives must still be recorded for crypto during live Round-2 scan
    btc_item = by_sym["BTCUSDT"]
    assert "S2" in btc_item["reason"]
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
    assert "抓取失敗" in by_sym["BTCUSDT"]["reason"]
    assert by_sym["BTCUSDT"]["derivatives"]["status"] == "抓取失敗"


def test_run_scan_closes_active_trade_on_disabled_instrument(tmp_path: Path) -> None:
    from src.config_loader import load_config
    import pytest

    cfg = load_config()
    eur_inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    assert eur_inst["enabled"] is False

    sid = "SIG-20261116-0001"
    entered_time = "2026-11-16T12:00:00Z"
    scan_time = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc)

    qty = 10000.0
    entry_px = 1.0800
    spread_bps = float(eur_inst.get("assumed_spread_bps", 1.0))
    fee_rate = float(eur_inst.get("fee_rate", 0.0001))
    slip_bps = 1.0
    overnight_bps = float(eur_inst.get("overnight_cost_daily_bps", 1.0))

    entry_fee = entry_px * qty * fee_rate
    legacy_state = {
        "sequences": {"signal": 1, "event": 1, "fill": 1},
        "latest_marks": {"EURUSD": entry_px},
        "ledger": {
            "initial_capital_usd": 100000.0,
            "strategy_capitals": {"S2": 10000.0},
            "strategy_realized_pnl": {"S2": 0.0},
            "trades": {
                sid: {
                    "signal_id": sid,
                    "strategy": "S2",
                    "symbol": "EURUSD",
                    "market": "forex",
                    "correlation_group": "usd_fx",
                    "direction": "long",
                    "state": "active",
                    "entry_price": entry_px,
                    "avg_entry_price": entry_px,
                    "remaining_qty": qty,
                    "initial_stop_loss": 1.0700,
                    "current_stop_loss": 1.0700,
                    "take_profit": None,
                    "r_plan_usd": 100.0,
                    "planned_notional_usd": entry_px * qty,
                    "subaccount_capital_usd": 10000.0,
                    "assumed_spread_bps": spread_bps,
                    "fee_rate": fee_rate,
                    "slippage_bps": slip_bps,
                    "overnight_cost_daily_bps": overnight_bps,
                    "entered_at_utc": entered_time,
                    "published_at_utc": entered_time,
                    "realized_pnl_usd": -entry_fee,
                    "total_fees_usd": entry_fee,
                    "total_overnight_usd": 0.0,
                }
            },
            "fills": [
                {
                    "fill_id": "FILL-1",
                    "signal_id": sid,
                    "symbol": "EURUSD",
                    "side": "buy",
                    "action": "entry",
                    "price": entry_px,
                    "qty": qty,
                    "notional_usd": entry_px * qty,
                    "fee_usd": entry_fee,
                    "realized_pnl_usd": -entry_fee,
                    "ts_utc": entered_time,
                }
            ],
        },
    }
    (tmp_path / "ledgers_state.json").write_text(json.dumps(legacy_state), encoding="utf-8")
    (tmp_path / "signals").mkdir(parents=True, exist_ok=True)
    (tmp_path / "signals" / f"{sid}.json").write_text(
        json.dumps({
            "signal_id": sid,
            "strategy": "S2",
            "symbol": "EURUSD",
            "market": "forex",
            "direction": "long",
            "published_at_utc": entered_time,
            "entered_at_utc": entered_time,
            "lifecycle_state": "active",
            "entry_price": entry_px,
            "stop_loss": 1.0700,
        }),
        encoding="utf-8",
    )

    exit_mid = 1.0900

    class FxCloseAdapter(DummyAdapter):
        def fetch_bars(self, source_symbol: str, interval: str, limit: int = 200, now_utc=None):
            if "EURUSD" in source_symbol:
                step = {"1m": 1, "15m": 15, "1d": 1440}.get(interval, 1)
                now = now_utc or scan_time
                bars = []
                for i in range(5):
                    dt = now - timedelta(minutes=step * (5 - i))
                    bars.append(
                        OHLCVBar(
                            ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            open=exit_mid,
                            high=exit_mid + 0.0005,
                            low=exit_mid - 0.0005,
                            close=exit_mid,
                            volume=100.0,
                        )
                    )
                return bars
            return super().fetch_bars(source_symbol, interval, limit=limit, now_utc=now_utc)

    fetcher = FallbackDataFetcher(
        adapters={k: FxCloseAdapter() for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")}
    )

    run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        now_utc=scan_time,
        probe_all_adapters=False,
    )

    saved_trades = json.loads((tmp_path / "trades.json").read_text(encoding="utf-8"))
    trade_out = next(t for t in saved_trades if t["signal_id"] == sid)
    assert trade_out["state"] == "closed"
    assert trade_out["exit_reason"] == "instrument_disabled"

    signals_idx = json.loads((tmp_path / "signals_index.json").read_text(encoding="utf-8"))
    sig_out = next(s for s in signals_idx if s["signal_id"] == sid)
    assert sig_out["lifecycle_state"] == "closed"
    assert sig_out["exit_reason"] == "instrument_disabled"
    assert sig_out["exit_reason_label"] == "標的停用平倉"

    bid_px = exit_mid * (1.0 - spread_bps / 20000.0)
    exec_px = bid_px * (1.0 - slip_bps / 10000.0)
    gross_pnl = (exec_px - entry_px) * qty
    exit_notional = exec_px * qty
    exit_fee = exit_notional * fee_rate
    days = 1.0 / 24.0
    overnight = (entry_px * qty) * (overnight_bps / 10000.0) * days
    expected_net_pnl = gross_pnl - entry_fee - exit_fee - overnight
    assert trade_out["realized_pnl_usd"] == pytest.approx(expected_net_pnl, abs=0.05)


def test_scan_closes_active_trade_on_retired_strategy(tmp_path: Path) -> None:
    """Active trades belonging to retired strategy S1 are closed cleanly as strategy_retired with historical PnL intact."""
    sid_active = "SIG-20261116-0091"
    sid_closed = "SIG-20261115-0090"
    entered_time = "2026-11-16T08:00:00Z"
    scan_time = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    entry_px = 65000.0
    qty = 0.05
    fee_rate = 0.0005
    entry_fee = entry_px * qty * fee_rate

    legacy_state = {
        "sequences": {"signal": 92, "event": 2, "fill": 3},
        "latest_marks": {"BTCUSDT": entry_px},
        "ledger": {
            "initial_capital_usd": 100000.0,
            "strategy_capitals": {"S1": 10000.0, "S2": 10000.0, "S3": 10000.0, "L1": 23333.33, "L2": 23333.33, "L3": 23333.34},
            "strategy_realized_pnl": {"S1": -50.0, "S2": 0.0, "S3": 0.0, "L1": 0.0, "L2": 0.0, "L3": 0.0},
            "trades": {
                sid_closed: {
                    "signal_id": sid_closed,
                    "strategy": "S1",
                    "symbol": "ETHUSDT",
                    "market": "crypto",
                    "correlation_group": "crypto_majors",
                    "direction": "long",
                    "state": "closed",
                    "entry_price": 3000.0,
                    "avg_entry_price": 3000.0,
                    "remaining_qty": 0.0,
                    "initial_filled_qty": 1.0,
                    "initial_stop_loss": 2950.0,
                    "current_stop_loss": 2950.0,
                    "r_plan_usd": 50.0,
                    "planned_notional_usd": 3000.0,
                    "subaccount_capital_usd": 10000.0,
                    "entered_at_utc": "2026-11-15T08:00:00Z",
                    "closed_at_utc": "2026-11-15T12:00:00Z",
                    "exit_price": 2950.0,
                    "exit_reason": "stop_loss",
                    "published_at_utc": "2026-11-15T08:00:00Z",
                    "realized_pnl_usd": -50.0,
                },
                sid_active: {
                    "signal_id": sid_active,
                    "strategy": "S1",
                    "symbol": "BTCUSDT",
                    "market": "crypto",
                    "correlation_group": "crypto_majors",
                    "direction": "long",
                    "state": "active",
                    "entry_price": entry_px,
                    "avg_entry_price": entry_px,
                    "remaining_qty": qty,
                    "initial_filled_qty": qty,
                    "initial_stop_loss": 64000.0,
                    "current_stop_loss": 64000.0,
                    "take_profit": None,
                    "r_plan_usd": 100.0,
                    "planned_notional_usd": entry_px * qty,
                    "subaccount_capital_usd": 10000.0,
                    "assumed_spread_bps": 2.0,
                    "fee_rate": fee_rate,
                    "slippage_bps": 1.0,
                    "overnight_cost_daily_bps": 0.0,
                    "entered_at_utc": entered_time,
                    "published_at_utc": entered_time,
                    "realized_pnl_usd": -entry_fee,
                    "total_fees_usd": entry_fee,
                    "total_overnight_usd": 0.0,
                },
            },
            "fills": [],
        },
    }
    (tmp_path / "ledgers_state.json").write_text(json.dumps(legacy_state), encoding="utf-8")
    (tmp_path / "signals").mkdir(parents=True, exist_ok=True)
    for sid, sym, st, extra in (
        (sid_closed, "ETHUSDT", "closed", {"exit_reason": "stop_loss", "closed_at_utc": "2026-11-15T12:00:00Z", "exit_price": 2950.0}),
        (sid_active, "BTCUSDT", "active", {"entry_price": entry_px, "stop_loss": 64000.0}),
    ):
        (tmp_path / "signals" / f"{sid}.json").write_text(
            json.dumps({"signal_id": sid, "strategy": "S1", "symbol": sym, "market": "crypto", "direction": "long", "published_at_utc": entered_time, "entered_at_utc": entered_time, "lifecycle_state": st, **extra}),
            encoding="utf-8",
        )

    fetcher = FallbackDataFetcher(
        adapters={k: DummyAdapter() for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")}
    )
    res = run_scan(
        out_dir=tmp_path,
        fetcher=fetcher,
        derivatives_fetcher=lambda _inst: {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0},
        now_utc=scan_time,
        probe_all_adapters=False,
    )
    saved_trades = json.loads((tmp_path / "trades.json").read_text(encoding="utf-8"))
    closed_active = next(t for t in saved_trades if t["signal_id"] == sid_active)
    assert closed_active["state"] == "closed"
    assert closed_active["exit_reason"] == "strategy_retired"
    signals_idx = json.loads((tmp_path / "signals_index.json").read_text(encoding="utf-8"))
    sig_row = next(s for s in signals_idx if s["signal_id"] == sid_active)
    assert sig_row["exit_reason"] == "strategy_retired"
    assert sig_row["exit_reason_label"] == "策略停用平倉"
    # Historical S1 closed (-50) + closed retired active trade PnL must remain in short_term pool (starting $20k)
    reloaded = json.loads((tmp_path / "ledgers_state.json").read_text(encoding="utf-8"))
    expected_retired_pnl = -50.0 + float(closed_active["realized_pnl_usd"])
    assert reloaded["ledger"]["retired_short_term_realized_pnl"] == pytest.approx(expected_retired_pnl, abs=1e-4)
    perf = json.loads((tmp_path / "performance_stats.json").read_text(encoding="utf-8"))
    assert perf["by_strategy"]["S1"]["retired"] is True
    health = json.loads((tmp_path / "data_health.json").read_text(encoding="utf-8"))
    assert health["short_term_equity_usd"] == pytest.approx(20000.0 + expected_retired_pnl, abs=0.05)


def test_cleanup_obsolete_data_files_preserves_probe_report(tmp_path: Path) -> None:
    """Verify scan/daily/weekly shared cleanup removes retired files while preserving data_probe_report.json."""
    from src.jobs.data_sync import cleanup_obsolete_data_files
    from src.jobs.weekly import run_weekly_screening

    for rel in ("orders.json", "trade_stages.json", "backtests/latest.json", "data_probe_report.json"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")

    removed = cleanup_obsolete_data_files(tmp_path)
    assert set(removed) == {"orders.json", "trade_stages.json", "backtests/latest.json"}
    assert not (tmp_path / "orders.json").exists()
    assert not (tmp_path / "trade_stages.json").exists()
    assert not (tmp_path / "backtests" / "latest.json").exists()
    assert (tmp_path / "data_probe_report.json").exists()

    # Re-plant and verify weekly job calls cleanup and does not re-create backtests/latest.json
    (tmp_path / "orders.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trade_stages.json").write_text("[]", encoding="utf-8")
    (tmp_path / "backtests" / "latest.json").write_text("{}", encoding="utf-8")
    dummy = DummyAdapter()
    fetcher = FallbackDataFetcher(
        adapters={k: dummy for k in ("binance_vision", "okx", "bybit", "kraken", "coinbase", "yfinance", "stooq")}
    )
    run_weekly_screening(out_dir=tmp_path, fetcher=fetcher)
    assert (tmp_path / "backtest_results.json").exists()
    assert not (tmp_path / "backtests" / "latest.json").exists()
    assert not (tmp_path / "orders.json").exists()
    assert not (tmp_path / "trade_stages.json").exists()
    assert (tmp_path / "data_probe_report.json").exists()

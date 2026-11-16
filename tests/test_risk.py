"""Unit tests for RiskManager: leverage, liquidation, margin, daily/weekly loss, drawdown, group risk, and rejection."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config_loader import load_config
from src.engine.simulator import CandidateSignal, SimulationEngine
from src.risk.manager import RiskManager
from src.signals.models import AddLevel, InputSnapshot


def _candidate(
    symbol: str = "BTCUSDT",
    strategy: str = "S1",
    direction: str = "long",
    entry: float = 90000.0,
    sl: float = 89000.0,
    tp1: float = 91800.0,
    tp2: float = 92600.0,
    leverage: float | None = None,
) -> CandidateSignal:
    return CandidateSignal(
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        entry_low=entry - 50.0 if direction == "long" else entry - 50.0,
        entry_high=entry + 50.0 if direction == "long" else entry + 50.0,
        add_levels=(),
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        invalidation_price=sl,
        invalidation_cond="結構失效",
        reasons=("測試條件1", "測試條件2"),
        snapshot=InputSnapshot(
            snapshot_id="SNAP-TEST",
            signal_id="PENDING",
            captured_at_utc="2026-11-16T02:00:00Z",
            symbol=symbol,
            kline_summary={"close": entry},
            indicators={"atr_1h": 500.0},
            filter_checks={"ok": True},
        ),
        atr=500.0,
        atr_ratio=1.0,
        leverage=leverage,
    )


def test_leverage_and_liquidation_safety_check() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")  # max_leverage = 10.0

    # At 10x leverage with 50% maintenance margin ratio, liq distance = (1 - 0.5)/10 = 5.0%
    # Safe stop distance <= 50% of 5.0% = 2.5%.
    # Test 4% stop distance -> stop_to_liq_ratio = 0.80 > 0.50 -> REJECTED!
    metrics_unsafe = rm.compute_liquidation_metrics(
        entry_price=100.0,
        stop_loss=96.0,
        direction="long",
        notional_usd=2000.0,
        leverage=10.0,
    )
    assert metrics_unsafe["liquidation_price"] == 95.0
    assert metrics_unsafe["stop_to_liq_ratio"] > 0.50

    res_reject = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=96.0,
        tp1=106.0,
        tp2=108.0,
        leverage=10.0,
        account_state={"equity_usd": 100000.0, "peak_equity_usd": 100000.0},
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_reject.approved is False
    assert "爆倉" in res_reject.reason

    # Test 2% stop distance at 10x -> ratio = 0.40 <= 0.50 -> APPROVED
    res_ok = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=10.0,
        account_state={"equity_usd": 100000.0, "peak_equity_usd": 100000.0},
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_ok.approved is True
    assert res_ok.stop_to_liq_ratio <= 0.50


def test_margin_usage_cap_30_percent() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # Account equity = 10,000 -> 30% max margin = 3,000. Already used = 2,960.
    # New notional capped at 570 -> at 10x needs 57 margin -> 3,017 > 3,000 -> REJECTED!
    res = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=10.0,
        account_state={
            "equity_usd": 10000.0,
            "peak_equity_usd": 10000.0,
            "used_margin_usd": 2960.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res.approved is False
    assert "保證金" in res.reason


def test_daily_and_weekly_loss_limits_block_new_signals() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # Daily loss limit = 1500 USD
    res_daily = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={
            "equity_usd": 98500.0,
            "peak_equity_usd": 100000.0,
            "daily_pnl_usd": -1500.0,
            "weekly_pnl_usd": -1500.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_daily.approved is False
    assert "單日" in res_daily.reason

    # Weekly loss limit = 4000 USD (even if today's loss is 0)
    res_weekly = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={
            "equity_usd": 96000.0,
            "peak_equity_usd": 100000.0,
            "daily_pnl_usd": 0.0,
            "weekly_pnl_usd": -4000.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_weekly.approved is False
    assert "單週" in res_weekly.reason


def test_drawdown_warning_halves_risk_and_pause_requires_control_json_review() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # 8% drawdown: warning mode halves unified risk budget (sub_eq=27,600 * 0.75% * 0.5 = $103.5)
    res_warn = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=94.0,
        tp1=110.0,
        tp2=115.0,
        leverage=3.0,
        account_state={"equity_usd": 92000.0, "peak_equity_usd": 100000.0},
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_warn.approved is True
    assert res_warn.risk_status == "warning"
    assert res_warn.r_plan_usd == pytest.approx(103.5, abs=0.5)

    # 12% drawdown: equity 88,000 vs peak 100,000 -> paused_drawdown
    res_pause = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={"equity_usd": 88000.0, "peak_equity_usd": 100000.0},
        control={"paused": False, "risk_review_done": False, "risk_review_note": ""},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_pause.approved is False
    assert res_pause.risk_status == "paused_drawdown"

    # Unlock via control.json: risk_review_done=True AND non-empty note
    res_unlocked = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={"equity_usd": 88000.0, "peak_equity_usd": 100000.0, "drawdown_paused": True},
        control={"paused": False, "risk_review_done": True, "risk_review_note": "已完成檢討並調降部位"},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_unlocked.approved is True
    assert res_unlocked.risk_event is not None
    assert res_unlocked.risk_event["event_type"] == "risk_review_completed"


def test_correlation_group_and_total_stop_risk_limits() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "ETHUSDT")  # group: crypto_majors

    # Group stop risk limit = 1,000 USD; already open in crypto_majors = 1,000 USD -> REJECTED!
    res_group = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={
            "equity_usd": 100000.0,
            "peak_equity_usd": 100000.0,
            "group_stop_risk_usd": {"crypto_majors": 1000.0},
            "total_stop_risk_usd": 1000.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_group.approved is False
    assert "群組" in res_group.reason

    # Total stop risk limit = 5,000 USD; already open across other groups = 5,000 USD -> REJECTED!
    res_total = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=5.0,
        account_state={
            "equity_usd": 100000.0,
            "peak_equity_usd": 100000.0,
            "group_stop_risk_usd": {"crypto_majors": 0.0},
            "total_stop_risk_usd": 5000.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_total.approved is False
    assert "總停損風險" in res_total.reason


def test_unified_sizing_short_term_sl_04pct_and_2pct_and_open_risk_cap() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    fx_inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    btc_inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # Short-term $30,000 pool: 0.75% risk = $225, 19% max notional = $5,700.
    # Tight 0.4% SL (raw = 225 / 0.004 = $56,250) -> capped at $5,700 (actual_leverage = 0.19x)
    res_04 = rm.check_new_signal(
        instrument=fx_inst,
        strategy="S2",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9960,
        tp1=1.0070,
        tp2=1.0090,
        leverage=10.0,
        account_state={"equity_usd": 100000.0, "peak_equity_usd": 100000.0, "subaccount_equity_usd": 30000.0},
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_04.approved is True
    assert res_04.notional_usd == pytest.approx(5700.0, abs=1.0)
    assert res_04.leverage == pytest.approx(0.19, abs=0.005)
    assert res_04.r_plan_usd < 40.0

    # 2% SL (raw ~225 / 0.02 = $11,250) -> also capped at $5,700 (actual_leverage = 0.19x)
    res_2 = rm.check_new_signal(
        instrument=btc_inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=10.0,
        account_state={"equity_usd": 100000.0, "peak_equity_usd": 100000.0, "subaccount_equity_usd": 30000.0},
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_2.approved is True
    assert res_2.notional_usd == pytest.approx(5700.0, abs=1.0)
    assert res_2.leverage == pytest.approx(0.19, abs=0.005)

    # Total open risk 5% ($1,500) already occupied -> rejected
    res_full = rm.check_new_signal(
        instrument=btc_inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=103.5,
        tp2=104.5,
        leverage=10.0,
        account_state={
            "equity_usd": 100000.0,
            "peak_equity_usd": 100000.0,
            "subaccount_equity_usd": 30000.0,
            "subaccount_open_risk_usd": 1500.0,
            "subaccount_open_positions": 3,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_full.approved is False
    assert "總風險已達子帳戶" in res_full.reason


def test_risk_rejected_signal_not_published_and_logged_to_watch_log(tmp_path: Path) -> None:
    cfg = load_config()
    engine = SimulationEngine(cfg, out_dir=tmp_path)

    # Paused via control.json -> rejected by risk/control -> must NOT publish signal
    cand = _candidate(symbol="BTCUSDT", strategy="S1", entry=90000.0, sl=89000.0, tp1=91800.0, tp2=92600.0, leverage=5.0)
    published = engine.submit_candidate_signal(
        cand,
        now_utc="2026-11-16T02:05:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control={"paused": True, "pause_reason": "測試手動暫停", "risk_review_done": False},
    )
    assert published is None
    signals_dir = tmp_path / "signals"
    assert not signals_dir.exists() or len(list(signals_dir.glob("*.json"))) == 0

    watch_file = tmp_path / "watch_log" / "2026-11.jsonl"
    assert watch_file.exists()
    content = watch_file.read_text(encoding="utf-8")
    assert "BTCUSDT" in content
    assert "手動暫停" in content

"""Unit tests for strategy-subaccount RiskManager & unified backtest sizing rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config_loader import load_config
from src.engine.simulator import CandidateSignal, SimulationEngine
from src.risk.manager import RiskManager
from src.signals.models import InputSnapshot


def _candidate(
    symbol: str = "BTCUSDT",
    strategy: str = "S1",
    direction: str = "long",
    ref_price: float = 90000.0,
    sl: float = 89000.0,
) -> CandidateSignal:
    return CandidateSignal(
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        ref_price=ref_price,
        stop_loss=sl,
        exit_rule="止損，或台北時間 07:45 前強制平倉",
        reasons=("規則成立",),
        snapshot=InputSnapshot(
            snapshot_id="SNAP-TEMP",
            signal_id="PENDING",
            captured_at_utc="2026-11-16T02:00:00Z",
            symbol=symbol,
            kline_summary={"close": ref_price},
            indicators={},
            filter_checks={"ok": True},
        ),
    )


def test_s1_10k_1pct_sl_sizing() -> None:
    """Requirement F: S1 subaccount $10,000 with 1% SL -> risk $100, notional ~$9,804-$9,960 after cost."""
    cfg = load_config()
    rm = RiskManager(cfg)

    # Pure FX / zero-fee instrument with 1% SL distance & 1 bps slippage (cost = 0.02% -> eff SL = 1.02%)
    fx_inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    res_fx = rm.check_new_signal(
        instrument=fx_inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={"subaccount_equity_usd": 10000.0},
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_fx.approved is True
    assert res_fx.r_plan_usd == pytest.approx(100.0, abs=1e-4)
    expected_fx_notional = 100.0 / (0.01 + 2.0 * (float(fx_inst["fee_rate"]) + 0.0001))
    assert res_fx.notional_usd == pytest.approx(expected_fx_notional, abs=0.5)

    # 10,000 S1 account, 1% SL, zero-cost baseline -> $10,000; 2 bps round-trip -> $9,960.16
    zero_cost_inst = {"symbol": "TEST", "market": "forex", "fee_rate": 0.0}
    rm_low_slip = RiskManager({**cfg, "risk": {**cfg["risk"], "default_slippage_bps": 0.2}})
    res_exact = rm_low_slip.check_new_signal(
        instrument=zero_cost_inst,
        strategy="S1",
        direction="long",
        entry_price=100.0,
        stop_loss=99.0,
        account_state={"subaccount_equity_usd": 10000.0},
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_exact.approved is True
    assert res_exact.r_plan_usd == pytest.approx(100.0, abs=1e-4)
    assert res_exact.notional_usd == pytest.approx(9960.16, abs=1.0)


def test_11th_position_rejected_and_notional_risk_caps_enforced() -> None:
    """Requirement F: reject 11th trade, scale down >$50,000 notional, and scale/reject >10% risk."""
    cfg = load_config()
    rm = RiskManager(cfg)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")

    # 1) 10 open positions -> 11th rejected
    res_11 = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={
            "subaccount_equity_usd": 10000.0,
            "subaccount_open_positions": 10,
            "subaccount_open_risk_usd": 500.0,
            "subaccount_open_notional_usd": 20000.0,
        },
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_11.approved is False
    assert "檔數已達上限" in res_11.reason

    # 2) Notional sum > $50,000 (5x of $10,000): $46,000 open -> scales new trade down to $4,000
    res_notional_scale = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={
            "subaccount_equity_usd": 10000.0,
            "subaccount_open_positions": 5,
            "subaccount_open_risk_usd": 500.0,
            "subaccount_open_notional_usd": 46000.0,
        },
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_notional_scale.approved is True
    assert res_notional_scale.notional_usd == pytest.approx(4000.0, abs=1e-2)
    assert res_notional_scale.r_plan_usd < 100.0

    # At $50,000 open notional -> rejected
    res_notional_full = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={
            "subaccount_equity_usd": 10000.0,
            "subaccount_open_positions": 5,
            "subaccount_open_risk_usd": 500.0,
            "subaccount_open_notional_usd": 50000.0,
        },
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_notional_full.approved is False
    assert "名目額度" in res_notional_full.reason

    # 3) Total risk > 10% ($1,000 on $10,000): $960 open risk -> scales down to $40 risk; $1,000 -> rejected
    res_risk_scale = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={
            "subaccount_equity_usd": 10000.0,
            "subaccount_open_positions": 9,
            "subaccount_open_risk_usd": 960.0,
            "subaccount_open_notional_usd": 30000.0,
        },
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_risk_scale.approved is True
    assert res_risk_scale.r_plan_usd == pytest.approx(40.0, abs=1e-2)

    res_risk_full = rm.check_new_signal(
        instrument=inst,
        strategy="S1",
        direction="long",
        entry_price=1.0000,
        stop_loss=0.9900,
        account_state={
            "subaccount_equity_usd": 10000.0,
            "subaccount_open_positions": 9,
            "subaccount_open_risk_usd": 1000.0,
            "subaccount_open_notional_usd": 30000.0,
        },
        control={"paused": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert res_risk_full.approved is False
    assert "總風險已達子帳戶" in res_risk_full.reason


def test_manual_pause_and_duplicate_pair_rejected(tmp_path: Path) -> None:
    cfg = load_config()
    engine = SimulationEngine(cfg, out_dir=tmp_path)

    published = engine.submit_candidate_signal(
        _candidate(),
        now_utc="2026-11-16T02:05:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control={"paused": True, "pause_reason": "測試手動暫停"},
    )
    assert published is None
    assert "手動暫停" in (tmp_path / "watch_log" / "2026-11.jsonl").read_text(encoding="utf-8")

    # Duplicate (symbol, strategy) blocked
    rm = RiskManager(cfg)
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    dup = rm.check_new_signal(
        instrument=btc,
        strategy="S1",
        direction="long",
        entry_price=90000.0,
        stop_loss=89000.0,
        account_state={"active_symbol_strategies": {("BTCUSDT", "S1")}},
        control={"paused": False},
        now_utc="2026-11-16T02:05:00Z",
    )
    assert dup.approved is False
    assert "已有進行中持倉" in dup.reason

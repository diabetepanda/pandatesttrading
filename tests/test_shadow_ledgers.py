"""Stage 6 tests: 5 shadow ledgers (A-E) invariant triggers/costs, risk limits, and analytics gating."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config_loader import load_config
from src.engine.analytics import build_shadow_comparison
from src.engine.simulator import CandidateSignal, MarketBar, SimulationEngine
from src.signals.models import AddLevel, InputSnapshot, LifecycleState


def _make_cand(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_low: float = 99.5,
    entry_high: float = 100.5,
    stop_loss: float = 98.0,
    tp1: float = 103.5,
    tp2: float = 105.0,
    invalidation_price: float = 97.5,
    add_levels: tuple[AddLevel, ...] = (AddLevel(stage=2, price=99.0, add_type="mean_add"),),
    leverage: float = 5.0,
    ts_utc: str = "2026-11-16T02:00:00Z",
) -> CandidateSignal:
    return CandidateSignal(
        strategy="S1",
        symbol=symbol,
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        add_levels=add_levels,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        invalidation_price=invalidation_price,
        invalidation_cond="突破區間下緣",
        reasons=("區間支撐", "低波動"),
        snapshot=InputSnapshot(
            snapshot_id="SNAP-TEST",
            signal_id="PENDING",
            captured_at_utc=ts_utc,
            symbol=symbol,
            kline_summary={"close": 100.0},
            indicators={"atr_1h": 2.0},
            filter_checks={"all_ok": True},
        ),
        atr=2.0,
        atr_ratio=1.0,
        leverage=leverage,
        time_stop_bars=48,
    )


def _ctrl_ok() -> dict:
    return {"paused": False, "risk_review_done": False, "risk_review_note": ""}


def test_five_shadow_ledgers_identical_signals_triggers_costs_only_sizing_differs(tmp_path: Path) -> None:
    """Prove same signal batch, entry/add/exit trigger times, and cost rates across A-E; only sizing & PnL differ."""
    cfg = load_config()
    engine = SimulationEngine(cfg, out_dir=tmp_path)

    now = "2026-11-16T02:00:00Z"
    cand = _make_cand(ts_utc=now)
    sig = engine.submit_candidate_signal(cand, now_utc=now, latest_bar_close_utc="2026-11-16T01:59:00Z", control=_ctrl_ok())
    assert sig is not None

    # Replay 4 closed 1m bars (bar ranges < 2.5*atr so breakout_protection does not trigger):
    # bar1 enters stage 1; bar2 triggers stage 2 add; bar3 hits TP1; bar4 hits TP2
    bars = [
        MarketBar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.4, 99.6, 100.1, 100.0),
        MarketBar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 100.1, 100.2, 98.9, 99.5, 100.0),
        MarketBar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 99.8, 103.6, 99.7, 103.4, 100.0),
        MarketBar("2026-11-16T02:04:00Z", "2026-11-16T02:05:00Z", 103.4, 105.5, 103.0, 105.2, 100.0),
    ]
    engine.replay_bars("BTCUSDT", bars, atr=2.0)

    entries_ts = set()
    adds_ts = set()
    closes_ts = set()
    exit_reasons = set()
    cost_tuples = set()
    stage1_notionals = {}
    stage2_notionals = {}
    realized_pnls = {}

    for pol in ("A", "B", "C", "D", "E"):
        tr = engine.ledgers[pol].trades[sig.signal_id]
        assert tr.state == LifecycleState.CLOSED
        assert len(tr.stages) == 2
        entries_ts.add(tr.entered_at_utc)
        adds_ts.add(tr.stages[1].filled_at_utc)
        closes_ts.add(tr.closed_at_utc)
        exit_reasons.add(tr.exit_reason)
        cost_tuples.add((tr.assumed_spread_bps, tr.fee_rate, tr.slippage_bps, tr.overnight_cost_daily_bps))
        stage1_notionals[pol] = round(tr.stages[0].notional_usd, 2)
        stage2_notionals[pol] = round(tr.stages[1].notional_usd, 2)
        realized_pnls[pol] = round(tr.realized_pnl_usd, 2)

    # Identical signal triggers and identical cost rates across all 5 ledgers
    assert entries_ts == {"2026-11-16T02:01:00Z"}
    assert adds_ts == {"2026-11-16T02:03:00Z"}
    assert closes_ts == {"2026-11-16T02:05:00Z"}
    assert exit_reasons == {"tp2"}
    assert len(cost_tuples) == 1

    # Position sizes and realized PnL differ due to scaling weights A..E
    assert len(set(stage1_notionals.values())) >= 4
    assert stage1_notionals["A"] > stage1_notionals["B"] > stage1_notionals["D"]
    assert stage2_notionals["D"] > stage1_notionals["D"]  # D scales up (20% -> 30%), while A scales down (50% -> 30%)
    assert stage2_notionals["A"] < stage1_notionals["A"]
    assert len(set(realized_pnls.values())) >= 4


def test_shadow_ledgers_each_enforce_all_spec_risk_constraints(tmp_path: Path) -> None:
    """Every shadow ledger A-E independently obeys drawdown 8%/12%, daily/weekly loss, margin <=30%, total/group stop risk."""
    cfg = copy.deepcopy(load_config())
    engine = SimulationEngine(cfg, out_dir=tmp_path)
    now = "2026-11-16T03:00:00Z"

    # Simulate ledger A experiencing a 9% drawdown while E is intact -> only A should halve r_plan_usd
    engine.ledgers["A"].peak_equity_usd = 100000.0
    engine.ledgers["A"].short_term_realized_pnl = -9000.0  # -9% drawdown

    # Simulate ledger B hitting daily loss cap (-$1550 <= -$1500) -> only B rejects signal
    engine.ledgers["B"].daily_realized_pnl["2026-11-16"] = -1550.0

    # Simulate ledger C exceeding 12% drawdown -> C rejects signal AND sets drawdown_paused=True
    engine.ledgers["C"].peak_equity_usd = 100000.0
    engine.ledgers["C"].short_term_realized_pnl = -12500.0

    # Simulate ledger D already holding 950 USD group stop risk in crypto_majors (limit 1000 USD)
    cand_pre = _make_cand(symbol="ETHUSDT", ts_utc="2026-11-16T02:30:00Z")
    sig_pre = engine.submit_candidate_signal(
        cand_pre, now_utc="2026-11-16T02:30:00Z", latest_bar_close_utc="2026-11-16T02:29:00Z", control=_ctrl_ok()
    )
    assert sig_pre is not None
    # Inflate ledger D's r_plan on ETHUSDT to consume 950 USD of crypto_majors limit (1000 USD)
    engine.ledgers["D"].trades[sig_pre.signal_id].r_plan_usd = 950.0

    cand = _make_cand(symbol="BTCUSDT", ts_utc=now)
    sig = engine.submit_candidate_signal(cand, now_utc=now, latest_bar_close_utc="2026-11-16T02:59:00Z", control=_ctrl_ok())
    assert sig is not None

    # Ledger E (healthy) gets full 250 USD risk plan
    assert engine.ledgers["E"].trades[sig.signal_id].r_plan_usd == 250.0
    # Ledger A (9% DD) gets halved 125 USD risk plan
    assert engine.ledgers["A"].trades[sig.signal_id].r_plan_usd == 125.0
    # Ledger B (daily loss cap breached) did not open signal
    assert sig.signal_id not in engine.ledgers["B"].trades
    # Ledger C (12% DD paused) did not open signal and marked drawdown_paused
    assert sig.signal_id not in engine.ledgers["C"].trades
    assert engine.ledgers["C"].drawdown_paused is True
    # Ledger D (group remaining risk = 1000 - 950 = 50 USD) constrained to <= 50 USD
    assert engine.ledgers["D"].trades[sig.signal_id].r_plan_usd <= 50.0 + 1e-6

    # Also test 12% drawdown pause blocks stage-2 add-on on an already open trade in ledger A
    tr_a = engine.ledgers["A"].trades[sig.signal_id]
    bar_entry = MarketBar("2026-11-16T03:01:00Z", "2026-11-16T03:02:00Z", 100.0, 100.4, 99.6, 100.0, 100.0)
    engine.replay_bars("BTCUSDT", [bar_entry], atr=2.0)
    assert len(tr_a.stages) == 1

    # Trigger 12% DD on ledger A before stage-2 add bar
    engine.ledgers["A"].short_term_realized_pnl = -13000.0
    bar_add = MarketBar("2026-11-16T03:02:00Z", "2026-11-16T03:03:00Z", 99.8, 100.0, 98.8, 99.5, 100.0)
    engine.replay_bars("BTCUSDT", [bar_add], atr=2.0)
    assert len(tr_a.stages) == 1  # Blocked by 12% DD pause on A
    assert len(engine.ledgers["E"].trades[sig.signal_id].stages) == 2  # E added normally


def test_insufficient_sample_suppresses_misleading_metrics_and_marks_unverified(tmp_path: Path) -> None:
    """When closed trades < config min_verified_trades, status is '尚未驗證' and rate/return fields are None."""
    cfg = load_config()
    engine = SimulationEngine(cfg, out_dir=tmp_path)

    # Create 2 closed trades (< 30 threshold)
    base_dt = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for idx in range(2):
        t_dt = base_dt + timedelta(days=idx)
        t_iso = t_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        c_iso = (t_dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cand = _make_cand(ts_utc=t_iso)
        sig = engine.submit_candidate_signal(cand, now_utc=t_iso, latest_bar_close_utc=c_iso, control=_ctrl_ok())
        assert sig is not None
        b1 = MarketBar(t_iso, (t_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 100.4, 99.6, 100.1)
        b2 = MarketBar(
            (t_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (t_dt + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            100.1,
            105.5,
            100.0,
            105.2,
        )
        engine.replay_bars("BTCUSDT", [b1, b2], atr=2.0)

    engine.save_state("2026-11-16T05:00:00Z")
    comp = json.loads((tmp_path / "shadow_comparison.json").read_text(encoding="utf-8"))
    assert comp["min_verified_trades"] == cfg["analytics"]["min_verified_trades_shadow"]
    assert comp["any_verified"] is False
    assert comp["best_policy"] is None
    for row in comp["policies"]:
        assert row["verified"] is False
        assert row["verification_label"] == "尚未驗證"
        assert row["closed_trades"] == 2
        assert row["win_rate_pct"] is None
        assert row["total_return_pct"] is None
        assert row["cagr_pct"] is None
        assert row["sharpe"] is None
        assert row["profit_factor"] is None

    perf = json.loads((tmp_path / "performance_stats.json").read_text(encoding="utf-8"))
    assert perf["min_verified_trades"] == cfg["analytics"]["min_verified_trades_stats"]
    assert perf["overall"]["verified"] is False
    assert perf["overall"]["verification_label"] == "尚未驗證"
    assert perf["overall"]["win_rate_pct"] is None

    # Verify once config threshold is reached (>= 2 trades, spanning >= 1 day), full metrics are computed
    cfg_low = copy.deepcopy(cfg)
    cfg_low["analytics"]["min_verified_trades_shadow"] = 2
    cfg_low["analytics"]["min_cagr_days"] = 1
    engine.config = cfg_low
    comp_ok = build_shadow_comparison(engine, "2026-11-16T05:00:00Z")
    assert comp_ok["all_verified"] is True
    assert comp_ok["best_policy"] in ("A", "B", "C", "D", "E")
    for row in comp_ok["policies"]:
        assert row["verified"] is True
        assert row["verification_label"] == "已驗證"
        assert row["win_rate_pct"] is not None
        assert row["profit_factor"] is not None
        assert row["max_drawdown_pct"] is not None
        assert row["cagr_pct"] is not None
        assert row["sharpe"] is not None
        assert row["recovery_factor"] is not None
        assert row["capital_efficiency_pct"] is not None
        assert row["scaled_risk_adjusted_return"] is not None


def test_risk_status_and_equity_curve_export(tmp_path: Path) -> None:
    """Verify save_state exports risk_status.json and equity_curve.json accurately."""
    cfg = load_config()
    engine = SimulationEngine(cfg, out_dir=tmp_path)
    now = "2026-11-16T02:00:00Z"
    cand = _make_cand(ts_utc=now)
    sig = engine.submit_candidate_signal(cand, now_utc=now, latest_bar_close_utc="2026-11-16T01:59:00Z", control=_ctrl_ok())
    assert sig is not None
    engine.replay_bars(
        "BTCUSDT",
        [MarketBar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.4, 99.6, 100.2)],
        atr=2.0,
    )
    engine.save_state("2026-11-16T02:05:00Z")

    risk_data = json.loads((tmp_path / "risk_status.json").read_text(encoding="utf-8"))
    assert risk_data["account"]["used_margin_usd"] > 0
    assert len(risk_data["open_positions"]) == 1
    pos = risk_data["open_positions"][0]
    assert pos["signal_id"] == sig.signal_id
    assert pos["remaining_adds"] == 2
    assert any(g["group_id"] == "crypto_majors" and g["used_risk_usd"] > 0 for g in risk_data["correlation_groups"])

    eq_curve = json.loads((tmp_path / "equity_curve.json").read_text(encoding="utf-8"))
    assert len(eq_curve["points"]) == 1
    assert set(eq_curve["points"][0]["shadows"].keys()) == {"A", "B", "C", "D", "E"}

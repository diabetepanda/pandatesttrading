"""Unit tests for Signal immutability, append-only updates, lifecycle, and copy format."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.signals.models import (
    InputSnapshot,
    InvalidStateTransitionError,
    LifecycleState,
    Signal,
    SignalLockedError,
    SignalUpdateEvent,
    format_signal_text,
    validate_transition,
)
from src.signals.store import SignalStore


def _sample_signal(signal_id: str = "SIG-20261116-001") -> Signal:
    return Signal(
        signal_id=signal_id,
        published_at_utc="2026-11-16T02:00:00Z",
        entered_at_utc="2026-11-16T02:00:00Z",
        strategy="S1",
        symbol="BTCUSDT",
        market="crypto",
        direction="long",
        entry_price=65291.0,
        stop_loss=64013.62,
        take_profit=None,
        exit_rule="止損，或台北時間 07:45 前強制平倉",
        risk_pct_pool=1.0,
        risk_usd=100.0,
        notional_usd=5100.0,
        subaccount_capital_usd=10000.0,
        reasons=("S1 開盤區間突破", "15m 收盤突破區間高點"),
        snapshot_id="SNAP-20261116-001",
    )


def _sample_snapshot(signal_id: str = "SIG-20261116-001") -> InputSnapshot:
    return InputSnapshot(
        snapshot_id="SNAP-20261116-001",
        signal_id=signal_id,
        captured_at_utc="2026-11-16T02:00:00Z",
        symbol="BTCUSDT",
        kline_summary={"15m_close": 90150.0, "15m_high_48": 92800.0, "15m_low_48": 89900.0},
        indicators={"adx_14": 15.2, "rsi_14": 31.4, "atr_1h": 500.0},
        filter_checks={"low_liquidity": True, "adx_ok": True, "no_high_impact_event": True},
    )


def test_signal_immutability_and_write_once_lock(tmp_path: Path) -> None:
    sig = _sample_signal()
    snap = _sample_snapshot()

    with pytest.raises(FrozenInstanceError):
        sig.stop_loss = 88000.0  # type: ignore[misc]

    store = SignalStore(tmp_path)
    sig_path = store.publish_signal(sig, snap)
    assert sig_path.exists()
    assert (tmp_path / "snapshots" / f"{snap.snapshot_id}.json").exists()
    original_bytes = sig_path.read_bytes()

    modified = _sample_signal()
    object.__setattr__(modified, "stop_loss", 85000.0)
    with pytest.raises(SignalLockedError):
        store.publish_signal(modified, snap)

    assert sig_path.read_bytes() == original_bytes


def test_signal_updates_append_only(tmp_path: Path) -> None:
    store = SignalStore(tmp_path)
    sig = _sample_signal()
    sig_path = store.publish_signal(sig, _sample_snapshot())
    sig_bytes_before = sig_path.read_bytes()

    ev1 = SignalUpdateEvent(
        event_id="EV-001",
        signal_id=sig.signal_id,
        ts_utc="2026-11-16T02:05:00Z",
        event_type="entered",
        price=90100.0,
        description="初始掛單成交",
    )
    ev2 = SignalUpdateEvent(
        event_id="EV-002",
        signal_id=sig.signal_id,
        ts_utc="2026-11-16T02:45:00Z",
        event_type="closed",
        price=66500.0,
        description="強制平倉結束",
    )

    upd_path = store.append_update(ev1)
    store.append_update(ev2)

    assert sig_path.read_bytes() == sig_bytes_before
    loaded = store.load_updates(sig.signal_id)
    assert [e.event_id for e in loaded] == ["EV-001", "EV-002"]
    assert upd_path.name == "2026-11.jsonl"


def test_signal_lifecycle_state_transitions() -> None:
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason="stop_loss") == LifecycleState.CLOSED
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason="trailing_stop") == LifecycleState.CLOSED
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason="time_stop") == LifecycleState.CLOSED
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason="session_stop") == LifecycleState.CLOSED
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason="rebalance_exit") == LifecycleState.CLOSED

    with pytest.raises(InvalidStateTransitionError):
        validate_transition(LifecycleState.CLOSED, LifecycleState.ACTIVE)

    with pytest.raises(InvalidStateTransitionError):
        validate_transition(LifecycleState.CANCELLED, LifecycleState.ACTIVE)

    with pytest.raises(InvalidStateTransitionError):
        validate_transition(LifecycleState.ACTIVE, LifecycleState.CLOSED, exit_reason=None)


def test_signal_copy_text_format() -> None:
    txt = format_signal_text(_sample_signal())
    assert "BTCUSDT" in txt
    assert "做多" in txt
    assert "65,291" in txt
    assert "台北時間 11/16 10:00 成交" in txt
    assert "64,013.62" in txt
    assert "-$100" in txt
    assert "佔 S1 策略資金 1.0%" in txt
    assert "出場規則：止損，或台北時間 07:45 前強制平倉" in txt
    assert "倉位名目：$5,100（佔 S1 策略資金 51.0%）" in txt
    assert "TP1" not in txt and "TP2" not in txt and "槓桿" not in txt and "有效期限" not in txt
    import re

    assert not re.search(r"\d{4}-\d{2}-\d{2}T|\d{2}:\d{2}Z", txt)
    assert txt.strip().endswith("模擬驗證中，非投資建議")

"""Unit tests for Signal immutability, append-only updates, lifecycle, and copy format."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.signals.models import (
    AddLevel,
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
        strategy="A",
        symbol="BTCUSDT",
        market="crypto",
        direction="long",
        entry_low=90000.0,
        entry_high=90200.0,
        add_levels=(
            AddLevel(stage=2, add_type="mean_add", price=89600.0, note="ATR逆勢補倉"),
        ),
        stop_loss=89000.0,
        tp1=91800.0,
        tp2=92600.0,
        rr_ratio=2.36,
        risk_pct_pool=1.0,
        risk_usd=200.0,
        notional_usd=2000.0,
        leverage=5.0,
        liquidation_price=81000.0,
        valid_until_utc="2026-11-16T06:00:00Z",
        invalidation_cond="15m 收盤跌破 89000 區間失效",
        time_stop_bars=24,
        reasons=("處於低流動性時段", "1H ADX < 20", "RSI < 35"),
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
        event_type="tp1_hit",
        price=91800.0,
        description="達成 TP1，平倉 50%",
    )

    upd_path = store.append_update(ev1)
    store.append_update(ev2)

    assert sig_path.read_bytes() == sig_bytes_before
    loaded = store.load_updates(sig.signal_id)
    assert [e.event_id for e in loaded] == ["EV-001", "EV-002"]
    assert upd_path.name == "2026-11.jsonl"


def test_signal_lifecycle_state_transitions() -> None:
    assert validate_transition(LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE) == LifecycleState.ACTIVE
    assert validate_transition(LifecycleState.ACTIVE, LifecycleState.TP1_HIT) == LifecycleState.TP1_HIT
    assert validate_transition(LifecycleState.TP1_HIT, LifecycleState.CLOSED, exit_reason="tp2") == LifecycleState.CLOSED
    assert (
        validate_transition(LifecycleState.WAITING_ENTRY, LifecycleState.CANCELLED, exit_reason="expired")
        == LifecycleState.CANCELLED
    )

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
    assert "90000" in txt
    assert "90200" in txt
    assert "89600" in txt
    assert "89000" in txt
    assert "91800" in txt
    assert "92600" in txt
    assert "1.0%" in txt or "1%" in txt
    assert "2026-11-16T06:00:00Z" in txt
    assert "15m 收盤跌破 89000" in txt
    assert txt.strip().endswith("模擬驗證中，非投資建議")

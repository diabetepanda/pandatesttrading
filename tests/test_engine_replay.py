"""Unit tests for SimulationEngine: fills, exits, gap open, replay, idempotency, and schedule delay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config_loader import load_config
from src.engine.simulator import CandidateSignal, MarketBar, SimulationEngine
from src.signals.models import AddLevel, InputSnapshot, LifecycleState


def _make_candidate(
    symbol: str = "BTCUSDT",
    strategy: str = "S1",
    direction: str = "long",
    entry_low: float = 99.5,
    entry_high: float = 100.5,
    add_levels: tuple[AddLevel, ...] = (),
    stop_loss: float = 98.0,
    tp1: float = 103.5,
    tp2: float = 105.0,
    invalidation_price: float = 97.5,
    leverage: float = 5.0,
    time_stop_bars: int = 24,
) -> CandidateSignal:
    return CandidateSignal(
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        add_levels=add_levels,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        invalidation_price=invalidation_price,
        invalidation_cond="價格觸及失效價",
        reasons=("區間邊緣測試", "低波動環境"),
        snapshot=InputSnapshot(
            snapshot_id="SNAP-001",
            signal_id="PENDING",
            captured_at_utc="2026-11-16T02:00:00Z",
            symbol=symbol,
            kline_summary={"close": 100.0},
            indicators={"atr_1h": 2.0},
            filter_checks={"all_ok": True},
        ),
        atr=2.0,
        atr_ratio=1.0,
        leverage=leverage,
        time_stop_bars=time_stop_bars,
    )


def _bar(ts: str, close_ts: str, o: float, h: float, l: float, c: float, v: float = 100.0) -> MarketBar:
    return MarketBar(ts_utc=ts, close_ts_utc=close_ts, open=o, high=h, low=l, close=c, volume=v)


def _ctrl_ok() -> dict:
    return {"paused": False, "risk_review_done": False, "risk_review_note": ""}


def test_stop_loss_trigger_long_and_short(tmp_path: Path) -> None:
    cfg = load_config()
    # Long SL
    eng = SimulationEngine(cfg, out_dir=tmp_path / "long")
    sig_l = eng.submit_candidate_signal(
        _make_candidate(direction="long", entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig_l is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.2, 100.4, 99.8, 100.0))
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 99.8, 99.9, 97.9, 98.1))
    trade_l = eng.primary_ledger.trades[sig_l.signal_id]
    assert trade_l.state == LifecycleState.CLOSED
    assert trade_l.exit_reason == "stop_loss"
    assert trade_l.realized_pnl_usd < 0
    assert trade_l.r_multiple < 0

    # Short SL
    eng_s = SimulationEngine(cfg, out_dir=tmp_path / "short")
    sig_s = eng_s.submit_candidate_signal(
        _make_candidate(direction="short", entry_low=99.5, entry_high=100.5, stop_loss=102.0, tp1=96.5, tp2=95.0, invalidation_price=102.5),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig_s is not None
    eng_s.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.3, 99.7, 100.0))
    eng_s.process_bar("BTCUSDT", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 100.2, 102.2, 100.1, 102.1))
    trade_s = eng_s.primary_ledger.trades[sig_s.signal_id]
    assert trade_s.state == LifecycleState.CLOSED
    assert trade_s.exit_reason == "stop_loss"
    assert trade_s.realized_pnl_usd < 0


def test_tp1_partial_close_and_tp2_full_close(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(direction="long", entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    trade = eng.primary_ledger.trades[sig.signal_id]
    init_qty = trade.remaining_qty

    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 101.0, 103.6, 100.9, 103.4))
    assert trade.state == LifecycleState.TP1_HIT
    assert trade.remaining_qty == pytest.approx(init_qty * 0.5)
    assert trade.current_stop_loss >= trade.avg_entry_price  # Moved to breakeven

    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 103.4, 105.2, 103.2, 105.1))
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "tp2"
    assert trade.remaining_qty == 0.0
    assert trade.realized_pnl_usd > 0


def test_partial_close_realized_pnl_uses_weighted_avg_cost(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(
            direction="long",
            entry_low=99.5,
            entry_high=100.5,
            add_levels=(AddLevel(stage=2, add_type="mean_add", price=98.5, note="逆勢補倉"),),
            stop_loss=96.0,
            tp1=106.0,
            tp2=108.0,
            invalidation_price=95.5,
            leverage=3.0,
        ),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    # Stage 1 fill around 100.0
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    # Stage 2 mean_add fill at 98.5
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 99.5, 99.6, 98.3, 98.6))
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert len(trade.stages) == 2
    expected_avg = sum(s.fill_price * s.qty for s in trade.stages) / sum(s.qty for s in trade.stages)
    assert trade.avg_entry_price == pytest.approx(expected_avg)

    # Trigger TP1 at 106.0
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 102.0, 106.2, 101.9, 106.0))
    assert trade.state == LifecycleState.TP1_HIT
    assert trade.avg_entry_price == pytest.approx(expected_avg)
    assert trade.realized_pnl_usd > 0


def test_same_bar_sl_and_tp_triggers_sl_conservatively(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(direction="long", entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.1, 99.9, 100.0))
    # Wide bar touching BOTH SL (98.0) and TP2 (105.0) -> must exit via stop_loss!
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 100.0, 106.0, 97.5, 105.5))
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "stop_loss"


def test_pending_order_expiration_and_pre_entry_invalidation(tmp_path: Path) -> None:
    cfg = load_config()
    # 1) Expiration after 4 hours without touching entry zone
    eng1 = SimulationEngine(cfg, out_dir=tmp_path / "exp")
    sig1 = eng1.submit_candidate_signal(
        _make_candidate(entry_low=98.5, entry_high=99.0, stop_loss=96.5, tp1=102.5, tp2=104.0, invalidation_price=96.0),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig1 is not None
    eng1.process_bar("BTCUSDT", _bar("2026-11-16T06:00:00Z", "2026-11-16T06:01:00Z", 101.0, 101.5, 100.8, 101.2))
    assert eng1.primary_ledger.trades[sig1.signal_id].state == LifecycleState.CANCELLED
    assert eng1.primary_ledger.trades[sig1.signal_id].exit_reason == "expired"

    # 2) Pre-entry invalidation when price breaks invalidation level before entering
    eng2 = SimulationEngine(cfg, out_dir=tmp_path / "inv")
    sig2 = eng2.submit_candidate_signal(
        _make_candidate(entry_low=99.0, entry_high=99.5, stop_loss=97.0, tp1=103.0, tp2=104.5, invalidation_price=96.5),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig2 is not None
    eng2.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 96.8, 96.9, 96.2, 96.4))
    assert eng2.primary_ledger.trades[sig2.signal_id].state == LifecycleState.CANCELLED
    assert eng2.primary_ledger.trades[sig2.signal_id].exit_reason == "pre_entry_invalidated"


def test_time_stop_closes_position(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0, time_stop_bars=2),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    # Entry at 02:01
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    # Hold for 2 * 15m = 30 minutes -> bar closing at 02:32 triggers time_stop
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:31:00Z", "2026-11-16T02:32:00Z", 100.4, 100.6, 100.2, 100.5))
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "time_stop"


def test_session_end_stop_closes_position(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    # Low-liquidity session ends at 04:00Z -> forced close at <= 30m before end (03:30Z)
    eng.process_bar(
        "BTCUSDT",
        _bar("2026-11-16T03:30:00Z", "2026-11-16T03:31:00Z", 100.5, 100.6, 100.4, 100.5),
        session_end_utc="2026-11-16T04:00:00Z",
    )
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "session_stop"


def test_breakout_protection_closes_at_adverse_bar_price(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(entry_low=99.5, entry_high=100.5, stop_loss=98.0, tp1=103.5, tp2=105.0),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    # Huge spike bar: range = 104.0 - 98.5 = 5.5 > 2.5 * atr (atr=2.0 -> 5.0)
    # Adverse price for long is bar.low (98.5, before hitting SL 98.0)
    eng.process_bar(
        "BTCUSDT",
        _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 100.0, 104.0, 98.5, 103.0),
        atr=2.0,
        atr_ratio=1.1,
    )
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "breakout_protection"
    assert trade.exit_price <= 98.5


def test_stop_loss_never_moves_in_adverse_direction_and_recalculates_after_add(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(
            direction="long",
            entry_low=99.5,
            entry_high=100.5,
            add_levels=(AddLevel(stage=2, add_type="mean_add", price=98.5, note="補倉"),),
            stop_loss=97.0,
            tp1=104.5,
            tp2=106.5,
            invalidation_price=96.5,
            leverage=4.0,
        ),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.2, 99.8, 100.0))
    trade = eng.primary_ledger.trades[sig.signal_id]
    # Attempt to move SL adversely (97.0 -> 95.0 for long) must fail / keep 97.0
    assert eng.move_stop_loss(sig.signal_id, 95.0, "2026-11-16T02:02:00Z", "嘗試惡化止損") is False
    assert trade.current_stop_loss == 97.0

    # Move SL favorably (97.0 -> 97.5) succeeds
    assert eng.move_stop_loss(sig.signal_id, 97.5, "2026-11-16T02:02:30Z", "保護性上移") is True
    assert trade.current_stop_loss == 97.5

    # Stage 2 add fill at 98.5 recalculates TP/SL without ever moving SL below 97.5
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 99.0, 99.2, 98.3, 98.6))
    assert len(trade.stages) == 2
    assert trade.current_stop_loss >= 97.5

    # Price reaches +2R -> trailing stop activates and moves SL above avg_entry_price
    eng.process_bar(
        "BTCUSDT",
        _bar("2026-11-16T02:04:00Z", "2026-11-16T02:05:00Z", 102.0, 104.2, 101.8, 104.0),
        atr=1.0,
    )
    assert trade.current_stop_loss > trade.avg_entry_price


def test_gap_open_beyond_stop_fills_at_open_price(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(symbol="EURUSD", entry_low=1.0800, entry_high=1.0820, stop_loss=1.0700, tp1=1.1000, tp2=1.1100, invalidation_price=1.0680, leverage=10.0),
        now_utc="2026-11-16T02:00:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    eng.process_bar("EURUSD", _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 1.0810, 1.0815, 1.0805, 1.0810))
    # Market gaps down at open to 1.0600 (well below SL 1.0700) -> fill at open (~1.0600), NOT 1.0700
    eng.process_bar("EURUSD", _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 1.0600, 1.0620, 1.0590, 1.0610))
    trade = eng.primary_ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price <= 1.0600


def test_schedule_delay_blocks_new_signal_and_logs_watch(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    delayed_sig = eng.submit_candidate_signal(
        _make_candidate(),
        now_utc="2026-11-16T02:35:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",  # 35m delay > 20m threshold
        control=_ctrl_ok(),
    )
    assert delayed_sig is None
    watch_txt = (tmp_path / "watch_log" / "2026-11.jsonl").read_text(encoding="utf-8")
    assert "排程延遲" in watch_txt


def test_catchup_replay_processes_missed_bars_chronologically(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(
            entry_low=99.5,
            entry_high=100.5,
            add_levels=(AddLevel(stage=2, add_type="mean_add", price=98.8, note="補倉"),),
            stop_loss=97.5,
            tp1=104.5,
            tp2=106.0,
            invalidation_price=97.0,
            leverage=4.0,
        ),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    # Feed bars out of order to verify chronological sorting during catch-up replay
    bars = [
        _bar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 101.0, 104.7, 100.9, 104.6),  # TP1
        _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 99.8, 99.9, 98.6, 99.0),     # Add fill
        _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.3, 99.8, 100.1),   # Entry
    ]
    events = eng.replay_bars("BTCUSDT", bars)
    assert len(events) >= 3
    assert eng.last_processed_ts["BTCUSDT"] == "2026-11-16T02:04:00Z"
    assert eng.primary_ledger.trades[sig.signal_id].state == LifecycleState.TP1_HIT
    eng.save_state()
    assert (tmp_path / "trades.json").exists()
    assert (tmp_path / "trade_stages.json").exists()
    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "equity_snapshots" / "2026-11.jsonl").exists()
    state_json = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state_json["last_processed_ts"]["BTCUSDT"] == "2026-11-16T02:04:00Z"


def test_idempotent_replay_does_not_duplicate_events_or_fills(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(entry_low=99.5, entry_high=100.5, stop_loss=97.5, tp1=104.5, tp2=106.0, leverage=4.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    bars = [
        _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.3, 99.8, 100.1),
        _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 100.1, 104.7, 100.0, 104.6),
    ]
    first_events = eng.replay_bars("BTCUSDT", bars)
    assert len(first_events) >= 2
    fills_before = len(eng.primary_ledger.fills)
    second_events = eng.replay_bars("BTCUSDT", bars)
    assert second_events == []
    assert len(eng.primary_ledger.fills) == fills_before


def test_five_shadow_ledgers_track_parallel_positions(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(
            entry_low=99.5,
            entry_high=100.5,
            add_levels=(AddLevel(stage=2, add_type="mean_add", price=98.8, note="補倉"),),
            stop_loss=97.5,
            tp1=104.5,
            tp2=106.0,
            invalidation_price=97.0,
            leverage=4.0,
        ),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
    )
    assert sig is not None
    bars = [
        _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.3, 99.8, 100.1),
        _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 99.8, 99.9, 98.6, 99.0),
        _bar("2026-11-16T02:03:00Z", "2026-11-16T02:04:00Z", 101.0, 104.7, 100.9, 104.6),
    ]
    eng.replay_bars("BTCUSDT", bars)
    for pol in ("A", "B", "C", "D", "E"):
        assert sig.signal_id in eng.ledgers[pol].trades
        assert eng.ledgers[pol].trades[sig.signal_id].state == LifecycleState.TP1_HIT

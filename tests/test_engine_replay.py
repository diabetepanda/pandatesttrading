"""Unit tests for unified SimulationEngine: fills, SL/TP, replay, migration, and screener exact parity."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.backtest.screener import simulate_single_symbol
from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.engine.simulator import CandidateSignal, MarketBar, SimulationEngine
from src.jobs.live_strategy import evaluate_live_single_strategy_step
from src.signals.models import InputSnapshot, LifecycleState


def _make_candidate(
    symbol: str = "BTCUSDT",
    strategy: str = "S1",
    direction: str = "long",
    ref_price: float = 100.0,
    stop_loss: float = 98.0,
    take_profit: float | None = None,
) -> CandidateSignal:
    return CandidateSignal(
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        ref_price=ref_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        exit_rule="止損或策略出場",
        reasons=("策略條件成立",),
        snapshot=InputSnapshot(
            snapshot_id="SNAP-001",
            signal_id="PENDING",
            captured_at_utc="2026-11-16T02:00:00Z",
            symbol=symbol,
            kline_summary={"close": ref_price},
            indicators={},
            filter_checks={"all_ok": True},
        ),
    )


def _bar(ts: str, close_ts: str, o: float, h: float, l: float, c: float, v: float = 100.0) -> MarketBar:
    return MarketBar(ts_utc=ts, close_ts_utc=close_ts, open=o, high=h, low=l, close=c, volume=v)


def _ctrl_ok() -> dict:
    return {"paused": False}


def test_stop_loss_trigger_long_and_short(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path / "long")
    sig_l = eng.submit_candidate_signal(
        _make_candidate(direction="long", ref_price=100.0, stop_loss=98.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
        entry_bar=_bar("2026-11-16T02:00:00Z", "2026-11-16T02:15:00Z", 100.0, 100.4, 99.8, 100.0),
    )
    assert sig_l is not None
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:15:00Z", "2026-11-16T02:16:00Z", 99.8, 99.9, 97.9, 98.1))
    trade_l = eng.ledger.trades[sig_l.signal_id]
    assert trade_l.state == LifecycleState.CLOSED
    assert trade_l.exit_reason == "stop_loss"
    assert trade_l.realized_pnl_usd < 0
    assert trade_l.r_multiple < 0

    eng_s = SimulationEngine(cfg, out_dir=tmp_path / "short")
    sig_s = eng_s.submit_candidate_signal(
        _make_candidate(direction="short", ref_price=100.0, stop_loss=102.0),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
        entry_bar=_bar("2026-11-16T02:00:00Z", "2026-11-16T02:15:00Z", 100.0, 100.3, 99.7, 100.0),
    )
    assert sig_s is not None
    eng_s.process_bar("BTCUSDT", _bar("2026-11-16T02:15:00Z", "2026-11-16T02:16:00Z", 100.2, 102.2, 100.1, 102.1))
    trade_s = eng_s.ledger.trades[sig_s.signal_id]
    assert trade_s.state == LifecycleState.CLOSED
    assert trade_s.exit_reason == "stop_loss"
    assert trade_s.realized_pnl_usd < 0


def test_same_bar_sl_and_tp_triggers_sl_conservatively(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(direction="long", ref_price=100.0, stop_loss=98.0, take_profit=103.5),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
        entry_bar=_bar("2026-11-16T02:00:00Z", "2026-11-16T02:15:00Z", 100.0, 100.3, 99.8, 100.1),
    )
    assert sig is not None
    # Single bar touches BOTH stop_loss (98.0) and take_profit (103.5) -> SL wins conservatively
    eng.process_bar("BTCUSDT", _bar("2026-11-16T02:15:00Z", "2026-11-16T02:16:00Z", 100.0, 104.5, 97.5, 104.0))
    trade = eng.ledger.trades[sig.signal_id]
    assert trade.state == LifecycleState.CLOSED
    assert trade.exit_reason == "stop_loss"


def test_gap_open_beyond_stop_fills_at_open_price(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(symbol="EURUSD", ref_price=1.0810, stop_loss=1.0700),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
        entry_bar=_bar("2026-11-16T02:00:00Z", "2026-11-16T02:15:00Z", 1.0810, 1.0815, 1.0805, 1.0810),
    )
    assert sig is not None
    eng.process_bar("EURUSD", _bar("2026-11-16T02:15:00Z", "2026-11-16T02:16:00Z", 1.0600, 1.0620, 1.0590, 1.0610))
    trade = eng.ledger.trades[sig.signal_id]
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


def test_catchup_replay_and_idempotency(tmp_path: Path) -> None:
    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        _make_candidate(ref_price=100.0, stop_loss=97.5, take_profit=104.5),
        now_utc="2026-11-16T02:01:00Z",
        latest_bar_close_utc="2026-11-16T02:00:00Z",
        control=_ctrl_ok(),
        entry_bar=_bar("2026-11-16T02:00:00Z", "2026-11-16T02:01:00Z", 100.0, 100.2, 99.8, 100.1),
    )
    assert sig is not None
    bars = [
        _bar("2026-11-16T02:02:00Z", "2026-11-16T02:03:00Z", 101.0, 104.7, 100.9, 104.6),
        _bar("2026-11-16T02:01:00Z", "2026-11-16T02:02:00Z", 100.0, 100.5, 99.9, 100.4),
    ]
    events = eng.replay_bars("BTCUSDT", bars)
    assert len(events) == 1
    assert eng.ledger.trades[sig.signal_id].state == LifecycleState.CLOSED
    assert eng.ledger.trades[sig.signal_id].exit_reason == "take_profit"

    fills_before = len(eng.ledger.fills)
    assert eng.replay_bars("BTCUSDT", bars) == []
    assert len(eng.ledger.fills) == fills_before
    eng.save_state()
    assert (tmp_path / "trades.json").exists()
    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "equity_snapshots" / "2026-11.jsonl").exists()


def test_legacy_state_migration_resets_sl_removes_tp_keeps_closed_intact(tmp_path: Path) -> None:
    """Requirement D: active trades keep entry & qty, reset SL=initial_stop_loss, drop TP1/TP2; closed stay untouched."""
    cfg = load_config()
    active_sid = "SIG-20261116-0009"
    closed_sid = "SIG-20261116-0012"

    legacy_state = {
        "sequences": {"signal": 13, "event": 11, "fill": 35},
        "latest_marks": {"BTCUSDT": 65500.0, "BNBUSDT": 585.0},
        "ledger": {
            "trades": {
                active_sid: {
                    "signal_id": active_sid,
                    "strategy": "S1",
                    "symbol": "BTCUSDT",
                    "market": "crypto",
                    "correlation_group": "crypto_majors",
                    "direction": "long",
                    "state": "tp1_hit",
                    "entry_low": 65200.0,
                    "entry_high": 65300.0,
                    "initial_stop_loss": 64013.62,
                    "current_stop_loss": 65232.61,  # moved breakeven SL must reset to initial_stop_loss
                    "current_tp1": 68000.0,
                    "current_tp2": 69500.0,
                    "r_plan_usd": 100.0,
                    "planned_notional_usd": 495.65,
                    "avg_entry_price": 65232.61,
                    "remaining_qty": 0.00759823,
                    "initial_filled_qty": 0.00759823,
                    "realized_pnl_usd": -0.2478,
                    "entered_at_utc": "2026-11-16T14:51:00Z",
                    "published_at_utc": "2026-11-16T14:52:00Z",
                },
                closed_sid: {
                    "signal_id": closed_sid,
                    "strategy": "S1",
                    "symbol": "BNBUSDT",
                    "market": "crypto",
                    "correlation_group": "crypto_majors",
                    "direction": "long",
                    "state": "closed",
                    "entry_low": 593.0,
                    "entry_high": 595.0,
                    "initial_stop_loss": 586.12,
                    "current_stop_loss": 586.12,
                    "r_plan_usd": 9.56,
                    "planned_notional_usd": 495.65,
                    "avg_entry_price": 594.198218,
                    "remaining_qty": 0.0,
                    "initial_filled_qty": 0.834149,
                    "realized_pnl_usd": -7.282,
                    "entered_at_utc": "2026-11-16T19:00:00Z",
                    "closed_at_utc": "2026-11-16T20:10:00Z",
                    "exit_price": 586.06,
                    "exit_reason": "stop_loss",
                    "published_at_utc": "2026-11-16T19:01:00Z",
                },
            },
            "fills": [
                {
                    "fill_id": "F1",
                    "signal_id": active_sid,
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "action": "entry",
                    "price": 65232.61,
                    "qty": 0.00759823,
                    "notional_usd": 495.65,
                    "fee_usd": 0.2478,
                    "realized_pnl_usd": -0.2478,
                    "ts_utc": "2026-11-16T14:51:00Z",
                },
                {
                    "fill_id": "F2",
                    "signal_id": closed_sid,
                    "symbol": "BNBUSDT",
                    "side": "buy",
                    "action": "entry",
                    "price": 594.198218,
                    "qty": 0.834149,
                    "notional_usd": 495.65,
                    "fee_usd": 0.2478,
                    "realized_pnl_usd": -0.2478,
                    "ts_utc": "2026-11-16T19:00:00Z",
                },
                {
                    "fill_id": "F3",
                    "signal_id": closed_sid,
                    "symbol": "BNBUSDT",
                    "side": "sell",
                    "action": "exit",
                    "price": 586.06,
                    "qty": 0.834149,
                    "notional_usd": 488.86,
                    "fee_usd": 0.2444,
                    "realized_pnl_usd": -7.0342,
                    "ts_utc": "2026-11-16T20:10:00Z",
                },
            ],
        },
    }
    (tmp_path / "ledgers_state.json").write_text(json.dumps(legacy_state), encoding="utf-8")
    (tmp_path / "signals").mkdir(parents=True, exist_ok=True)
    for sid, sym in ((active_sid, "BTCUSDT"), (closed_sid, "BNBUSDT")):
        (tmp_path / "signals" / f"{sid}.json").write_text(
            json.dumps({"signal_id": sid, "symbol": sym, "strategy": "S1", "published_at_utc": "2026-11-16T15:00:00Z"}),
            encoding="utf-8",
        )

    eng = SimulationEngine(cfg, out_dir=tmp_path)
    eng.load_state()
    eng.save_state("2026-11-16T21:00:00Z")

    tr_act = eng.ledger.trades[active_sid]
    assert tr_act.state == LifecycleState.ACTIVE
    assert tr_act.avg_entry_price == pytest.approx(65232.61, abs=1e-4)
    assert tr_act.remaining_qty == pytest.approx(0.00759823, abs=1e-8)
    assert tr_act.current_stop_loss == pytest.approx(64013.62, abs=1e-4)
    assert tr_act.take_profit is None

    tr_closed = eng.ledger.trades[closed_sid]
    assert tr_closed.state == LifecycleState.CLOSED
    assert tr_closed.exit_reason == "stop_loss"

    # Realized PnL exported to signals_index.json matches sum of fills
    idx = json.loads((tmp_path / "signals_index.json").read_text(encoding="utf-8"))
    closed_row = next(r for r in idx if r["signal_id"] == closed_sid)
    assert closed_row["realized_pnl_usd"] == pytest.approx(-7.28, abs=0.01)
    assert closed_row["exit_reason_label"] == "止損"


def _make_parity_bars(strategy_id: str) -> tuple[list[OHLCVBar], str, dict]:
    if strategy_id == "S1":
        # 15m bars across 2 UTC days: Day 1 builds ATR, Day 2 00:00-01:00 builds ORB ~100..101,
        # breaks out at 01:15 -> enters at 01:30 open -> hits EOD exit at 23:15 -> closes at 23:30 open
        bars: list[OHLCVBar] = []
        t0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(96):  # Day 1: 96 bars
            dt = t0 + timedelta(minutes=15 * i)
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 100.6, 99.4, 100.0, 100.0))
        for i in range(96):  # Day 2
            dt = t0 + timedelta(days=1, minutes=15 * i)
            if i < 4:  # 00:00..00:45 ORB window (range 99.5..100.5)
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 100.5, 99.5, 100.1, 100.0))
            elif i == 5:  # 01:15 breakout close > 100.5 -> triggers entry at 01:30 open
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.2, 101.8, 100.1, 101.5, 100.0))
            elif i < 93:
                px = 102.0 + 0.02 * (i - 5)
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), px, px + 0.3, px - 0.2, px + 0.1, 100.0))
            else:
                # 23:15 triggers EOD close -> exits at 23:30 open!
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 103.5, 103.7, 103.3, 103.4, 100.0))
        return bars, "15m", {"session_open_utc": "00:00", "range_minutes": 60, "sl_mode": "atr_2.0", "eod_close_utc": "23:45"}

    if strategy_id == "S2":
        # 4h bars: 25 calm bars (100..101), breakout up on bar 25 -> enter bar 26 open -> drift down below 6-bar low -> exit next open
        bars = []
        t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(45):
            dt = t0 + timedelta(hours=4 * i)
            if i < 25:
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 101.0, 99.5, 100.2, 100.0))
            elif i == 25:
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.5, 105.0, 100.4, 104.5, 100.0))
            elif i < 33:
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 104.8, 106.0, 104.0, 105.5, 100.0))
            elif i == 33:
                # Drop below trailing 6-bar low (~104.0) without hitting SL (~101.5) -> triggers trailing_stop exit!
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 104.2, 104.4, 103.0, 103.2, 100.0))
            else:
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 103.1, 103.5, 102.8, 103.0, 100.0))
        return bars, "4h", {"breakout_bars": 12, "trail_bars": 6, "sl_atr_mult": 2.5, "max_hold_bars": 30}

    if strategy_id == "S3":
        # 1d bars: 205 uptrend bars > 200 SMA, sharp 2-day pullback triggers RSI(2)<15 -> enters -> bounces > 5 SMA -> exits
        bars = []
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(220):
            dt = t0 + timedelta(days=i)
            if i < 204:
                c = 100.0 + i * 0.25
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), c - 0.2, c + 0.5, c - 0.5, c, 100.0))
            elif i in (204, 205):
                prev_c = bars[-1].close
                c = prev_c - 3.0
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), prev_c, prev_c + 0.2, c - 0.2, c, 100.0))
            elif i == 206:
                prev_c = bars[-1].close
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), prev_c, prev_c + 1.0, prev_c - 0.5, prev_c + 0.8, 100.0))
            else:
                prev_c = bars[-1].close
                c = prev_c + 3.0
                bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), prev_c, c + 0.5, prev_c - 0.2, c, 100.0))
        return bars, "1d", {"trend_ma_period": 200, "rsi_period": 2, "rsi_long_thresh": 15.0, "rsi_short_thresh": 85.0, "exit_ma_period": 5, "allow_short": False, "sl_atr_mult": 4.0, "max_hold_bars": 10}

    # L1: Donchian 20/10 on 1d
    bars = []
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    for i in range(55):
        dt = t0 + timedelta(days=i)
        if i < 25:
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0, 101.0, 99.5, 100.2, 100.0))
        elif i == 25:
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.3, 104.0, 100.2, 103.5, 100.0))
        elif i < 37:
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 103.8, 105.0, 103.0, 104.2, 100.0))
        elif i == 37:
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 103.2, 103.3, 102.0, 102.1, 100.0))
        else:
            bars.append(OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), 102.0, 102.5, 101.8, 102.1, 100.0))
    return bars, "1d", {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.015}


@pytest.mark.parametrize("strategy_id", ["S1", "S2", "S3", "L1"])
def test_backtest_screener_vs_live_simulator_exact_parity(tmp_path: Path, strategy_id: str) -> None:
    """Requirement F: screener and live simulator produce identical entry/exit time, price, qty, and PnL."""
    cfg = load_config()
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars, interval, params = _make_parity_bars(strategy_id)
    dur_sec = {"15m": 900, "4h": 14400, "1d": 86400}[interval]

    bt_trades, _ = simulate_single_symbol(
        bars=bars,
        strategy_id=strategy_id,
        params=params,
        instrument=inst,
        config=cfg,
        interval=interval,
        allow_short=True,
    )
    assert len(bt_trades) >= 1, f"Expected >= 1 backtest trade for {strategy_id}"

    eng = SimulationEngine(cfg, out_dir=tmp_path / strategy_id)
    runtime_cfg = (params, "已驗證")
    for idx in range(15, len(bars) - 1):
        b = bars[idx]
        nxt = bars[idx + 1]
        close_dt = datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(seconds=dur_sec)
        nxt_close_dt = datetime.strptime(nxt.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(seconds=dur_sec)

        # Evaluate at close of bar `idx`, filling entry/strategy-exit at `nxt.open` (`idx + 1`), matching screener.py order!
        nxt_mkt = MarketBar(
            ts_utc=nxt.ts_utc,
            close_ts_utc=nxt_close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=nxt.open,
            high=nxt.high,
            low=nxt.low,
            close=nxt.close,
            volume=nxt.volume,
        )
        evaluate_live_single_strategy_step(
            engine=eng,
            strategy_id=strategy_id,
            inst=inst,
            closed_bars=bars[: idx + 1],
            runtime_cfg=runtime_cfg,
            now_iso=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            control=_ctrl_ok(),
            allow_new_entry=True,
            next_bar=nxt_mkt,
        )
        # Process intra-bar SL / TP on bar `nxt` (`idx + 1`) identical to screener step 3
        eng.process_bar("BTCUSDT", nxt_mkt)

    live_closed = sorted(
        [t for t in eng.ledger.trades.values() if t.state == LifecycleState.CLOSED],
        key=lambda t: t.entered_at_utc or "",
    )
    assert len(live_closed) == len(bt_trades), (
        f"{strategy_id} trade count mismatch: live={len(live_closed)} vs bt={len(bt_trades)}"
    )
    for bt, lt in zip(bt_trades, live_closed):
        assert lt.entered_at_utc == bt.entry_ts_utc
        assert lt.closed_at_utc == bt.exit_ts_utc
        assert round(lt.avg_entry_price, 6) == bt.entry_price
        assert round(lt.exit_price or 0.0, 6) == bt.exit_price
        assert lt.realized_pnl_usd == pytest.approx(bt.net_pnl_usd, abs=1e-2)

"""Unit tests for Strategy A filters, range detection, closed-bar rule, and integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.strategies.strategy_a import StrategyA


def _build_strategy_a_fixture_bars(
    now_utc: datetime,
    direction: str = "long",
    include_unclosed_spike: bool = False,
) -> list[OHLCVBar]:
    """Construct closed 15m bars spanning 25 days with clear low-liquidity window,
    low ADX/BB compression, and a well-tested 48-bar 15m range ending in an RSI extreme.
    """
    # Build 2000 15m bars (~20.8 days) ending at now_utc
    total = 2000
    start = now_utc - timedelta(minutes=15 * total)
    bars: list[OHLCVBar] = []

    # First 1952 bars: earlier bars swing across hours so 1H closes vary (wide BB width earlier),
    # and hours 02..09 UTC have lowest volume so they fall in bottom 1/3 low-liquidity hours.
    for i in range(total - 48):
        dt = start + timedelta(minutes=15 * i)
        vol = 10.0 if dt.hour in (2, 3, 4, 5, 6, 7, 8, 9) else 500.0
        amp = 4.0 if i < 1500 else 0.55
        hour_idx = i // 4
        c = 100.0 + (amp if hour_idx % 2 == 0 else -amp)
        bars.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=c,
                high=c + amp * 0.4,
                low=c - amp * 0.4,
                close=c,
                volume=vol,
            )
        )

    # Last 48 bars (12 hours): clean range [99.0, 101.9] (height = 2.9, ~2.45x 1H ATR)
    # Ensure >= 2 touches on top (101.5..101.9) and bottom (99.0..99.4)
    range_start = now_utc - timedelta(minutes=15 * 48)
    for j in range(48):
        dt = range_start + timedelta(minutes=15 * j)
        vol = 10.0 if dt.hour in (2, 3, 4, 5, 6, 7, 8, 9) else 500.0
        if j in (4, 14):
            # Touch upper edge
            o, h, l, c = 101.2, 101.9, 101.0, 101.6
        elif j in (8, 18):
            # Touch lower edge
            o, h, l, c = 99.6, 99.8, 99.0, 99.2
        elif j >= 34:
            if direction == "long":
                # Smooth decline from 101.2 down to 99.10 -> RSI(14) < 35 and inside lower 15%
                step = (101.2 - 99.10) / 13.0
                c = round(101.2 - (j - 34) * step, 4)
                o = round(c + 0.12, 4)
                h = round(o + 0.05, 4)
                l = round(max(99.0, c - 0.05), 4)
            else:
                # Smooth rise from 99.8 up to 101.80 -> RSI(14) > 65 and inside upper 15%
                step = (101.80 - 99.8) / 13.0
                c = round(99.8 + (j - 34) * step, 4)
                o = round(c - 0.12, 4)
                h = round(min(101.9, c + 0.05), 4)
                l = round(o - 0.05, 4)
        else:
            o, h, l, c = 100.4, 100.8, 100.1, 100.5

        bars.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
            )
        )

    if include_unclosed_spike:
        # Bar starting at now_utc is NOT closed yet (closes at now_utc + 15m)
        bars.append(
            OHLCVBar(
                ts_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=99.1,
                high=150.0,
                low=50.0,
                close=140.0,
                volume=99999.0,
            )
        )

    return bars


def _ok_rep() -> dict:
    return {"status": "ok", "missing_ratio": 0.0, "delay_minutes": 1.0}


def _ok_crypto_meta() -> dict:
    return {"funding_rate": 0.0001, "oi_change_1h_pct": 1.0}


def test_strategy_a_uses_only_closed_bars_no_lookahead() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    # Build bars ending cleanly inside range, then append an UNCLOSED bar at 04:00 that looks extreme
    bars_clean = _build_strategy_a_fixture_bars(now, direction="long", include_unclosed_spike=False)
    bars_with_unclosed = _build_strategy_a_fixture_bars(now, direction="long", include_unclosed_spike=True)

    strat = StrategyA(cfg, events=[])
    ev_clean = strat.evaluate_instrument(inst, bars_clean, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())
    ev_unclosed = strat.evaluate_instrument(inst, bars_with_unclosed, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())

    assert ev_clean.decision == "signal"
    assert ev_unclosed.decision == "signal"
    assert ev_unclosed.candidate is not None
    assert ev_clean.candidate is not None
    # Unclosed bar MUST be completely ignored -> identical entry & indicator snapshot
    assert ev_unclosed.candidate.entry_low == ev_clean.candidate.entry_low
    assert ev_unclosed.indicators["rsi_14"] == ev_clean.indicators["rsi_14"]


def test_strategy_a_excludes_unstable_1m_or_15m_data_and_logs_watch() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "AUDUSD")
    bars = _build_strategy_a_fixture_bars(now, direction="long")
    unstable_1m = {"status": "unstable", "missing_ratio": 0.498, "delay_minutes": 0.5}

    strat = StrategyA(cfg, events=[])
    ev = strat.evaluate_instrument(inst, bars, unstable_1m, _ok_rep(), now_utc=now)
    assert ev.decision == "watch"
    assert ev.candidate is None
    assert "資料不穩" in ev.watch_reason
    assert ev.filter_checks["data_quality_ok"] is False


def test_strategy_a_schedule_delay_blocks_signal() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)
    bar_end = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)  # 30m delay > 20m limit
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars = _build_strategy_a_fixture_bars(bar_end, direction="long")

    strat = StrategyA(cfg, events=[])
    ev = strat.evaluate_instrument(inst, bars, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())
    assert ev.decision == "watch"
    assert "排程延遲" in ev.watch_reason
    assert ev.filter_checks["schedule_timely"] is False


def test_strategy_a_low_liquidity_session_and_settlement_avoidance() -> None:
    cfg = load_config()
    strat = StrategyA(cfg, events=[])
    now_low = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    bars = _build_strategy_a_fixture_bars(now_low, direction="long")

    low_hours, _ = strat.compute_low_liquidity_profile(bars, market="crypto")
    assert 4 in low_hours
    assert 15 not in low_hours

    # High-liquidity hour (15:00 UTC) must be rejected on weekday
    now_high = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
    bars_high = _build_strategy_a_fixture_bars(now_high, direction="long")
    inst_btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    ev_high = strat.evaluate_instrument(inst_btc, bars_high, _ok_rep(), _ok_rep(), now_utc=now_high)
    assert ev_high.decision == "watch"
    assert ev_high.filter_checks["low_liquidity_session"] is False

    # Forex settlement window (22:00 UTC) must be avoided even if in low-liquidity hours
    now_settle = datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc)
    inst_eur = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    ok_settle, _, msg_settle = strat.is_low_liquidity_window(now_settle, market="forex", low_liq_hours=[22, 23, 0, 1])
    assert ok_settle is False
    assert "結算" in msg_settle or "換日" in msg_settle


def test_strategy_a_market_state_filters_events_crypto_and_correlation() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars = _build_strategy_a_fixture_bars(now, direction="long")

    # 1) Upcoming high-impact event within 4 hours blocks signal
    events = [{"id": "cpi", "title": "US CPI", "currency": "USD", "impact": "high", "time_utc": "2026-11-16T06:00:00Z"}]
    strat_ev = StrategyA(cfg, events=events)
    res_ev = strat_ev.evaluate_instrument(inst, bars, _ok_rep(), _ok_rep(), now_utc=now)
    assert res_ev.decision == "watch"
    assert res_ev.filter_checks["no_high_impact_event"] is False

    # 2) Extreme crypto funding rate blocks signal
    strat = StrategyA(cfg, events=[])
    res_fund = strat.evaluate_instrument(
        inst, bars, _ok_rep(), _ok_rep(), now_utc=now,
        crypto_meta={"funding_rate": 0.005, "oi_change_1h_pct": 1.0},
    )
    assert res_fund.decision == "watch"
    assert res_fund.filter_checks["crypto_derivatives_ok"] is False

    # 3) Correlated peer violent move blocks signal
    res_corr = strat.evaluate_instrument(
        inst, bars, _ok_rep(), _ok_rep(), now_utc=now,
        peer_metrics={"crypto_majors": {"violent_move": True}},
        crypto_meta=_ok_crypto_meta(),
    )
    assert res_corr.decision == "watch"
    assert res_corr.filter_checks["correlated_peers_calm"] is False


def test_strategy_a_rejects_crypto_when_derivatives_data_missing() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars = _build_strategy_a_fixture_bars(now, direction="long")
    strat = StrategyA(cfg, events=[])

    ev = strat.evaluate_instrument(inst, bars, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=None)
    assert ev.decision == "watch"
    assert ev.filter_checks["crypto_derivatives_ok"] is False
    assert "資料不足" in ev.watch_reason


def test_strategy_a_range_and_entry_generates_valid_long_and_short_signal() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    strat = StrategyA(cfg, events=[])

    # Long setup
    bars_l = _build_strategy_a_fixture_bars(now, direction="long")
    ev_l = strat.evaluate_instrument(inst, bars_l, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())
    assert ev_l.decision == "signal"
    assert ev_l.candidate is not None
    c_l = ev_l.candidate
    assert c_l.strategy == "A"
    assert c_l.direction == "long"
    assert c_l.stop_loss < c_l.entry_low
    assert c_l.tp1 > c_l.entry_high
    assert float(c_l.tp2) > c_l.tp1
    assert all(lvl.add_type == "mean_add" for lvl in c_l.add_levels)
    assert len(c_l.reasons) >= 5

    # Short setup
    bars_s = _build_strategy_a_fixture_bars(now, direction="short")
    ev_s = strat.evaluate_instrument(inst, bars_s, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())
    assert ev_s.decision == "signal"
    assert ev_s.candidate is not None
    c_s = ev_s.candidate
    assert c_s.direction == "short"
    assert c_s.stop_loss > c_s.entry_high
    assert c_s.tp1 < c_s.entry_low
    assert float(c_s.tp2) < c_s.tp1
    assert all(lvl.add_type == "mean_add" for lvl in c_s.add_levels)


def test_strategy_a_rejects_trending_high_adx_or_high_atr_ratio() -> None:
    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    strat = StrategyA(cfg, events=[])

    # Strong monotonic uptrend -> ADX(14) >= 20 -> rejected
    start = now - timedelta(minutes=15 * 2000)
    trending_bars = []
    for i in range(2000):
        dt = start + timedelta(minutes=15 * i)
        vol = 10.0 if dt.hour in (2, 3, 4, 5, 6, 7, 8, 9) else 500.0
        base = 100.0 + i * 0.25
        trending_bars.append(
            OHLCVBar(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), base, base + 0.5, base - 0.1, base + 0.25, vol)
        )
    ev = strat.evaluate_instrument(
        inst, trending_bars, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta()
    )
    assert ev.decision == "watch"
    assert ev.filter_checks.get("adx_below_max") is False


def test_strategy_a_breakout_protection_and_range_invalidation(tmp_path) -> None:
    from src.engine.simulator import MarketBar, SimulationEngine
    from src.signals.models import LifecycleState

    cfg = load_config()
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars = _build_strategy_a_fixture_bars(now, direction="long")
    strat = StrategyA(cfg, events=[])
    ev = strat.evaluate_instrument(inst, bars, _ok_rep(), _ok_rep(), now_utc=now, crypto_meta=_ok_crypto_meta())
    assert ev.decision == "signal" and ev.candidate is not None

    eng = SimulationEngine(cfg, out_dir=tmp_path)
    sig = eng.submit_candidate_signal(
        ev.candidate,
        now_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        latest_bar_close_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        control={"paused": False, "risk_review_done": False, "risk_review_note": ""},
    )
    assert sig is not None
    # Enter trade inside entry zone
    eng.process_bar(
        "BTCUSDT",
        MarketBar("2026-11-16T04:00:00Z", "2026-11-16T04:01:00Z", sig.entry_low + 0.05, sig.entry_high, sig.entry_low, sig.entry_low + 0.05, 10.0),
    )
    assert eng.primary_ledger.trades[sig.signal_id].state == LifecycleState.ACTIVE

    # Trigger breakout protection with > 2.5x ATR spike bar (still above SL)
    atr_val = ev.candidate.atr
    spike_low = max(sig.stop_loss + 0.2, sig.entry_low - 0.1)
    spike_high = spike_low + 2.8 * atr_val
    eng.process_bar(
        "BTCUSDT",
        MarketBar("2026-11-16T04:01:00Z", "2026-11-16T04:02:00Z", sig.entry_low, spike_high, spike_low, spike_high - 0.1, 100.0),
        atr=atr_val,
        atr_ratio=1.1,
    )
    tr = eng.primary_ledger.trades[sig.signal_id]
    assert tr.state == LifecycleState.CLOSED
    assert tr.exit_reason == "breakout_protection"

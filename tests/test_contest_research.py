"""Unit tests for research/contest_15m/backtest.py simulation invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research.contest_15m.backtest import (
    MAX_HOLD_BARS,
    SignalCandidate,
    SimTrade,
    SymbolSeries,
    precompute_symbol_series,
    select_single_position_sequence,
    simulate_single_candidate,
    simulate_weekly_contest_oos,
)
from src.data.models import OHLCVBar


def _make_bars(count: int, base_price: float = 100.0) -> list[OHLCVBar]:
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    for i in range(count):
        ts = (start + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        px = base_price + (i % 4) * 0.1
        bars.append(OHLCVBar(ts_utc=ts, open=px, high=px + 0.5, low=px - 0.5, close=px + 0.05, volume=100.0))
    return bars


def _make_series(bars: list[OHLCVBar], spread_bps: float = 0.0, fee_rate: float = 0.0, slippage_bps: float = 0.0) -> SymbolSeries:
    return precompute_symbol_series({
        "symbol": "TEST",
        "market": "crypto",
        "session": "utc0",
        "bars": bars,
        "spread_bps": spread_bps,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "max_leverage": 10.0,
    })


def test_conservative_sl_beats_tp_when_both_hit_same_bar() -> None:
    """If both SL and TP are hit inside the same 15m bar, SL must be taken conservatively."""
    bars = _make_bars(130, 100.0)
    # Entry on bar 121 open = 100.0; make bar 122 hit BOTH low <= 98 (SL) and high >= 104 (TP)
    bars[121] = OHLCVBar(ts_utc=bars[121].ts_utc, open=100.0, high=101.0, low=99.0, close=100.5, volume=100.0)
    bars[122] = OHLCVBar(ts_utc=bars[122].ts_utc, open=100.5, high=105.0, low=97.0, close=102.0, volume=100.0)
    ser = _make_series(bars)
    cand = SignalCandidate(
        symbol="TEST", strategy_id="A", signal_bar_idx=120, entry_bar_idx=121,
        ts_utc=bars[121].ts_utc, direction="long", raw_open=100.0,
        sl_px=98.0, tp_px=104.0, sl_dist_pct=2.0,
    )
    tr = simulate_single_candidate(ser, cand, len(bars), spread_mult=1.0)
    assert tr is not None
    assert tr.exit_reason == "sl"
    assert tr.r_multiple == -1.0


def test_16_bar_time_stop_closes_at_16th_bar_close() -> None:
    """Trade held without hitting SL or TP exits at the 16th bar close."""
    bars = _make_bars(150, 100.0)
    for idx in range(121, 140):
        bars[idx] = OHLCVBar(ts_utc=bars[idx].ts_utc, open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0)
    # 16th held bar is idx = 121 + 16 - 1 = 136
    bars[136] = OHLCVBar(ts_utc=bars[136].ts_utc, open=100.0, high=101.5, low=99.5, close=101.0, volume=100.0)
    ser = _make_series(bars)
    cand = SignalCandidate(
        symbol="TEST", strategy_id="B", signal_bar_idx=120, entry_bar_idx=121,
        ts_utc=bars[121].ts_utc, direction="long", raw_open=100.0,
        sl_px=95.0, tp_px=110.0, sl_dist_pct=5.0,
    )
    tr = simulate_single_candidate(ser, cand, len(bars), spread_mult=1.0)
    assert tr is not None
    assert tr.exit_reason == "time_stop"
    assert tr.exit_ts_utc == bars[121 + MAX_HOLD_BARS - 1].ts_utc
    assert abs(tr.exit_px - 101.0) < 1e-6


def test_h1_ema50_no_lookahead() -> None:
    """15m bars inside an incomplete 1h bucket must only see the prior closed 1h bar."""
    # Build 260 bars (65 hours); spike closes only in the final hour (bars 256..259)
    bars = _make_bars(260, 100.0)
    for idx in range(256, 260):
        bars[idx] = OHLCVBar(ts_utc=bars[idx].ts_utc, open=100.0, high=500.0, low=100.0, close=500.0, volume=100.0)
    ser = _make_series(bars)
    # Bars 256, 257, 258 close at :15, :30, :45 inside the 64th hour; must NOT see 500.0 yet!
    assert ser.h1_close[256] < 150.0
    assert ser.h1_close[257] < 150.0
    assert ser.h1_close[258] < 150.0
    # Bar 259 closes at the exact end of the 64th hour -> now the 1h bar closes at 500.0
    assert abs(ser.h1_close[259] - 500.0) < 1e-6


def test_single_position_ranking_and_tie_break() -> None:
    """Simultaneous signals pick highest IS expectancy R, tie-break smallest SL distance %."""
    t1 = SimTrade("BTCUSD", "A", "long", "2025-05-01T00:00:00Z", "2025-05-01T02:00:00Z", 1000.0, 8200.0, 100.0, 102.0, 99.0, 102.0, 1.0, 1.0, "tp")
    t2 = SimTrade("ETHUSD", "A", "long", "2025-05-01T00:00:00Z", "2025-05-01T02:00:00Z", 1000.0, 8200.0, 100.0, 102.0, 99.5, 102.0, 0.5, 1.0, "tp")
    t3 = SimTrade("XAUUSD", "C", "long", "2025-05-01T01:00:00Z", "2025-05-01T03:00:00Z", 4600.0, 11800.0, 100.0, 99.0, 99.0, 102.0, 1.0, -1.0, "sl")
    # Same IS expectancy for (A, BTCUSD) and (A, ETHUSD) -> tie-break selects smaller sl_dist_pct (t2: 0.5% < 1.0%);
    # t3 starts while t2 is open -> skipped!
    rank = {("A", "BTCUSD"): 0.15, ("A", "ETHUSD"): 0.15, ("C", "XAUUSD"): 0.50}
    seq = select_single_position_sequence([t1, t2, t3], rank)
    assert len(seq) == 1
    assert seq[0].symbol == "ETHUSD"


def test_stress_test_spread_multiplier_reduces_r() -> None:
    """Spread x1.5 must reduce net R compared to 1.0x spread."""
    bars = _make_bars(140, 100.0)
    bars[121] = OHLCVBar(ts_utc=bars[121].ts_utc, open=100.0, high=101.0, low=99.5, close=100.5, volume=100.0)
    bars[122] = OHLCVBar(ts_utc=bars[122].ts_utc, open=100.5, high=103.0, low=100.0, close=102.5, volume=100.0)
    ser = _make_series(bars, spread_bps=10.0, fee_rate=0.0005, slippage_bps=1.0)
    cand = SignalCandidate(
        symbol="TEST", strategy_id="A", signal_bar_idx=120, entry_bar_idx=121,
        ts_utc=bars[121].ts_utc, direction="long", raw_open=100.0,
        sl_px=99.0, tp_px=102.0, sl_dist_pct=1.0,
    )
    t_norm = simulate_single_candidate(ser, cand, len(bars), spread_mult=1.0)
    t_stress = simulate_single_candidate(ser, cand, len(bars), spread_mult=1.5)
    assert t_norm is not None and t_stress is not None
    assert t_stress.r_multiple < t_norm.r_multiple


def test_weekly_contest_daily_stop_and_bust_rule() -> None:
    """1-week contest simulation stops trading after 5% daily loss and busts below $9,000."""
    bars = _make_bars(10, 100.0)
    ser = _make_series(bars)
    base_dt = datetime(2025, 5, 1, 0, 0, tzinfo=timezone.utc)
    # 3 consecutive -1R losses (4.5% each) on Day 1 -> 2nd loss hits -9% (< $9,100 > $9,000? Wait: 10000 - 450 = 9550 -> daily drop 4.5% < 5%;
    # 2nd loss: -9550*0.045 = -429.75 -> eq = 9120.25 -> daily drop 8.8% >= 5% -> Day 1 stops -> 3rd trade on Day 1 skipped!
    # Day 2 loss pushes eq < 9000 -> bust!)
    trades = [
        SimTrade("TEST", "A", "long", "2025-05-01T01:00:00Z", "2025-05-01T02:00:00Z",
                 (base_dt + timedelta(hours=1)).timestamp(), (base_dt + timedelta(hours=2)).timestamp(),
                 100.0, 99.0, 99.0, 102.0, 1.0, -1.0, "sl"),
        SimTrade("TEST", "A", "long", "2025-05-01T03:00:00Z", "2025-05-01T04:00:00Z",
                 (base_dt + timedelta(hours=3)).timestamp(), (base_dt + timedelta(hours=4)).timestamp(),
                 100.0, 99.0, 99.0, 102.0, 1.0, -1.0, "sl"),
        SimTrade("TEST", "A", "long", "2025-05-01T05:00:00Z", "2025-05-01T06:00:00Z",
                 (base_dt + timedelta(hours=5)).timestamp(), (base_dt + timedelta(hours=6)).timestamp(),
                 100.0, 105.0, 99.0, 105.0, 1.0, 5.0, "tp"),  # skipped by daily 5% stop!
        SimTrade("TEST", "A", "long", "2025-05-02T01:00:00Z", "2025-05-02T02:00:00Z",
                 (base_dt + timedelta(days=1, hours=1)).timestamp(), (base_dt + timedelta(days=1, hours=2)).timestamp(),
                 100.0, 99.0, 99.0, 102.0, 1.0, -1.0, "sl"),  # busts < 9000!
    ]
    res = simulate_weekly_contest_oos(
        trades, {"TEST": ser},
        oos_start_sec=base_dt.timestamp(),
        oos_end_sec=(base_dt + timedelta(days=8)).timestamp(),
        step_days=7,
    )
    assert res["weeks"] == 1
    assert res["bust_rate_pct"] == 100.0
    assert res["median_return_pct"] < -10.0

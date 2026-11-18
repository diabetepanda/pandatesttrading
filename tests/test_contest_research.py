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


def test_round2_the5ers_mt5_costs_config_and_modes() -> None:
    """Verify The5ers MT5 costs config (0.001% fee, typical spreads) and normal/stress/zero modes."""
    from research.contest_15m.backtest_round2 import (
        MAX_HOLD_BY_TF,
        SignalCandidateR2,
        load_costs_config,
        precompute_symbol_series_r2,
        simulate_candidate_r2,
    )

    cfg = load_costs_config()
    assert cfg["defaults"]["fee_rate_per_side"] == 0.00001
    assert cfg["instruments"]["XAUUSD"]["spread_points"] == 0.2
    assert cfg["instruments"]["SP500"]["spread_points"] == 0.8
    assert cfg["instruments"]["EURUSD"]["spread_points"] == 0.00001
    assert MAX_HOLD_BY_TF["15m"] == 16
    assert MAX_HOLD_BY_TF["1h"] == 24

    bars = _make_bars(150, 2000.0)
    bars[121] = OHLCVBar(ts_utc=bars[121].ts_utc, open=2000.0, high=2002.0, low=1999.0, close=2001.0, volume=100.0)
    bars[122] = OHLCVBar(ts_utc=bars[122].ts_utc, open=2001.0, high=2015.0, low=2000.0, close=2012.0, volume=100.0)
    ser = precompute_symbol_series_r2({
        "symbol": "XAUUSD", "market": "commodity", "session": "ny", "timeframe": "1h",
        "bars": bars, "spread_points": 0.20, "fee_rate": 0.00001, "slippage_bps": 0.0, "max_leverage": 20.0,
    })
    cand = SignalCandidateR2(
        symbol="XAUUSD", strategy_id="E", signal_bar_idx=120, entry_bar_idx=121,
        ts_utc=bars[121].ts_utc, direction="long", raw_open=2000.0,
        sl_px=1990.0, tp_px=2010.0, sl_dist_pct=0.5,
    )
    t_zero = simulate_candidate_r2(ser, cand, len(bars), 24, spread_mult=0.0, zero_cost=True)
    t_norm = simulate_candidate_r2(ser, cand, len(bars), 24, spread_mult=1.0, zero_cost=False)
    t_stress = simulate_candidate_r2(ser, cand, len(bars), 24, spread_mult=1.5, zero_cost=False)
    assert t_zero is not None and t_norm is not None and t_stress is not None
    assert t_zero.r_multiple > t_norm.r_multiple > t_stress.r_multiple


def test_round2_strategies_e_f_g_and_dukascopy_natgas_mapping() -> None:
    """Verify new strategies E/F/G generate valid non-lookahead signals and NATGAS uses GASCMDUSD."""
    from research.contest_15m.backtest_round2 import generate_signals_r2, precompute_symbol_series_r2
    from src.data.adapters import DUKASCOPY_SYMBOL_MAP

    assert DUKASCOPY_SYMBOL_MAP["NATGAS"][0] == "GASCMDUSD"

    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    for i in range(180):
        ts = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        px = 100.0
        bars.append(OHLCVBar(ts_utc=ts, open=px, high=px + 0.4, low=px - 0.4, close=px, volume=100.0))

    # Build a confirmed swing low at idx 120 (pivot_k=3 confirmed at 123), then poke & reclaim at 130 (Strategy G & E)
    bars[120] = OHLCVBar(ts_utc=bars[120].ts_utc, open=99.5, high=99.8, low=98.0, close=99.2, volume=100.0)
    bars[130] = OHLCVBar(ts_utc=bars[130].ts_utc, open=98.4, high=99.5, low=97.6, close=99.0, volume=100.0)
    # Build bearish candle at 140 followed by strong bullish displacement >= 1.5*ATR at 141, retest at 144 (Strategy F)
    bars[140] = OHLCVBar(ts_utc=bars[140].ts_utc, open=100.2, high=100.4, low=99.4, close=99.5, volume=100.0)
    bars[141] = OHLCVBar(ts_utc=bars[141].ts_utc, open=99.5, high=104.0, low=99.5, close=103.5, volume=200.0)
    bars[142] = OHLCVBar(ts_utc=bars[142].ts_utc, open=103.5, high=103.8, low=102.0, close=102.5, volume=100.0)
    bars[143] = OHLCVBar(ts_utc=bars[143].ts_utc, open=102.5, high=102.6, low=101.0, close=101.2, volume=100.0)
    bars[144] = OHLCVBar(ts_utc=bars[144].ts_utc, open=99.8, high=101.5, low=99.6, close=101.0, volume=150.0)

    ser = precompute_symbol_series_r2({
        "symbol": "TEST", "market": "forex", "session": "london", "timeframe": "1h",
        "bars": bars, "spread_points": 0.01, "fee_rate": 0.00001, "slippage_bps": 0.0, "max_leverage": 20.0,
    })
    sig_e = generate_signals_r2(ser, "E", {"lookback": 60, "pivot_k": 3, "touch_atr": 0.5, "sl_buf_atr": 0.2, "tp_r": 1.0}, 124, 135)
    sig_g = generate_signals_r2(ser, "G", {"lookback": 60, "pivot_k": 3, "sl_buf_atr": 0.2, "tp_r": 1.0}, 124, 135)
    sig_f = generate_signals_r2(ser, "F", {"disp_atr": 1.5, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 1.0}, 136, 150)

    assert any(s.direction == "long" and s.signal_bar_idx == 130 for s in sig_e)
    assert any(s.direction == "long" and s.signal_bar_idx == 130 for s in sig_g)
    assert any(s.direction == "long" and s.signal_bar_idx == 144 for s in sig_f)


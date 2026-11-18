"""Unit tests for research/combo/ synergistic combination & refinement study."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from research.combo.run_combo_research import (
    DAILY_SPLITS,
    INTRADAY_SPLITS,
    SimTradeRecord,
    build_enhanced_daily_cache,
    combine_weighted_strategy_trades,
    compute_adx_series,
    compute_atr_pct_percentile,
    compute_causal_weekly_features,
    compute_deflated_sharpe_and_bootstrap,
    compute_segment_metrics,
    simulate_kraken_prop_exam,
    simulate_the5ers_weekly_contest,
    simulate_variant,
)
from src.data.models import OHLCVBar


def _make_daily_bars(count: int, start_price: float = 100.0, trend_step: float = 0.2) -> list[OHLCVBar]:
    start = datetime(2022, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    px = start_price
    for i in range(count):
        ts = (start + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        wave = ((i % 7) - 3) * 0.3
        c = max(5.0, px + wave)
        o = max(5.0, c - 0.2)
        h = c + 1.5
        l = max(1.0, c - 1.5)
        bars.append(OHLCVBar(ts_utc=ts, open=o, high=h, low=l, close=c, volume=1000.0))
        px += trend_step
    return bars


def test_three_way_splits_non_overlapping_and_lockbox_at_least_one_year() -> None:
    """Verify Dev (~60%), Val (~25%), and Lockbox (~15%, >= 1 year) are chronological and non-overlapping."""
    for split_map in (DAILY_SPLITS, INTRADAY_SPLITS):
        dev_t0, dev_t1, dev_yrs = split_map["dev"]
        val_t0, val_t1, val_yrs = split_map["val"]
        lock_t0, lock_t1, lock_yrs = split_map["lockbox"]

        assert dev_t0 < dev_t1 <= val_t0 < val_t1 <= lock_t0 < lock_t1
        assert lock_yrs >= 1.0
        total_yrs = dev_yrs + val_yrs + lock_yrs
        assert 0.55 <= (dev_yrs / total_yrs) <= 0.65
        assert 0.20 <= (val_yrs / total_yrs) <= 0.30
        assert 0.14 <= (lock_yrs / total_yrs) <= 0.20


def test_causal_weekly_features_no_intraweek_lookahead() -> None:
    """Weekly trend features on Monday-Saturday must not see Sunday's unclosed weekly close."""
    # Start on Monday 2024-01-01, build 160 daily bars (~22 weeks)
    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    for i in range(160):
        ts = (start + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars.append(OHLCVBar(ts_utc=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0))

    # In the final week (days 154..159 = Mon..Sat), spike price to 500.0 on Tue..Sat,
    # before Sunday (day 160) closes the crypto ISO week
    for i in range(154, 159):
        bars[i] = OHLCVBar(ts_utc=bars[i].ts_utc, open=500.0, high=510.0, low=490.0, close=500.0, volume=100.0)

    wf = compute_causal_weekly_features(bars, market="crypto")
    # Mon..Fri (154..158) are inside an incomplete ISO week (next bar is same ISO week), so w_sma10 must still be 100.0!
    for i in range(154, 159):
        assert wf["w_sma10"][i] is not None
        assert abs(wf["w_sma10"][i] - 100.0) < 1e-6


def test_adx_and_atr_percentile_causal() -> None:
    """Verify ADX(14) and 252-day ATR% percentile produce valid bounded values."""
    bars = _make_daily_bars(300, start_price=100.0, trend_step=0.5)
    adx = compute_adx_series(bars, period=14)
    assert adx[10] is None
    assert adx[-1] is not None
    assert 0.0 <= adx[-1] <= 100.0

    from src.indicators.technical import compute_atr_series

    raw_atr = compute_atr_series(bars, period=14, market="crypto", interval="1d")
    atr14: list[float | None] = [None] * len(bars)
    for k, v in enumerate(raw_atr):
        if 14 + k < len(atr14):
            atr14[14 + k] = v

    pct = compute_atr_pct_percentile(bars, atr14, window=252)
    assert pct[-1] is not None
    assert 0.0 <= pct[-1] <= 100.0


def test_segment_metrics_and_deflated_sharpe_bootstrap() -> None:
    """Verify segment metrics computation (MAR, max_dd_days, PF) and DSR / Bootstrap p-values."""
    trades = [
        SimTradeRecord("BTCUSDT", "L2", "long", "2025-06-01T00:00:00Z", "2025-06-20T00:00:00Z", 100.0, 115.0, 95.0, 2.5, 0.025, 500.0, 5.0, 0.05),
        SimTradeRecord("ETHUSDT", "L2", "long", "2025-07-01T00:00:00Z", "2025-07-10T00:00:00Z", 100.0, 95.0, 95.0, -1.0, -0.010, -200.0, 5.0, 0.05),
        SimTradeRecord("XAUUSD", "L2", "long", "2025-08-01T00:00:00Z", "2025-09-01T00:00:00Z", 100.0, 120.0, 95.0, 3.5, 0.035, 700.0, 5.0, 0.05),
    ]
    m = compute_segment_metrics(trades, "2025-05-17T00:00:00Z", "2026-05-17T00:00:00Z", 1.0, include_daily_series=True)
    assert m["trades"] == 3
    assert abs(m["win_rate_pct"] - 66.67) < 0.1
    assert m["expectancy_r"] > 1.5
    assert m["profit_factor"] > 4.0
    assert m["cagr_pct"] > 4.0
    assert m["max_drawdown_pct"] > 0.0
    assert m["mar_ratio"] > 1.0
    assert m["max_dd_days"] >= 10

    rng = np.random.default_rng(7)
    base_ret = rng.normal(0.0002, 0.005, size=500)
    cand_ret = base_ret + 0.0008  # strong positive excess return
    stats = compute_deflated_sharpe_and_bootstrap(cand_ret, base_ret, [0.4, 0.6, 0.8, 1.0, 1.2], n_bootstrap=300)
    assert 0.0 <= stats["dsr_skill_prob_pct"] <= 100.0
    assert stats["bootstrap_p_value"] < 0.05


def test_exit_and_filter_variants_execution() -> None:
    """Verify Group 2 exit variants (2a Chandelier, 2b Partial TP, 2c Time Stop, 2d Breakeven) and Group 3/4 hooks run cleanly."""
    bars = _make_daily_bars(320, start_price=100.0, trend_step=0.6)
    inst = {
        "symbol": "BTCUSDT",
        "market": "crypto",
        "assumed_spread_bps": 2.0,
        "fee_rate": 0.0005,
        "overnight_cost_daily_bps": 1.0,
    }
    caches = build_enhanced_daily_cache({"BTCUSDT": bars, "SP500": bars}, {"BTCUSDT": inst, "SP500": inst})
    cfg = {
        "backtest": {
            "risk_per_trade_pct": 1.0,
            "max_total_open_risk_pct": 10.0,
            "max_concurrent_positions": 5,
            "max_position_notional_pct": 500.0,
            "max_total_notional_multiple": 5.0,
        }
    }
    for kwargs in [
        {"exit_mode": "2a_chandelier", "exit_params": {"trail_atr_mult": 3.0}},
        {"exit_mode": "2b_partial_tp", "exit_params": {"tp_r": 1.0}},
        {"exit_mode": "2c_time_stop", "exit_params": {"stagnant_days": 10}},
        {"exit_mode": "2d_breakeven", "exit_params": {"be_r": 1.0}},
        {"filter_mode": "3a_sma200"},
        {"filter_mode": "3d_wk_sma10"},
        {"consensus_mode": "4a_strict_2_boost3"},
    ]:
        trades, info = simulate_variant("L2", caches, ["BTCUSDT"], {"BTCUSDT": inst}, cfg, **kwargs)
        assert isinstance(trades, list)
        assert info["fill_rate_pct"] >= 0.0


def test_scenario_simulators_and_generated_artifacts() -> None:
    """Verify Kraken Prop Exam, The5ers 1-Week Contest simulators, and research/combo/ artifacts."""
    trades = [
        SimTradeRecord(
            "BTCUSDT", "L2", "long",
            (datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(days=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (datetime(2023, 1, 10, tzinfo=timezone.utc) + timedelta(days=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            100.0, 110.0, 95.0, 2.0, 0.02, 200.0, 2.0, 0.05,
        )
        for i in range(60)
    ]
    kr = simulate_kraken_prop_exam(trades, start_ts="2023-01-01T00:00:00Z", end_ts="2025-06-01T00:00:00Z")
    assert kr["runs"] > 0
    assert kr["pass_rate_pct"] == 100.0
    assert kr["bust_rate_pct"] == 0.0
    assert kr["mean_pass_days"] > 0.0

    th = simulate_the5ers_weekly_contest(trades, start_ts="2023-01-01T00:00:00Z", end_ts="2024-06-01T00:00:00Z")
    assert th["weeks"] > 0
    assert th["bust_rate_pct"] == 0.0

    combo_trades = combine_weighted_strategy_trades({"L2": trades}, {"L2": 1.0}, max_concurrent=1, ranking_mode="roc_latest")
    assert len(combo_trades) == len(trades)

    root = Path(__file__).resolve().parent.parent
    results_path = root / "research" / "combo" / "results.json"
    report_path = root / "research" / "combo" / "report.md"
    assert results_path.exists()
    assert report_path.exists()

    res = json.loads(results_path.read_text(encoding="utf-8"))
    assert res["total_ideas_tested"] == 20
    assert res["total_parameter_combinations_tested"] == 61
    assert len(res["top5_candidates"]) == 5
    report_md = report_path.read_text(encoding="utf-8")
    for required_heading in (
        "白話總結論",
        "防過度擬合機制",
        "晉級鎖定段（Lockbox）的 5 大候選方案",
        "海妖考試",
        "The5ers 一週交易競賽",
    ):
        assert required_heading in report_md

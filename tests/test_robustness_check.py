"""Offline unit & schema tests for `research/robustness_check` (zero network required)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.robustness_check import engine as eng
from research.robustness_check import run_robustness as rr

RESULTS_JSON = Path(__file__).resolve().parent.parent / "research" / "robustness_check" / "results.json"


@pytest.fixture
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee synthetic unit tests run strictly offline without any socket calls."""
    def _deny_socket(*args, **kwargs):
        raise RuntimeError("Network access is strictly forbidden in robustness unit tests!")

    monkeypatch.setattr(socket, "create_connection", _deny_socket)


def test_xirr_calculation_synthetic(block_network: None) -> None:
    """Test 1: Verify XIRR solver accuracy on analytical lump-sum, DCA, loss, and withdrawal schedules."""
    # 1) Exact 1-year +12% return: invest 10,000 at day 0, receive 11,200 at day 365.25
    r_1y = eng.compute_xirr([0.0, 365.25], [-10_000.0, 11_200.0])
    assert abs(r_1y - 0.12) < 1e-6

    # 2) Exact 2-year -50% total loss -> annualized (0.5 ** 0.5 - 1) = -29.2893%
    r_loss = eng.compute_xirr([0.0, 730.5], [-100_000.0, 50_000.0])
    assert abs(r_loss - (0.5 ** 0.5 - 1.0)) < 1e-6

    # 3) Multi-period DCA at known 10% annualized rate: each cashflow compounded at 10% to T_end
    target_r = 0.10
    days = np.array([0.0, 91.3125, 182.625, 273.9375, 365.25])
    contribs = np.array([-10_000.0, -10_000.0, -10_000.0, -10_000.0])
    rem_yrs = (365.25 - days[:-1]) / 365.25
    fv = float(np.sum((-contribs) * np.power(1.0 + target_r, rem_yrs)))
    solved_r = eng.compute_xirr(days, np.append(contribs, fv))
    assert abs(solved_r - target_r) < 1e-6

    # 4) Fast y-grid vectorized solver matches scalar solver within 1e-4
    y_grid = eng.precompute_xirr_y_grid(rem_yrs)
    grid_r = float(eng.solve_xirr_from_y_grid(y_grid, np.array([fv / 10_000.0]))[0])
    assert abs(grid_r - target_r) < 1e-4


def test_tax_nhi_and_fx_deduction_synthetic(block_network: None) -> None:
    """Test 2: Verify US 30% withholding, TW 2.11% NHI (>=20k threshold), TW 0%/12% tax, 0.3% FX spread & $15 min fee."""
    # Below 20,000 TWD payout: zero 2.11% NHI supplement
    below = eng.compute_net_tw_dividend_cash(19_999.0, income_tax_rate=0.0)
    assert below["nhi_fee"] == 0.0
    assert below["net"] == pytest.approx(19_999.0)

    # At/above 20,000 TWD payout with 12% income tax: deduct both 2.11% NHI and 12% income tax
    above = eng.compute_net_tw_dividend_cash(20_000.0, income_tax_rate=0.12)
    assert above["nhi_fee"] == pytest.approx(20_000.0 * 0.0211)
    assert above["income_tax"] == pytest.approx(20_000.0 * 0.12)
    assert above["net"] == pytest.approx(20_000.0 * (1.0 - 0.0211 - 0.12))

    # US 30% withholding tax
    assert eng.compute_net_us_dividend_usd(100.0, withholding_rate=0.30) == pytest.approx(70.0)

    # Full synthetic ledger simulation over 24 business days (2 monthly inflows)
    dates = pd.bdate_range("2024-01-02", periods=30)
    n = len(dates)
    tw_pr = np.zeros(n)
    tw_div = np.zeros(n)
    tw_div[10] = 0.05  # 5% dividend on day 10
    us_pr = np.zeros(n)
    us_div = np.zeros(n)
    us_div[10] = 0.04  # 4% US dividend on day 10
    fx = np.full(n, 32.0)

    res_0 = eng.simulate_tw_us_dca_with_taxes_and_fx(
        dates, tw_pr, tw_div, us_pr, us_div, fx,
        w_tw=0.5, w_us=0.5, monthly_contrib_twd=1_000_000.0,  # 500k in TW -> 25k div >= 20k triggers NHI!
        fx_spread_rate=0.003, tw_income_tax_rate=0.0, us_min_fee_usd=0.0,
    )
    res_12 = eng.simulate_tw_us_dca_with_taxes_and_fx(
        dates, tw_pr, tw_div, us_pr, us_div, fx,
        w_tw=0.5, w_us=0.5, monthly_contrib_twd=1_000_000.0,
        fx_spread_rate=0.003, tw_income_tax_rate=0.12, us_min_fee_usd=0.0,
    )
    res_min15 = eng.simulate_tw_us_dca_with_taxes_and_fx(
        dates, tw_pr, tw_div, us_pr, us_div, fx,
        w_tw=0.5, w_us=0.5, monthly_contrib_twd=10_000.0,
        fx_spread_rate=0.003, tw_income_tax_rate=0.0, us_min_fee_usd=15.0,
    )
    res_nomin = eng.simulate_tw_us_dca_with_taxes_and_fx(
        dates, tw_pr, tw_div, us_pr, us_div, fx,
        w_tw=0.5, w_us=0.5, monthly_contrib_twd=10_000.0,
        fx_spread_rate=0.003, tw_income_tax_rate=0.0, us_min_fee_usd=0.0,
    )

    assert res_0["nhi_trigger_count"] == 1
    assert res_0["nhi_paid_twd"] == pytest.approx(25_000.0 * 0.0211)
    assert res_12["tw_income_tax_paid_twd"] == pytest.approx(25_000.0 * 0.12)
    assert res_12["final_value_twd"] < res_0["final_value_twd"]
    assert res_min15["final_value_twd"] < res_nomin["final_value_twd"]


def test_behavioral_scenario_triggers_synthetic(block_network: None) -> None:
    """Test 3: Verify Scenarios A (-30% pause 6m), B (-40% panic sell 20%), and C (-30% unemployed 12m + 6x30k wd)."""
    # Build 20 months (~420 business days) of synthetic 1-asset path:
    # Months 0..2 flat at 1.0 -> Month 3 drops to 0.65 (-35%) -> Month 4 drops to 0.52 (-48%) -> recovers to 1.15
    dates = pd.bdate_range("2020-01-01", periods=420)
    n = len(dates)
    price = np.ones(n, dtype=np.float64)
    price[60:85] = 0.65    # -35% drop (triggers A and C, NOT B yet)
    price[85:160] = 0.52   # -48% drop (triggers B < -40%)
    price[160:260] = np.linspace(0.52, 1.05, 100)  # recovers above prior peak
    price[260:] = 1.20

    w_mat = price[:, None]
    w_1d = np.array([1.0])
    is_tw = np.array([True])
    n_months = len(eng.get_dca_Indices_by_timing(dates, "start"))

    ctrl = eng.simulate_behavioral_dca(dates, w_mat, w_1d, is_tw, [], None, scenario="control", trigger_drawdown_series=(price / np.maximum.accumulate(price) - 1.0))
    sc_a = eng.simulate_behavioral_dca(dates, w_mat, w_1d, is_tw, [], None, scenario="A_pause_6m", trigger_drawdown_series=(price / np.maximum.accumulate(price) - 1.0))
    sc_b = eng.simulate_behavioral_dca(dates, w_mat, w_1d, is_tw, [], None, scenario="B_panic_sell_20pct", trigger_drawdown_series=(price / np.maximum.accumulate(price) - 1.0))
    sc_c = eng.simulate_behavioral_dca(dates, w_mat, w_1d, is_tw, [], None, scenario="C_unemployed_12m_withdraw", trigger_drawdown_series=(price / np.maximum.accumulate(price) - 1.0))

    assert ctrl["trigger_count"] == 0
    assert ctrl["total_contributed_twd"] == pytest.approx(n_months * 10_000.0)

    # Scenario A paused exactly 6 monthly contributions during crash -> missed cheap shares -> lower wealth & XIRR
    assert sc_a["trigger_count"] == 1
    assert sc_a["total_contributed_twd"] == pytest.approx((n_months - 6) * 10_000.0)
    assert sc_a["effective_xirr_pct"] < ctrl["effective_xirr_pct"]

    # Scenario B sold 20% at -48% and bought back after recovery -> strictly lower final value & XIRR despite same contributions
    assert sc_b["trigger_count"] == 1
    assert sc_b["total_contributed_twd"] == pytest.approx(n_months * 10_000.0)
    assert sc_b["final_value_twd"] < ctrl["final_value_twd"]
    assert sc_b["effective_xirr_pct"] < ctrl["effective_xirr_pct"]

    # Scenario C paused 12 monthly contributions AND withdrew living expenses from the account
    assert sc_c["trigger_count"] == 1
    assert sc_c["total_contributed_twd"] == pytest.approx((n_months - 12) * 10_000.0)
    assert sc_c["total_withdrawn_twd"] > 0.0
    assert sc_c["final_value_twd"] < sc_a["final_value_twd"]


def test_bootstrap_block_lengths_and_stationary_mean(block_network: None) -> None:
    """Test 4: Verify exact block lengths for 1Y (12m), 3Y (36m), 5Y (60m) and mean block length ~36m for Stationary."""
    n_hist = 240
    n_sim = 120

    for blk_m in (12, 36, 60):
        idx_mat, realized = eng.generate_bootstrap_month_indices(n_hist, n_sim, mode="block", block_months=blk_m, n_paths=200, seed=42)
        assert idx_mat.shape == (200, 120)
        assert int(idx_mat.min()) >= 0 and int(idx_mat.max()) < n_hist
        # Verify every full block is strictly consecutive (+1 per month)
        n_full_blocks = n_sim // blk_m
        for b in range(n_full_blocks):
            seg = idx_mat[:, b * blk_m : (b + 1) * blk_m]
            diffs = np.diff(seg, axis=1)
            assert np.all(diffs == 1), f"Non-consecutive step inside block of length {blk_m}"

    # Stationary bootstrap with expected block length = 36 months
    stat_mat, stat_lens = eng.generate_bootstrap_month_indices(
        n_hist, n_sim_months=360, mode="stationary", block_months=36, n_paths=1500, seed=12345
    )
    assert stat_mat.shape == (1500, 360)
    mean_len = float(np.mean(stat_lens))
    assert 32.0 <= mean_len <= 40.0, f"Stationary bootstrap mean block length {mean_len:.2f} too far from 36"


def test_dca_timing_and_rebalance_flags_synthetic(block_network: None) -> None:
    """Test 5: Verify Gate 7 DCA day selection (start/mid/end) and monthly/quarterly/annual rebalance flags."""
    dates = pd.bdate_range("2023-01-02", "2024-12-31")
    idx_start = eng.get_dca_Indices_by_timing(dates, "start")
    idx_mid = eng.get_dca_Indices_by_timing(dates, "mid")
    idx_end = eng.get_dca_Indices_by_timing(dates, "end")

    assert len(idx_start) == 24 and len(idx_mid) == 24 and len(idx_end) == 24
    assert np.all(idx_start < idx_mid) and np.all(idx_mid < idx_end)

    m_dates = dates[idx_start]
    assert int(np.sum(eng.get_rebalance_flags(m_dates, "none"))) == 0
    assert int(np.sum(eng.get_rebalance_flags(m_dates, "monthly"))) == 23
    assert int(np.sum(eng.get_rebalance_flags(m_dates, "quarterly"))) == 7
    assert int(np.sum(eng.get_rebalance_flags(m_dates, "annual"))) == 1


def test_lump_sum_vs_dca_schedule_synthetic(block_network: None) -> None:
    """Test 6: Verify Gate 8 100萬 lump-sum vs 12m/24m DCA behavior in bull vs crash markets."""
    w_1d = np.array([0.5, 0.5])
    is_tw = np.array([True, False])

    # Steady bull (+1%/month for 36 months): lump sum beats 12m & 24m DCA
    bull_g = np.full((36, 2), 1.01, dtype=np.float64)
    ls_bull = eng.simulate_entry_schedule_monthly(bull_g, w_1d, is_tw, entry_months=1)
    d12_bull = eng.simulate_entry_schedule_monthly(bull_g, w_1d, is_tw, entry_months=12)
    d24_bull = eng.simulate_entry_schedule_monthly(bull_g, w_1d, is_tw, entry_months=24)
    assert ls_bull["final_value_twd"] > d12_bull["final_value_twd"] > d24_bull["final_value_twd"]

    # Immediate -50% crash in month 0 followed by flat: 24m DCA loses far less and has smaller MDD than lump sum
    crash_g = np.ones((36, 2), dtype=np.float64)
    crash_g[0, :] = 0.50
    ls_cr = eng.simulate_entry_schedule_monthly(crash_g, w_1d, is_tw, entry_months=1)
    d24_cr = eng.simulate_entry_schedule_monthly(crash_g, w_1d, is_tw, entry_months=24)
    assert d24_cr["final_value_twd"] > ls_cr["final_value_twd"]
    assert d24_cr["max_drawdown_pct"] > ls_cr["max_drawdown_pct"]


def test_retirement_withdrawal_30y_ruin_synthetic(block_network: None) -> None:
    """Test 7: Verify Gate 8 30Y 4% + 2% inflation withdrawal survival vs early-crash ruin."""
    w_1d = np.array([0.6, 0.4])
    is_tw = np.array([True, False])

    # Path 0: steady +0.7%/month (~8.7%/yr) -> 0% ruin; Path 1: severe -8%/month crash for 24m -> 100% ruin
    g_batch = np.ones((2, 360, 2), dtype=np.float64)
    g_batch[0, :, :] = 1.007
    g_batch[1, :24, :] = 0.92
    res = eng.simulate_retirement_withdrawal_batch(g_batch, w_1d, is_tw, initial_capital_twd=10_000_000.0)
    assert bool(res["ruined_mask"][0]) is False
    assert bool(res["ruined_mask"][1]) is True
    assert res["ruin_prob_pct"] == pytest.approx(50.0)
    assert res["terminal_wealth_twd"][0] > 10_000_000.0
    assert res["terminal_wealth_twd"][1] == 0.0


def test_verdict_criteria_boundaries(block_network: None) -> None:
    """Test 5: Verify pre-registered criteria.md decision boundaries."""
    assert eng.judge_r1_rank_stability(2.5, 25.0) == "通過"
    assert eng.judge_r1_rank_stability(3.8, 25.0) == "有條件通過"
    assert eng.judge_r1_rank_stability(2.5, 42.0) == "有條件通過"
    assert eng.judge_r1_rank_stability(6.2, 65.0) == "不通過"

    assert eng.judge_risk_cap(8.5) == "通過"
    assert eng.judge_risk_cap(18.0) == "有條件通過"
    assert eng.judge_risk_cap(31.0) == "不通過"

    assert eng.judge_r4_leverage_conclusion(72.0) == "通過"
    assert eng.judge_r4_leverage_conclusion(20.0) == "不通過"

    assert eng.judge_r5_recovery_time(25.0, baseline_months=20.24) == "通過"
    assert eng.judge_r5_recovery_time(45.0, baseline_months=20.24) == "不通過"

    assert eng.judge_overall_verdict(["通過"] * 8) == "站得住"
    assert eng.judge_overall_verdict(["通過"] * 7 + ["有條件通過"]) == "有條件成立"
    assert eng.judge_overall_verdict(["通過"] * 6 + ["有條件通過", "不通過"]) == "被推翻"


def test_results_json_structure_and_artifacts(block_network: None) -> None:
    """Test 6: Validate synthetic schema checker AND verify generated research/robustness_check/results.json & PNGs."""
    # 1) Synthetic payload check (100% independent of disk state)
    fake_gates = {f"gate_{i}": {"verdict": "通過"} for i in range(1, 9)}
    fake_payload = {
        "meta": {"study_id": "robustness_check"},
        "data_sources": [],
        "data_issues": [],
        "gate_1_data": {},
        "gate_2_walk_forward": {},
        "gate_3_intl_crashes": {},
        "gate_4_tax_fx": {},
        "gate_5_behavior": {},
        "gate_6_bootstrap": {},
        "gate_7_sensitivity": {},
        "gate_8_life_stage": {},
        "verdicts": {
            "by_recommendation": {
                r: {"overall_verdict": "站得住", "gates": fake_gates}
                for r in ("R1", "R2", "R3", "R4", "R5")
            }
        },
        "revised_recommendations": {},
    }
    assert rr.validate_results_schema(fake_payload) is True

    # 2) Verify actual generated results.json & 4 PNG charts
    assert RESULTS_JSON.exists(), f"Missing {RESULTS_JSON}; run run_robustness.py first."
    real_data = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    assert rr.validate_results_schema(real_data) is True
    assert len(real_data["data_issues"]) >= 4

    charts_map = real_data["meta"]["charts"]
    assert len(charts_map) >= 4
    for _, fname in charts_map.items():
        p = RESULTS_JSON.parent / fname
        assert p.exists(), f"Missing PNG chart {p}"
        raw = p.read_bytes()
        assert len(raw) > 20_000 and raw[:8] == b"\x89PNG\r\n\x1a\n"

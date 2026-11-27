"""Unit tests for research/final_check engine, calculations, and generated artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.final_check import engine as fc_eng


# ---------------------------------------------------------------------------
# 1. US Non-Resident Alien Estate Tax Tests (Form 706-NA, IRC §§ 2001(c), 2102)
# ---------------------------------------------------------------------------

def test_us_nra_estate_tax_exempt_at_or_below_60k() -> None:
    for amt in (0.0, 10_000.0, 59_999.99, 60_000.0):
        res = fc_eng.compute_us_nra_estate_tax_usd(amt)
        assert res["net_estate_tax_usd"] == pytest.approx(0.0, abs=1e-6)
        assert res["net_inherited_usd"] == pytest.approx(amt, abs=1e-6)


def test_us_nra_estate_tax_progressive_brackets() -> None:
    # $100,000 US-situs: tentative = $23,800, credit = $13,000 -> net tax = $10,800
    r100 = fc_eng.compute_us_nra_estate_tax_usd(100_000.0)
    assert r100["tentative_tax_usd"] == pytest.approx(23_800.0, abs=1e-6)
    assert r100["net_estate_tax_usd"] == pytest.approx(10_800.0, abs=1e-6)

    # $500,000 US-situs: tentative = $155,800 -> net tax = $142,800
    r500 = fc_eng.compute_us_nra_estate_tax_usd(500_000.0)
    assert r500["tentative_tax_usd"] == pytest.approx(155_800.0, abs=1e-6)
    assert r500["net_estate_tax_usd"] == pytest.approx(142_800.0, abs=1e-6)

    # $1,000,000 US-situs: tentative = $345,800 -> net tax = $332,800
    r1m = fc_eng.compute_us_nra_estate_tax_usd(1_000_000.0)
    assert r1m["tentative_tax_usd"] == pytest.approx(345_800.0, abs=1e-6)
    assert r1m["net_estate_tax_usd"] == pytest.approx(332_800.0, abs=1e-6)

    # $2,000,000 US-situs: $345,800 + 40% * $1,000,000 - $13,000 = $732,800
    r2m = fc_eng.compute_us_nra_estate_tax_usd(2_000_000.0)
    assert r2m["net_estate_tax_usd"] == pytest.approx(732_800.0, abs=1e-6)
    assert r2m["effective_tax_rate_pct"] == pytest.approx(36.64, abs=1e-4)


# ---------------------------------------------------------------------------
# 2. Glidepath Annual Weights Accuracy Tests
# ---------------------------------------------------------------------------

def test_glidepath_weights_exact_by_age() -> None:
    expected = {
        30: (0.50, 0.50, 0.00),
        54: (0.50, 0.50, 0.00),
        55: (0.46, 0.46, 0.08),
        56: (0.42, 0.42, 0.16),
        57: (0.38, 0.38, 0.24),
        58: (0.34, 0.34, 0.32),
        59: (0.30, 0.30, 0.40),
        60: (0.30, 0.30, 0.40),
        85: (0.30, 0.30, 0.40),
    }
    for age, exp_w in expected.items():
        got = fc_eng.get_glidepath_weights(age)
        assert got == pytest.approx(exp_w, abs=1e-6), f"Age {age}: expected {exp_w}, got {got}"
        assert sum(got) == pytest.approx(1.0, abs=1e-6)


def test_continuous_simulator_applies_exact_glidepath_each_year() -> None:
    n_m = (62 - 53) * 12  # ages 53..62
    zeros = np.zeros(n_m, dtype=np.float64)
    fx = np.full(n_m + 1, 32.0, dtype=np.float64)
    res = fc_eng.simulate_lifetime_continuous(
        r_tw_m=zeros, r_us_usd_m=zeros, r_bond_usd_m=zeros, fx_m=fx,
        start_age=53, glide_start_age=55, retire_age=60, end_age=62,
        monthly_contrib_twd=10_000.0,
    )
    used = res["annual_weights_used"]
    assert used[53] == (0.50, 0.50, 0.00)
    assert used[54] == (0.50, 0.50, 0.00)
    assert used[55] == (0.46, 0.46, 0.08)
    assert used[56] == (0.42, 0.42, 0.16)
    assert used[57] == (0.38, 0.38, 0.24)
    assert used[58] == (0.34, 0.34, 0.32)
    assert used[59] == (0.30, 0.30, 0.40)
    assert used[60] == (0.30, 0.30, 0.40)
    assert used[61] == (0.30, 0.30, 0.40)


def test_continuous_simulator_accumulation_only_records_retire_wealth() -> None:
    n_m = (60 - 50) * 12
    zeros = np.zeros(n_m, dtype=np.float64)
    fx = np.full(n_m + 1, 32.0, dtype=np.float64)
    res = fc_eng.simulate_lifetime_continuous(
        r_tw_m=zeros, r_us_usd_m=zeros, r_bond_usd_m=zeros, fx_m=fx,
        start_age=50, glide_start_age=55, retire_age=60, end_age=60,
        monthly_contrib_twd=10_000.0, fx_fee_rate=0.0, us_comm_rate=0.0,
    )
    assert res["wealth_at_retire_twd"] > 1_000_000.0
    assert res["wealth_at_retire_twd"] == pytest.approx(res["wealth_at_end_twd"], rel=1e-6)


# ---------------------------------------------------------------------------
# 3. Withdrawal Protection Rule Trigger & Recovery Tests
# ---------------------------------------------------------------------------

def test_withdrawal_protection_rule_triggers_and_recovers() -> None:
    # 5-year retirement starting with 10,000,000 TWD:
    # Year 0 (age 60): crash -30% in month 0 -> Jan of age 61 wealth is well below 8,000,000 (< -20%) -> triggers 3.5%!
    # Year 1 (age 61): stays flat -> still below 10M -> age 62 stays in protection (3.5% of Jan wealth)!
    # Year 2 (age 62): rallies +80% -> Jan of age 63 wealth > 10,000,000 -> exits protection & resumes 4% * 1.02^3!
    n_m = 5 * 12
    r = np.zeros(n_m, dtype=np.float64)
    r[6] = -0.35   # Mid Year 0 big crash
    r[30] = +0.90  # Mid Year 2 big recovery back above 10M
    fx = np.full(n_m + 1, 32.0, dtype=np.float64)

    res = fc_eng.simulate_lifetime_continuous(
        r_tw_m=r, r_us_usd_m=r, r_bond_usd_m=r, fx_m=fx,
        start_age=60, glide_start_age=55, retire_age=60, end_age=65,
        monthly_contrib_twd=0.0, initial_lump_sum_twd=10_000_000.0,
        withdrawal_rule="protect_20_35_plus_2",
        fx_fee_rate=0.0, us_comm_rate=0.0,
    )
    # Protection must trigger at age 61 and age 62 (2 years), then recover at age 63 & 64
    assert res["protection_years"] == 2
    spend = res["annual_spending_nominal"]
    w0 = res["wealth_at_retire_twd"]
    assert spend[0] == pytest.approx(w0 * 0.04, rel=1e-4)
    # Age 61 (index 1) must be 3.5% of Jan age-61 wealth, strictly lower than Year-1 spending
    w_61 = res["annual_wealth"][61]
    assert spend[1] == pytest.approx(w_61 * 0.035, rel=1e-3)
    assert spend[1] < spend[0] * 0.70
    # Age 63 (index 3) recovers above w0 -> resumes baseline 4% * 1.02^3
    assert spend[3] == pytest.approx(w0 * 0.04 * (1.02 ** 3), rel=1e-3)


# ---------------------------------------------------------------------------
# 4. Filter Rule B Full Sell Execution vs Rule A & Rule C Tests
# ---------------------------------------------------------------------------

def test_filter_rule_b_sells_all_00631l_with_fees_and_buys_back() -> None:
    dates = pd.bdate_range("2020-01-02", periods=100)
    w_50 = np.linspace(100.0, 120.0, len(dates))
    w_631 = np.linspace(100.0, 140.0, len(dates))
    # Below SMA200 from day 40 to day 65, then recovers on day 66
    below_exec = np.zeros(len(dates), dtype=bool)
    below_exec[40:66] = True

    res_b = fc_eng.simulate_aggressive_filter_rule(
        dates, w_50, w_631, below_exec, rule="B", tw_share=1.0, lev_frac_in_tw=0.20, rebalance_annual=False,
    )
    res_a = fc_eng.simulate_aggressive_filter_rule(
        dates, w_50, w_631, below_exec, rule="A", tw_share=1.0, lev_frac_in_tw=0.20, rebalance_annual=False,
    )
    res_c = fc_eng.simulate_aggressive_filter_rule(
        dates, w_50, w_631, below_exec, rule="C", tw_share=1.0, lev_frac_in_tw=0.20, rebalance_annual=False,
    )

    # Rule B must execute 1 full sell on day 40 and 1 full buyback on day 66
    assert len(res_b["full_sell_log"]) == 1
    assert res_b["full_sell_log"][0]["date"] == dates[40].strftime("%Y-%m-%d")
    assert res_b["full_sell_log"][0]["sold_00631l_twd"] > 0.0
    assert res_b["full_sell_log"][0]["fee_twd"] > 0.0
    assert len(res_b["full_buyback_log"]) == 1
    assert res_b["full_buyback_log"][0]["date"] == dates[66].strftime("%Y-%m-%d")
    assert res_b["switch_events"] == 2
    assert res_b["total_fees_twd"] > 0.0

    # Rule A and C must NEVER do mid-month full sells
    assert res_a["switch_events"] == 0
    assert len(res_a["full_sell_log"]) == 0
    assert res_c["switch_events"] == 0
    assert len(res_c["full_sell_log"]) == 0


# ---------------------------------------------------------------------------
# 5. FX Scenario Path Tests (Appreciate to 25 / Depreciate to 40 over 5 yrs)
# ---------------------------------------------------------------------------

def test_fx_scenario_paths_exact_linearity_and_flat_tail() -> None:
    # Pre-retirement shock (month 0..60)
    p25_pre = fc_eng.build_fx_scenario_path(120, base_fx=32.0, scenario="appreciate_to_25", shock_start_month=0, shock_duration_months=60)
    assert p25_pre[0] == pytest.approx(32.0)
    assert p25_pre[30] == pytest.approx(28.5)
    assert p25_pre[60] == pytest.approx(25.0)
    assert p25_pre[120] == pytest.approx(25.0)

    # Post-retirement shock (month 60..120)
    p40_post = fc_eng.build_fx_scenario_path(180, base_fx=32.0, scenario="depreciate_to_40", shock_start_month=60, shock_duration_months=60)
    assert p40_post[0] == pytest.approx(32.0)
    assert p40_post[60] == pytest.approx(32.0)
    assert p40_post[90] == pytest.approx(36.0)
    assert p40_post[120] == pytest.approx(40.0)
    assert p40_post[180] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# 6. Bond Yield Synthesis Formula Sanity Test
# ---------------------------------------------------------------------------

def test_bond_yield_synthesis_duration_direction() -> None:
    dates = pd.bdate_range("2022-01-03", periods=5)
    # Yield jumps +1.0% (+0.01) on day 2 with duration 5.0 -> ~-5% price shock!
    y = pd.Series([0.02, 0.02, 0.03, 0.03, 0.02], index=dates)
    r = fc_eng.synthesize_bond_daily_returns(y, duration=5.0, convexity=30.0, expense_ratio=0.0003)
    assert r.iloc[2] < -0.045
    assert r.iloc[4] > +0.045


# ---------------------------------------------------------------------------
# 7. Generated Artifacts Integrity Test (results.json & PNGs)
# ---------------------------------------------------------------------------

def test_final_check_artifacts_integrity() -> None:
    root = Path(__file__).resolve().parent.parent
    fc_dir = root / "research" / "final_check"
    assert (fc_dir / "criteria.md").exists()
    res_path = fc_dir / "results.json"
    if not res_path.exists():
        pytest.skip("results.json not generated yet")
    data = json.loads(res_path.read_text(encoding="utf-8"))
    for k in (
        "test_1_aggressive_filter",
        "test_2_estate_tax_domicile",
        "test_3_real_bonds",
        "test_4_lifetime_continuous",
        "test_5_withdrawal_protection",
        "test_6_shiller_inflation",
        "test_7_fx_swings",
        "test_8_different_people",
    ):
        assert k in data, f"Missing {k} in results.json"
    # Verify all 5 Shiller crisis years exist
    five = data["test_6_shiller_inflation"]["five_crisis_paths"]
    for yr in ("1929", "1966", "1973", "2000", "2008"):
        assert yr in five
    # Verify selected bond ETF is a concrete tradable ticker
    assert data["test_3_real_bonds"]["selected_bond_etf"] in ("SHY", "VGIT", "IEF")

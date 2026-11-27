"""Core offline-testable simulation engine for research/final_check (8 loophole checks).

Reuses XIRR, MDD, cost conventions, and bootstrap utilities from
`research/robustness_check/engine.py` without rewriting them.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from research.robustness_check import engine as base_eng

# Re-export core constants & helpers
TRADING_DAYS_PER_YEAR = base_eng.TRADING_DAYS_PER_YEAR
MONTHLY_CONTRIB_TWD = base_eng.MONTHLY_CONTRIB_TWD
BASE_FX_FEE_RATE = base_eng.BASE_FX_FEE_RATE
US_COMM_RATE = base_eng.US_COMM_RATE
US_DIV_TAX_RATE = base_eng.US_DIV_TAX_RATE
IE_DIV_TAX_RATE = 0.15  # Ireland-domiciled UCITS US equity ETF dividend withholding (US-IE treaty)
TW_REBAL_BUY_FEE = base_eng.TW_REBAL_BUY_FEE      # 0.1425%
TW_REBAL_SELL_FEE = base_eng.TW_REBAL_SELL_FEE    # 0.1425% + 0.1% ETF tax = 0.2425%
TW_NHI_RATE = base_eng.TW_NHI_RATE
TW_NHI_THRESHOLD_TWD = base_eng.TW_NHI_THRESHOLD_TWD

compute_xirr = base_eng.compute_xirr
compute_max_drawdown_pct = base_eng.compute_max_drawdown_pct
get_dca_Indices_by_timing = base_eng.get_dca_Indices_by_timing
get_rebalance_flags = base_eng.get_rebalance_flags
generate_bootstrap_month_indices = base_eng.generate_bootstrap_month_indices


# ---------------------------------------------------------------------------
# 1. Test 1: Aggressive 200-day SMA Filter Rules (A vs B vs C)
# ---------------------------------------------------------------------------

def compute_sma_below_signal(
    close_series: pd.Series,
    window: int = 200,
    band: float = 0.0,
) -> pd.Series:
    """Compute end-of-day boolean state (True = below 200-day SMA / conservative).

    Callers MUST shift by 1 trading day before execution to guarantee strict causality
    ('跌破後下一個交易日執行').
    """
    vals = close_series.to_numpy(dtype=np.float64)
    sma = close_series.rolling(window, min_periods=window).mean().to_numpy(dtype=np.float64)
    state = np.zeros(len(vals), dtype=bool)
    cons = False
    lo_mult = 1.0 - band
    hi_mult = 1.0 + band
    for i in range(len(vals)):
        s = sma[i]
        if not np.isnan(s) and s > 0.0:
            if band <= 0.0:
                cons = bool(vals[i] < s)
            else:
                if not cons and vals[i] < s * lo_mult:
                    cons = True
                elif cons and vals[i] > s * hi_mult:
                    cons = False
        state[i] = cons
    return pd.Series(state, index=close_series.index)


def simulate_aggressive_filter_rule(
    dates: pd.DatetimeIndex,
    w_0050: np.ndarray,
    w_00631l: np.ndarray,
    below_sma_exec: np.ndarray,       # Causal: shifted by 1 day so day t acts on day t-1 close
    rule: str,                        # "A", "B", or "C"
    w_us: np.ndarray | None = None,   # Optional US equity wealth (TWD) for full 40/10/50 portfolio
    tw_share: float = 1.0,            # 1.0 = pure TW 80/20 leg; 0.5 = 50% TW (80/20 inside) + 50% US
    lev_frac_in_tw: float = 0.20,     # 20% 00631L inside TW leg -> 80/20
    monthly_contrib_twd: float = MONTHLY_CONTRIB_TWD,
    rebalance_annual: bool = True,
    fx_fee_rate: float = BASE_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
    sell_lev_on_date: pd.Timestamp | None = None,  # Optional age-50 exit date for 00631L
) -> dict[str, Any]:
    """Simulate Rule A (new DCA only), Rule B (full position switch next day), or Rule C (no filter).

    All TW switches charge TW_REBAL_SELL_FEE (0.2425%: 0.1425% comm + 0.1% ETF tax) on sell
    and TW_REBAL_BUY_FEE (0.1425%) on buy.
    """
    if rule not in ("A", "B", "C"):
        raise ValueError(f"Unknown filter rule: {rule}")

    n_days = len(dates)
    ms = get_dca_Indices_by_timing(dates, "start")
    m_dates = dates[ms]
    reb_flags = get_rebalance_flags(m_dates, "annual" if rebalance_annual else "none")
    ms_map = {int(ms[m]): m for m in range(len(ms))}

    g_50 = np.ones(n_days, dtype=np.float64)
    g_631 = np.ones(n_days, dtype=np.float64)
    g_50[1:] = w_0050[1:] / np.maximum(w_0050[:-1], 1e-30)
    g_631[1:] = w_00631l[1:] / np.maximum(w_00631l[:-1], 1e-30)

    has_us = (w_us is not None) and (tw_share < 1.0 - 1e-9)
    g_us = np.ones(n_days, dtype=np.float64)
    if has_us:
        assert w_us is not None
        g_us[1:] = w_us[1:] / np.maximum(w_us[:-1], 1e-30)

    pos_50 = 0.0
    pos_631 = 0.0
    pos_us = 0.0
    us_buy_net = (1.0 - fx_fee_rate) * (1.0 - us_comm_rate)

    v_daily = np.empty(n_days, dtype=np.float64)
    switch_events = 0
    rebalance_events = 0
    total_sell_volume = 0.0
    total_fees_twd = 0.0
    full_sell_log: list[dict[str, Any]] = []
    full_buyback_log: list[dict[str, Any]] = []

    prev_below = False
    lev_disabled = False

    for t in range(n_days):
        if t > 0:
            pos_50 *= float(g_50[t])
            pos_631 *= float(g_631[t])
            if has_us:
                pos_us *= float(g_us[t])

        # Check optional permanent 00631L exit date (e.g., age 50 January)
        if sell_lev_on_date is not None and (not lev_disabled) and dates[t] >= sell_lev_on_date:
            lev_disabled = True
            if pos_631 > 1.0:
                sold = pos_631
                pos_631 = 0.0
                net_in = sold * (1.0 - TW_REBAL_SELL_FEE) * (1.0 - TW_REBAL_BUY_FEE)
                total_fees_twd += (sold - net_in)
                total_sell_volume += sold
                pos_50 += net_in
                switch_events += 1

        is_below = bool(below_sma_exec[t]) if (rule in ("A", "B") and not lev_disabled) else False

        # Rule B: Daily transition execution (sell all 00631L -> 0050 on drop; restore 80/20 on recovery)
        if rule == "B" and (not lev_disabled):
            if is_below and (not prev_below):
                # Closed below SMA200 yesterday -> sell ALL 00631L today into 0050
                if pos_631 > 1.0:
                    sold = pos_631
                    pos_631 = 0.0
                    net_in = sold * (1.0 - TW_REBAL_SELL_FEE) * (1.0 - TW_REBAL_BUY_FEE)
                    fee = sold - net_in
                    pos_50 += net_in
                    total_fees_twd += fee
                    total_sell_volume += sold
                    switch_events += 1
                    full_sell_log.append({
                        "date": dates[t].strftime("%Y-%m-%d"),
                        "sold_00631l_twd": round(sold, 2),
                        "fee_twd": round(fee, 2),
                    })
            elif (not is_below) and prev_below:
                # Closed back above SMA200 yesterday -> restore 80/20 TW leg today
                tw_tot = pos_50 + pos_631
                if tw_tot > 1.0:
                    tgt_631 = tw_tot * lev_frac_in_tw
                    if tgt_631 > pos_631 + 1.0:
                        sell_50 = min(pos_50, tgt_631 - pos_631)
                        pos_50 -= sell_50
                        net_in = sell_50 * (1.0 - TW_REBAL_SELL_FEE) * (1.0 - TW_REBAL_BUY_FEE)
                        fee = sell_50 - net_in
                        pos_631 += net_in
                        total_fees_twd += fee
                        total_sell_volume += sell_50
                        switch_events += 1
                        full_buyback_log.append({
                            "date": dates[t].strftime("%Y-%m-%d"),
                            "sold_0050_twd": round(sell_50, 2),
                            "fee_twd": round(fee, 2),
                        })

        prev_below = is_below

        # Monthly DCA & Annual Jan Rebalance
        if t in ms_map:
            m_idx = ms_map[t]
            tw_in = monthly_contrib_twd * tw_share
            us_in = monthly_contrib_twd * (1.0 - tw_share)

            if lev_disabled or (rule in ("A", "B") and is_below):
                pos_50 += tw_in
            else:
                pos_50 += tw_in * (1.0 - lev_frac_in_tw)
                pos_631 += tw_in * lev_frac_in_tw

            if has_us and us_in > 1e-9:
                pos_us += us_in * us_buy_net
                total_fees_twd += us_in * (1.0 - us_buy_net)

            if m_idx > 0 and reb_flags[m_idx]:
                tot_v = pos_50 + pos_631 + pos_us
                if tot_v > 1.0:
                    if lev_disabled or (rule == "B" and is_below):
                        tgt_50 = tot_v * tw_share
                        tgt_631 = 0.0
                    elif rule == "A" and is_below:
                        # Under Rule A when below SMA200: do not buy MORE 00631L
                        tgt_tw = tot_v * tw_share
                        tgt_631 = min(pos_631, tgt_tw * lev_frac_in_tw)
                        tgt_50 = tgt_tw - tgt_631
                    else:
                        tgt_tw = tot_v * tw_share
                        tgt_631 = tgt_tw * lev_frac_in_tw
                        tgt_50 = tgt_tw - tgt_631
                    tgt_us = tot_v * (1.0 - tw_share) if has_us else 0.0

                    # Execute rebalance with explicit fees
                    cur = np.array([pos_50, pos_631, pos_us], dtype=np.float64)
                    tgt = np.array([tgt_50, tgt_631, tgt_us], dtype=np.float64)
                    diff = tgt - cur
                    sell = np.maximum(0.0, -diff)
                    buy = np.maximum(0.0, diff)
                    s_sum = float(np.sum(sell))
                    if s_sum > 1.0:
                        rebalance_events += 1
                        total_sell_volume += s_sum
                        c_tw = float((sell[0] + sell[1]) * (1.0 - TW_REBAL_SELL_FEE))
                        c_us = float(sell[2] * (1.0 - us_comm_rate))
                        want_tw = float(buy[0] + buy[1])
                        want_us = float(buy[2])
                        loc_tw = min(c_tw, want_tw)
                        loc_us = min(c_us, want_us)
                        al_tw = loc_tw + (c_us - loc_us) * (1.0 - fx_fee_rate)
                        al_us = loc_us + (c_tw - loc_tw) * (1.0 - fx_fee_rate)
                        cur -= sell
                        added = 0.0
                        if want_tw > 1e-9:
                            net_tw_add = al_tw * (1.0 - TW_REBAL_BUY_FEE)
                            cur[0] += buy[0] * (net_tw_add / want_tw)
                            cur[1] += buy[1] * (net_tw_add / want_tw)
                            added += net_tw_add
                        if want_us > 1e-9:
                            net_us_add = al_us * (1.0 - us_comm_rate)
                            cur[2] += net_us_add
                            added += net_us_add
                        total_fees_twd += max(0.0, s_sum - added)
                        pos_50, pos_631, pos_us = float(cur[0]), float(cur[1]), float(cur[2])

        v_daily[t] = pos_50 + pos_631 + pos_us

    days_arr = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    yrs = max(float(days_arr[-1]) / 365.25, 1e-6)
    cf_days = np.append(days_arr[ms], days_arr[-1])
    cf_amts = np.append(-np.full(len(ms), monthly_contrib_twd), float(v_daily[-1]))
    xirr_pct = compute_xirr(cf_days, cf_amts) * 100.0
    mdd_pct = compute_max_drawdown_pct(v_daily)
    avg_nav = max(float(np.mean(v_daily)), 1.0)

    return {
        "rule": rule,
        "years": round(yrs, 2),
        "final_value_twd": float(v_daily[-1]),
        "total_invested_twd": float(len(ms) * monthly_contrib_twd),
        "xirr_pct": xirr_pct,
        "max_drawdown_pct": mdd_pct,
        "switch_events": switch_events,
        "rebalance_events": rebalance_events,
        "total_trade_events": switch_events + rebalance_events,
        "switches_per_year": switch_events / yrs,
        "total_events_per_year": (switch_events + rebalance_events) / yrs,
        "annual_turnover_pct": (total_sell_volume / avg_nav / yrs) * 100.0,
        "total_fees_twd": total_fees_twd,
        "full_sell_log": full_sell_log,
        "full_buyback_log": full_buyback_log,
        "daily_values": v_daily,
    }


# ---------------------------------------------------------------------------
# 2. Test 2: US Non-Resident Alien Estate Tax (IRC §§ 2101, 2102, 2001(c))
# ---------------------------------------------------------------------------

US_ESTATE_TAX_BRACKETS_USD: list[tuple[float, float, float, float]] = [
    # (lower_bound, upper_bound, base_tentative_tax, marginal_rate)
    (0.0, 10_000.0, 0.0, 0.18),
    (10_000.0, 20_000.0, 1_800.0, 0.20),
    (20_000.0, 40_000.0, 3_800.0, 0.22),
    (40_000.0, 60_000.0, 8_200.0, 0.24),
    (60_000.0, 80_000.0, 13_000.0, 0.26),
    (80_000.0, 100_000.0, 18_200.0, 0.28),
    (100_000.0, 150_000.0, 23_800.0, 0.30),
    (150_000.0, 250_000.0, 38_800.0, 0.32),
    (250_000.0, 500_000.0, 70_800.0, 0.34),
    (500_000.0, 750_000.0, 155_800.0, 0.37),
    (750_000.0, 1_000_000.0, 248_300.0, 0.39),
    (1_000_000.0, math.inf, 345_800.0, 0.40),
]
US_NRA_UNIFIED_CREDIT_USD = 13_000.0  # Shields exact first $60,000 of US-situs estate


def compute_us_nra_estate_tax_usd(us_situs_usd: float) -> dict[str, float]:
    """Compute exact US Federal Estate Tax for a Non-Resident Alien (Form 706-NA, IRC §2001(c) & §2102(b)(1))."""
    amt = max(0.0, float(us_situs_usd))
    tentative = 0.0
    for lo, hi, base_tax, rate in US_ESTATE_TAX_BRACKETS_USD:
        if amt <= hi:
            tentative = base_tax + (amt - lo) * rate
            break
    net_tax = max(0.0, tentative - US_NRA_UNIFIED_CREDIT_USD)
    eff_rate_pct = (net_tax / amt * 100.0) if amt > 0.0 else 0.0
    return {
        "us_situs_usd": amt,
        "tentative_tax_usd": tentative,
        "unified_credit_usd": US_NRA_UNIFIED_CREDIT_USD,
        "net_estate_tax_usd": net_tax,
        "effective_tax_rate_pct": eff_rate_pct,
        "net_inherited_usd": amt - net_tax,
    }


# ---------------------------------------------------------------------------
# 3. Test 3: Bond ETF Synthesis from Treasury Yields (^IRX, ^FVX, ^TNX)
# ---------------------------------------------------------------------------

def synthesize_bond_daily_returns(
    yield_decimal: pd.Series,
    duration: float,
    convexity: float,
    expense_ratio: float,
    trading_days_per_year: float = TRADING_DAYS_PER_YEAR,
) -> pd.Series:
    """Synthesize daily bond ETF total return from benchmark Treasury yield series (in decimal, e.g. 0.045).

    Formula:
        r_t = y_{t-1} / 252 - D * (y_t - y_{t-1}) + 0.5 * C * (y_t - y_{t-1})^2 - ER / 252
    """
    y = yield_decimal.astype(np.float64).clip(lower=0.0)
    y_prev = y.shift(1).bfill()
    dy = y - y_prev
    r = (y_prev / trading_days_per_year) - duration * dy + 0.5 * convexity * (dy ** 2) - (expense_ratio / trading_days_per_year)
    r.iloc[0] = 0.0
    return r.clip(lower=-0.50)


def synthesize_shiller_monthly_10y_bond_returns(gs10_pct: np.ndarray) -> np.ndarray:
    """Compute monthly total return of a constant-maturity 10-year US Treasury bond from Shiller GS10 (%).

    Uses exact par-bond modified duration D = (1/y) * (1 - (1 + y/2)^(-20)) and convexity approx D^2.
    """
    y = np.maximum(np.asarray(gs10_pct, dtype=np.float64) / 100.0, 0.0005)
    n = len(y)
    r = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        y0 = y[i - 1]
        y1 = y[i]
        dy = y1 - y0
        # Exact semi-annual coupon par-bond modified duration for 10Y maturity
        macaulay_d = ((1.0 + y0 / 2.0) / y0) * (1.0 - (1.0 + y0 / 2.0) ** (-20.0))
        mod_d = macaulay_d / (1.0 + y0 / 2.0)
        conv = mod_d * (mod_d + 1.0)
        r[i] = (y0 / 12.0) - mod_d * dy + 0.5 * conv * (dy ** 2)
    return np.clip(r, -0.40, 0.60)


# ---------------------------------------------------------------------------
# 4. Tests 4, 5, 6, 7, 8: Glidepath, FX Scenarios, & Continuous Lifetime Engine
# ---------------------------------------------------------------------------

def get_glidepath_weights(
    age: int,
    retire_age: int = 60,
    glide_start_age: int = 55,
    acc_tw: float = 0.50,
    acc_us: float = 0.50,
    acc_bond: float = 0.00,
    retire_tw: float = 0.30,
    retire_us: float = 0.30,
    retire_bond: float = 0.40,
) -> tuple[float, float, float]:
    """Return exact (w_tw, w_us, w_bond) weights for a given integer age.

    Default schedule matches article recommendation:
      - age < 55:  50 / 50 / 0
      - age == 55: 46 / 46 / 8
      - age == 56: 42 / 42 / 16
      - age == 57: 38 / 38 / 24
      - age == 58: 34 / 34 / 32
      - age >= 59: 30 / 30 / 40
    """
    if age < glide_start_age:
        return (acc_tw, acc_us, acc_bond)
    glide_steps = max(1, retire_age - glide_start_age)  # 5 steps: 55, 56, 57, 58, 59
    step = min(glide_steps, age - glide_start_age + 1)
    frac = step / float(glide_steps)
    w_tw = acc_tw + (retire_tw - acc_tw) * frac
    w_us = acc_us + (retire_us - acc_us) * frac
    w_bond = acc_bond + (retire_bond - acc_bond) * frac
    return (round(w_tw, 6), round(w_us, 6), round(w_bond, 6))


def build_fx_scenario_path(
    n_months: int,
    base_fx: float = 32.0,
    scenario: str = "flat",
    shock_start_month: int = 0,
    shock_duration_months: int = 60,
) -> np.ndarray:
    """Build deterministic monthly USD/TWD exchange rate path of length n_months + 1 (index 0..n_months)."""
    fx = np.full(n_months + 1, float(base_fx), dtype=np.float64)
    if scenario == "flat":
        return fx
    if scenario == "drift_down_2pct":
        # TWD appreciates 2%/yr (USD/TWD falls 2%/yr)
        for m in range(n_months + 1):
            if m >= shock_start_month:
                fx[m] = base_fx * (0.98 ** ((m - shock_start_month) / 12.0))
        return fx
    if scenario == "drift_up_2pct":
        # TWD depreciates 2%/yr (USD/TWD rises 2%/yr)
        for m in range(n_months + 1):
            if m >= shock_start_month:
                fx[m] = base_fx * (1.02 ** ((m - shock_start_month) / 12.0))
        return fx
    if scenario in ("appreciate_to_25", "depreciate_to_40"):
        target_fx = 25.0 if scenario == "appreciate_to_25" else 40.0
        for m in range(n_months + 1):
            if m < shock_start_month:
                fx[m] = base_fx
            elif m <= shock_start_month + shock_duration_months:
                alpha = (m - shock_start_month) / float(max(1, shock_duration_months))
                fx[m] = base_fx + (target_fx - base_fx) * alpha
            else:
                fx[m] = target_fx
        return fx
    raise ValueError(f"Unknown FX scenario: {scenario}")


def simulate_lifetime_continuous(
    r_tw_m: np.ndarray,                # Monthly total return of TW stock (in TWD, decimal)
    r_us_usd_m: np.ndarray,            # Monthly total return of US stock (in USD, decimal)
    r_bond_usd_m: np.ndarray,          # Monthly total return of US bond ETF (in USD, decimal)
    fx_m: np.ndarray,                  # Length >= n_months + 1, USD/TWD rate at each month boundary
    cpi_m: np.ndarray | None = None,   # Optional CPI level array length >= n_months + 1
    start_age: int = 30,
    glide_start_age: int = 55,
    retire_age: int = 60,
    end_age: int = 90,
    monthly_contrib_twd: float = MONTHLY_CONTRIB_TWD,
    contrib_growth_rate: float = 0.0,  # 0.0 = fixed, 0.03 = +3%/yr salary raise
    acc_weights: tuple[float, float, float] = (0.50, 0.50, 0.00),
    retire_weights: tuple[float, float, float] = (0.30, 0.30, 0.40),
    twd_cash_share_in_retire: float = 0.0,   # Optional TWD cash/deposit slice carved out of retire bond
    twd_cash_annual_rate: float = 0.015,
    initial_withdrawal_rate: float = 0.04,
    withdrawal_rule: str = "protect_20_35_plus_2",
    inflation_fixed_rate: float = 0.02,
    protection_drop_threshold: float = 0.20,  # 20% below W_retire
    protection_rate: float = 0.035,           # 3.5% of current January wealth
    protection_floor_pct: float | None = None,  # Optional floor e.g. 0.75 of Year-1 nominal
    fx_fee_rate: float = BASE_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
    r_lev_tw_m: np.ndarray | None = None,     # Optional 00631L monthly return
    lev_frac_in_tw: float = 0.0,              # e.g. 0.20 inside TW leg
    sell_lev_age: int | None = None,          # e.g. 50, 55, or None (never sell)
    initial_lump_sum_twd: float = 0.0,        # Optional starting balance (e.g. retirement-only start_age == retire_age)
) -> dict[str, Any]:
    """Run a single continuous asset lifecycle from start_age through glidepath to end_age.

    Assets tracked in TWD (USD assets multiplied by monthly FX return ratio fx[m+1]/fx[m]):
      0: TW 1x equity (0050)
      1: US 1x equity (SPY/VOO/CSPX)
      2: US Bond ETF (SHY/VGIT/IEF)
      3: TWD Cash/Deposit buffer (optional, used in FX-hedge test)
      4: TW 2x equity (00631L, optional for Test 1 age-50 exit check)
    """
    n_years = end_age - start_age
    n_months = n_years * 12
    assert len(r_tw_m) >= n_months
    assert len(r_us_usd_m) >= n_months
    assert len(r_bond_usd_m) >= n_months
    assert len(fx_m) >= n_months + 1

    if cpi_m is None:
        cpi = np.power(1.0 + inflation_fixed_rate, np.arange(n_months + 1, dtype=np.float64) / 12.0)
    else:
        cpi = np.asarray(cpi_m[: n_months + 1], dtype=np.float64)

    pos = np.zeros(5, dtype=np.float64)
    is_tw_asset = np.array([True, False, False, True, True], dtype=bool)
    buy_net = np.where(is_tw_asset, 1.0, (1.0 - fx_fee_rate) * (1.0 - us_comm_rate))
    sell_net = np.where(is_tw_asset, 1.0 - TW_REBAL_SELL_FEE, (1.0 - us_comm_rate) * (1.0 - fx_fee_rate))
    # Cash (idx 3) has zero transaction fees
    buy_net[3] = 1.0
    sell_net[3] = 1.0

    def _target_weights_5(age: int) -> np.ndarray:
        w_tw, w_us, w_bond = get_glidepath_weights(
            age,
            retire_age=retire_age,
            glide_start_age=glide_start_age,
            acc_tw=acc_weights[0],
            acc_us=acc_weights[1],
            acc_bond=acc_weights[2],
            retire_tw=retire_weights[0],
            retire_us=retire_weights[1],
            retire_bond=retire_weights[2],
        )
        w_cash = 0.0
        if age >= retire_age and twd_cash_share_in_retire > 0.0:
            w_cash = min(w_bond, twd_cash_share_in_retire)
            w_bond = max(0.0, w_bond - w_cash)
        elif age >= glide_start_age and twd_cash_share_in_retire > 0.0 and retire_weights[2] > 0.0:
            # Proportionally build TWD cash share during glidepath too
            cash_ratio = min(1.0, twd_cash_share_in_retire / retire_weights[2])
            w_cash = w_bond * cash_ratio
            w_bond = w_bond - w_cash

        use_lev = (r_lev_tw_m is not None) and (lev_frac_in_tw > 0.0) and (
            sell_lev_age is None or age < sell_lev_age
        )
        if use_lev:
            w_lev = w_tw * lev_frac_in_tw
            w_tw_1x = w_tw - w_lev
        else:
            w_lev = 0.0
            w_tw_1x = w_tw
        return np.array([w_tw_1x, w_us, w_bond, w_cash, w_lev], dtype=np.float64)

    def _rebalance(p: np.ndarray, tgt_w: np.ndarray) -> np.ndarray:
        tot = float(np.sum(p))
        if tot <= 1.0:
            return p
        tgt = tot * tgt_w
        diff = tgt - p
        sell = np.maximum(0.0, -diff)
        buy = np.maximum(0.0, diff)
        if float(np.sum(sell)) <= 1.0:
            return p
        # TW securities: idx 0, 4; TW cash: idx 3; US securities: idx 1, 2
        c_tw = float((sell[0] + sell[4]) * (1.0 - TW_REBAL_SELL_FEE) + sell[3])
        c_us = float((sell[1] + sell[2]) * (1.0 - us_comm_rate))
        want_tw_sec = float(buy[0] + buy[4])
        want_tw_cash = float(buy[3])
        want_tw = want_tw_sec + want_tw_cash
        want_us = float(buy[1] + buy[2])

        loc_tw = min(c_tw, want_tw)
        loc_us = min(c_us, want_us)
        al_tw = loc_tw + (c_us - loc_us) * (1.0 - fx_fee_rate)
        al_us = loc_us + (c_tw - loc_tw) * (1.0 - fx_fee_rate)

        p = p - sell
        if want_tw > 1e-9:
            scale_tw = al_tw / want_tw
            p[0] += buy[0] * scale_tw * (1.0 - TW_REBAL_BUY_FEE)
            p[4] += buy[4] * scale_tw * (1.0 - TW_REBAL_BUY_FEE)
            p[3] += buy[3] * scale_tw
        if want_us > 1e-9:
            scale_us = (al_us * (1.0 - us_comm_rate)) / want_us
            p[1] += buy[1] * scale_us
            p[2] += buy[2] * scale_us
        return p

    if initial_lump_sum_twd > 0.0:
        w0 = _target_weights_5(start_age)
        pos += (initial_lump_sum_twd * w0) * buy_net

    total_contributed = float(initial_lump_sum_twd)
    retire_month = (retire_age - start_age) * 12

    wealth_at_retire = 0.0
    cpi_at_retire = float(cpi[retire_month]) if retire_month <= n_months else 1.0
    base_annual_wd = 0.0
    in_protection = False
    protection_years = 0
    ruined = False
    ruin_age: float | None = None

    annual_wealth: dict[int, float] = {start_age: float(np.sum(pos))}
    annual_weights_used: dict[int, tuple[float, float, float]] = {}
    annual_spending_nominal: list[float] = []
    annual_spending_real_pct: list[float] = []
    annual_spending_nom_pct: list[float] = []
    monthly_wealth_retire: list[float] = []

    peak_50_60 = 0.0
    worst_dd_50_60 = 0.0
    peak_retire = 0.0
    worst_dd_retire = 0.0

    cash_g_m = (1.0 + twd_cash_annual_rate) ** (1.0 / 12.0)

    current_year_monthly_wd = 0.0
    current_year_actual_wd = 0.0

    for m in range(n_months):
        yr_offset = m // 12
        age = start_age + yr_offset
        is_jan = (m % 12 == 0)

        # At exact start of retirement (age == retire_age, Jan)
        if m == retire_month:
            wealth_at_retire = float(np.sum(pos))
            cpi_at_retire = float(cpi[m])
            base_annual_wd = wealth_at_retire * initial_withdrawal_rate
            peak_retire = wealth_at_retire
            in_protection = False

        # Annual January rebalance & withdrawal determination
        if is_jan:
            tgt_w = _target_weights_5(age)
            annual_weights_used[age] = (
                round(float(tgt_w[0] + tgt_w[4]), 4),
                round(float(tgt_w[1]), 4),
                round(float(tgt_w[2] + tgt_w[3]), 4),
            )
            if m > 0 and not ruined:
                pos = _rebalance(pos, tgt_w)

            if age >= retire_age:
                ret_yr = age - retire_age  # 0 for age 60, 1 for age 61, ...
                w_jan = float(np.sum(pos))
                if ruined or w_jan <= 1e-6:
                    current_year_monthly_wd = 0.0
                else:
                    if withdrawal_rule == "fixed_4_plus_2":
                        ann_wd = base_annual_wd * ((1.0 + inflation_fixed_rate) ** ret_yr)
                    elif withdrawal_rule == "fixed_4_cpi":
                        ann_wd = base_annual_wd * (float(cpi[m]) / max(cpi_at_retire, 1e-9))
                    elif withdrawal_rule == "flat_4_no_adj":
                        ann_wd = base_annual_wd
                    elif withdrawal_rule in ("protect_20_35_plus_2", "protect_20_35_cpi"):
                        if ret_yr == 0:
                            ann_wd = base_annual_wd
                            in_protection = False
                        else:
                            if not in_protection and w_jan < wealth_at_retire * (1.0 - protection_drop_threshold):
                                in_protection = True
                            elif in_protection and w_jan >= wealth_at_retire:
                                in_protection = False

                            if in_protection:
                                protection_years += 1
                                ann_wd = w_jan * protection_rate
                                if protection_floor_pct is not None:
                                    ann_wd = max(ann_wd, base_annual_wd * protection_floor_pct)
                            else:
                                if withdrawal_rule == "protect_20_35_plus_2":
                                    ann_wd = base_annual_wd * ((1.0 + inflation_fixed_rate) ** ret_yr)
                                else:
                                    ann_wd = base_annual_wd * (float(cpi[m]) / max(cpi_at_retire, 1e-9))
                    else:
                        raise ValueError(f"Unknown withdrawal_rule: {withdrawal_rule}")

                    current_year_monthly_wd = ann_wd / 12.0
                current_year_actual_wd = 0.0

        # Monthly cashflow: contribution (age < retire_age) OR withdrawal (age >= retire_age)
        if age < retire_age:
            contrib_now = monthly_contrib_twd * ((1.0 + contrib_growth_rate) ** yr_offset)
            tgt_w = _target_weights_5(age)
            pos += (contrib_now * tgt_w) * buy_net
            total_contributed += contrib_now
        else:
            if not ruined:
                tot_now = float(np.sum(pos))
                if tot_now <= current_year_monthly_wd + 1e-6:
                    # Exhausted!
                    current_year_actual_wd += max(0.0, tot_now)
                    pos[:] = 0.0
                    ruined = True
                    ruin_age = start_age + (m + 1) / 12.0
                else:
                    # Withdraw pro-rata across assets
                    avg_s_net = float(np.sum((pos / tot_now) * sell_net))
                    gross_wd = min(tot_now, current_year_monthly_wd / max(avg_s_net, 0.5))
                    frac = gross_wd / tot_now
                    pos *= (1.0 - frac)
                    current_year_actual_wd += current_year_monthly_wd

        # Apply monthly market + FX returns
        fx_ratio = float(fx_m[m + 1]) / max(float(fx_m[m]), 1e-9)
        pos[0] *= (1.0 + float(r_tw_m[m]))
        pos[1] *= (1.0 + float(r_us_usd_m[m])) * fx_ratio
        pos[2] *= (1.0 + float(r_bond_usd_m[m])) * fx_ratio
        pos[3] *= cash_g_m
        if r_lev_tw_m is not None:
            pos[4] *= max(0.0, 1.0 + float(r_lev_tw_m[m]))

        tot_end = float(np.sum(pos))

        # Track 50-60 drawdown
        if 50 <= age < retire_age:
            if tot_end > peak_50_60:
                peak_50_60 = tot_end
            if peak_50_60 > 1e-9:
                worst_dd_50_60 = min(worst_dd_50_60, (tot_end - peak_50_60) / peak_50_60)

        if age >= retire_age:
            monthly_wealth_retire.append(tot_end)
            if tot_end > peak_retire:
                peak_retire = tot_end
            if peak_retire > 1e-9:
                worst_dd_retire = min(worst_dd_retire, (tot_end - peak_retire) / peak_retire)

        # End of year bookkeeping
        if (m + 1) % 12 == 0:
            next_age = age + 1
            annual_wealth[next_age] = tot_end
            if age >= retire_age and base_annual_wd > 1e-9:
                annual_spending_nominal.append(current_year_actual_wd)
                nom_pct = (current_year_actual_wd / base_annual_wd) * 100.0
                # Deflate by CPI ratio from retirement to year start (m - 11)
                cpi_yr_start = float(cpi[m - 11])
                real_pct = nom_pct * (cpi_at_retire / max(cpi_yr_start, 1e-9))
                annual_spending_nom_pct.append(nom_pct)
                annual_spending_real_pct.append(real_pct)

    # Handle accumulation-only simulations where retire_age == end_age (retire_month == n_months)
    if retire_month == n_months:
        wealth_at_retire = float(np.sum(pos))
        base_annual_wd = wealth_at_retire * initial_withdrawal_rate

    min_nom_pct = float(min(annual_spending_nom_pct)) if annual_spending_nom_pct else 0.0
    min_real_pct = float(min(annual_spending_real_pct)) if annual_spending_real_pct else 0.0
    last_real_pct = float(annual_spending_real_pct[-1]) if annual_spending_real_pct else 0.0

    return {
        "start_age": start_age,
        "retire_age": retire_age,
        "end_age": end_age,
        "total_contributed_twd": total_contributed,
        "wealth_at_retire_twd": wealth_at_retire,
        "wealth_at_75_twd": annual_wealth.get(75, 0.0),
        "wealth_at_end_twd": annual_wealth.get(end_age, 0.0),
        "retire_multiple": wealth_at_retire / max(total_contributed, 1.0),
        "year1_annual_wd_twd": base_annual_wd,
        "ruined": ruined,
        "ruin_age": ruin_age,
        "protection_years": protection_years,
        "min_annual_spending_nom_pct": min_nom_pct,
        "min_annual_spending_real_pct": min_real_pct,
        "final_year_spending_real_pct": last_real_pct,
        "worst_dd_50_60_pct": worst_dd_50_60 * 100.0,
        "worst_dd_retire_pct": worst_dd_retire * 100.0,
        "annual_wealth": annual_wealth,
        "annual_weights_used": annual_weights_used,
        "annual_spending_nominal": annual_spending_nominal,
        "annual_spending_nom_pct": annual_spending_nom_pct,
        "annual_spending_real_pct": annual_spending_real_pct,
    }

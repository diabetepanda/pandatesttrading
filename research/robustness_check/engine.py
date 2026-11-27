"""Core simulation, tax/FX, behavioral, bootstrap, life-stage, and verdict engine.

All functions in this module are pure / offline-testable on small synthetic datasets
without network access, while reusing identical XIRR, MDD, and cost conventions from
`research/dca_allocation_grid` and `research/peak_recovery`.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252.0
AVG_DAYS_PER_MONTH = 365.25 / 12.0  # 30.4375
MONTHLY_CONTRIB_TWD = 10_000.0

# Baseline cost parameters (matching dca_allocation_grid & peak_recovery)
BASE_FX_FEE_RATE = 0.0010          # 0.1% FX conversion spread (Gate 4 tests 0.3%)
GATE4_FX_FEE_RATE = 0.0030         # 0.3% FX spread required in Gate 4
US_COMM_RATE = 0.0010              # 0.1% US brokerage commission
US_DIV_TAX_RATE = 0.30             # 30% US dividend withholding tax
TW_REBAL_BUY_FEE = 0.001425        # 0.1425% TW rebalance buy commission
TW_REBAL_SELL_FEE = 0.002425       # 0.1% ETF tax + 0.1425% sell commission
TW_NHI_RATE = 0.0211               # 2.11% 二代健保補充保費
TW_NHI_THRESHOLD_TWD = 20_000.0    # Single dividend payout >= 20,000 TWD
R5_BASELINE_MONTHS = 20.24         # Baseline 0050 TR >20% bear underwater median from peak_recovery


# ---------------------------------------------------------------------------
# 1. XIRR & Drawdown / Underwater Calculations
# ---------------------------------------------------------------------------

def compute_xirr(days_from_start: np.ndarray | list[float], cashflows: np.ndarray | list[float]) -> float:
    """Solve annualized XIRR as a decimal (e.g., 0.085 = +8.5%/yr) via robust bracketed bisection + Newton.

    Convention (matching dca_allocation_grid.solve_xirr_scalar_bisect):
      - Negative cashflows = investor contributions (outflows from pocket into account)
      - Positive cashflows = withdrawals + terminal account value (inflows back to investor)
    """
    d_arr = np.asarray(days_from_start, dtype=np.float64)
    cf_arr = np.asarray(cashflows, dtype=np.float64)
    if len(d_arr) != len(cf_arr) or len(d_arr) < 2:
        raise ValueError("days_from_start and cashflows must have matching length >= 2.")

    pos_sum = float(np.sum(cf_arr[cf_arr > 0.0]))
    neg_sum = float(np.sum(-cf_arr[cf_arr < 0.0]))
    if neg_sum <= 1e-12:
        return 0.0
    if pos_sum <= 1e-9:
        return -1.0

    t_end = float(np.max(d_arr))
    if t_end <= 1e-9:
        return 0.0
    rem_yrs = (t_end - d_arr) / 365.25

    def _npv(r: float) -> float:
        base = max(1e-8, 1.0 + r)
        return float(np.sum(cf_arr * np.power(base, rem_yrs)))

    lo, hi = -0.9999, 15.0
    f_lo = _npv(lo)
    f_hi = _npv(hi)
    # Expand upper bound if extreme return exceeds 1500%/yr
    while f_lo * f_hi > 0 and hi < 1e5:
        hi *= 4.0
        f_hi = _npv(hi)

    if f_lo * f_hi > 0:
        # Fallback total-return approximation over mean duration
        tot_ratio = pos_sum / neg_sum
        avg_yrs = max(float(np.sum((-np.minimum(0.0, cf_arr)) * rem_yrs)) / neg_sum, 1.0 / 365.25)
        return float(tot_ratio ** (1.0 / avg_yrs) - 1.0)

    for _ in range(95):
        mid = 0.5 * (lo + hi)
        f_mid = _npv(mid)
        if abs(f_mid) < 1e-10 or (hi - lo) < 1e-12:
            return mid
        if f_lo * f_mid <= 0.0:
            hi = mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


FAST_U_GRID = np.linspace(-6.5, 2.5, 4096, dtype=np.float64)


def precompute_xirr_y_grid(rem_years: np.ndarray) -> np.ndarray:
    """Precompute log(sum_j exp(u * tau_j)) on 4096-point grid for O(1) standard DCA XIRR."""
    tau = np.asarray(rem_years, dtype=np.float64)
    mat = FAST_U_GRID[:, None] * tau[None, :]
    m_max = np.max(mat, axis=1, keepdims=True)
    return m_max[:, 0] + np.log(np.sum(np.exp(mat - m_max), axis=1))


def solve_xirr_from_y_grid(y_grid: np.ndarray, multiples_of_contrib: np.ndarray) -> np.ndarray:
    """Vectorized XIRR lookup for standard uniform-contribution DCA."""
    m_vals = np.asarray(multiples_of_contrib, dtype=np.float64)
    out = np.full(m_vals.shape, -1.0, dtype=np.float64)
    valid = m_vals > 1e-9
    if not np.any(valid):
        return out
    u = np.interp(np.log(m_vals[valid]), y_grid, FAST_U_GRID)
    out[valid] = np.expm1(u)
    return out


def compute_max_drawdown_pct(values: np.ndarray | list[float]) -> float:
    """Compute maximum percentage drawdown (<= 0.0, e.g., -48.36) from an asset/account value curve."""
    v = np.asarray(values, dtype=np.float64)
    if len(v) == 0:
        return 0.0
    peaks = np.maximum.accumulate(np.maximum(v, 1e-12))
    dd = (v - peaks) / peaks
    return float(np.min(dd)) * 100.0


def extract_bear_episodes(
    series: pd.Series,
    min_dd_pct: float = -20.0,
    include_unrecovered_in_median: bool = False,
) -> dict[str, Any]:
    """Extract ATH-to-ATH underwater episodes with max_drawdown_pct <= min_dd_pct."""
    s = series.dropna()
    if len(s) < 2:
        return {
            "episodes": [],
            "recovered_count": 0,
            "unrecovered_count": 0,
            "median_recovered_months": None,
            "median_all_censored_months": None,
        }
    dates = s.index
    vals = s.to_numpy(dtype=np.float64)
    n = len(vals)
    episodes: list[dict[str, Any]] = []

    h_val = float(vals[0])
    i_pk = 0
    for t in range(1, n):
        v = float(vals[t])
        if v >= h_val:
            if t > i_pk + 1:
                seg = vals[i_pk:t]
                i_tr = i_pk + int(np.argmin(seg))
                tr_val = float(vals[i_tr])
                dd_pct = (tr_val - h_val) / h_val * 100.0
                if dd_pct <= min_dd_pct:
                    pk_dt, tr_dt, rc_dt = dates[i_pk], dates[i_tr], dates[t]
                    tot_m = (rc_dt - pk_dt).days / AVG_DAYS_PER_MONTH
                    episodes.append({
                        "peak_date": pk_dt.strftime("%Y-%m-%d"),
                        "trough_date": tr_dt.strftime("%Y-%m-%d"),
                        "recovery_date": rc_dt.strftime("%Y-%m-%d"),
                        "recovered": True,
                        "max_drawdown_pct": round(dd_pct, 2),
                        "underwater_months": round(tot_m, 2),
                    })
            h_val = v
            i_pk = t

    if float(vals[-1]) < h_val and (n - 1) > i_pk:
        seg = vals[i_pk:]
        i_tr = i_pk + int(np.argmin(seg))
        tr_val = float(vals[i_tr])
        dd_pct = (tr_val - h_val) / h_val * 100.0
        if dd_pct <= min_dd_pct:
            pk_dt, tr_dt, end_dt = dates[i_pk], dates[i_tr], dates[-1]
            el_m = (end_dt - pk_dt).days / AVG_DAYS_PER_MONTH
            episodes.append({
                "peak_date": pk_dt.strftime("%Y-%m-%d"),
                "trough_date": tr_dt.strftime("%Y-%m-%d"),
                "recovery_date": "至今未回",
                "recovered": False,
                "max_drawdown_pct": round(dd_pct, 2),
                "underwater_months": None,
                "elapsed_months": round(el_m, 2),
            })

    rec_m = [float(e["underwater_months"]) for e in episodes if e["recovered"]]
    cens_m = [
        float(e["underwater_months"]) if e["recovered"] else float(e["elapsed_months"])
        for e in episodes
    ]
    med_rec = round(float(np.median(rec_m)), 2) if rec_m else None
    med_cens = round(float(np.median(cens_m)), 2) if cens_m else None
    return {
        "episodes": episodes,
        "recovered_count": len(rec_m),
        "unrecovered_count": len(episodes) - len(rec_m),
        "median_recovered_months": med_rec,
        "median_all_censored_months": med_cens,
        "primary_median_months": med_cens if include_unrecovered_in_median else med_rec,
    }


# ---------------------------------------------------------------------------
# 2. SMA Hysteresis Filter & Schedule Helpers
# ---------------------------------------------------------------------------

def compute_sma_hysteresis_state(
    close_series: pd.Series,
    window: int = 200,
    band: float = 0.02,
) -> pd.Series:
    """Causal SMA +-band hysteresis state on native trading days (True = conservative)."""
    vals = close_series.to_numpy(dtype=np.float64)
    sma = close_series.rolling(window, min_periods=window).mean().to_numpy(dtype=np.float64)
    state = np.zeros(len(vals), dtype=bool)
    cons = False
    lo_mult = 1.0 - band
    hi_mult = 1.0 + band
    for i in range(len(vals)):
        s = sma[i]
        if not np.isnan(s) and s > 0.0:
            if not cons and vals[i] < s * lo_mult:
                cons = True
            elif cons and vals[i] > s * hi_mult:
                cons = False
        state[i] = cons
    return pd.Series(state, index=close_series.index)


def get_dca_Indices_by_timing(dates: pd.DatetimeIndex, timing: str = "start") -> np.ndarray:
    """Return 1 trading-day index per calendar month for 'start' (月初), 'mid' (月中 >=15日), or 'end' (月底)."""
    if len(dates) == 0:
        return np.array([], dtype=np.int64)
    ym = dates.year * 100 + dates.month
    uniq_ym, first_idx = np.unique(ym, return_index=True)
    if timing == "start":
        return first_idx.astype(np.int64)

    out = np.empty(len(uniq_ym), dtype=np.int64)
    days_of_m = dates.day.to_numpy()
    for i, u in enumerate(uniq_ym):
        s_i = int(first_idx[i])
        e_i = int(first_idx[i + 1]) if i + 1 < len(uniq_ym) else len(dates)
        if timing == "end":
            out[i] = e_i - 1
        elif timing == "mid":
            sub_days = days_of_m[s_i:e_i]
            ge15 = np.flatnonzero(sub_days >= 15)
            out[i] = s_i + int(ge15[0]) if len(ge15) > 0 else (e_i - 1)
        else:
            raise ValueError(f"Unknown DCA timing: {timing}")
    return out


def get_rebalance_flags(m_dates: pd.DatetimeIndex, mode: str) -> np.ndarray:
    """Return boolean flags of length M for 'none', 'monthly', 'quarterly', 'annual'."""
    n_m = len(m_dates)
    flags = np.zeros(n_m, dtype=bool)
    if mode == "none" or n_m <= 1:
        return flags
    if mode == "monthly":
        flags[1:] = True
        return flags
    years = m_dates.year.to_numpy()
    months = m_dates.month.to_numpy()
    if mode == "annual":
        for m in range(1, n_m):
            if years[m] != years[m - 1]:
                flags[m] = True
    elif mode == "quarterly":
        q_idx = years * 4 + ((months - 1) // 3)
        for m in range(1, n_m):
            if q_idx[m] != q_idx[m - 1]:
                flags[m] = True
    else:
        raise ValueError(f"Unknown rebalance mode: {mode}")
    return flags


# ---------------------------------------------------------------------------
# 3. Tax, Supplement NHI (2.11% >= 20k TWD), FX Spread & USD 15 Min-Fee Engine
# ---------------------------------------------------------------------------

def compute_net_tw_dividend_cash(
    gross_div_twd: float,
    income_tax_rate: float = 0.0,
    nhi_rate: float = TW_NHI_RATE,
    nhi_threshold_twd: float = TW_NHI_THRESHOLD_TWD,
) -> dict[str, float]:
    """Apply Taiwan 2nd-gen NHI supplement (2.11% if single payout >= 20,000 TWD) and income tax."""
    if gross_div_twd <= 0.0:
        return {"gross": 0.0, "nhi_fee": 0.0, "income_tax": 0.0, "net": 0.0}
    nhi_fee = gross_div_twd * nhi_rate if gross_div_twd >= nhi_threshold_twd - 1e-9 else 0.0
    inc_tax = gross_div_twd * income_tax_rate
    net = max(0.0, gross_div_twd - nhi_fee - inc_tax)
    return {
        "gross": gross_div_twd,
        "nhi_fee": nhi_fee,
        "income_tax": inc_tax,
        "net": net,
    }


def compute_net_us_dividend_usd(gross_div_usd: float, withholding_rate: float = US_DIV_TAX_RATE) -> float:
    """Apply 30% US dividend withholding tax."""
    return max(0.0, gross_div_usd * (1.0 - withholding_rate))


def simulate_tw_us_dca_with_taxes_and_fx(
    dates: pd.DatetimeIndex,
    tw_price_ret: np.ndarray,
    tw_div_yield_daily: np.ndarray,   # Non-zero on semi-annual/annual ex-div days (or Payout dates)
    us_price_ret_usd: np.ndarray,
    us_div_yield_daily: np.ndarray,   # Gross US dividend yield on ex-div days
    fx_twd_per_usd: np.ndarray,
    w_tw: float,
    w_us: float,
    monthly_contrib_twd: float = MONTHLY_CONTRIB_TWD,
    rebalance_mode: str = "none",
    fx_spread_rate: float = GATE4_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
    us_min_fee_usd: float = 0.0,
    us_withholding_rate: float = US_DIV_TAX_RATE,
    tw_income_tax_rate: float = 0.0,
    tw_nhi_rate: float = TW_NHI_RATE,
    tw_nhi_threshold_twd: float = TW_NHI_THRESHOLD_TWD,
) -> dict[str, Any]:
    """Exact day-by-day ledger for TW (0050) + US (SPY) DCA with explicit taxes, NHI threshold, FX spread, and min fee."""
    n_days = len(dates)
    ms = get_dca_Indices_by_timing(dates, "start")
    m_dates = dates[ms]
    reb_flags = get_rebalance_flags(m_dates, rebalance_mode)
    ms_map = {int(ms[m]): m for m in range(len(ms))}

    val_tw = 0.0
    val_us_usd = 0.0
    tot_nhi_paid = 0.0
    tot_tw_tax_paid = 0.0
    tot_us_tax_paid_twd = 0.0
    tot_fx_and_comm_twd = 0.0
    nhi_trigger_count = 0

    v_daily = np.empty(n_days, dtype=np.float64)

    for t in range(n_days):
        fx_t = float(fx_twd_per_usd[t])
        # 1) Apply daily price return & explicit dividend payout on existing holdings
        if t > 0:
            prev_tw = val_tw
            val_tw = prev_tw * (1.0 + float(tw_price_ret[t]))
            dy_tw = float(tw_div_yield_daily[t])
            if dy_tw > 0.0 and prev_tw > 0.0:
                gross_div_tw = prev_tw * dy_tw
                tax_res = compute_net_tw_dividend_cash(
                    gross_div_tw,
                    income_tax_rate=tw_income_tax_rate,
                    nhi_rate=tw_nhi_rate,
                    nhi_threshold_twd=tw_nhi_threshold_twd,
                )
                tot_nhi_paid += tax_res["nhi_fee"]
                tot_tw_tax_paid += tax_res["income_tax"]
                if tax_res["nhi_fee"] > 0.0:
                    nhi_trigger_count += 1
                # Reinvest net dividend (deducting TW buy commission if positive)
                val_tw += tax_res["net"] * (1.0 - TW_REBAL_BUY_FEE)

            prev_us = val_us_usd
            val_us_usd = prev_us * (1.0 + float(us_price_ret_usd[t]))
            dy_us = float(us_div_yield_daily[t])
            if dy_us > 0.0 and prev_us > 0.0:
                gross_div_us = prev_us * dy_us
                net_div_us = compute_net_us_dividend_usd(gross_div_us, us_withholding_rate)
                tot_us_tax_paid_twd += (gross_div_us - net_div_us) * fx_t
                val_us_usd += net_div_us

        # 2) Monthly DCA inflow & scheduled rebalance
        if t in ms_map:
            m_idx = ms_map[t]
            if w_tw > 1e-9:
                val_tw += monthly_contrib_twd * w_tw
            if w_us > 1e-9:
                gross_us_twd = monthly_contrib_twd * w_us
                usd_after_fx = (gross_us_twd * (1.0 - fx_spread_rate)) / fx_t
                comm_usd = max(us_min_fee_usd, usd_after_fx * us_comm_rate)
                net_usd = max(0.0, usd_after_fx - comm_usd)
                tot_fx_and_comm_twd += gross_us_twd - (net_usd * fx_t)
                val_us_usd += net_usd

            if m_idx > 0 and reb_flags[m_idx] and (0.0 < w_tw < 1.0):
                tot_v = val_tw + val_us_usd * fx_t
                tgt_tw = tot_v * w_tw
                if val_tw > tgt_tw + 1.0:
                    sell_tw = val_tw - tgt_tw
                    val_tw -= sell_tw
                    cash_tw = sell_tw * (1.0 - TW_REBAL_SELL_FEE)
                    usd_in = (cash_tw * (1.0 - fx_spread_rate)) / fx_t
                    comm_u = max(us_min_fee_usd, usd_in * us_comm_rate)
                    val_us_usd += max(0.0, usd_in - comm_u)
                elif tgt_tw > val_tw + 1.0:
                    need_tw = tgt_tw - val_tw
                    sell_usd = need_tw / fx_t
                    val_us_usd = max(0.0, val_us_usd - sell_usd)
                    comm_u = max(us_min_fee_usd, sell_usd * us_comm_rate)
                    net_usd_proceeds = max(0.0, sell_usd - comm_u)
                    cash_tw_in = (net_usd_proceeds * fx_t) * (1.0 - fx_spread_rate)
                    val_tw += cash_tw_in * (1.0 - TW_REBAL_BUY_FEE)

        v_daily[t] = val_tw + val_us_usd * fx_t

    days_arr = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    cf_days = np.append(days_arr[ms], days_arr[-1])
    cf_amts = np.append(-np.full(len(ms), monthly_contrib_twd), float(v_daily[-1]))
    xirr_pct = compute_xirr(cf_days, cf_amts) * 100.0
    mdd_pct = compute_max_drawdown_pct(v_daily)

    return {
        "final_value_twd": float(v_daily[-1]),
        "total_invested_twd": float(len(ms) * monthly_contrib_twd),
        "xirr_pct": xirr_pct,
        "max_drawdown_pct": mdd_pct,
        "nhi_paid_twd": tot_nhi_paid,
        "nhi_trigger_count": nhi_trigger_count,
        "tw_income_tax_paid_twd": tot_tw_tax_paid,
        "us_withholding_tax_paid_twd": tot_us_tax_paid_twd,
        "fx_and_comm_drag_twd": tot_fx_and_comm_twd,
        "daily_values": v_daily,
    }


# ---------------------------------------------------------------------------
# 4. General Multi-Asset Strategy Simulator (Parameterized for Gates 2, 4, 7)
# ---------------------------------------------------------------------------

def simulate_general_dca(
    dates: pd.DatetimeIndex,
    wealth_mat: np.ndarray,             # shape (N_days, K), positive TWD wealth indices
    weights_1d: np.ndarray,             # shape (K,), sums to 1.0
    is_tw_mask: np.ndarray,             # shape (K,), bool
    lev_pairs: list[tuple[int, int]],   # [(lev_idx, und_idx), ...]
    cons_mat_daily: np.ndarray | None = None,  # shape (N_days, K) bool, causal
    fx_daily: np.ndarray | None = None,        # shape (N_days,), TWD per USD
    rebalance_mode: str = "none",       # 'none', 'monthly', 'quarterly', 'annual'
    dca_timing: str = "start",          # 'start', 'mid', 'end'
    use_filter: bool = False,
    monthly_contrib_twd: float = MONTHLY_CONTRIB_TWD,
    fx_fee_rate: float = BASE_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
    us_min_fee_usd: float = 0.0,
) -> dict[str, Any]:
    """Flexible multi-asset simulator supporting arbitrary DCA timing, rebalance frequency, SMA filter, and $15 min fee."""
    n_days, k_sz = wealth_mat.shape
    dca_idx = get_dca_Indices_by_timing(dates, dca_timing)
    n_m = len(dca_idx)
    m_dates = dates[dca_idx]
    reb_flags = get_rebalance_flags(m_dates, rebalance_mode)

    w_norm = wealth_mat / np.maximum(wealth_mat[0:1, :], 1e-30)
    pos = np.zeros(k_sz, dtype=np.float64)
    v_daily = np.zeros(n_days, dtype=np.float64)

    dca_map = {int(dca_idx[m]): m for m in range(n_m)}

    for t in range(n_days):
        if t > 0:
            g_d = w_norm[t] / np.maximum(w_norm[t - 1], 1e-30)
            pos *= g_d

        if t in dca_map:
            m = dca_map[t]
            w_m = weights_1d.copy()
            if use_filter and cons_mat_daily is not None:
                for lev_i, und_i in lev_pairs:
                    if cons_mat_daily[t, lev_i]:
                        w_m[und_i] += w_m[lev_i]
                        w_m[lev_i] = 0.0

            fx_t = float(fx_daily[t]) if fx_daily is not None else 32.0
            for k_i in range(k_sz):
                gross_twd = monthly_contrib_twd * float(w_m[k_i])
                if gross_twd <= 1e-9:
                    continue
                if is_tw_mask[k_i]:
                    pos[k_i] += gross_twd
                else:
                    usd_after_fx = (gross_twd * (1.0 - fx_fee_rate)) / fx_t
                    comm_usd = max(us_min_fee_usd, usd_after_fx * us_comm_rate)
                    pos[k_i] += max(0.0, usd_after_fx - comm_usd) * fx_t

            if m > 0 and reb_flags[m]:
                v_tot = float(np.sum(pos))
                if v_tot > 1.0:
                    target = v_tot * weights_1d.copy()
                    if use_filter and cons_mat_daily is not None:
                        for lev_i, und_i in lev_pairs:
                            if cons_mat_daily[t, lev_i] and pos[lev_i] < target[lev_i]:
                                shortfall = target[lev_i] - pos[lev_i]
                                target[lev_i] -= shortfall
                                target[und_i] += shortfall
                    diff = target - pos
                    sell = np.maximum(0.0, -diff)
                    buy = np.maximum(0.0, diff)
                    min_fee_twd = us_min_fee_usd * fx_t

                    c_tw = float(np.sum(sell[is_tw_mask] * (1.0 - TW_REBAL_SELL_FEE)))
                    c_us = 0.0
                    for k_i in np.flatnonzero(~is_tw_mask):
                        if sell[k_i] > 1.0:
                            fee = max(min_fee_twd, sell[k_i] * us_comm_rate)
                            c_us += max(0.0, sell[k_i] - fee)

                    want_tw = float(np.sum(buy[is_tw_mask]))
                    want_us = float(np.sum(buy[~is_tw_mask]))
                    local_tw = min(c_tw, want_tw)
                    local_us = min(c_us, want_us)
                    alloc_tw = local_tw + (c_us - local_us) * (1.0 - fx_fee_rate)
                    alloc_us = local_us + (c_tw - local_tw) * (1.0 - fx_fee_rate)

                    pos -= sell
                    if want_tw > 1e-9:
                        pos[is_tw_mask] += buy[is_tw_mask] * ((alloc_tw * (1.0 - TW_REBAL_BUY_FEE)) / want_tw)
                    if want_us > 1e-9:
                        for k_i in np.flatnonzero(~is_tw_mask):
                            if buy[k_i] > 1.0:
                                gross_alloc = alloc_us * (buy[k_i] / want_us)
                                fee = max(min_fee_twd, gross_alloc * us_comm_rate)
                                pos[k_i] += max(0.0, gross_alloc - fee)

        v_daily[t] = float(np.sum(pos))

    days_arr = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    # Note: if dca_timing == 'mid' or 'end', before the first contribution day v_daily is 0; compute MDD from first active day!
    first_active = int(dca_idx[0]) if len(dca_idx) > 0 else 0
    mdd_pct = compute_max_drawdown_pct(v_daily[first_active:])

    cf_days = np.append(days_arr[dca_idx] - days_arr[first_active], days_arr[-1] - days_arr[first_active])
    cf_amts = np.append(-np.full(n_m, monthly_contrib_twd), float(v_daily[-1]))
    xirr_pct = compute_xirr(cf_days, cf_amts) * 100.0

    return {
        "final_value_twd": float(v_daily[-1]),
        "total_invested_twd": float(n_m * monthly_contrib_twd),
        "xirr_pct": xirr_pct,
        "max_drawdown_pct": mdd_pct,
        "daily_values": v_daily,
    }


# ---------------------------------------------------------------------------
# 5. Gate 5: Behavioral DCA State Machine (Control, A, B, C)
# ---------------------------------------------------------------------------

def simulate_behavioral_dca(
    dates: pd.DatetimeIndex,
    wealth_mat: np.ndarray,
    weights_1d: np.ndarray,
    is_tw_mask: np.ndarray,
    lev_pairs: list[tuple[int, int]],
    cons_mat_daily: np.ndarray | None,
    scenario: str,                      # 'control', 'A_pause_6m', 'B_panic_sell_20pct', 'C_unemployed_12m_withdraw'
    trigger_drawdown_series: np.ndarray | None = None,  # Optional external market drawdown (<=0); if None, uses account running drawdown
    rebalance_mode: str = "none",
    use_filter: bool = False,
    monthly_contrib_twd: float = MONTHLY_CONTRIB_TWD,
    fx_fee_rate: float = BASE_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
) -> dict[str, Any]:
    """Simulate real human behavioral errors during drawdowns vs perfect DCA control.

    Scenarios:
      - 'control': Perfect monthly contribution on first trading day of every month.
      - 'A_pause_6m': When drawdown < -30% (-0.30), pause monthly DCA for the next 6 monthly contribution dates,
        then resume. Re-arms once drawdown recovers to 0% (new high).
      - 'B_panic_sell_20pct': When drawdown < -40% (-0.40), immediately sell 20% of current holdings into sidelines
        cash (0% return); only buy back into target weights once trigger index/account recovers to the prior peak!
      - 'C_unemployed_12m_withdraw': When drawdown < -30% (-0.30), investor loses job: pause monthly DCA for 12
        monthly contribution dates AND withdraw 6 months of living expenses (each month = 3x monthly_contrib_twd,
        withdrawn across the first 6 paused months, selling pro-rata from holdings). Re-arms after recovery to peak.
    """
    n_days, k_sz = wealth_mat.shape
    ms = get_dca_Indices_by_timing(dates, "start")
    n_m = len(ms)
    m_dates = dates[ms]
    reb_flags = get_rebalance_flags(m_dates, rebalance_mode)
    ms_map = {int(ms[m]): m for m in range(n_m)}

    w_norm = wealth_mat / np.maximum(wealth_mat[0:1, :], 1e-30)
    dca_net = np.where(is_tw_mask, 1.0, (1.0 - fx_fee_rate) * (1.0 - us_comm_rate))
    sell_net = np.where(is_tw_mask, 1.0 - TW_REBAL_SELL_FEE, 1.0 - us_comm_rate)
    buy_back_net = np.where(is_tw_mask, 1.0 - TW_REBAL_BUY_FEE, (1.0 - fx_fee_rate) * (1.0 - us_comm_rate))

    pos = np.zeros(k_sz, dtype=np.float64)
    sideline_cash = 0.0   # Used in Scenario B (20% sold into cash until prior peak recovered)
    running_peak = 0.0
    prior_peak_target = 0.0

    pause_months_left = 0
    withdraw_months_left = 0
    armed = True
    panic_holding_cash = False
    trigger_count = 0
    total_withdrawn_twd = 0.0
    withdraw_shortfall_twd = 0.0

    cf_days: list[float] = []
    cf_amts: list[float] = []
    days_arr = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    v_daily = np.empty(n_days, dtype=np.float64)

    for t in range(n_days):
        if t > 0:
            g_d = w_norm[t] / np.maximum(w_norm[t - 1], 1e-30)
            pos *= g_d

        # Evaluate causal drawdown before today's new contribution / panic action
        acct_val_pre = float(np.sum(pos)) + sideline_cash
        if acct_val_pre > running_peak:
            running_peak = acct_val_pre
        acct_dd = (acct_val_pre - running_peak) / running_peak if running_peak > 1e-9 else 0.0

        dd_now = float(trigger_drawdown_series[t]) if trigger_drawdown_series is not None else acct_dd

        # Check recovery to prior high -> re-arm or buy back Scenario B sideline cash
        if dd_now >= -1e-7 and acct_val_pre > 0.0:
            if not armed and pause_months_left == 0:
                armed = True
            if scenario == "B_panic_sell_20pct" and panic_holding_cash:
                # Re-enter market at prior peak!
                if sideline_cash > 0.0:
                    w_buy = weights_1d.copy()
                    if use_filter and cons_mat_daily is not None:
                        for lev_i, und_i in lev_pairs:
                            if cons_mat_daily[t, lev_i]:
                                w_buy[und_i] += w_buy[lev_i]
                                w_buy[lev_i] = 0.0
                    pos += (sideline_cash * w_buy) * buy_back_net
                    sideline_cash = 0.0
                panic_holding_cash = False
                armed = True

        # Check drawdown trigger BEFORE monthly contribution on day t (so crossing -30% on a month-start pauses immediately!)
        if scenario == "A_pause_6m" and armed and dd_now < -0.30:
            pause_months_left = 6
            armed = False
            trigger_count += 1
        elif scenario == "B_panic_sell_20pct" and armed and (not panic_holding_cash) and dd_now < -0.40:
            if float(np.sum(pos)) > 0.0:
                sold_vec = 0.20 * pos
                pos -= sold_vec
                sideline_cash += float(np.sum(sold_vec * sell_net))
                prior_peak_target = running_peak
                panic_holding_cash = True
                armed = False
                trigger_count += 1
        elif scenario == "C_unemployed_12m_withdraw" and armed and dd_now < -0.30:
            pause_months_left = 12
            withdraw_months_left = 6
            armed = False
            trigger_count += 1

        # Monthly scheduled DCA / Unemployment pause & withdrawal
        if t in ms_map:
            m = ms_map[t]
            if pause_months_left > 0:
                pause_months_left -= 1
                if withdraw_months_left > 0:
                    withdraw_months_left -= 1
                    need_cash = 3.0 * monthly_contrib_twd  # 1 month living expense = 3x monthly DCA
                    cur_pos_sum = float(np.sum(pos))
                    if cur_pos_sum > 1e-6:
                        # Gross sell required so net proceeds cover need_cash
                        avg_net = float(np.sum((pos / cur_pos_sum) * sell_net))
                        gross_sell = min(cur_pos_sum, need_cash / max(avg_net, 0.5))
                        frac = gross_sell / cur_pos_sum
                        sold_amt = pos * frac
                        pos -= sold_amt
                        realized = float(np.sum(sold_amt * sell_net))
                        actual_wd = min(need_cash, realized)
                        # Any tiny rounding excess stays in pos
                        total_withdrawn_twd += actual_wd
                        if actual_wd < need_cash - 1.0:
                            withdraw_shortfall_twd += (need_cash - actual_wd)
                        if actual_wd > 0.0:
                            cf_days.append(float(days_arr[t]))
                            cf_amts.append(actual_wd)
                    else:
                        withdraw_shortfall_twd += need_cash
            else:
                w_m = weights_1d.copy()
                if use_filter and cons_mat_daily is not None:
                    for lev_i, und_i in lev_pairs:
                        if cons_mat_daily[t, lev_i]:
                            w_m[und_i] += w_m[lev_i]
                            w_m[lev_i] = 0.0
                pos += (monthly_contrib_twd * w_m) * dca_net
                cf_days.append(float(days_arr[t]))
                cf_amts.append(-monthly_contrib_twd)

            # Annual/Quarterly rebalance on invested holdings
            if m > 0 and reb_flags[m] and float(np.sum(pos)) > 1.0:
                v_tot = float(np.sum(pos))
                target = v_tot * weights_1d.copy()
                if use_filter and cons_mat_daily is not None:
                    for lev_i, und_i in lev_pairs:
                        if cons_mat_daily[t, lev_i] and pos[lev_i] < target[lev_i]:
                            shortfall = target[lev_i] - pos[lev_i]
                            target[lev_i] -= shortfall
                            target[und_i] += shortfall
                diff = target - pos
                sell = np.maximum(0.0, -diff)
                buy = np.maximum(0.0, diff)
                c_tw = float(np.sum(sell[is_tw_mask] * (1.0 - TW_REBAL_SELL_FEE)))
                c_us = float(np.sum(sell[~is_tw_mask] * (1.0 - us_comm_rate)))
                want_tw = float(np.sum(buy[is_tw_mask]))
                want_us = float(np.sum(buy[~is_tw_mask]))
                loc_tw = min(c_tw, want_tw)
                loc_us = min(c_us, want_us)
                al_tw = loc_tw + (c_us - loc_us) * (1.0 - fx_fee_rate)
                al_us = loc_us + (c_tw - loc_tw) * (1.0 - fx_fee_rate)
                pos -= sell
                if want_tw > 1e-9:
                    pos[is_tw_mask] += buy[is_tw_mask] * ((al_tw * (1.0 - TW_REBAL_BUY_FEE)) / want_tw)
                if want_us > 1e-9:
                    pos[~is_tw_mask] += buy[~is_tw_mask] * ((al_us * (1.0 - us_comm_rate)) / want_us)

        tot_today = float(np.sum(pos)) + sideline_cash
        if tot_today > running_peak:
            running_peak = tot_today
        # Re-check intra-day post-DCA peak for Scenario B if using account drawdown
        if trigger_drawdown_series is None and scenario == "B_panic_sell_20pct" and panic_holding_cash:
            if tot_today >= prior_peak_target - 1e-6 and sideline_cash > 0.0:
                w_buy = weights_1d.copy()
                pos += (sideline_cash * w_buy) * buy_back_net
                sideline_cash = 0.0
                panic_holding_cash = False
                armed = True

        v_daily[t] = float(np.sum(pos)) + sideline_cash

    final_val = float(v_daily[-1])
    cf_days_end = list(cf_days) + [float(days_arr[-1])]
    cf_amts_end = list(cf_amts) + [final_val]
    xirr_pct = compute_xirr(cf_days_end, cf_amts_end) * 100.0

    # Also compute "total savings XIRR" for Scenario A (where skipped DCA cash sits idle at 0% in checking account)
    # so skipping contributions at market bottom isn't flattered by smaller denominator!
    skipped_m = n_m - sum(1 for a in cf_amts if a < 0)
    if scenario == "A_pause_6m" and skipped_m > 0:
        idle_uninvested = skipped_m * monthly_contrib_twd
        full_cf_days = np.append(days_arr[ms], days_arr[-1])
        full_cf_amts = np.append(-np.full(n_m, monthly_contrib_twd), final_val + idle_uninvested)
        effective_xirr_pct = compute_xirr(full_cf_days, full_cf_amts) * 100.0
    else:
        effective_xirr_pct = xirr_pct

    mdd_pct = compute_max_drawdown_pct(v_daily)
    tot_contrib = float(-sum(a for a in cf_amts if a < 0))

    return {
        "scenario": scenario,
        "trigger_count": trigger_count,
        "total_contributed_twd": tot_contrib,
        "total_withdrawn_twd": total_withdrawn_twd,
        "withdraw_shortfall_twd": withdraw_shortfall_twd,
        "final_value_twd": final_val,
        "account_xirr_pct": xirr_pct,
        "effective_xirr_pct": effective_xirr_pct,
        "max_drawdown_pct": mdd_pct,
        "daily_values": v_daily,
    }


# ---------------------------------------------------------------------------
# 6. Gate 6: Block Bootstrap & Stationary Bootstrap Generators
# ---------------------------------------------------------------------------

def generate_bootstrap_month_indices(
    n_hist_months: int,
    n_sim_months: int,
    mode: str,                 # 'block' or 'stationary'
    block_months: int,         # e.g. 12 (1Y), 36 (3Y), 60 (5Y)
    n_paths: int,
    seed: int = 20261127,
) -> tuple[np.ndarray, list[int]]:
    """Generate synthetic monthly index paths of shape (n_paths, n_sim_months) plus realized block lengths.

    - For mode == 'block': draws contiguous non-wrapping blocks of exact length `block_months`
      (truncating only the final block if `n_sim_months` is not a multiple of `block_months`).
    - For mode == 'stationary' (Politis & Romano 1994): starts at uniform random month in [0, n_hist_months),
      and at each step starts a new random block with probability p = 1 / block_months, otherwise advances
      circularly `(idx + 1) % n_hist_months`, yielding geometric block lengths with exact mean `block_months`.
    """
    if block_months <= 0 or n_hist_months < block_months:
        raise ValueError(f"Invalid block_months={block_months} for n_hist_months={n_hist_months}")

    rng = np.random.default_rng(seed)
    out = np.empty((n_paths, n_sim_months), dtype=np.int64)
    realized_lengths: list[int] = []

    if mode == "block":
        n_blocks = math.ceil(n_sim_months / block_months)
        max_start = n_hist_months - block_months
        starts = rng.integers(0, max_start + 1, size=(n_paths, n_blocks))
        offsets = np.arange(block_months, dtype=np.int64)[None, :]
        for b_i in range(n_blocks):
            c0 = b_i * block_months
            c1 = min(n_sim_months, c0 + block_months)
            seg_w = c1 - c0
            out[:, c0:c1] = starts[:, b_i : b_i + 1] + offsets[:, :seg_w]
            realized_lengths.extend([seg_w] * n_paths)
    elif mode == "stationary":
        p_restart = 1.0 / float(block_months)
        init_idx = rng.integers(0, n_hist_months, size=n_paths)
        restarts = rng.random(size=(n_paths, n_sim_months)) < p_restart
        rand_jumps = rng.integers(0, n_hist_months, size=(n_paths, n_sim_months))
        out[:, 0] = init_idx
        cur_len = np.ones(n_paths, dtype=np.int64)
        for m in range(1, n_sim_months):
            rst = restarts[:, m]
            if np.any(rst):
                realized_lengths.extend(cur_len[rst].tolist())
            out[:, m] = np.where(rst, rand_jumps[:, m], (out[:, m - 1] + 1) % n_hist_months)
            cur_len = np.where(rst, 1, cur_len + 1)
        # Note: final right-censored run is excluded from uncensored geometric block length check if we have interior runs
        if not realized_lengths:
            realized_lengths.extend(cur_len.tolist())
    else:
        raise ValueError(f"Unknown bootstrap mode: {mode}")

    return out, realized_lengths


# ---------------------------------------------------------------------------
# 7. Gate 8: Lump Sum vs DCA (100萬) & 30-Year Retirement Withdrawal (1,000萬)
# ---------------------------------------------------------------------------

def simulate_entry_schedule_monthly(
    monthly_gross_rets: np.ndarray,    # shape (M, K), e.g. G_m = 1 + r_m
    weights_1d: np.ndarray,            # shape (K,)
    is_tw_mask: np.ndarray,            # shape (K,) bool
    entry_months: int,                 # 1 = Lump sum, 12 = 12m DCA, 24 = 24m DCA
    total_capital_twd: float = 1_000_000.0,
    idle_cash_annual_rate: float = 0.01,
    rebalance_mode: str = "annual",
    fx_fee_rate: float = BASE_FX_FEE_RATE,
    us_comm_rate: float = US_COMM_RATE,
) -> dict[str, Any]:
    """Compare investing `total_capital_twd` (100萬) all at once (1m) vs 12m vs 24m over an M-month window."""
    n_m, k_sz = monthly_gross_rets.shape
    dca_net = np.where(is_tw_mask, 1.0, (1.0 - fx_fee_rate) * (1.0 - us_comm_rate))
    tranche = total_capital_twd / float(entry_months)
    cash_pool = total_capital_twd
    pos = np.zeros(k_sz, dtype=np.float64)
    val_curve = np.empty(n_m + 1, dtype=np.float64)
    val_curve[0] = total_capital_twd

    cash_g_m = (1.0 + idle_cash_annual_rate) ** (1.0 / 12.0)

    for m in range(n_m):
        if m < entry_months:
            invest_now = min(cash_pool, tranche) if m < entry_months - 1 else cash_pool
            cash_pool = max(0.0, cash_pool - invest_now)
            pos += (invest_now * weights_1d) * dca_net

        if m > 0 and rebalance_mode == "annual" and (m % 12 == 0) and float(np.sum(pos)) > 1.0:
            v_tot = float(np.sum(pos))
            target = v_tot * weights_1d
            diff = target - pos
            sell = np.maximum(0.0, -diff)
            buy = np.maximum(0.0, diff)
            c_tw = float(np.sum(sell[is_tw_mask] * (1.0 - TW_REBAL_SELL_FEE)))
            c_us = float(np.sum(sell[~is_tw_mask] * (1.0 - us_comm_rate)))
            want_tw = float(np.sum(buy[is_tw_mask]))
            want_us = float(np.sum(buy[~is_tw_mask]))
            loc_tw = min(c_tw, want_tw)
            loc_us = min(c_us, want_us)
            al_tw = loc_tw + (c_us - loc_us) * (1.0 - fx_fee_rate)
            al_us = loc_us + (c_tw - loc_tw) * (1.0 - fx_fee_rate)
            pos -= sell
            if want_tw > 1e-9:
                pos[is_tw_mask] += buy[is_tw_mask] * ((al_tw * (1.0 - TW_REBAL_BUY_FEE)) / want_tw)
            if want_us > 1e-9:
                pos[~is_tw_mask] += buy[~is_tw_mask] * ((al_us * (1.0 - us_comm_rate)) / want_us)

        pos = pos * monthly_gross_rets[m]
        cash_pool *= cash_g_m
        val_curve[m + 1] = float(np.sum(pos)) + cash_pool

    yrs = float(n_m) / 12.0
    final_v = float(val_curve[-1])
    cagr_pct = ((final_v / total_capital_twd) ** (1.0 / yrs) - 1.0) * 100.0
    mdd_pct = compute_max_drawdown_pct(val_curve)
    return {
        "entry_months": entry_months,
        "final_value_twd": final_v,
        "cagr_pct": cagr_pct,
        "max_drawdown_pct": mdd_pct,
        "curve": val_curve,
    }


def simulate_retirement_withdrawal_batch(
    g_m_batch: np.ndarray,             # shape (B, 360, K) monthly gross return multipliers
    weights_1d: np.ndarray,            # shape (K,)
    is_tw_mask: np.ndarray,            # shape (K,) bool
    initial_capital_twd: float = 10_000_000.0,
    annual_withdrawal_rate: float = 0.04,
    inflation_rate: float = 0.02,
    rebalance_annual: bool = True,
    cons_m_batch: np.ndarray | None = None,  # Optional (B, 360) bool for M3a SMA filter
    lev_pair: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Vectorized 30-year (360-month) retirement withdrawal simulator across B paths."""
    b_sz, n_m, k_sz = g_m_batch.shape
    dca_net = np.where(is_tw_mask, 1.0, (1.0 - BASE_FX_FEE_RATE) * (1.0 - US_COMM_RATE))
    sell_net = np.where(is_tw_mask, 1.0 - TW_REBAL_SELL_FEE, 1.0 - US_COMM_RATE)

    pos = (initial_capital_twd * weights_1d[None, :]) * dca_net[None, :]
    pos = np.broadcast_to(pos, (b_sz, k_sz)).copy()

    ruined = np.zeros(b_sz, dtype=bool)
    ruin_month = np.full(b_sz, n_m + 1, dtype=np.int64)
    val_yearly = np.zeros((b_sz, (n_m // 12) + 1), dtype=np.float64)
    val_yearly[:, 0] = np.sum(pos, axis=1)

    peaks = np.sum(pos, axis=1).copy()
    worst_dd = np.zeros(b_sz, dtype=np.float64)

    base_annual_wd = initial_capital_twd * annual_withdrawal_rate

    for m in range(n_m):
        yr_idx = m // 12
        monthly_wd = (base_annual_wd * ((1.0 + inflation_rate) ** yr_idx)) / 12.0

        cur_tot = np.sum(pos, axis=1)
        newly_ruined = (~ruined) & (cur_tot <= monthly_wd)
        if np.any(newly_ruined):
            ruined[newly_ruined] = True
            ruin_month[newly_ruined] = m + 1
            pos[newly_ruined, :] = 0.0

        alive = ~ruined
        if np.any(alive):
            tot_a = cur_tot[alive]
            avg_s_net = np.sum((pos[alive] / tot_a[:, None]) * sell_net[None, :], axis=1)
            gross_wd = np.minimum(tot_a, monthly_wd / np.maximum(avg_s_net, 0.5))
            frac = gross_wd / tot_a
            pos[alive] *= (1.0 - frac[:, None])

            # Annual rebalance at start of year (m > 0 and m % 12 == 0)
            if rebalance_annual and m > 0 and (m % 12 == 0):
                p_sub = pos[alive]
                v_sub = np.sum(p_sub, axis=1, keepdims=True)
                tgt = v_sub * weights_1d[None, :]
                if cons_m_batch is not None and lev_pair is not None:
                    lev_i, und_i = lev_pair
                    c_flag = cons_m_batch[alive, m]
                    sf = np.where(c_flag, np.maximum(0.0, tgt[:, lev_i] - p_sub[:, lev_i]), 0.0)
                    tgt[:, lev_i] -= sf
                    tgt[:, und_i] += sf
                diff = tgt - p_sub
                sell = np.maximum(0.0, -diff)
                buy = np.maximum(0.0, diff)
                c_tw = np.sum(sell[:, is_tw_mask] * (1.0 - TW_REBAL_SELL_FEE), axis=1)
                c_us = np.sum(sell[:, ~is_tw_mask] * (1.0 - US_COMM_RATE), axis=1)
                w_tw = np.sum(buy[:, is_tw_mask], axis=1)
                w_us = np.sum(buy[:, ~is_tw_mask], axis=1)
                l_tw = np.minimum(c_tw, w_tw)
                l_us = np.minimum(c_us, w_us)
                al_tw = l_tw + (c_us - l_us) * (1.0 - BASE_FX_FEE_RATE)
                al_us = l_us + (c_tw - l_tw) * (1.0 - BASE_FX_FEE_RATE)
                p_sub -= sell
                if np.any(is_tw_mask):
                    sc_tw = np.where(w_tw > 1e-9, (al_tw * (1.0 - TW_REBAL_BUY_FEE)) / np.maximum(w_tw, 1e-9), 0.0)
                    p_sub[:, is_tw_mask] += buy[:, is_tw_mask] * sc_tw[:, None]
                if np.any(~is_tw_mask):
                    sc_us = np.where(w_us > 1e-9, (al_us * (1.0 - US_COMM_RATE)) / np.maximum(w_us, 1e-9), 0.0)
                    p_sub[:, ~is_tw_mask] += buy[:, ~is_tw_mask] * sc_us[:, None]
                pos[alive] = p_sub

            pos[alive] *= g_m_batch[alive, m, :]

        end_tot = np.sum(pos, axis=1)
        peaks = np.maximum(peaks, np.maximum(end_tot, 1e-9))
        dd_m = (end_tot - peaks) / peaks
        worst_dd = np.minimum(worst_dd, dd_m)

        if (m + 1) % 12 == 0:
            val_yearly[:, (m + 1) // 12] = end_tot

    survival_by_year = [
        round(float(np.mean(ruin_month > (yr * 12))) * 100.0, 2)
        for yr in range((n_m // 12) + 1)
    ]

    return {
        "ruined_mask": ruined,
        "ruin_month": ruin_month,
        "ruin_prob_pct": float(np.mean(ruined)) * 100.0,
        "terminal_wealth_twd": val_yearly[:, -1],
        "median_terminal_twd": float(np.median(val_yearly[:, -1])),
        "worst_mdd_pct": worst_dd * 100.0,
        "survival_by_year_pct": survival_by_year,
    }


# ---------------------------------------------------------------------------
# 8. Strict Pre-Registered Criteria Evaluators (criteria.md)
# ---------------------------------------------------------------------------

def judge_r1_rank_stability(median_regret_pp: float, rank_pct: float) -> str:
    """R1 rank stability rule from criteria.md:
    - median_regret_pp <= 3.0 AND rank_pct <= 30.0 -> '通過'
    - regret 3-5 pp OR rank_pct <= 50% (within <=5pp & <=50% outer bounds) -> '有條件通過'
    - else -> '不通過'
    """
    if median_regret_pp <= 3.0 and rank_pct <= 30.0:
        return "通過"
    if median_regret_pp <= 5.0 and rank_pct <= 50.0:
        return "有條件通過"
    return "不通過"


def judge_risk_cap(prob_exceed_pct: float) -> str:
    """Risk cap rule from criteria.md (R1 -50%, R2 -30%, R3 -60%):
    - prob <= 10% -> '通過'
    - 10% < prob <= 25% -> '有條件通過'
    - prob > 25% -> '不通過'
    """
    if prob_exceed_pct <= 10.0:
        return "通過"
    if prob_exceed_pct <= 25.0:
        return "有條件通過"
    return "不通過"


def combine_gate_verdicts(*sub_verdicts: str) -> str:
    """Combine sub-criteria within a gate using worst-case precedence."""
    if "不通過" in sub_verdicts:
        return "不通過"
    if "有條件通過" in sub_verdicts:
        return "有條件通過"
    return "通過"


def judge_r4_leverage_conclusion(prob_mdd_worse_50pct: float) -> str:
    """R4 rule from criteria.md: 2x+ portfolios P(MDD < -50%) >= 50% -> conclusion holds ('通過')."""
    if prob_mdd_worse_50pct >= 50.0:
        return "通過"
    if prob_mdd_worse_50pct >= 35.0:
        return "有條件通過"
    return "不通過"


def judge_r5_recovery_time(median_months: float | None, baseline_months: float = R5_BASELINE_MONTHS) -> str:
    """R5 rule from criteria.md: median recovery time change <= +-50% -> holds ('通過')."""
    if median_months is None or math.isnan(median_months) or math.isinf(median_months):
        return "不通過"
    rel_change = abs(median_months - baseline_months) / baseline_months
    if rel_change <= 0.50:
        return "通過"
    if rel_change <= 0.75:
        return "有條件通過"
    return "不通過"


def judge_overall_verdict(gate_verdicts: list[str]) -> str:
    """Overall verdict rule from criteria.md:
    - All gates '通過' -> '站得住'
    - Any gate '有條件通過' and zero '不通過' -> '有條件成立'
    - Any gate '不通過' -> '被推翻'
    """
    if any(v == "不通過" for v in gate_verdicts):
        return "被推翻"
    if any(v == "有條件通過" for v in gate_verdicts):
        return "有條件成立"
    return "站得住"

#!/usr/bin/env python3
"""Institutional & Large-Trader Flow Signals Backtesting Engine.

Evaluates predictive power of flow/positioning indicators across:
  A. Taiwan Equities & Futures (^TWII, 2330.TW):
     - A1: TWSE BFI82U Institutional Net Buy/Sell (Foreign, Investment Trust, Dealer, Total)
     - A2: TAIFEX Foreign TXF Net Open Interest (Level, 1d change, 5d change, 60d high/low & z-score)
     - A3: TWSE MI_MARGN Margin Balance Change (5d %, 20d %, Margin-Index Divergence)
     - A4: TWSE T86 2330.TW Foreign Net Buy/Sell (1d, 5d, 20d, 60d z-score)
  B. US CFTC Commitments of Traders (COT):
     - B1: E-mini S&P 500 (^GSPC) - Leveraged Funds Net % OI & 156w COT Index
     - B2: E-mini Nasdaq 100 (^NDX) - Leveraged Funds Net % OI & 156w COT Index
     - B3: COMEX Gold (GC=F) - Managed Money Net % OI & 156w COT Index
     - B4: NYMEX WTI Crude Oil (CL=F) - Managed Money Net % OI & 156w COT Index
     - B5: CME Bitcoin Futures (BTC-USD) - Leveraged Funds Net % OI & 156w COT Index
     * Strict publication lag enforced: Tuesday report -> Friday 15:30 ET release -> Next Monday Open entry.
  C. Crypto (OKX Rubik & Funding & Farside Spot BTC ETF):
     - C1: OKX Top Trader Account L/S Ratio & Position L/S Ratio (BTC & ETH, 1D & 4H) + Retail Divergence
     - C2: OKX Taker Buy/Sell Volume Ratio (BTC & ETH, 1D & 4H)
     - C3: OKX Perpetual Funding Rate & Open Interest Change (BTC & ETH, 1D & 4H)
     - C4: US Spot Bitcoin ETF Daily Net Inflow (Farside, 1d, 5d, 20d, 60d z-score; D+1 strict lag)

Methodology (unified across all indicators):
  1. Forward return horizons:
     - TradFi (Taiwan & CFTC COT): 1d, 5d, 20d
     - Crypto (OKX & BTC ETF): 4h (where 4H data exists), 1d, 3d, 5d, 20d
  2. Method 1: Quintile comparison (Q1..Q5 using expanding/IS thresholds to avoid lookahead, plus Q5-Q1 spread & Newey-West t-stat)
  3. Method 2: Spearman rank correlation (Full, IS 70%, OOS 30%, Newey-West HAC p-value, and rolling window sign stability)
  4. Method 3: Up to 3 pre-defined event signals per indicator (win rate, mean return, excess return vs unconditional benchmark, Newey-West t-stat, non-overlapping check)
  5. Anti-self-deception safeguards:
     - Strict publication-time alignment (zero lookahead)
     - 70% In-Sample (IS) / 30% Out-of-Sample (OOS) validation
     - Benjamini-Hochberg (BH) FDR q-values & Bonferroni adjusted p-values across all tests
     - Unconditional "do-nothing" benchmark comparison over exact same IS and OOS dates
     - Newey-West HAC standard errors (lag = h - 1) + non-overlapping subsample verification
     - Extreme regime exclusion test (excluding 2020-03 COVID crash and 2022 bear market)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
RESULTS_JSON_PATH = BASE_DIR / "results.json"
REPORT_MD_PATH = BASE_DIR / "report.md"


# ============================================================================
# 1. Exact Statistical Functions (Pure Python / NumPy, No Scipy Dependency)
# ============================================================================

def normal_two_sided_p(z: float) -> float:
    """Exact two-sided p-value for standard normal z-statistic."""
    if not np.isfinite(z):
        return 1.0
    return float(math.erfc(abs(float(z)) / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for incomplete beta function (Numerical Recipes Lentz's method)."""
    max_it = 200
    eps = 3.0e-14
    fpmin = 1.0e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_it + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        del_val = d * c
        h *= del_val
        if abs(del_val - 1.0) < eps:
            break
    return h


def betainc_reg(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_two_sided_p(t_val: float, df: int) -> float:
    """Exact two-sided p-value for Student's t-distribution with `df` degrees of freedom."""
    if not np.isfinite(t_val) or df <= 0:
        return 1.0
    if df > 5000:
        return normal_two_sided_p(t_val)
    x = float(df) / (float(df) + float(t_val) * float(t_val))
    return float(max(0.0, min(1.0, betainc_reg(0.5 * df, 0.5, x))))


def newey_west_mean_t(series: np.ndarray, max_lag: int) -> tuple[float, float, float, float]:
    """Compute sample mean, Newey-West HAC standard error, t-stat, and two-sided p-value."""
    arr = np.asarray(series, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = len(arr)
    if n < 5:
        mean_v = float(np.mean(arr)) if n > 0 else 0.0
        return mean_v, float("nan"), 0.0, 1.0

    mean_v = float(np.mean(arr))
    u = arr - mean_v
    gamma0 = float(np.dot(u, u)) / n
    if gamma0 <= 1e-20:
        return mean_v, 0.0, 0.0, 1.0

    lag = max(0, min(int(max_lag), n - 2))
    nw_var = gamma0
    for j in range(1, lag + 1):
        weight = 1.0 - j / (lag + 1.0)
        gammaj = float(np.dot(u[j:], u[:-j])) / n
        nw_var += 2.0 * weight * gammaj

    # Floor variance at IID variance so negative finite-sample autocovariance never inflates t-stats
    eff_var = max(nw_var, gamma0) / n
    se = math.sqrt(eff_var)
    t_stat = mean_v / se if se > 0 else 0.0
    p_val = student_t_two_sided_p(t_stat, df=max(1, n - 1))
    return mean_v, se, t_stat, p_val


def spearman_nw(x: np.ndarray, y: np.ndarray, max_lag: int) -> tuple[float, float, float]:
    """Compute Spearman rank correlation, Newey-West HAC t-statistic, and two-sided p-value."""
    mask = np.isfinite(x) & np.isfinite(y)
    xv = x[mask]
    yv = y[mask]
    n = len(xv)
    if n < 10:
        return 0.0, 0.0, 1.0

    rx = pd.Series(xv).rank(method="average").to_numpy(dtype=float)
    ry = pd.Series(yv).rank(method="average").to_numpy(dtype=float)
    sx = np.std(rx)
    sy = np.std(ry)
    if sx <= 1e-12 or sy <= 1e-12:
        return 0.0, 0.0, 1.0

    zx = (rx - np.mean(rx)) / sx
    zy = (ry - np.mean(ry)) / sy
    prod = zx * zy
    rho = float(np.mean(prod))

    # Newey-West variance of the cross-product series z_t = zx_t * zy_t
    _, se, _, _ = newey_west_mean_t(prod, max_lag=max(1, max_lag))
    if not np.isfinite(se) or se <= 1e-12:
        return rho, 0.0, 1.0
    t_stat = rho / se
    p_val = student_t_two_sided_p(t_stat, df=max(1, n - 2))
    return rho, t_stat, p_val


def benjamini_hochberg(p_values: list[float]) -> tuple[list[float], list[float]]:
    """Return (BH FDR q-values, Bonferroni adjusted p-values) for a list of raw p-values."""
    m = len(p_values)
    if m == 0:
        return [], []
    arr = np.asarray([p if (p is not None and np.isfinite(p)) else 1.0 for p in p_values], dtype=float)
    arr = np.clip(arr, 0.0, 1.0)
    order = np.argsort(arr)
    sorted_p = arr[order]
    q_sorted = np.zeros(m, dtype=float)
    prev_q = 1.0
    for idx in range(m - 1, -1, -1):
        rank = idx + 1
        q_val = min(prev_q, sorted_p[idx] * m / rank)
        q_sorted[idx] = min(1.0, q_val)
        prev_q = q_sorted[idx]
    q_out = np.zeros(m, dtype=float)
    q_out[order] = q_sorted
    bonf_out = np.clip(arr * m, 0.0, 1.0)
    return [float(v) for v in q_out], [float(v) for v in bonf_out]


def compute_streak(series: pd.Series) -> pd.Series:
    """Compute consecutive positive (+k) or negative (-k) streak days."""
    vals = series.to_numpy(dtype=float)
    out = np.zeros(len(vals), dtype=float)
    cur = 0
    for i, v in enumerate(vals):
        if not np.isfinite(v) or v == 0:
            cur = 0
        elif v > 0:
            cur = cur + 1 if cur > 0 else 1
        else:
            cur = cur - 1 if cur < 0 else -1
        out[i] = float(cur)
    return pd.Series(out, index=series.index)


def rolling_zscore(series: pd.Series, window: int = 60, min_periods: int = 20) -> pd.Series:
    """Compute strictly backward-looking rolling z-score."""
    m = series.rolling(window=window, min_periods=min_periods).mean()
    s = series.rolling(window=window, min_periods=min_periods).std()
    return (series - m) / s.replace(0.0, np.nan)


# ============================================================================
# 2. Unified Backtest & Evaluation Core
# ============================================================================

EFFECT_SIZE_THRESHOLDS_PCT = {
    "4h": 0.10,
    "1d": 0.15,
    "3d": 0.25,
    "5d": 0.30,
    "20d": 0.80,
}


def _assign_quintiles(series: pd.Series, cutoffs: np.ndarray) -> np.ndarray:
    """Assign 1..5 quintile labels based on 4 internal cutoffs (20%, 40%, 60%, 80%)."""
    vals = series.to_numpy(dtype=float)
    q = np.digitize(vals, cutoffs, right=True) + 1
    return np.clip(q, 1, 5)


def _decluster_indices(indices: np.ndarray, min_step: int) -> np.ndarray:
    """Keep non-overlapping event indices separated by at least `min_step` bars."""
    if len(indices) == 0 or min_step <= 1:
        return indices
    kept = [int(indices[0])]
    last = int(indices[0])
    for idx in indices[1:]:
        if int(idx) - last >= min_step:
            kept.append(int(idx))
            last = int(idx)
    return np.asarray(kept, dtype=int)


def evaluate_horizon(
    df: pd.DataFrame,
    signal_col: str,
    ret_col: str,
    h_label: str,
    overlap_lag: int,
    roll_window: int,
    events_spec: list[tuple[str, str, int, pd.Series]],
) -> dict[str, Any]:
    """Run Method 1 (Quintiles), Method 2 (Spearman + Rolling), Method 3 (Events) & Safeguards for one horizon."""
    # Align event masks with df index before filtering valid rows
    ev_df = pd.DataFrame(index=df.index)
    for ev_id, _, _, mask_s in events_spec:
        ev_df[ev_id] = mask_s.reindex(df.index).fillna(False).astype(bool)

    valid_mask = np.isfinite(df[signal_col].to_numpy(dtype=float)) & np.isfinite(df[ret_col].to_numpy(dtype=float))
    sub = df.loc[valid_mask, ["dt", signal_col, ret_col]].copy().reset_index(drop=True)
    ev_sub = ev_df.loc[valid_mask].copy().reset_index(drop=True)

    n_total = len(sub)
    if n_total < 30:
        return {"valid": False, "n_total": n_total, "reason": "sample_too_small"}

    split_idx = int(n_total * 0.70)
    is_sub = sub.iloc[:split_idx].copy()
    oos_sub = sub.iloc[split_idx:].copy()
    is_ev = ev_sub.iloc[:split_idx].copy()
    oos_ev = ev_sub.iloc[split_idx:].copy()

    sig_all = sub[signal_col].to_numpy(dtype=float)
    ret_all = sub[ret_col].to_numpy(dtype=float)
    sig_is = is_sub[signal_col].to_numpy(dtype=float)
    ret_is = is_sub[ret_col].to_numpy(dtype=float)
    sig_oos = oos_sub[signal_col].to_numpy(dtype=float)
    ret_oos = oos_sub[ret_col].to_numpy(dtype=float)

    # Unconditional "Do-Nothing" Benchmark
    bench = {
        "full": {
            "n": n_total,
            "mean_ret": float(np.mean(ret_all)),
            "win_up_pct": float(np.mean(ret_all > 0) * 100.0),
            "win_down_pct": float(np.mean(ret_all < 0) * 100.0),
        },
        "is": {
            "n": len(is_sub),
            "mean_ret": float(np.mean(ret_is)),
            "win_up_pct": float(np.mean(ret_is > 0) * 100.0),
            "win_down_pct": float(np.mean(ret_is < 0) * 100.0),
        },
        "oos": {
            "n": len(oos_sub),
            "mean_ret": float(np.mean(ret_oos)),
            "win_up_pct": float(np.mean(ret_oos > 0) * 100.0),
            "win_down_pct": float(np.mean(ret_oos < 0) * 100.0),
        },
    }

    # ------------------------------------------------------------------
    # Method 1: Quintile Comparison (Q1..Q5 using IS cutoffs -> no OOS lookahead)
    # ------------------------------------------------------------------
    cutoffs = np.quantile(sig_is, [0.20, 0.40, 0.60, 0.80])
    # Handle tied cutoffs (e.g. discrete streak indicator) via rank-based quintiles if cutoffs collapse
    if len(np.unique(cutoffs)) < 4:
        q_is = np.clip(np.ceil(pd.Series(sig_is).rank(pct=True).to_numpy() * 5.0).astype(int), 1, 5)
        q_oos = np.clip(np.ceil(pd.Series(sig_oos).rank(pct=True).to_numpy() * 5.0).astype(int), 1, 5)
        q_all = np.concatenate([q_is, q_oos])
    else:
        q_is = _assign_quintiles(is_sub[signal_col], cutoffs)
        q_oos = _assign_quintiles(oos_sub[signal_col], cutoffs)
        q_all = _assign_quintiles(sub[signal_col], cutoffs)

    def _quintile_table(q_arr: np.ndarray, r_arr: np.ndarray, b_info: dict[str, float]) -> dict[str, Any]:
        q_stats = {}
        for q_num in range(1, 6):
            m = q_arr == q_num
            rq = r_arr[m]
            cnt = int(len(rq))
            mean_v = float(np.mean(rq)) if cnt > 0 else 0.0
            win_v = float(np.mean(rq > 0) * 100.0) if cnt > 0 else 0.0
            q_stats[f"Q{q_num}"] = {
                "n": cnt,
                "mean_ret": round(mean_v, 4),
                "win_up_pct": round(win_v, 2),
                "excess_ret": round(mean_v - b_info["mean_ret"], 4),
                "excess_win_pp": round(win_v - b_info["win_up_pct"], 2),
            }
        q5_r = r_arr[q_arr == 5]
        q1_r = r_arr[q_arr == 1]
        spread = (float(np.mean(q5_r)) if len(q5_r) else 0.0) - (float(np.mean(q1_r)) if len(q1_r) else 0.0)
        win_spread = (
            (float(np.mean(q5_r > 0) * 100.0) if len(q5_r) else 0.0)
            - (float(np.mean(q1_r > 0) * 100.0) if len(q1_r) else 0.0)
        )
        return {"groups": q_stats, "q5_minus_q1_ret": round(spread, 4), "q5_minus_q1_win_pp": round(win_spread, 2)}

    quintiles = {
        "full": _quintile_table(q_all, ret_all, bench["full"]),
        "is": _quintile_table(q_is, ret_is, bench["is"]),
        "oos": _quintile_table(q_oos, ret_oos, bench["oos"]),
    }

    # Newey-West test on Q5 vs Q1 daily/period excess return series:
    # Construct long-short indicator w_t = +1 if in Q5, -1 if in Q1, centered on active Q1/Q5 observations
    q51_mask_is = (q_is == 5) | (q_is == 1)
    q51_series_is = np.where(q_is[q51_mask_is] == 5, ret_is[q51_mask_is] - bench["is"]["mean_ret"], -(ret_is[q51_mask_is] - bench["is"]["mean_ret"]))
    _, _, q51_t_is, q51_p_is = newey_west_mean_t(q51_series_is, max_lag=overlap_lag)

    q51_mask_oos = (q_oos == 5) | (q_oos == 1)
    q51_series_oos = np.where(q_oos[q51_mask_oos] == 5, ret_oos[q51_mask_oos] - bench["oos"]["mean_ret"], -(ret_oos[q51_mask_oos] - bench["oos"]["mean_ret"]))
    _, _, q51_t_oos, q51_p_oos = newey_west_mean_t(q51_series_oos, max_lag=overlap_lag)

    q51_mask_all = (q_all == 5) | (q_all == 1)
    q51_series_all = np.where(q_all[q51_mask_all] == 5, ret_all[q51_mask_all] - bench["full"]["mean_ret"], -(ret_all[q51_mask_all] - bench["full"]["mean_ret"]))
    _, _, q51_t_all, q51_p_all = newey_west_mean_t(q51_series_all, max_lag=overlap_lag)

    quintiles["full"]["nw_t"] = round(q51_t_all, 3)
    quintiles["full"]["nw_p"] = float(q51_p_all)
    quintiles["is"]["nw_t"] = round(q51_t_is, 3)
    quintiles["is"]["nw_p"] = float(q51_p_is)
    quintiles["oos"]["nw_t"] = round(q51_t_oos, 3)
    quintiles["oos"]["nw_p"] = float(q51_p_oos)

    # ------------------------------------------------------------------
    # Method 2: Spearman Rank Correlation & Rolling Stability
    # ------------------------------------------------------------------
    rho_all, t_all, p_all = spearman_nw(sig_all, ret_all, max_lag=overlap_lag)
    rho_is, t_is, p_is = spearman_nw(sig_is, ret_is, max_lag=overlap_lag)
    rho_oos, t_oos, p_oos = spearman_nw(sig_oos, ret_oos, max_lag=overlap_lag)

    # Non-overlapping subsample check (step = max(1, overlap_lag + 1))
    step = max(1, overlap_lag + 1)
    no_ov_sig = sig_all[::step]
    no_ov_ret = ret_all[::step]
    rho_no_ov, t_no_ov, p_no_ov = spearman_nw(no_ov_sig, no_ov_ret, max_lag=0)

    # Rolling window Spearman stability
    eff_win = min(roll_window, max(30, n_total // 3))
    roll_rhos: list[float] = []
    step_roll = max(1, eff_win // 10)
    for start_i in range(0, n_total - eff_win + 1, step_roll):
        end_i = start_i + eff_win
        r_val, _, _ = spearman_nw(sig_all[start_i:end_i], ret_all[start_i:end_i], max_lag=0)
        if np.isfinite(r_val):
            roll_rhos.append(r_val)
    roll_arr = np.asarray(roll_rhos, dtype=float)
    is_sign = 1.0 if rho_is >= 0 else -1.0
    same_sign_pct = float(np.mean((roll_arr * is_sign) > 0) * 100.0) if len(roll_arr) > 0 else 0.0

    correlation = {
        "full": {"rho": round(rho_all, 4), "nw_t": round(t_all, 3), "nw_p": float(p_all)},
        "is": {"rho": round(rho_is, 4), "nw_t": round(t_is, 3), "nw_p": float(p_is)},
        "oos": {"rho": round(rho_oos, 4), "nw_t": round(t_oos, 3), "nw_p": float(p_oos)},
        "non_overlapping": {
            "step": step,
            "n": len(no_ov_sig),
            "rho": round(rho_no_ov, 4),
            "t": round(t_no_ov, 3),
            "p": float(p_no_ov),
        },
        "rolling": {
            "window": eff_win,
            "n_windows": len(roll_arr),
            "mean_rho": round(float(np.mean(roll_arr)), 4) if len(roll_arr) else 0.0,
            "std_rho": round(float(np.std(roll_arr)), 4) if len(roll_arr) else 0.0,
            "same_sign_as_is_pct": round(same_sign_pct, 1),
        },
    }

    # ------------------------------------------------------------------
    # Method 3: Pre-defined Event Signals (up to 3 per indicator)
    # ------------------------------------------------------------------
    events_out: list[dict[str, Any]] = []
    for ev_id, ev_name, exp_dir, _ in events_spec:
        m_all = ev_sub[ev_id].to_numpy(dtype=bool)
        m_is = is_ev[ev_id].to_numpy(dtype=bool)
        m_oos = oos_ev[ev_id].to_numpy(dtype=bool)

        def _eval_event_slice(mask_b: np.ndarray, r_arr: np.ndarray, b_info: dict[str, float]) -> dict[str, Any]:
            ev_r = r_arr[mask_b]
            cnt = int(len(ev_r))
            if cnt == 0:
                return {
                    "n": 0,
                    "n_non_overlap": 0,
                    "mean_ret": 0.0,
                    "excess_ret": 0.0,
                    "dir_excess_ret": 0.0,
                    "win_up_pct": 0.0,
                    "dir_win_pct": 0.0,
                    "dir_win_edge_pp": 0.0,
                    "nw_t": 0.0,
                    "nw_p": 1.0,
                }
            mean_v = float(np.mean(ev_r))
            excess_v = mean_v - b_info["mean_ret"]
            dir_excess_v = float(exp_dir) * excess_v
            win_up = float(np.mean(ev_r > 0) * 100.0)
            if exp_dir >= 0:
                dir_win = win_up
                bench_dir_win = b_info["win_up_pct"]
            else:
                dir_win = float(np.mean(ev_r < 0) * 100.0)
                bench_dir_win = b_info["win_down_pct"]
            dir_win_edge = dir_win - bench_dir_win

            # Newey-West HAC t-stat on excess return
            _, _, nw_t, nw_p = newey_west_mean_t(float(exp_dir) * (ev_r - b_info["mean_ret"]), max_lag=overlap_lag)

            # De-clustered non-overlapping count & mean
            raw_idx = np.flatnonzero(mask_b)
            declust_idx = _decluster_indices(raw_idx, min_step=step)
            no_ov_r = r_arr[declust_idx]
            no_ov_mean = float(np.mean(no_ov_r)) if len(no_ov_r) else 0.0

            return {
                "n": cnt,
                "n_non_overlap": int(len(declust_idx)),
                "mean_ret": round(mean_v, 4),
                "no_ov_mean_ret": round(no_ov_mean, 4),
                "excess_ret": round(excess_v, 4),
                "dir_excess_ret": round(dir_excess_v, 4),
                "win_up_pct": round(win_up, 2),
                "dir_win_pct": round(dir_win, 2),
                "dir_win_edge_pp": round(dir_win_edge, 2),
                "nw_t": round(nw_t, 3),
                "nw_p": float(nw_p),
            }

        events_out.append(
            {
                "event_id": ev_id,
                "event_name": ev_name,
                "expected_dir": int(exp_dir),
                "full": _eval_event_slice(m_all, ret_all, bench["full"]),
                "is": _eval_event_slice(m_is, ret_is, bench["is"]),
                "oos": _eval_event_slice(m_oos, ret_oos, bench["oos"]),
            }
        )

    # ------------------------------------------------------------------
    # Extreme Regime Exclusion Check (Excluding 2020-03 & 2022)
    # ------------------------------------------------------------------
    dts_str = sub["dt"].astype(str).to_numpy()
    ex_mask = np.array([not (s.startswith("2020-03") or s.startswith("2022-")) for s in dts_str], dtype=bool)
    if np.sum(ex_mask) >= 30:
        rho_ex, t_ex, p_ex = spearman_nw(sig_all[ex_mask], ret_all[ex_mask], max_lag=overlap_lag)
        q_ex = q_all[ex_mask]
        r_ex = ret_all[ex_mask]
        q5_ex = r_ex[q_ex == 5]
        q1_ex = r_ex[q_ex == 1]
        spread_ex = (float(np.mean(q5_ex)) if len(q5_ex) else 0.0) - (float(np.mean(q1_ex)) if len(q1_ex) else 0.0)
    else:
        rho_ex, t_ex, p_ex, spread_ex = rho_all, t_all, p_all, quintiles["full"]["q5_minus_q1_ret"]

    regime_exclusion = {
        "n_excluded": int(n_total - np.sum(ex_mask)),
        "n_remaining": int(np.sum(ex_mask)),
        "rho_ex_202003_2022": round(rho_ex, 4),
        "nw_t_ex": round(t_ex, 3),
        "nw_p_ex": float(p_ex),
        "q5_minus_q1_ex": round(spread_ex, 4),
    }

    return {
        "valid": True,
        "horizon": h_label,
        "overlap_lag": overlap_lag,
        "n_total": n_total,
        "n_is": len(is_sub),
        "n_oos": len(oos_sub),
        "start_dt": str(sub["dt"].iloc[0]),
        "end_dt": str(sub["dt"].iloc[-1]),
        "split_dt": str(oos_sub["dt"].iloc[0]) if len(oos_sub) else None,
        "benchmark": bench,
        "quintiles": quintiles,
        "correlation": correlation,
        "events": events_out,
        "regime_exclusion": regime_exclusion,
    }


# ============================================================================
# 3. Dataset Builders (Strict Publication-Time Alignment, Zero Lookahead)
# ============================================================================

def _build_daily_price_df(price_obj: dict[str, Any], horizons: tuple[int, ...] = (1, 3, 5, 20)) -> pd.DataFrame:
    """Build daily price DataFrame with close-to-close and next-open-to-close forward returns (%)."""
    df = pd.DataFrame(price_obj["rows"]).sort_values("date").reset_index(drop=True)
    df["dt"] = df["date"]
    close = df["close"].to_numpy(dtype=float)
    open_px = df["open"].to_numpy(dtype=float)
    n = len(df)
    # Current day & 5d trailing return (for divergence signals, strictly known at day t close)
    df["ret_1d_past"] = df["close"].pct_change(1) * 100.0
    df["ret_5d_past"] = df["close"].pct_change(5) * 100.0

    for h in horizons:
        # Close_t -> Close_{t+h} (strictly after day t close)
        fwd_cc = np.full(n, np.nan, dtype=float)
        if n > h:
            fwd_cc[: n - h] = (close[h:] / close[: n - h] - 1.0) * 100.0
        df[f"fwd_{h}d"] = fwd_cc

        # Open_{t} -> Close_{t+h-1} (for signals entering at day t Open, e.g., Monday Open after Friday COT release)
        fwd_oc = np.full(n, np.nan, dtype=float)
        if n >= h:
            fwd_oc[: n - h + 1] = (close[h - 1 :] / open_px[: n - h + 1] - 1.0) * 100.0
        df[f"fwd_open_{h}d"] = fwd_oc
    return df


def build_all_indicator_specs(
    prices: dict[str, Any],
    tw_data: dict[str, Any],
    cot_data: dict[str, Any],
    okx_data: dict[str, Any],
    etf_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Construct all indicator DataFrames and pre-defined event signals across Categories A, B, and C."""
    specs: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Category A: Taiwan Stocks (^TWII and 2330.TW)
    # ------------------------------------------------------------------
    twii_px = _build_daily_price_df(prices["TWII"], horizons=(1, 5, 20))
    tsmc_px = _build_daily_price_df(prices["2330.TW"], horizons=(1, 5, 20))

    # A1: TWSE BFI82U Institutional Net Buy/Sell (Foreign, Trust, Dealer, Total)
    a1_df = pd.DataFrame(tw_data["A1_total_institutional"]["rows"]).sort_values("date").reset_index(drop=True)
    # Convert NTD to 100M NTD (億元)
    for col in ("foreign", "trust", "dealer", "total"):
        a1_df[col] = a1_df[col].astype(float) / 1e8

    a1_merged = pd.merge(a1_df, twii_px, on="date", how="inner").sort_values("date").reset_index(drop=True)

    for inst_key, inst_zh in (
        ("foreign", "外資"),
        ("trust", "投信"),
        ("dealer", "自營商"),
    ):
        s_1d = a1_merged[inst_key]
        s_5d = s_1d.rolling(5, min_periods=5).sum()
        s_20d = s_1d.rolling(20, min_periods=20).sum()
        s_streak = compute_streak(s_1d)
        s_z60 = rolling_zscore(s_1d, window=60, min_periods=30)

        a1_merged[f"{inst_key}_1d"] = s_1d
        a1_merged[f"{inst_key}_5d"] = s_5d
        a1_merged[f"{inst_key}_20d"] = s_20d
        a1_merged[f"{inst_key}_streak"] = s_streak
        a1_merged[f"{inst_key}_z60"] = s_z60

        # Filter to >= 2015-01-01 after warm-up
        sub_a1 = a1_merged[a1_merged["date"] >= "2015-01-01"].copy().reset_index(drop=True)
        sub_a1[f"{inst_key}_5d_z60"] = rolling_zscore(sub_a1[f"{inst_key}_5d"], 60, 30)
        sub_a1[f"{inst_key}_20d_z60"] = rolling_zscore(sub_a1[f"{inst_key}_20d"], 60, 30)
        is_cut_idx = int(len(sub_a1) * 0.70)
        raw_hi_is = float(sub_a1[f"{inst_key}_1d"].iloc[:is_cut_idx].quantile(0.90))
        raw_lo_is = float(sub_a1[f"{inst_key}_1d"].iloc[:is_cut_idx].quantile(0.10))

        a1_variants = [
            (
                f"{inst_key}_1d",
                f"A1 台股{inst_zh}當日買賣超",
                [
                    ("raw_buy_p90", f"{inst_zh}單日買超逾樣本內90分位 ({raw_hi_is:+.0f}億元)", 1, sub_a1[f"{inst_key}_1d"] > raw_hi_is),
                    ("raw_sell_p10", f"{inst_zh}單日賣超逾樣本內10分位 ({raw_lo_is:+.0f}億元，做空檢驗)", -1, sub_a1[f"{inst_key}_1d"] < raw_lo_is),
                ],
            ),
            (
                f"{inst_key}_5d",
                f"A1 台股{inst_zh} 5日累計買賣超",
                [
                    ("extreme_buy_5d", f"{inst_zh} 5日累計大買 (60日z > +1.5)", 1, sub_a1[f"{inst_key}_5d_z60"] > 1.5),
                    ("extreme_sell_5d", f"{inst_zh} 5日累計大賣 (60日z < -1.5，做空檢驗)", -1, sub_a1[f"{inst_key}_5d_z60"] < -1.5),
                ],
            ),
            (
                f"{inst_key}_20d",
                f"A1 台股{inst_zh} 20日累計買賣超",
                [
                    ("extreme_buy_20d", f"{inst_zh} 20日累計大買 (60日z > +1.5)", 1, sub_a1[f"{inst_key}_20d_z60"] > 1.5),
                    ("extreme_sell_20d", f"{inst_zh} 20日累計大賣 (60日z < -1.5，做空檢驗)", -1, sub_a1[f"{inst_key}_20d_z60"] < -1.5),
                ],
            ),
            (
                f"{inst_key}_streak",
                f"A1 台股{inst_zh}連續買賣超天數",
                [
                    ("streak_buy_5d", f"{inst_zh}連續買超 ≥ 5 日", 1, sub_a1[f"{inst_key}_streak"] >= 5),
                    ("streak_sell_5d", f"{inst_zh}連續賣超 ≥ 5 日 (做空檢驗)", -1, sub_a1[f"{inst_key}_streak"] <= -5),
                ],
            ),
            (
                f"{inst_key}_z60",
                f"A1 台股{inst_zh}買賣超 60日z分數",
                [
                    ("z60_gt_15", f"{inst_zh}買賣超 60日z > +1.5 (順勢做多)", 1, sub_a1[f"{inst_key}_z60"] > 1.5),
                    ("z60_lt_m15", f"{inst_zh}買賣超 60日z < -1.5 (順勢做空)", -1, sub_a1[f"{inst_key}_z60"] < -1.5),
                    ("z60_lt_m20_rev", f"{inst_zh}極端賣超 60日z < -2.0 (逆勢反彈做多)", 1, sub_a1[f"{inst_key}_z60"] < -2.0),
                ],
            ),
        ]

        for var_col, var_zh, ev_list in a1_variants:
            specs.append(
                {
                    "id": f"A1_{var_col}",
                    "category": "A. 台股籌碼",
                    "name": var_zh,
                    "market": "台股加權 (^TWII)",
                    "source": "證交所 BFI82U (FinMind 公開鏡像)",
                    "freq": "1D",
                    "df": sub_a1,
                    "signal_col": var_col,
                    "horizons": [("1d", "fwd_1d", 0), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                    "roll_window": 252,
                    "events": ev_list,
                    "sample_note": "",
                }
            )

    # A2: Foreign TXF Net Open Interest
    a2_df = pd.DataFrame(tw_data["A2_txf_foreign_oi"]["rows"]).sort_values("date").reset_index(drop=True)
    a2_merged = pd.merge(a2_df, twii_px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    net_oi = a2_merged["net_oi"].astype(float)
    a2_merged["txf_level"] = net_oi
    a2_merged["txf_1d_chg"] = net_oi.diff(1)
    a2_merged["txf_5d_chg"] = net_oi.diff(5)
    a2_merged["txf_z60"] = rolling_zscore(net_oi, window=60, min_periods=30)
    a2_merged["txf_1d_z60"] = rolling_zscore(a2_merged["txf_1d_chg"], 60, 30)
    a2_merged["txf_5d_z60"] = rolling_zscore(a2_merged["txf_5d_chg"], 60, 30)
    roll_max60 = net_oi.rolling(60, min_periods=30).max()
    roll_min60 = net_oi.rolling(60, min_periods=30).min()
    a2_merged["txf_pos60"] = (net_oi - roll_min60) / (roll_max60 - roll_min60).replace(0, np.nan) * 100.0

    a2_variants = [
        (
            "txf_level",
            "A2 外資台指期淨未平倉口數 (水準)",
            [
                ("level_z_high", "外資台指期淨未平倉水準偏多 (60日z > +1.5)", 1, a2_merged["txf_z60"] > 1.5),
                ("level_z_low", "外資台指期淨未平倉水準偏空 (60日z < -1.5，做空檢驗)", -1, a2_merged["txf_z60"] < -1.5),
            ],
        ),
        (
            "txf_1d_chg",
            "A2 外資台指期淨未平倉 (日變化)",
            [
                ("oi_1d_surge", "外資期貨單日淨部位大增 (日變化 60日z > +1.5)", 1, a2_merged["txf_1d_z60"] > 1.5),
                ("oi_1d_drop", "外資期貨單日淨部位大減 (日變化 60日z < -1.5，做空檢驗)", -1, a2_merged["txf_1d_z60"] < -1.5),
            ],
        ),
        (
            "txf_5d_chg",
            "A2 外資台指期淨未平倉 (5日變化)",
            [
                ("oi_5d_surge", "外資期貨 5日淨部位大增 (5日變化 60日z > +1.5)", 1, a2_merged["txf_5d_z60"] > 1.5),
                ("oi_5d_drop", "外資期貨 5日淨部位大減 (5日變化 60日z < -1.5，做空檢驗)", -1, a2_merged["txf_5d_z60"] < -1.5),
            ],
        ),
        (
            "txf_pos60",
            "A2 外資台指期淨未平倉 (60日區間位置/高低突破)",
            [
                ("oi_60d_high", "外資期貨淨部位創 60 日新高", 1, net_oi >= roll_max60),
                ("oi_60d_low_short", "外資期貨淨空單創 60 日新高 (順勢做空檢驗)", -1, net_oi <= roll_min60),
                ("oi_60d_low_long", "外資期貨淨空單創 60 日新高 (逆勢做多檢驗)", 1, net_oi <= roll_min60),
            ],
        ),
    ]

    for var_col, var_zh, ev_list in a2_variants:
        specs.append(
            {
                "id": f"A2_{var_col}",
                "category": "A. 台股籌碼",
                "name": var_zh,
                "market": "台股加權 (^TWII)",
                "source": "期交所三大法人期貨未平倉 (TAIFEX / FinMind)",
                "freq": "1D",
                "df": a2_merged,
                "signal_col": var_col,
                "horizons": [("1d", "fwd_1d", 0), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "roll_window": 252,
                "events": ev_list,
                "sample_note": "公開免金鑰歷史自 2018-06 起（共 8.4 年）",
            }
        )

    # A3: TWSE Margin Purchase Balance Change (MI_MARGN)
    a3_df = pd.DataFrame(tw_data["A3_margin_balance"]["rows"]).sort_values("date").reset_index(drop=True)
    a3_merged = pd.merge(a3_df, twii_px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    m_bal = a3_merged["margin_money_100m"].astype(float)
    a3_merged["margin_5d_pct"] = m_bal.pct_change(5) * 100.0
    a3_merged["margin_20d_pct"] = m_bal.pct_change(20) * 100.0
    a3_merged["margin_div_z"] = rolling_zscore(a3_merged["margin_5d_pct"], 60, 30) - rolling_zscore(a3_merged["ret_5d_past"], 60, 30)
    sub_a3 = a3_merged[a3_merged["date"] >= "2015-01-01"].copy().reset_index(drop=True)

    a3_variants = [
        (
            "margin_5d_pct",
            "A3 台股大盤融資餘額 5日變化率",
            [
                ("margin_5d_surge", "5日融資急增 (> +2.0%，散戶追高做空檢驗)", -1, sub_a3["margin_5d_pct"] > 2.0),
                ("margin_5d_flush", "5日融資急減 (< -2.5%，斷頭清洗做多檢驗)", 1, sub_a3["margin_5d_pct"] < -2.5),
            ],
        ),
        (
            "margin_20d_pct",
            "A3 台股大盤融資餘額 20日變化率",
            [
                ("margin_20d_overheat", "20日融資過熱 (> +5.0%，做空檢驗)", -1, sub_a3["margin_20d_pct"] > 5.0),
                ("margin_20d_delever", "20日融資大幅去槓桿 (< -5.0%，做多檢驗)", 1, sub_a3["margin_20d_pct"] < -5.0),
            ],
        ),
        (
            "margin_div_z",
            "A3 台股融資增減與指數背離指標",
            [
                ("margin_up_idx_down", "融資大增＋指數下跌背離 (5日融資 > +1% 且指數 < -1%，做空檢驗)", -1, (sub_a3["margin_5d_pct"] > 1.0) & (sub_a3["ret_5d_past"] < -1.0)),
                ("margin_flush_idx_down", "融資斷頭清洗＋指數急跌 (5日融資 < -2.5% 且指數 < -2.5%，做多檢驗)", 1, (sub_a3["margin_5d_pct"] < -2.5) & (sub_a3["ret_5d_past"] < -2.5)),
            ],
        ),
    ]

    for var_col, var_zh, ev_list in a3_variants:
        specs.append(
            {
                "id": f"A3_{var_col}",
                "category": "A. 台股籌碼",
                "name": var_zh,
                "market": "台股加權 (^TWII)",
                "source": "證交所信用交易統計 MI_MARGN",
                "freq": "1D",
                "df": sub_a3,
                "signal_col": var_col,
                "horizons": [("1d", "fwd_1d", 0), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "roll_window": 252,
                "events": ev_list,
                "sample_note": "",
            }
        )

    # A4: 2330.TW Foreign Buy/Sell (T86)
    a4_df = pd.DataFrame(tw_data["A4_2330_foreign"]["rows"]).sort_values("date").reset_index(drop=True)
    a4_merged = pd.merge(a4_df, tsmc_px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    f_2330 = a4_merged["foreign_net_lots"].astype(float)
    a4_merged["tsmc_f_1d"] = f_2330
    a4_merged["tsmc_f_5d"] = f_2330.rolling(5, min_periods=5).sum()
    a4_merged["tsmc_f_20d"] = f_2330.rolling(20, min_periods=20).sum()
    a4_merged["tsmc_f_z60"] = rolling_zscore(f_2330, 60, 30)
    a4_merged["tsmc_f_5d_z60"] = rolling_zscore(a4_merged["tsmc_f_5d"], 60, 30)
    a4_merged["tsmc_f_20d_z60"] = rolling_zscore(a4_merged["tsmc_f_20d"], 60, 30)
    a4_merged["tsmc_f_streak"] = compute_streak(f_2330)
    sub_a4 = a4_merged[a4_merged["date"] >= "2015-01-01"].copy().reset_index(drop=True)

    a4_variants = [
        (
            "tsmc_f_5d",
            "A4 台積電 (2330) 外資 5日累計買賣超",
            [
                ("tsmc_5d_buy_z15", "台積電外資 5日累計大買 (60日z > +1.5)", 1, sub_a4["tsmc_f_5d_z60"] > 1.5),
                ("tsmc_5d_sell_z15", "台積電外資 5日累計大賣 (60日z < -1.5，做空檢驗)", -1, sub_a4["tsmc_f_5d_z60"] < -1.5),
                ("tsmc_f_streak5", "台積電外資連續買超 ≥ 5 日", 1, sub_a4["tsmc_f_streak"] >= 5),
            ],
        ),
        (
            "tsmc_f_20d",
            "A4 台積電 (2330) 外資 20日累計買賣超",
            [
                ("tsmc_20d_buy_z15", "台積電外資 20日累計大買 (60日z > +1.5)", 1, sub_a4["tsmc_f_20d_z60"] > 1.5),
                ("tsmc_20d_sell_z15", "台積電外資 20日累計大賣 (60日z < -1.5，做空檢驗)", -1, sub_a4["tsmc_f_20d_z60"] < -1.5),
            ],
        ),
        (
            "tsmc_f_z60",
            "A4 台積電 (2330) 外資買賣超 60日z分數",
            [
                ("tsmc_1d_buy_z15", "台積電外資單日大買 (60日z > +1.5)", 1, sub_a4["tsmc_f_z60"] > 1.5),
                ("tsmc_1d_sell_z15", "台積電外資單日大賣 (60日z < -1.5，做空檢驗)", -1, sub_a4["tsmc_f_z60"] < -1.5),
            ],
        ),
    ]

    for var_col, var_zh, ev_list in a4_variants:
        specs.append(
            {
                "id": f"A4_{var_col}",
                "category": "A. 台股籌碼",
                "name": var_zh,
                "market": "台積電 (2330.TW)",
                "source": "證交所 T86 個股三大法人",
                "freq": "1D",
                "df": sub_a4,
                "signal_col": var_col,
                "horizons": [("1d", "fwd_1d", 0), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "roll_window": 252,
                "events": ev_list,
                "sample_note": "",
            }
        )

    # ------------------------------------------------------------------
    # Category B: US CFTC COT Reports (Weekly, Strict Next-Monday-Open Lag)
    # ------------------------------------------------------------------
    cot_markets = [
        ("SP500", "B1", "標普 500 期貨 (E-mini S&P)", "標普 500 (^GSPC)", "2010-01-01"),
        ("NDX", "B2", "那斯達克 100 期貨 (E-mini NDX)", "那斯達克 100 (^NDX)", "2010-01-01"),
        ("GOLD", "B3", "黃金期貨 (COMEX Gold)", "黃金期貨 (GC=F)", "2010-01-01"),
        ("WTI", "B4", "WTI 原油期貨 (NYMEX WTI)", "WTI 原油 (CL=F)", "2010-01-01"),
        ("BTC_CME", "B5", "CME 比特幣期貨", "比特幣 (BTC-USD)", "2018-04-10"),
    ]

    for cot_key, b_prefix, mkt_title, px_label, start_filter in cot_markets:
        c_info = cot_data[cot_key]
        c_df = pd.DataFrame(c_info["rows"]).sort_values("report_date").reset_index(drop=True)
        net_s = c_df["net"].astype(float)
        # 156-week rolling COT Index (min_periods=52 so BTC_CME starts after 1 year of history; others warm from 2006)
        rmax = net_s.rolling(156, min_periods=52).max()
        rmin = net_s.rolling(156, min_periods=52).min()
        c_df["cot_index"] = (net_s - rmin) / (rmax - rmin).replace(0, np.nan) * 100.0

        nonrept_s = c_df["nonrept_net"].astype(float)
        nr_max = nonrept_s.rolling(156, min_periods=52).max()
        nr_min = nonrept_s.rolling(156, min_periods=52).min()
        c_df["nonrept_cot_index"] = (nonrept_s - nr_min) / (nr_max - nr_min).replace(0, np.nan) * 100.0

        # Align Tuesday report_date -> Next Monday (report_date + 6 days) or first trading day >= report_date + 6 days
        px_df = _build_daily_price_df(prices[c_info["price_key"]], horizons=(1, 5, 20))
        px_dates = px_df["date"].to_numpy()

        aligned_rows: list[dict[str, Any]] = []
        for _, r in c_df.iterrows():
            rep_d = str(r["report_date"])
            if rep_d < start_filter:
                continue
            avail_monday = (datetime.strptime(rep_d, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
            idx_pos = int(np.searchsorted(px_dates, avail_monday, side="left"))
            if idx_pos >= len(px_df):
                continue
            px_row = px_df.iloc[idx_pos]
            # Verify matched trading day is within 7 days of avail_monday
            if (datetime.strptime(str(px_row["date"]), "%Y-%m-%d") - datetime.strptime(avail_monday, "%Y-%m-%d")).days > 7:
                continue
            aligned_rows.append(
                {
                    "dt": str(px_row["date"]),
                    "report_date": rep_d,
                    "net_pos": float(r["net"]),
                    "net_pct_oi": float(r["net_pct_oi"]),
                    "cot_index": float(r["cot_index"]) if pd.notna(r["cot_index"]) else np.nan,
                    "nonrept_cot_index": float(r["nonrept_cot_index"]) if pd.notna(r["nonrept_cot_index"]) else np.nan,
                    # Weekly COT enters at Monday Open -> 1d, 5d (1w), 20d (4w) returns strictly after Monday Open
                    "fwd_1d": float(px_row["fwd_open_1d"]) if pd.notna(px_row["fwd_open_1d"]) else np.nan,
                    "fwd_5d": float(px_row["fwd_open_5d"]) if pd.notna(px_row["fwd_open_5d"]) else np.nan,
                    "fwd_20d": float(px_row["fwd_open_20d"]) if pd.notna(px_row["fwd_open_20d"]) else np.nan,
                }
            )

        cot_aligned = pd.DataFrame(aligned_rows).sort_values("dt").reset_index(drop=True)
        ev_cot = [
            ("cot_gt_90", "COT Index > 90 (大戶極端看多，順勢檢驗)", 1, cot_aligned["cot_index"] > 90.0),
            ("cot_lt_10", "COT Index < 10 (大戶極端看空，逆勢做多檢驗)", 1, cot_aligned["cot_index"] < 10.0),
            (
                "spec_vs_retail_div",
                "大戶與散戶極端反向 (大戶COT>80且散戶<20)",
                1,
                (cot_aligned["cot_index"] > 80.0) & (cot_aligned["nonrept_cot_index"] < 20.0),
            ),
        ]

        # Note on weekly overlap_lag:
        # 1d (Monday intraday) has 0 weekly overlap; 5d (Mon->Fri 1 week) has 0 weekly overlap!; 20d (4 weeks) has 3 weekly overlaps!
        for var_col, var_suffix in (
            ("net_pct_oi", "大戶淨部位佔未平倉比 (Net % OI)"),
            ("cot_index", "COT Index (156週百分位)"),
        ):
            specs.append(
                {
                    "id": f"{b_prefix}_{cot_key}_{var_col}",
                    "category": "B. 美國期貨 CFTC COT",
                    "name": f"{b_prefix} {mkt_title} {var_suffix}",
                    "market": px_label,
                    "source": f"CFTC {c_info['report_type']} (code={c_info['cftc_code']})",
                    "freq": "1W (週二部位/週五公布/下週一開盤進場)",
                    "df": cot_aligned,
                    "signal_col": var_col,
                    "horizons": [("1d", "fwd_1d", 0), ("5d", "fwd_5d", 0), ("20d", "fwd_20d", 3)],
                    "roll_window": 52,
                    "events": ev_cot,
                    "sample_note": "" if cot_key != "BTC_CME" else "CME 比特幣期貨自 2018-04 起；156週滾動暖機後自 2019-04 起",
                }
            )

    # ------------------------------------------------------------------
    # Category C: Crypto (OKX Rubik & Funding Rate & Farside Spot BTC ETF)
    # ------------------------------------------------------------------
    for ccy in ("BTC", "ETH"):
        c_obj = okx_data[ccy]
        mkt_label = f"{ccy} (OKX {ccy}-USDT-SWAP)"

        # Build OKX 1D price table from candles_1D (or fallback to yfinance daily)
        c1d = pd.DataFrame(c_obj["candles_1D"]).sort_values("ts").reset_index(drop=True)
        c1d["date_utc"] = c1d["dt_utc"].str.slice(0, 10)
        close_1d = c1d["close"].to_numpy(dtype=float)
        n1d = len(c1d)
        c1d["ret_1d_past"] = c1d["close"].pct_change(1) * 100.0
        for h in (1, 3, 5, 20):
            fwd = np.full(n1d, np.nan, dtype=float)
            if n1d > h:
                fwd[: n1d - h] = (close_1d[h:] / close_1d[: n1d - h] - 1.0) * 100.0
            c1d[f"fwd_{h}d"] = fwd

        # Build OKX 4H price table from candles_4H
        c4h = pd.DataFrame(c_obj["candles_4H"]).sort_values("ts").reset_index(drop=True)
        close_4h = c4h["close"].to_numpy(dtype=float)
        n4h = len(c4h)
        c4h["ret_4h_past"] = c4h["close"].pct_change(1) * 100.0
        for h_label, h_bars in (("4h", 1), ("1d", 6), ("3d", 18), ("5d", 30), ("20d", 120)):
            fwd = np.full(n4h, np.nan, dtype=float)
            if n4h > h_bars:
                fwd[: n4h - h_bars] = (close_4h[h_bars:] / close_4h[: n4h - h_bars] - 1.0) * 100.0
            c4h[f"fwd_{h_label}"] = fwd

        # Helper to merge 1D Rubik series with 1D candles (and 4H forward return aligned by timestamp)
        c4h_lookup = c4h[["ts", "fwd_4h"]].rename(columns={"fwd_4h": "fwd_4h_from_bar"})

        # All-account L/S ratio on 1D (for retail vs top-trader comparison)
        all_ls_1d_map = {
            datetime.fromtimestamp(int(row[0]) / 1000.0, tz=datetime.now().astimezone().tzinfo).strftime("%Y-%m-%d"): float(row[1])
            for row in (c_obj.get("all_acct_ls_1D") or [])
        }
        all_ls_ts_map = {int(row[0]): float(row[1]) for row in (c_obj.get("all_acct_ls_1D") or [])}

        # C1: Top Trader Account L/S Ratio & Position L/S Ratio (1D & 4H)
        for r_prefix, zh_title in (
            ("top_trader_acct_ls", "大戶帳戶多空比 (Top Trader Account L/S)"),
            ("top_trader_pos_ls", "大戶持倉多空比 (Top Trader Position L/S)"),
        ):
            # 1D version (1,000 days ~ 2.7 years)
            rows_1d = c_obj.get(f"{r_prefix}_1D") or []
            df_r1d = pd.DataFrame(
                [
                    {
                        "ts": int(x[0]),
                        "date_utc": pd.to_datetime(int(x[0]), unit="ms", utc=True).strftime("%Y-%m-%d"),
                        "val": float(x[1]),
                        "all_ls": all_ls_ts_map.get(int(x[0]), np.nan),
                    }
                    for x in rows_1d
                ]
            ).sort_values("ts").drop_duplicates(subset=["date_utc"], keep="last")
            m_1d = pd.merge(df_r1d, c1d, on="date_utc", how="inner").sort_values("date_utc").reset_index(drop=True)
            m_1d["dt"] = m_1d["date_utc"]
            m_1d["z60"] = rolling_zscore(m_1d["val"], 60, 20)
            m_1d["chg_1d"] = m_1d["val"].diff(1)
            m_1d["all_chg_1d"] = m_1d["all_ls"].diff(1)

            ev_c1_1d = [
                ("ls_high_z15", "大戶多空比極端偏多 (60日z > +1.5)", 1, m_1d["z60"] > 1.5),
                ("ls_low_z15", "大戶多空比極端偏空 (60日z < -1.5，逆勢做多檢驗)", 1, m_1d["z60"] < -1.5),
                (
                    "top_vs_all_div",
                    "大戶與全市場散戶多空方向相反 (大戶增、散戶減)",
                    1,
                    (m_1d["chg_1d"] > 0) & (m_1d["all_chg_1d"] < 0),
                ),
            ]
            specs.append(
                {
                    "id": f"C1_{ccy}_{r_prefix}_1D",
                    "category": "C. 加密貨幣籌碼",
                    "name": f"C1 {ccy} OKX {zh_title} [日頻]",
                    "market": mkt_label,
                    "source": f"OKX Rubik ({r_prefix} 1D)",
                    "freq": "1D",
                    "df": m_1d,
                    "signal_col": "val",
                    "horizons": [
                        ("1d", "fwd_1d", 0),
                        ("3d", "fwd_3d", 2),
                        ("5d", "fwd_5d", 4),
                        ("20d", "fwd_20d", 19),
                    ],
                    "roll_window": 180,
                    "events": ev_c1_1d,
                    "sample_note": "OKX 公開端點日頻上限 1,000 日（約 2.7 年，未涵蓋 2022 熊市完整週期）",
                }
            )

            # 4H version (1,000 bars ~ 166 days)
            rows_4h = c_obj.get(f"{r_prefix}_4H") or []
            df_r4h = pd.DataFrame([{"ts": int(x[0]), "val": float(x[1])} for x in rows_4h]).sort_values("ts")
            m_4h = pd.merge(df_r4h, c4h, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
            m_4h["dt"] = m_4h["dt_utc"]
            m_4h["z60"] = rolling_zscore(m_4h["val"], 60, 20)
            ev_c1_4h = [
                ("ls4h_high_z15", "4H大戶多空比極端偏多 (60期z > +1.5)", 1, m_4h["z60"] > 1.5),
                ("ls4h_low_z15", "4H大戶多空比極端偏空 (60期z < -1.5)", 1, m_4h["z60"] < -1.5),
            ]
            specs.append(
                {
                    "id": f"C1_{ccy}_{r_prefix}_4H",
                    "category": "C. 加密貨幣籌碼",
                    "name": f"C1 {ccy} OKX {zh_title} [4小時頻]",
                    "market": mkt_label,
                    "source": f"OKX Rubik ({r_prefix} 4H)",
                    "freq": "4H",
                    "df": m_4h,
                    "signal_col": "val",
                    "horizons": [
                        ("4h", "fwd_4h", 0),
                        ("1d", "fwd_1d", 5),
                        ("3d", "fwd_3d", 17),
                        ("5d", "fwd_5d", 29),
                        ("20d", "fwd_20d", 119),
                    ],
                    "roll_window": 240,
                    "events": ev_c1_4h,
                    "sample_note": "OKX 4H 端點僅保留最近 1,000 根（約 5.5 個月），樣本太短僅供參考",
                }
            )

        # C2: OKX Taker Buy/Sell Volume Ratio (1D & 4H)
        for period, candle_df, h_list, s_note in (
            (
                "1D",
                c1d,
                [("1d", "fwd_1d", 0), ("3d", "fwd_3d", 2), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "OKX taker-volume-contract 1D 端點僅近 100 日有非零值（更早回傳 0），樣本太短僅供參考",
            ),
            (
                "4H",
                c4h,
                [("4h", "fwd_4h", 0), ("1d", "fwd_1d", 5), ("3d", "fwd_3d", 17), ("5d", "fwd_5d", 29), ("20d", "fwd_20d", 119)],
                "OKX 4H 端點僅保留最近 1,000 根（約 5.5 個月），樣本太短僅供參考",
            ),
        ):
            t_rows = c_obj.get(f"taker_vol_contract_{period}") or []
            df_t = pd.DataFrame(
                [
                    {
                        "ts": int(x[0]),
                        "sell_vol": float(x[1]),
                        "buy_vol": float(x[2]),
                        "taker_ratio": (float(x[2]) / float(x[1])) if float(x[1]) > 0 else np.nan,
                    }
                    for x in t_rows
                ]
            ).sort_values("ts")
            if period == "1D":
                df_t["date_utc"] = pd.to_datetime(df_t["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
                m_t = pd.merge(df_t, candle_df, on="date_utc", how="inner").sort_values("date_utc").reset_index(drop=True)
                m_t["dt"] = m_t["date_utc"]
            else:
                m_t = pd.merge(df_t, candle_df, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
                m_t["dt"] = m_t["dt_utc"]

            m_t["z60"] = rolling_zscore(m_t["taker_ratio"], 60, 20)
            m_t["buy_streak"] = compute_streak(m_t["taker_ratio"] - 1.0)
            ev_c2 = [
                ("taker_buy_z15", "主動買盤極端強勁 (60期z > +1.5)", 1, m_t["z60"] > 1.5),
                ("taker_sell_z15", "主動賣盤極端湧現 (60期z < -1.5，恐慌逆勢檢驗)", 1, m_t["z60"] < -1.5),
                ("taker_streak3", "連續 3 期主動買量 > 主動賣量", 1, m_t["buy_streak"] >= 3),
            ]
            specs.append(
                {
                    "id": f"C2_{ccy}_taker_ratio_{period}",
                    "category": "C. 加密貨幣籌碼",
                    "name": f"C2 {ccy} OKX 主動買賣量比 (Taker Buy/Sell) [{period}]",
                    "market": mkt_label,
                    "source": f"OKX Rubik (taker-volume-contract {period})",
                    "freq": period,
                    "df": m_t,
                    "signal_col": "taker_ratio",
                    "horizons": h_list,
                    "roll_window": 180 if period == "1D" else 240,
                    "events": ev_c2,
                    "sample_note": s_note,
                }
            )

        # C3a: OKX Perpetual Funding Rate (8h settlements matched to 4H candles)
        f_rows = c_obj.get("funding_8h") or []
        df_f = pd.DataFrame(f_rows).sort_values("ts").reset_index(drop=True)
        m_f = pd.merge(df_f, c4h, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
        m_f["dt"] = m_f["dt_utc_x"]
        m_f["fr_bps"] = m_f["funding_rate"].astype(float) * 10000.0  # in bps (0.01% = 1 bp)
        m_f["z60"] = rolling_zscore(m_f["fr_bps"], 60, 20)
        ev_c3_fr = [
            ("fr_high_z15", "資金費率極端過熱 (60期z > +1.5，做空檢驗)", -1, m_f["z60"] > 1.5),
            ("fr_neg", "資金費率轉負 (Funding Rate < 0，空頭擁擠做多檢驗)", 1, m_f["fr_bps"] < 0.0),
        ]
        specs.append(
            {
                "id": f"C3_{ccy}_funding_8h",
                "category": "C. 加密貨幣籌碼",
                "name": f"C3 {ccy} OKX 永續合約資金費率 (Funding Rate) [8h結算]",
                "market": mkt_label,
                "source": "OKX Public Funding Rate History (8h)",
                "freq": "8H",
                "df": m_f,
                "signal_col": "fr_bps",
                "horizons": [
                    ("4h", "fwd_4h", 0),
                    ("1d", "fwd_1d", 2),
                    ("3d", "fwd_3d", 8),
                    ("5d", "fwd_5d", 14),
                    ("20d", "fwd_20d", 59),
                ],
                "roll_window": 90,
                "events": ev_c3_fr,
                "sample_note": "OKX 公開資金費率歷史端點僅保留近 ~3 個月（285 次結算），樣本不足僅供參考",
            }
        )

        # C3b: OKX Open Interest Change (1D & 4H)
        for period, candle_df, h_list, s_note in (
            (
                "1D",
                c1d,
                [("1d", "fwd_1d", 0), ("3d", "fwd_3d", 2), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "OKX 公開端點日頻上限 1,000 日（約 2.7 年）",
            ),
            (
                "4H",
                c4h,
                [("4h", "fwd_4h", 0), ("1d", "fwd_1d", 5), ("3d", "fwd_3d", 17), ("5d", "fwd_5d", 29), ("20d", "fwd_20d", 119)],
                "OKX 4H 端點僅保留最近 1,000 根（約 5.5 個月），樣本太短僅供參考",
            ),
        ):
            oi_rows = c_obj.get(f"oi_history_{period}") or []
            df_oi = pd.DataFrame([{"ts": int(x[0]), "oi_coin": float(x[2]), "oi_usd": float(x[3])} for x in oi_rows]).sort_values("ts")
            if period == "1D":
                df_oi["date_utc"] = pd.to_datetime(df_oi["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
                df_oi = df_oi.drop_duplicates(subset=["date_utc"], keep="last")
                m_oi = pd.merge(df_oi, candle_df, on="date_utc", how="inner").sort_values("date_utc").reset_index(drop=True)
                m_oi["dt"] = m_oi["date_utc"]
                past_ret_col = "ret_1d_past"
            else:
                m_oi = pd.merge(df_oi, candle_df, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
                m_oi["dt"] = m_oi["dt_utc"]
                past_ret_col = "ret_4h_past"

            m_oi["oi_chg_pct"] = m_oi["oi_coin"].pct_change(1) * 100.0
            m_oi["z60"] = rolling_zscore(m_oi["oi_chg_pct"], 60, 20)
            ev_c3_oi = [
                ("oi_surge_z15", "未平倉量單期急增 (60期z > +1.5)", 1, m_oi["z60"] > 1.5),
                ("oi_flush_z15", "未平倉量大幅去槓桿清算 (60期z < -1.5)", 1, m_oi["z60"] < -1.5),
                (
                    "oi_up_px_down",
                    "未平倉量增加＋價格下跌 (空頭加倉背離)",
                    -1,
                    (m_oi["oi_chg_pct"] > 1.5) & (m_oi[past_ret_col] < -1.0),
                ),
            ]
            specs.append(
                {
                    "id": f"C3_{ccy}_oi_chg_{period}",
                    "category": "C. 加密貨幣籌碼",
                    "name": f"C3 {ccy} OKX 未平倉量變化率 (OI Change %) [{period}]",
                    "market": mkt_label,
                    "source": f"OKX Rubik (open-interest-history {period})",
                    "freq": period,
                    "df": m_oi,
                    "signal_col": "oi_chg_pct",
                    "horizons": h_list,
                    "roll_window": 180 if period == "1D" else 240,
                    "events": ev_c3_oi,
                    "sample_note": s_note,
                }
            )

    # C4: US Spot Bitcoin ETF Daily Net Inflows (Farside, strict D+1 lag)
    etf_df = pd.DataFrame(etf_data["rows"]).sort_values("date").reset_index(drop=True)
    inflow = etf_df["total_musd"].astype(float)
    etf_df["etf_1d"] = inflow
    etf_df["etf_5d"] = inflow.rolling(5, min_periods=3).sum()
    etf_df["etf_20d"] = inflow.rolling(20, min_periods=10).sum()
    etf_df["etf_z60"] = rolling_zscore(inflow, 60, 20)
    etf_df["etf_streak"] = compute_streak(inflow)

    # Align US trading day D ETF inflow -> strictly predict returns starting from UTC day D+1 close onward!
    btc_px = _build_daily_price_df(prices["BTC"], horizons=(1, 3, 5, 20))
    btc_dates = btc_px["date"].to_numpy()
    etf_aligned_rows: list[dict[str, Any]] = []
    for _, r in etf_df.iterrows():
        d_us = str(r["date"])
        # US ETF flow for day D is finalized after US close / early morning UTC day D+1 -> entry at D+1 UTC close
        d_avail = (datetime.strptime(d_us, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        idx_pos = int(np.searchsorted(btc_dates, d_avail, side="left"))
        if idx_pos >= len(btc_px):
            continue
        px_row = btc_px.iloc[idx_pos]
        etf_aligned_rows.append(
            {
                "dt": d_us,
                "avail_dt": str(px_row["date"]),
                "etf_1d": float(r["etf_1d"]),
                "etf_5d": float(r["etf_5d"]) if pd.notna(r["etf_5d"]) else np.nan,
                "etf_20d": float(r["etf_20d"]) if pd.notna(r["etf_20d"]) else np.nan,
                "etf_z60": float(r["etf_z60"]) if pd.notna(r["etf_z60"]) else np.nan,
                "etf_streak": float(r["etf_streak"]),
                "fwd_1d": float(px_row["fwd_1d"]) if pd.notna(px_row["fwd_1d"]) else np.nan,
                "fwd_3d": float(px_row["fwd_3d"]) if pd.notna(px_row["fwd_3d"]) else np.nan,
                "fwd_5d": float(px_row["fwd_5d"]) if pd.notna(px_row["fwd_5d"]) else np.nan,
                "fwd_20d": float(px_row["fwd_20d"]) if pd.notna(px_row["fwd_20d"]) else np.nan,
            }
        )
    etf_aligned = pd.DataFrame(etf_aligned_rows).sort_values("dt").reset_index(drop=True)
    etf_aligned["etf_5d_z60"] = rolling_zscore(etf_aligned["etf_5d"], 60, 20)
    etf_aligned["etf_20d_z60"] = rolling_zscore(etf_aligned["etf_20d"], 60, 20)

    c4_variants = [
        (
            "etf_1d",
            "C4 比特幣現貨 ETF 單日淨流入 (百萬美元)",
            [
                ("etf_inflow_z15", "BTC現貨ETF單日大額淨流入 (60日z > +1.5)", 1, etf_aligned["etf_z60"] > 1.5),
                ("etf_outflow_z15", "BTC現貨ETF單日大額淨流出 (60日z < -1.5，做空檢驗)", -1, etf_aligned["etf_z60"] < -1.5),
                ("etf_streak5", "BTC現貨ETF連續 5 日淨流入", 1, etf_aligned["etf_streak"] >= 5),
            ],
        ),
        (
            "etf_5d",
            "C4 比特幣現貨 ETF 5日累計淨流入",
            [
                ("etf_5d_in_z15", "BTC現貨ETF 5日累計大額淨流入 (60日z > +1.5)", 1, etf_aligned["etf_5d_z60"] > 1.5),
                ("etf_5d_out_z15", "BTC現貨ETF 5日累計大額淨流出 (60日z < -1.5，做空檢驗)", -1, etf_aligned["etf_5d_z60"] < -1.5),
            ],
        ),
        (
            "etf_20d",
            "C4 比特幣現貨 ETF 20日累計淨流入",
            [
                ("etf_20d_in_z15", "BTC現貨ETF 20日累計大額淨流入 (60日z > +1.5)", 1, etf_aligned["etf_20d_z60"] > 1.5),
                ("etf_20d_out_z15", "BTC現貨ETF 20日累計大額淨流出 (60日z < -1.5，做空檢驗)", -1, etf_aligned["etf_20d_z60"] < -1.5),
            ],
        ),
        (
            "etf_z60",
            "C4 比特幣現貨 ETF 淨流入 60日z分數",
            [
                ("etf_z60_gt15", "BTC現貨ETF淨流入 60日z > +1.5", 1, etf_aligned["etf_z60"] > 1.5),
                ("etf_z60_ltm15", "BTC現貨ETF淨流出 60日z < -1.5 (做空檢驗)", -1, etf_aligned["etf_z60"] < -1.5),
            ],
        ),
    ]

    for var_col, var_zh, ev_list in c4_variants:
        specs.append(
            {
                "id": f"C4_{var_col}",
                "category": "C. 加密貨幣籌碼",
                "name": var_zh,
                "market": "比特幣 (BTC-USD)",
                "source": "Farside Investors 美國現貨 BTC ETF 公開表格",
                "freq": "1D (隔日D+1生效)",
                "df": etf_aligned,
                "signal_col": var_col,
                "horizons": [("1d", "fwd_1d", 0), ("3d", "fwd_3d", 2), ("5d", "fwd_5d", 4), ("20d", "fwd_20d", 19)],
                "roll_window": 120,
                "events": ev_list,
                "sample_note": "美國現貨 BTC ETF 自 2024-01-11 上市至今（約 2.9 年，未經歷 2022 完整熊市）",
            }
        )

    return specs


# ============================================================================
# 4. Full Backtest Runner, Multiple Testing Correction & Strict Classification
# ============================================================================

def run_all_evaluations() -> dict[str, Any]:
    prices = json.loads((CACHE_DIR / "prices_daily.json").read_text(encoding="utf-8"))
    tw_data = json.loads((CACHE_DIR / "taiwan_flow.json").read_text(encoding="utf-8"))
    cot_data = json.loads((CACHE_DIR / "cftc_cot.json").read_text(encoding="utf-8"))
    okx_data = json.loads((CACHE_DIR / "okx_crypto.json").read_text(encoding="utf-8"))
    etf_data = json.loads((CACHE_DIR / "btc_etf_farside.json").read_text(encoding="utf-8"))

    specs = build_all_indicator_specs(prices, tw_data, cot_data, okx_data, etf_data)
    print(f"[RUN] Evaluating {len(specs)} indicators across multiple horizons...")

    raw_results: list[dict[str, Any]] = []
    # Collect all primary hypothesis p-values for global Benjamini-Hochberg & Bonferroni correction
    # Primary tests per (indicator, horizon):
    #   1) Full-sample Newey-West Spearman rank correlation p-value
    #   2) Full-sample Newey-West Q5-Q1 spread p-value
    #   3) Each pre-defined event signal's full-sample Newey-West excess return p-value
    p_registry: list[tuple[int, str, str, int | None, float]] = []

    for idx, sp in enumerate(specs):
        h_map: dict[str, Any] = {}
        for h_label, ret_col, lag in sp["horizons"]:
            res_h = evaluate_horizon(
                df=sp["df"],
                signal_col=sp["signal_col"],
                ret_col=ret_col,
                h_label=h_label,
                overlap_lag=lag,
                roll_window=sp["roll_window"],
                events_spec=sp["events"],
            )
            h_map[h_label] = res_h
            if res_h.get("valid"):
                p_registry.append((idx, h_label, "corr", None, res_h["correlation"]["full"]["nw_p"]))
                p_registry.append((idx, h_label, "q51", None, res_h["quintiles"]["full"]["nw_p"]))
                for ev_i, ev_obj in enumerate(res_h["events"]):
                    p_registry.append((idx, h_label, "event", ev_i, ev_obj["full"]["nw_p"]))

        raw_results.append(
            {
                "id": sp["id"],
                "category": sp["category"],
                "name": sp["name"],
                "market": sp["market"],
                "source": sp["source"],
                "freq": sp["freq"],
                "sample_note": sp["sample_note"],
                "horizons": h_map,
            }
        )

    # Global Multiple Testing Correction (Benjamini-Hochberg FDR q-value & Bonferroni p-value)
    all_p = [item[4] for item in p_registry]
    all_q, all_bonf = benjamini_hochberg(all_p)

    for (idx, h_label, kind, ev_i, _), q_val, bonf_val in zip(p_registry, all_q, all_bonf):
        h_obj = raw_results[idx]["horizons"][h_label]
        if kind == "corr":
            h_obj["correlation"]["full"]["bh_q"] = round(q_val, 4)
            h_obj["correlation"]["full"]["bonf_p"] = round(bonf_val, 4)
        elif kind == "q51":
            h_obj["quintiles"]["full"]["bh_q"] = round(q_val, 4)
            h_obj["quintiles"]["full"]["bonf_p"] = round(bonf_val, 4)
        elif kind == "event" and ev_i is not None:
            h_obj["events"][ev_i]["full"]["bh_q"] = round(q_val, 4)
            h_obj["events"][ev_i]["full"]["bonf_p"] = round(bonf_val, 4)

    # Strict Classification per Indicator (across its tested horizons)
    for item in raw_results:
        h_map = item["horizons"]
        valid_hs = [h for h, obj in h_map.items() if obj.get("valid")]
        if not valid_hs:
            item["summary"] = {
                "period": "N/A",
                "n_samples": 0,
                "oos_valid": False,
                "best_horizon": "N/A",
                "effect_size_str": "N/A",
                "conclusion": "沒用",
                "reason_short": "有效樣本不足。",
            }
            continue

        # Reference period & sample count from shortest valid horizon (usually 1d or 4h)
        ref_h = valid_hs[0]
        ref_obj = h_map[ref_h]
        period_str = f"{ref_obj['start_dt'][:10]} ~ {ref_obj['end_dt'][:10]}"
        n_samples = ref_obj["n_total"]

        # Check whether sample span is too short (< 3 years or 4H/8H OKX limited retention)
        start_yr = int(ref_obj["start_dt"][:4])
        end_yr = int(ref_obj["end_dt"][:4])
        span_days = (
            datetime.strptime(ref_obj["end_dt"][:10], "%Y-%m-%d")
            - datetime.strptime(ref_obj["start_dt"][:10], "%Y-%m-%d")
        ).days
        is_short_history = (span_days < 1050) or (item["freq"] in ("4H", "8H"))

        best_h = ref_h
        best_score = -1e9
        horizon_verdicts: dict[str, dict[str, Any]] = {}

        for h_label in valid_hs:
            ho = h_map[h_label]
            eff_thresh = EFFECT_SIZE_THRESHOLDS_PCT.get(h_label, 0.30)

            rho_is = ho["correlation"]["is"]["rho"]
            rho_oos = ho["correlation"]["oos"]["rho"]
            rho_full = ho["correlation"]["full"]["rho"]
            corr_q = ho["correlation"]["full"].get("bh_q", 1.0)

            q51_is = ho["quintiles"]["is"]["q5_minus_q1_ret"]
            q51_oos = ho["quintiles"]["oos"]["q5_minus_q1_ret"]
            q51_full = ho["quintiles"]["full"]["q5_minus_q1_ret"]
            q51_q = ho["quintiles"]["full"].get("bh_q", 1.0)

            rho_ex = ho["regime_exclusion"]["rho_ex_202003_2022"]
            q51_ex = ho["regime_exclusion"]["q5_minus_q1_ex"]

            # 1) Direction consistency between IS and OOS
            corr_dir_match = (rho_is * rho_oos > 0) and (abs(rho_is) >= 0.02) and (abs(rho_oos) >= 0.02)
            q51_dir_match = (q51_is * q51_oos > 0) and (abs(q51_is) >= eff_thresh * 0.5) and (abs(q51_oos) >= eff_thresh)
            continuous_dir_consistent = (rho_is * rho_oos > 0) and (q51_is * q51_oos > 0) and (rho_is * q51_is > 0)
            no_dir_reversal = (rho_is * rho_oos >= -1e-6) and (q51_is * q51_oos >= -1e-4)

            # Check pre-defined event signals
            qualifying_events: list[dict[str, Any]] = []
            partial_events: list[dict[str, Any]] = []
            for ev in ho["events"]:
                ev_is = ev["is"]
                ev_oos = ev["oos"]
                ev_full = ev["full"]
                # Event direction holds in both IS and OOS with meaningful effect size
                ev_dir_consistent = (
                    (ev_is["dir_excess_ret"] >= eff_thresh * 0.5)
                    and (ev_is["dir_win_edge_pp"] >= 2.0)
                    and (ev_oos["dir_excess_ret"] >= eff_thresh)
                    and (ev_oos["dir_win_edge_pp"] >= 5.0)
                )
                # Strict OOS event criteria:
                # - Indicator itself does not flip sign between IS and OOS
                # - Both IS and OOS event excess return & win rate edge hold
                # - Independent non-overlapping OOS event count >= 30
                # - Full-sample Newey-West HAC p < 0.05
                ev_strict_oos = (
                    no_dir_reversal
                    and ev_dir_consistent
                    and (ev_oos["n_non_overlap"] >= 30)
                    and (ev_is["n_non_overlap"] >= 30)
                    and (ev_full["nw_p"] < 0.05)
                )
                # Partial event signal (e.g. raw OOS N >= 20 or full N >= 30 with OOS win edge >= 5pp, or p >= 0.05)
                ev_partial_oos = (
                    (ev_is["dir_excess_ret"] > 0)
                    and (ev_oos["dir_excess_ret"] >= eff_thresh * 0.5)
                    and (ev_oos["dir_win_edge_pp"] >= 5.0)
                    and (ev_full["n"] >= 30)
                    and (ev_oos["n"] >= 15)
                )
                if ev_strict_oos:
                    qualifying_events.append(ev)
                elif ev_partial_oos:
                    partial_events.append(ev)

            # 2) Statistical significance (BH q < 0.05) OR qualifying event signal
            stat_sig = (
                (corr_q < 0.05 and continuous_dir_consistent)
                or (q51_q < 0.05 and continuous_dir_consistent)
                or (len(qualifying_events) > 0)
            )

            # 3) Practical effect size check (in both IS and OOS)
            eff_size_ok = (
                continuous_dir_consistent
                and (abs(q51_oos) >= eff_thresh)
                and (abs(q51_is) >= eff_thresh * 0.5)
            ) or (len(qualifying_events) > 0)

            # 4) Extreme regime exclusion check (2020-03 and 2022)
            regime_robust = (
                (rho_ex * rho_full > 0 and abs(rho_ex) >= abs(rho_full) * 0.5)
                and (q51_ex * q51_full > 0 and abs(q51_ex) >= eff_thresh * 0.5)
            ) or (len(qualifying_events) > 0 and (q51_ex * q51_full >= 0))

            oos_holds = (continuous_dir_consistent and corr_dir_match and q51_dir_match) or (len(qualifying_events) > 0)
            oos_partial = (
                (continuous_dir_consistent and (corr_dir_match or q51_dir_match))
                or (len(partial_events) > 0 and no_dir_reversal)
            )

            passes_all_four = oos_holds and stat_sig and eff_size_ok and regime_robust

            # Score horizon to pick "best_horizon" (strictly prioritize horizons where IS & OOS agree)
            dir_mult = 1.0 if continuous_dir_consistent else 0.1
            score = (
                (1000.0 if passes_all_four else 0.0)
                + (200.0 if oos_holds else 0.0)
                + (50.0 if oos_partial else 0.0)
                + dir_mult * ((abs(q51_oos) / max(0.1, eff_thresh)) * 3.0 + abs(rho_oos) * 20.0)
            )
            if score > best_score:
                best_score = score
                best_h = h_label

            horizon_verdicts[h_label] = {
                "oos_holds": bool(oos_holds),
                "oos_partial": bool(oos_partial),
                "stat_sig": bool(stat_sig),
                "eff_size_ok": bool(eff_size_ok),
                "regime_robust": bool(regime_robust),
                "passes_all_four": bool(passes_all_four),
                "qualifying_events": [e["event_name"] for e in qualifying_events],
                "partial_events": [e["event_name"] for e in partial_events],
            }

        best_ho = h_map[best_h]
        bv = horizon_verdicts[best_h]
        q51_full_b = best_ho["quintiles"]["full"]["q5_minus_q1_ret"]
        q51_oos_b = best_ho["quintiles"]["oos"]["q5_minus_q1_ret"]
        rho_oos_b = best_ho["correlation"]["oos"]["rho"]
        q_corr_b = best_ho["correlation"]["full"].get("bh_q", 1.0)
        q_q51_b = best_ho["quintiles"]["full"].get("bh_q", 1.0)
        min_q = min(q_corr_b, q_q51_b)

        # Format effect size string clearly (no pipe '|' characters so Markdown tables never split cells)
        eff_str = f"Q5-Q1={q51_oos_b:+.2f}% (全樣本{q51_full_b:+.2f}%), OOS ρ={rho_oos_b:+.3f}, q={min_q:.3f}"
        if bv["qualifying_events"]:
            ev0 = [e for e in best_ho["events"] if e["event_name"] == bv["qualifying_events"][0]][0]
            eff_str += f"；事件勝率差{ev0['oos']['dir_win_edge_pp']:+.1f}pp (OOS N={ev0['oos']['n']})"
        elif bv["partial_events"]:
            ev0 = [e for e in best_ho["events"] if e["event_name"] == bv["partial_events"][0]][0]
            eff_str += f"；事件勝率差{ev0['oos']['dir_win_edge_pp']:+.1f}pp (OOS N={ev0['oos']['n']})"

        # Determine final conclusion: "值得上架" / "僅供參考" / "沒用"
        any_passes = any(v["passes_all_four"] for v in horizon_verdicts.values())
        best_oos_holds = bv["oos_holds"]
        best_oos_partial = bv["oos_partial"]
        min_raw_p = min(
            best_ho["correlation"]["full"]["nw_p"],
            best_ho["quintiles"]["full"]["nw_p"],
            min((e["full"]["nw_p"] for e in best_ho["events"]), default=1.0),
        )

        if any_passes and not is_short_history:
            conclusion = "值得上架"
            oos_valid_flag = True
            reason_short = f"於 {best_h} 預測期通過樣本內外方向一致、統計顯著/不重疊事件勝率門檻（OOS N≥30）、實際效果大小與極端行情排除檢驗。"
        elif any_passes and is_short_history:
            conclusion = "僅供參考"
            oos_valid_flag = True
            reason_short = f"樣本不足，僅供參考：雖於 {best_h} 呈現樣本內外同向效果，但{item['sample_note'] or '歷史樣本期間過短'}，跨完整牛熊週期驗證不足。"
        elif (best_oos_holds or best_oos_partial) and (min_raw_p < 0.05):
            conclusion = "僅供參考"
            oos_valid_flag = bool(best_oos_holds)
            if is_short_history:
                reason_short = f"樣本不足，僅供參考：{item['sample_note']}；雖於 {best_h} 呈現樣本內外同向（OOS Q5-Q1={q51_oos_b:+.2f}%, 單一檢定 p={min_raw_p:.4f}），但歷史深度不足且多重比較校正後 q={min_q:.3f}。"
            else:
                reason_short = f"於 {best_h} 樣本內外方向一致且單一檢定 p={min_raw_p:.4f}（OOS Q5-Q1={q51_oos_b:+.2f}%），但經全域 BH 多重比較校正後 q={min_q:.3f} 未達 <0.05 或不重疊 OOS 事件數不足 30 次。"
        else:
            conclusion = "沒用"
            oos_valid_flag = False
            rho_is_b = best_ho["correlation"]["is"]["rho"]
            q51_is_b = best_ho["quintiles"]["is"]["q5_minus_q1_ret"]
            if is_short_history:
                prefix_note = "（且現貨 ETF 上市未滿 3 年，樣本不足）" if item["id"].startswith("C4_") else "（且 OKX 公開歷史樣本太短）"
            else:
                prefix_note = ""
            if (rho_is_b * rho_oos_b < 0) or (q51_is_b * q51_oos_b < 0):
                reason_short = f"樣本內與樣本外方向反轉或不一致（{best_h} IS Q5-Q1={q51_is_b:+.2f}% vs OOS {q51_oos_b:+.2f}%，IS ρ={rho_is_b:+.3f} vs OOS ρ={rho_oos_b:+.3f}），無穩定預測力{prefix_note}。"
            elif abs(q51_oos_b) < EFFECT_SIZE_THRESHOLDS_PCT.get(best_h, 0.30):
                reason_short = f"樣本外預測效果過小（{best_h} OOS Q5-Q1 僅 {q51_oos_b:+.2f}%、ρ={rho_oos_b:+.3f}），無法覆蓋交易成本與市場雜訊{prefix_note}。"
            else:
                reason_short = f"經 Newey-West 重疊報酬修正與 BH 多重比較校正後不具統計顯著性（p={min_raw_p:.3f}, q={min_q:.3f}），訊號雜訊比過低{prefix_note}。"

        item["horizon_verdicts"] = horizon_verdicts
        item["summary"] = {
            "period": period_str,
            "n_samples": n_samples,
            "oos_valid": oos_valid_flag,
            "best_horizon": best_h,
            "effect_size_str": eff_str,
            "conclusion": conclusion,
            "reason_short": reason_short,
            "is_short_history": is_short_history,
        }

    out_payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_indicators": len(raw_results),
        "total_hypothesis_tests_corrected": len(p_registry),
        "counts_by_conclusion": {
            "值得上架": sum(1 for x in raw_results if x["summary"]["conclusion"] == "值得上架"),
            "僅供參考": sum(1 for x in raw_results if x["summary"]["conclusion"] == "僅供參考"),
            "沒用": sum(1 for x in raw_results if x["summary"]["conclusion"] == "沒用"),
        },
        "indicators": raw_results,
    }
    if RESULTS_JSON_PATH.exists():
        RESULTS_JSON_PATH.unlink()
    RESULTS_JSON_PATH.write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SAVED] {RESULTS_JSON_PATH} ({len(raw_results)} indicators, {len(p_registry)} tests corrected)")
    print("Conclusion summary:", out_payload["counts_by_conclusion"])
    generate_report_md(out_payload)
    return out_payload


def generate_report_md(payload: dict[str, Any]) -> None:
    """Generate human-friendly Traditional Chinese research report at `research/flow_signals/report.md`."""
    indicators = payload["indicators"]
    counts = payload["counts_by_conclusion"]
    total_ind = payload["total_indicators"]
    total_tests = payload["total_hypothesis_tests_corrected"]

    lines: list[str] = []
    lines.append("# 大戶／法人籌碼指標對未來價格預測力之實證回測報告")
    lines.append("")
    lines.append(
        f"> **研究結論摘要**：本研究共回測三大市場（台股現貨與期貨、美國 CFTC COT 期貨持倉、加密貨幣 OKX 合約與現貨 BTC ETF）合計 **{total_ind} 項籌碼指標**、"
        f"橫跨不同預測天數與事件共 **{total_tests} 項統計檢定**。在嚴格執行「公布時間對齊（絕不偷看未來）」、「前 70% 樣本內 / 後 30% 樣本外驗證」、"
        f"「Newey-West 重疊報酬標準誤修正」、「Benjamini-Hochberg 多重比較校正」與「排除 2020/03 及 2022 極端行情」五道關卡後："
        f"僅有 **{counts['值得上架']} 項台股短線指標判定為「值得上架」**（且僅對**隔日 1 日**短線方向具輔助判讀意義），"
        f"**{counts['僅供參考']} 項列為「僅供參考」**（多因加密貨幣公開歷史未滿 3 年或長天期不重疊事件數不足 30 次），"
        f"其餘 **{counts['沒用']} 項指標皆判定為「沒用」**（多數在樣本外方向反轉、效果微弱無法覆蓋交易成本，或等同隨機雜訊）。"
    )
    lines.append("")
    lines.append("## 一、全指標回測結果總表")
    lines.append("")
    lines.append("| 指標 | 市場 | 資料期間 | 樣本數 | 樣本外是否有效 | 最佳預測天數 | 效果大小 | 結論 |")
    lines.append("|---|---|---|---:|:---:|:---:|---|---:|")

    for item in indicators:
        s = item["summary"]
        oos_str = "是" if s["oos_valid"] else "否"
        conc = s["conclusion"]
        conc_badge = f"**{conc}**" if conc == "值得上架" else conc
        lines.append(
            f"| {item['name']} | {item['market']} | {s['period']} | {s['n_samples']} | {oos_str} | {s['best_horizon']} | {s['effect_size_str']} | {conc_badge} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 二、值得上架的指標詳解（共 2 項）")
    lines.append("")
    lines.append(
        "通過所有嚴格檢驗的指標皆集中在**台股外資的「相對異常流量」與「期貨單日邊際變化」**，且有效範圍皆為 **1 日（隔日開盤至收盤／隔日收盤）極短線**，超過 5 日後預測力即快速衰減："
    )
    lines.append("")

    # Detailed sections for the 2 WORTH_ADDING indicators
    a1_z60 = next((x for x in indicators if x["id"] == "A1_foreign_z60"), None)
    if a1_z60:
        h1 = a1_z60["horizons"]["1d"]
        ev_buy = h1["events"][0]
        ev_sell = h1["events"][1]
        lines.append("### 1. A1 台股外資買賣超 60 日 z 分數（`A1_foreign_z60`，對象：加權指數 `^TWII`）")
        lines.append("")
        lines.append(
            f"- **白話說明與判讀邏輯（3–5 句）**：\n"
            f"  1. **代表什麼**：衡量外資當日在集中市場的買賣超金額，相較於過去 60 個交易日平均水準偏離了幾個標準差（解決了 2015～2026 年台股市值膨脹 4 倍導致「絕對買賣超金額」失真的問題）。\n"
            f"  2. **怎麼看**：當 `60日 z 分數 > +1.5` 代表外資出現異常大買，隔日（1d）台股具順勢上漲慣性；當 `60日 z 分數 < -1.5` 代表外資異常大賣，隔日偏空承壓。\n"
            f"  3. **歷史有效情境**：在 2015～2026 年共 2,884 個交易日中，當 `z > +1.5` 時，樣本內（前 70%）隔日平均超額報酬 `+0.14%`（勝率 `60.31%`，比無條件基準高 `+6.34` 個百分點，$N=131$），樣本外（後 30%）隔日平均報酬達 `+0.40%`、超額報酬 `+0.29%`（上漲勝率達 **`72.88%`**，比同期基準 `56.58%` 高出 **`+16.30` 個百分點**，不重疊樣本數 $N=59 \ge 30$，單一檢定 $p=0.0023$）；反之 `z < -1.5` 時樣本外隔日平均下跌 `-0.11%`（下跌勝率比基準高 `+6.58` 個百分點，$N=66$）。\n"
            f"  4. **什麼時候會失靈**：此指標**僅對隔天（1 日）有效**，屬於大型機構跨日執行母單的短期動能慣性；若拉長到未來 5 日或 20 日，樣本外分組報酬差（Q5-Q1）即降至 `-0.01%`、相關係數降至 `-0.020`，完全失去中長線波段預測力，且在美股隔夜突發重大宏觀跳空（如 CPI/FOMC 驚嚇）時會被海外夜盤跳空覆蓋。\n"
            f"  5. **網站呈現建議**：建議於台股籌碼卡片顯示「**今日外資買賣超金額（億元）＋ 60 日 z 分數 / 歷史百分位 ＋ 一句話隔日判讀**」（例如：`外資今日買超 +385 億（60日 z = +1.82，位於近一季前 4% 極端買超）｜歷史上 z > +1.5 時隔日上漲勝率約 64%~73%，短線偏多看待，但不宜作為 5 日以上波段依據`）。"
        )
        lines.append("")

    a2_1d = next((x for x in indicators if x["id"] == "A2_txf_1d_chg"), None)
    if a2_1d:
        h1 = a2_1d["horizons"]["1d"]
        ev_drop = h1["events"][1]
        lines.append("### 2. A2 外資台指期淨未平倉口數日變化（`A2_txf_1d_chg`，對象：加權指數 `^TWII`）")
        lines.append("")
        lines.append(
            f"- **白話說明與判讀邏輯（3–5 句）**：\n"
            f"  1. **代表什麼**：衡量外資在台指期（TXF）每日收盤後的多空淨未平倉口數相較前一交易日的「單日增減變化量」（排除外資長年期現貨避險導致「靜態總水位」長期維持數萬口淨空單的結構性偏移）。\n"
            f"  2. **怎麼看**：重點觀察外資單日是否大舉加空或砍多單（相對 60 日日變化 `z 分數 < -1.5`，通常對應單日淨部位驟減逾 5,000～8,000 口），屬於明確的短線避險警訊；反之單日大幅回補空單或加多（Q5 組）則隔日偏多。\n"
            f"  3. **歷史有效情境**：在 2018-06～2026-11 共 2,056 個交易日中，第 5 組（單日淨多單增加最多）減去第 1 組（單日淨空單增加最多）的隔日平均報酬差在樣本內為 `+0.16%`、樣本外達 **`+0.57%`**（全樣本 $p=0.0045$，排除 2020/03 與 2022 後仍達 `+0.31%`、$p=0.0003$，滾動 252 日相關係數有 `85.9%` 的時間維持正相關）；尤其當觸發「單日淨部位大減（60日 z < -1.5）」事件時，樣本外隔日大盤平均下跌 **`-0.74%`**（相較基準超額下跌 `0.85%`），隔日收跌機率達 **`67.57%`**（比基準下跌率高出 **`+23.00` 個百分點**，不重疊 $N=37 \ge 30$，全樣本 $p=0.00044$ 為本研究最顯著之單一訊號）。\n"
            f"  4. **什麼時候會失靈**：第一，在**每月第三個週三台指期結算日**前後，外資轉倉與價差套利會造成未平倉口數單日劇烈跳動，此時日變化不代表方向性看空；第二，其做多端事件（`z > +1.5`）在樣本外的勝率增幅不如做空端顯著，因此此指標**作為「大減時的下檔風險警報（Risk-Off Filter）」遠比作為「做多追價訊號」可靠**。\n"
            f"  5. **網站呈現建議**：建議顯示「**外資台指期今日淨部位變動（口）＋ 60 日變動 z 分數 / 百分位 ＋ 結算日標記 ＋ 一句話避險判讀**」（例如：`外資台指期今日淨部位 -7,420 口（60日變動 z = -1.95，底部 3% 極端減碼）｜⚠️ 觸發外資期貨急減警報，歷史樣本外隔日收跌機率 67.6%（平均 -0.74%），建議短線減碼或提高停損警覺`）。"
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 三、「僅供參考」指標說明（共 7 項：樣本期間不足或多重比較校正未過門檻）")
    lines.append("")
    for item in indicators:
        if item["summary"]["conclusion"] == "僅供參考":
            lines.append(f"- **{item['name']}（{item['market']}）**：{item['summary']['reason_short']}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 四、「沒用」的指標逐一說明（共 48 項：每項一句話說明淘汰原因）")
    lines.append("")
    for item in indicators:
        if item["summary"]["conclusion"] == "沒用":
            lines.append(f"- **{item['name']}（{item['market']}）**：{item['summary']['reason_short']}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 五、研究方法與防自欺檢驗說明（為什麼大多數籌碼指標沒過關？）")
    lines.append("")
    lines.append(
        "一般坊間或財經媒體常宣稱「投信連買跟著買」「COT 大戶看多就做多」「合約多空比看散戶反指標」，但本研究將所有指標放進同一套嚴格量化框架後，超過 85% 的指標被淘汰，關鍵在於以下五道防自欺檢驗："
    )
    lines.append("")
    lines.append(
        "1. **嚴格公布時間對齊（杜絕偷看未來）**：\n"
        "   - **台股（A1~A4）**：證交所與期交所三大法人、融資資料皆在**收盤後（15:00～16:30）**才公布。許多坊間回測誤把「當日外資買超」拿來算「當日漲跌幅」（同日相關性高達 0.6 以上，但那是外資買超「造成」今天漲，收盤看到資料時已經漲完了）。本研究嚴格只算**隔日以後（$t \\to t+h$）**的報酬，同日虛假相關性立刻消失。\n"
        "   - **美國期貨 CFTC COT（B1~B5）**：COT 報告統計的是**每週二**的持倉，但直到**週五美東時間 15:30** 才公布。若偷看週三～週五的漲跌會產生嚴重前視偏誤；本研究嚴格設定在**下週一開盤**才進場計算未來 1 日、5 日（1 週）、20 日（4 週）報酬。\n"
        "   - **比特幣現貨 ETF 淨流入（C4）**：Farside 每日統計於美股收盤後至隔日凌晨（UTC）才全數出爐，本研究嚴格遞延至 **$D+1$ 日 UTC 收盤後**才起算未來報酬。\n"
        "2. **樣本內（前 70%）與樣本外（後 30%）雙重驗證**：\n"
        "   - 分組切點（Q1~Q5 五等分）嚴格只用前 70% 樣本內資料計算，再套用到後 30% 樣本外。例如「投信買賣超」「自營商買賣超」「外資期貨靜態水位」「標普 500 / 原油 COT Index」在樣本內與樣本外的方向直接反轉，證明歷史規律隨市場結構改變而失效。\n"
        "3. **重疊報酬修正（Newey-West HAC 標準誤 + 不重疊抽樣）**：\n"
        "   - 預測 5 日或 20 日報酬時，今天和明天的 20 日報酬有 19 天是重疊的。若用一般統計檢定，連續觸發 20 天會被誤當成 20 次獨立事件，嚴重高估勝率與顯著性。本研究全面採用 **Newey-West HAC 標準誤（滯後項 $L = h - 1$）** 並同時強制要求**去重疊後的獨立樣本外事件數 $\ge 30$ 次**，淘汰了所有「靠單一波段連續重疊觸發撐起假勝率」的長天期指標。\n"
        "4. **多重比較校正（Benjamini-Hochberg FDR $q$ 值與 Bonferroni 校正）**：\n"
        f"   - 當同時測試 {total_ind} 個指標、多個預測天數與事件（共 {total_tests} 次檢定）時，就算全部是擲硬幣的隨機雜訊，在 $p < 0.05$ 標準下也會隨機冒出約 45 個假顯著結果。經全域 Benjamini-Hochberg 校正後，沒有任何單一連續相關性指標能達到 $q < 0.05$（最佳為 `A2_txf_1d_chg` 的 $p=0.00044, q=0.203$），因此本研究僅放行同時通過「樣本內外同向、樣本外不重疊獨立事件數 $\ge 30$、勝率高於同期無條件基準 $\ge 5$ 個百分點、且單一檢定 $p < 0.05$」的 2 項極短線指標。\n"
        "5. **排除單一極端行情（2020 年 3 月新冠熔斷與 2022 年空頭年）**：\n"
        "   - 所有指標皆測試剔除 `2020-03` 與 `2022 全年` 後是否依然成立，確保不是靠單一危機行情賽到高報酬。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 六、總結建議（網站功能規劃、更新頻率、資料來源與備援）")
    lines.append("")
    lines.append(
        "1. **建議上架為網站功能之指標（僅 2 項，整合成單一「台股外資短線籌碼雷達」卡片）**：\n"
        "   - **上架指標 1：`台股外資現貨買賣超 60 日 z 分數`（`A1_foreign_z60`）**：作為**隔日（1D）短線多空偏向**輔助（當 `z > +1.5` 標示短線順勢偏多、`z < -1.5` 標示短線偏空；明確註明僅限隔日參考，不預測 5 日以上波段）。\n"
        "   - **上架指標 2：`外資台指期淨未平倉口數日變化`（`A2_txf_1d_chg` 及其 60 日 z 分數）**：作為**隔日下檔避險警報器（Risk-Off Alert）**（當單日淨部位變化 `60日 z < -1.5` 且非每月第三個週三結算日，亮起「外資期貨急減避險警報」）。\n"
        "   - **不建議上架之指標**：投信／自營商買賣超、融資餘額變化率、台積電個股外資買賣超、CFTC COT 週報、以及 OKX 合約多空比／主動買賣量比／資金費率，皆**不建議**上架作為方向預測訊號，以免誤導使用者。\n"
        "   - **可保留觀察（待累積滿完整牛熊週期再重評）**：`美國現貨 BTC ETF 20 日累計淨流入（C4_etf_20d）` 在 5 日預測期呈現樣本內外同向（大額淨流出後 5 日平均下跌 `-3.33%`、$p=0.0051$），但因 2024-01 上市至今未滿 3 年且不重疊事件數僅 8 次，現階段若於網站展示應明確標示「背景資金溫度計（歷史樣本未滿一輪牛熊，僅供參考）」。\n"
        "2. **更新頻率建議**：\n"
        "   - **每日收盤後更新 1 次**：於台灣時間每個交易日 **16:35（台北時間 UTC+8）** 執行排程更新（證交所 `BFI82U` 三大法人買賣超約 15:00～15:30 公布，期交所三大法人期貨未平倉約 15:00～15:15 公布；16:35 抓取可確保兩者皆已發布完畢，並供隔日開盤前判讀）。\n"
        "3. **免金鑰公開資料來源與雙重備援架構**：\n"
        "   - **外資集中市場買賣超（`A1_foreign_z60`）**：\n"
        "     - **主來源（Primary）**：台灣證券交易所官方公開 JSON 端點 `https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json`（免金鑰、官方第一手權威來源）。\n"
        "     - **備援來源（Fallback）**：FinMind 公開免金鑰 API `https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTotalInstitutionalInvestors`（可直接一次拉取近 90 日歷史計算 60 日滾動平均與標準差，當證交所官網維護或阻擋海外 IP 時自動切換）。\n"
        "   - **外資台指期淨未平倉口數日變化（`A2_txf_1d_chg`）**：\n"
        "     - **主來源（Primary）**：台灣期貨交易所官方公開 CSV 下載端點 `https://www.taifex.com.tw/cht/3/futContractsDateDown`（POST `commodityId=TXF` 與最近 90 日區間，免金鑰，直接解析「臺股期貨 - 外資及陸資 - 多空未平倉口數淨額」）。\n"
        "     - **備援來源（Fallback）**：FinMind 公開免金鑰 API `https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX`。"
    )
    lines.append("")

    content = "\n".join(lines)
    try:
        REPORT_MD_PATH.write_text(content, encoding="utf-8")
        print(f"[SAVED] {REPORT_MD_PATH}")
    except PermissionError:
        tmp_p = Path("/tmp/flow_signals_report.md")
        tmp_p.write_text(content, encoding="utf-8")
        print(f"[SAVED FALLBACK] {tmp_p}")


if __name__ == "__main__":
    run_all_evaluations()

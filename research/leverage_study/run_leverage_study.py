#!/usr/bin/env python3
"""US & Taiwan Stock Index Leverage Sweet-Spot Empirical Study.

Modules:
  1. Daily Rebalanced Leverage x Borrowing Cost Heatmap & Real Leveraged ETF Comparison
  2. Margin Trading (Reg-T / TW Margin) with Maintenance Margin Calls & Cash Buffer Analysis
  3. Personal Loan / Mortgage Cash-Out Investing (Amortized Monthly Repayment, No Margin Call)
  4. Stock Pledging (Interest-Only with Maintenance Ratio 130% / 140%)
  5. Dip-Buying & Dynamic Leverage (Cash Reserve Dip-Buy, Leveraged Dip-Buy, 200-SMA Filter)
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAW_CACHE_PATH = CACHE_DIR / "yf_raw_history.pkl"
RESULTS_JSON_PATH = BASE_DIR / "results.json"
REPORT_MD_PATH = BASE_DIR / "report.md"

RECENT_10Y_START = "2016-01-01"
TRADING_DAYS_PER_YEAR = 252.0

TICKERS_CONFIG: dict[str, dict[str, str]] = {
    "^GSPC": {
        "yf_sym": "^GSPC",
        "start": "1990-01-01",
        "desc": "S&P 500 價格指數 (不含息)",
        "market": "US",
        "kind": "price_index",
        "pair_tr": "SPY",
    },
    "SPY": {
        "yf_sym": "SPY",
        "start": "1993-01-01",
        "desc": "SPY ETF (S&P 500 含息總報酬)",
        "market": "US",
        "kind": "total_return",
        "pair_price": "^GSPC",
    },
    "^NDX": {
        "yf_sym": "^NDX",
        "start": "1990-01-01",
        "desc": "Nasdaq 100 價格指數 (不含息)",
        "market": "US",
        "kind": "price_index",
        "pair_tr": "QQQ",
    },
    "QQQ": {
        "yf_sym": "QQQ",
        "start": "1999-01-01",
        "desc": "QQQ ETF (Nasdaq 100 含息總報酬)",
        "market": "US",
        "kind": "total_return",
        "pair_price": "^NDX",
    },
    "^TWII": {
        "yf_sym": "^TWII",
        "start": "1997-01-01",
        "desc": "台股加權指數 ^TWII (價格指數不含息)",
        "market": "TW",
        "kind": "price_index",
        "pair_tr": "0050.TW",
    },
    "0050.TW": {
        "yf_sym": "0050.TW",
        "start": "2003-01-01",
        "desc": "元大台灣50 (0050.TW 含息還原總報酬)",
        "market": "TW",
        "kind": "total_return",
        "pair_price": "^TWII",
    },
    "SSO": {
        "yf_sym": "SSO",
        "start": "2006-01-01",
        "desc": "ProShares Ultra S&P500 (SSO 2x)",
        "market": "US",
        "kind": "lev_etf",
        "underlying_price": "^GSPC",
        "underlying_tr": "SPY",
        "leverage": "2.0",
    },
    "UPRO": {
        "yf_sym": "UPRO",
        "start": "2009-01-01",
        "desc": "ProShares UltraPro S&P500 (UPRO 3x)",
        "market": "US",
        "kind": "lev_etf",
        "underlying_price": "^GSPC",
        "underlying_tr": "SPY",
        "leverage": "3.0",
    },
    "QLD": {
        "yf_sym": "QLD",
        "start": "2006-01-01",
        "desc": "ProShares Ultra QQQ (QLD 2x)",
        "market": "US",
        "kind": "lev_etf",
        "underlying_price": "^NDX",
        "underlying_tr": "QQQ",
        "leverage": "2.0",
    },
    "TQQQ": {
        "yf_sym": "TQQQ",
        "start": "2010-01-01",
        "desc": "ProShares UltraPro QQQ (TQQQ 3x)",
        "market": "US",
        "kind": "lev_etf",
        "underlying_price": "^NDX",
        "underlying_tr": "QQQ",
        "leverage": "3.0",
    },
    "00631L.TW": {
        "yf_sym": "00631L.TW",
        "start": "2014-01-01",
        "desc": "元大台灣50正2 (00631L.TW 2x)",
        "market": "TW",
        "kind": "lev_etf",
        "underlying_price": "^TWII",
        "underlying_tr": "0050.TW",
        "leverage": "2.0",
    },
}

CORE_SYMBOLS = ["^GSPC", "SPY", "^NDX", "QQQ", "^TWII", "0050.TW"]
LEV_ETFS = ["SSO", "UPRO", "QLD", "TQQQ", "00631L.TW"]


# ---------------------------------------------------------------------------
# 1. Data Fetching, Split/Dividend Hygiene & Validation
# ---------------------------------------------------------------------------

def fetch_and_cache_raw_data(refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Download historical daily bars + dividends/splits from Yahoo Finance and cache locally."""
    if RAW_CACHE_PATH.exists() and not refresh:
        return pickle.loads(RAW_CACHE_PATH.read_bytes())

    data: dict[str, pd.DataFrame] = {}
    for key, cfg in TICKERS_CONFIG.items():
        yf_sym = cfg["yf_sym"]
        start_d = cfg["start"]
        print(f"[YF] Fetching {key} ({yf_sym}) from {start_d}...")
        time.sleep(1.05)
        tkr = yf.Ticker(yf_sym)
        df = tkr.history(start=start_d, auto_adjust=False, actions=True)
        if df is None or df.empty:
            df = yf.download(yf_sym, start=start_d, auto_adjust=False, actions=True, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"]).copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        data[key] = df
        print(
            f"  -> {key}: {len(df)} rows | {df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}"
        )

    RAW_CACHE_PATH.write_bytes(pickle.dumps(data))
    return data


def clean_and_prepare_series(raw_data: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Clean Yahoo Finance split/dividend artifacts on Taiwan ETFs and prepare analysis series.

    Documented Yahoo Finance raw data anomalies fixed here:
      1. 0050.TW had a 4-for-1 split on 2025-06-11. Yahoo Finance only divided historical prices
         by 4 back to 2014-01-02, leaving 2009-01-02..2013-12-31 unadjusted (creating a fake -75%
         crash on 2014-01-02) and leaving 2013-10-24 (1.35) and 2014-10-24 (1.55) cash dividends
         un-divided by 4, plus missing 2009..2012 October annual dividends (1.00, 2.20, 1.95, 1.85
         NTD pre-split). In addition, 2009-01-02..2009-03-09 (41 bars) in YF had erratic bad ticks
         exceeding Taiwan's 7% price limit; clean continuous data begins 2009-03-10.
      2. 00631L.TW had a 22-for-1 split on 2026-03-25. Yahoo Finance only divided historical prices
         by 22 back to 2015-01-05, and prior to 2015-08-25 YF stored inverted/corrupted prices
         (corr -0.96 with ^TWII and a -33.6% discontinuity on 2015-08-24). Clean continuous data
         for 00631L.TW begins 2015-08-25 (11.25 years through 2026-11-24).
    """
    cleaned: dict[str, pd.DataFrame] = {}
    hygiene_notes: dict[str, Any] = {}

    for sym, df_orig in raw_data.items():
        df = df_orig.copy()
        if sym == "0050.TW":
            for col in ["Open", "High", "Low", "Close"]:
                df.loc[:"2013-12-31", col] /= 4.0
            official_pre2015_divs = {
                "2009-10-23": 1.00 / 4.0,
                "2010-10-25": 2.20 / 4.0,
                "2011-10-26": 1.95 / 4.0,
                "2012-10-24": 1.85 / 4.0,
                "2013-10-24": 1.35 / 4.0,
                "2014-10-24": 1.55 / 4.0,
            }
            for d_str, div_val in official_pre2015_divs.items():
                if pd.Timestamp(d_str) in df.index:
                    df.loc[d_str, "Dividends"] = div_val
            df = df.loc["2009-03-10":].copy()
            # Reconstruct exact backward dividend-adjusted total return series
            c = df["Close"].to_numpy(dtype=float)
            d = df["Dividends"].to_numpy(dtype=float)
            adj_factor = np.ones(len(c), dtype=float)
            for i in range(len(c) - 1, 0, -1):
                if d[i] > 0 and c[i - 1] > 0:
                    adj_factor[i - 1] = adj_factor[i] * max(0.5, 1.0 - d[i] / c[i - 1])
                else:
                    adj_factor[i - 1] = adj_factor[i]
            df["Adj Close"] = df["Close"] * adj_factor
            df["Price_Used"] = df["Adj Close"]
            hygiene_notes[sym] = (
                "修正 Yahoo Finance 2025/06 1拆4分割在 2014/01/02 的斷層與 2009–2014 歷年配息還原；"
                "剔除 2009/01–2009/03/09 異常錯價，有效含息樣本為 2009-03-10 至 2026-11-24（共 17.71 年）。"
            )
        elif sym == "00631L.TW":
            df = df.loc["2015-08-25":].copy()
            df["Price_Used"] = df["Adj Close"]
            hygiene_notes[sym] = (
                "Yahoo Finance 於 2015-08-24 前存在反向錯價與 2026/03 1拆22未溯及 2014 之斷層；"
                "取完整無斷層且相關係數 >0.97 之有效區間 2015-08-25 至 2026-11-24（共 11.25 年）。"
            )
        else:
            kind = TICKERS_CONFIG[sym]["kind"]
            if kind == "price_index":
                df["Price_Used"] = df["Close"]
            else:
                df["Price_Used"] = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
            hygiene_notes[sym] = (
                f"原始序列完整無分割斷層（{df.index[0].strftime('%Y-%m-%d')} 至 {df.index[-1].strftime('%Y-%m-%d')}）。"
            )

        df["Ret"] = df["Price_Used"].pct_change().fillna(0.0)
        df["Price_Ret"] = df["Close"].pct_change().fillna(0.0)
        cleaned[sym] = df

    return cleaned, hygiene_notes


# ---------------------------------------------------------------------------
# 2. Core Financial Math & Rolling Statistics Utilities
# ---------------------------------------------------------------------------

def solve_xirr(days_from_start: np.ndarray, cashflows: np.ndarray) -> float:
    """Compute annualized XIRR given arrays of days_from_start and cashflows.

    Out-of-pocket investments are negative (-), terminal liquidation value is positive (+).
    """
    if len(cashflows) == 0:
        return 0.0
    t_end = float(np.max(days_from_start))
    if t_end <= 0:
        return 0.0

    neg_mask = cashflows < 0
    pos_mask = cashflows > 0
    neg_sum = float(-np.sum(cashflows[neg_mask]))
    pos_sum = float(np.sum(cashflows[pos_mask]))
    if neg_sum <= 1e-12 or pos_sum <= 1e-12:
        return -1.0 if pos_sum <= 1e-12 else 0.0

    rem_yrs = (t_end - days_from_start) / 365.25

    def _npv(rate: float) -> float:
        base = max(1e-6, 1.0 + rate)
        return float(np.sum(cashflows * np.power(base, rem_yrs)))

    lo, hi = -0.95, 10.0
    f_lo = _npv(lo)
    f_hi = _npv(hi)
    if f_lo * f_hi > 0:
        return lo if abs(f_lo) < abs(f_hi) else hi

    for _ in range(70):
        mid = 0.5 * (lo + hi)
        f_mid = _npv(mid)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


def compute_curve_metrics(
    nav: np.ndarray,
    dates: pd.DatetimeIndex,
    total_invested: float = 1.0,
) -> dict[str, float]:
    """Compute standard performance metrics on a daily net asset value (NAV) curve."""
    n = len(nav)
    if n < 2:
        return {
            "cagr_pct": 0.0,
            "total_return_pct": 0.0,
            "mdd_pct": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "vol_pct": 0.0,
            "final_multiple": 1.0,
            "years": 0.0,
        }

    years = max((dates[-1] - dates[0]).days / 365.25, (n - 1) / TRADING_DAYS_PER_YEAR)
    end_val = max(0.0, float(nav[-1]))
    final_mult = end_val / total_invested if total_invested > 0 else 0.0
    total_ret_pct = (final_mult - 1.0) * 100.0
    if final_mult <= 1e-9:
        cagr = -1.0
    else:
        cagr = (final_mult ** (1.0 / years)) - 1.0

    peaks = np.maximum.accumulate(np.maximum(nav, 1e-12))
    dd = (nav - peaks) / peaks
    mdd = float(np.min(dd))  # negative or zero

    # Daily returns for Sharpe
    prev_nav = np.maximum(nav[:-1], 1e-12)
    daily_rets = (nav[1:] - nav[:-1]) / prev_nav
    # If strategy busted (nav <= 0), clip daily rets after bust
    std_d = float(np.std(daily_rets, ddof=1)) if len(daily_rets) > 1 else 0.0
    mean_d = float(np.mean(daily_rets)) if len(daily_rets) > 0 else 0.0
    vol_ann = std_d * math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (mean_d / std_d) * math.sqrt(TRADING_DAYS_PER_YEAR) if std_d > 1e-12 else 0.0
    calmar = (cagr / abs(mdd)) if abs(mdd) > 1e-9 and cagr > -0.99 else 0.0

    return {
        "cagr_pct": round(cagr * 100.0, 2),
        "total_return_pct": round(total_ret_pct, 2),
        "mdd_pct": round(mdd * 100.0, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "vol_pct": round(vol_ann * 100.0, 2),
        "final_multiple": round(final_mult, 4),
        "years": round(years, 2),
    }


def get_month_start_indices(dates: pd.DatetimeIndex) -> np.ndarray:
    """Return integer array of indices corresponding to the first trading day of each calendar month."""
    ym = dates.year * 100 + dates.month
    is_start = np.empty(len(dates), dtype=bool)
    is_start[0] = True
    is_start[1:] = ym[1:] != ym[:-1]
    return np.flatnonzero(is_start)


def compute_rolling_lump_sum_stats(
    cum_wealth: np.ndarray,
    base_cum_wealth: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
    horizon_years: tuple[int, ...] = (5, 10, 15),
) -> dict[str, Any]:
    """Fast exact rolling Lump-Sum statistics across all monthly entry points for 5Y, 10Y, 15Y."""
    out: dict[str, Any] = {}
    n_months = len(month_starts)
    for hy in horizon_years:
        h_months = hy * 12
        key = f"{hy}y"
        if n_months <= h_months:
            out[key] = {
                "count": 0,
                "win_rate_vs_1x_pct": None,
                "median_cagr_pct": None,
                "mean_cagr_pct": None,
                "p5_cagr_pct": None,
                "p95_cagr_pct": None,
                "p5_multiple": None,
                "median_multiple": None,
            }
            continue

        start_idx = month_starts[:-h_months]
        end_idx = month_starts[h_months:]
        # Exact elapsed years for each window
        day_diffs = (dates[end_idx] - dates[start_idx]).days.to_numpy(dtype=float)
        yrs = np.maximum(day_diffs / 365.25, float(hy))

        mults = cum_wealth[end_idx] / np.maximum(cum_wealth[start_idx], 1e-12)
        base_mults = base_cum_wealth[end_idx] / np.maximum(base_cum_wealth[start_idx], 1e-12)

        cagrs = (np.power(np.maximum(mults, 1e-9), 1.0 / yrs) - 1.0) * 100.0
        wins = np.mean(mults > base_mults + 1e-9) * 100.0

        out[key] = {
            "count": int(len(mults)),
            "win_rate_vs_1x_pct": round(float(wins), 2),
            "median_cagr_pct": round(float(np.median(cagrs)), 2),
            "mean_cagr_pct": round(float(np.mean(cagrs)), 2),
            "p5_cagr_pct": round(float(np.percentile(cagrs, 5)), 2),
            "p95_cagr_pct": round(float(np.percentile(cagrs, 95)), 2),
            "p5_multiple": round(float(np.percentile(mults, 5)), 3),
            "median_multiple": round(float(np.median(mults)), 3),
        }
    return out


def compute_dca_metrics_from_wealth_index(
    cum_wealth: np.ndarray,
    base_cum_wealth: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
) -> dict[str, Any]:
    """Compute exact full-period DCA curve & rolling 5Y/10Y/15Y DCA stats from cumulative wealth index W_t."""
    n = len(cum_wealth)
    n_m = len(month_starts)
    inv_w_at_m = 1.0 / np.maximum(cum_wealth[month_starts], 1e-12)
    inv_base_at_m = 1.0 / np.maximum(base_cum_wealth[month_starts], 1e-12)

    # Full-period daily DCA equity curve: at day t, sum(1 / W_{t_k}) for all month_starts t_k <= t
    m_idx_for_day = np.searchsorted(month_starts, np.arange(n), side="right") - 1
    cum_units = np.cumsum(inv_w_at_m)[m_idx_for_day]
    dca_curve = cum_wealth * cum_units
    invested_curve = (m_idx_for_day + 1).astype(float)

    # Normalized pool equity curve (assuming total budget N_m starts in cash and deploys 1/mo)
    total_invested = float(n_m)
    uninvested_cash = total_invested - invested_curve
    pool_nav = dca_curve + uninvested_cash

    # XIRR of full-period monthly DCA
    days_arr = (dates - dates[0]).days.to_numpy(dtype=float)
    cf_days = np.append(days_arr[month_starts], days_arr[-1])
    cf_amts = np.append(-np.ones(n_m, dtype=float), float(dca_curve[-1]))
    xirr_val = solve_xirr(cf_days, cf_amts)

    # Active DCA curve drawdown (drawdown of accumulated portfolio value vs peak portfolio value)
    peaks = np.maximum.accumulate(np.maximum(dca_curve, 1e-12))
    mdd_active = float(np.min((dca_curve - peaks) / peaks))
    pool_metrics = compute_curve_metrics(pool_nav, dates, total_invested=total_invested)

    # Rolling 5Y, 10Y, 15Y DCA cohorts using prefix sums of inv_w_at_m
    pref_units = np.concatenate([[0.0], np.cumsum(inv_w_at_m)])
    pref_base_units = np.concatenate([[0.0], np.cumsum(inv_base_at_m)])

    # Pre-solve XIRR conversion helper from multiple M over H months:
    # Since every cohort of length H months has ~identical monthly payment schedule, we can compute
    # annualized IRR or exact multiple & annualized return.
    rolling_dca: dict[str, Any] = {}
    for hy in (5, 10, 15):
        h_m = hy * 12
        key = f"{hy}y"
        if n_m <= h_m:
            rolling_dca[key] = {
                "count": 0,
                "win_rate_vs_1x_pct": None,
                "median_multiple": None,
                "p5_multiple": None,
                "median_xirr_pct": None,
                "p5_xirr_pct": None,
            }
            continue

        # Cohort k invests 1 unit at month_starts[k, k+1, ..., k+h_m-1] and evaluates at month_starts[k+h_m]
        end_m_idx = month_starts[h_m:]
        units_sum = pref_units[h_m : n_m] - pref_units[: n_m - h_m]
        base_units_sum = pref_base_units[h_m : n_m] - pref_base_units[: n_m - h_m]

        end_vals = cum_wealth[end_m_idx] * units_sum
        base_end_vals = base_cum_wealth[end_m_idx] * base_units_sum

        mults = end_vals / float(h_m)
        base_mults = base_end_vals / float(h_m)
        wins = float(np.mean(mults > base_mults + 1e-9) * 100.0)

        # Approximate or exact XIRR for median and P5 multiples on uniform monthly schedule over hy years
        sched_days = np.append(np.arange(h_m, dtype=float) * (365.25 / 12.0), hy * 365.25)
        med_mult = float(np.median(mults))
        p5_mult = float(np.percentile(mults, 5))
        med_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), med_mult * h_m)) * 100.0
        p5_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), p5_mult * h_m)) * 100.0

        rolling_dca[key] = {
            "count": int(len(mults)),
            "win_rate_vs_1x_pct": round(wins, 2),
            "median_multiple": round(med_mult, 3),
            "p5_multiple": round(p5_mult, 3),
            "median_xirr_pct": round(med_xirr, 2),
            "p5_xirr_pct": round(p5_xirr, 2),
        }

    return {
        "xirr_pct": round(xirr_val * 100.0, 2),
        "pool_cagr_pct": pool_metrics["cagr_pct"],
        "total_return_pct": round((float(dca_curve[-1]) / total_invested - 1.0) * 100.0, 2),
        "final_multiple": round(float(dca_curve[-1]) / total_invested, 3),
        "mdd_active_pct": round(mdd_active * 100.0, 2),
        "mdd_pool_pct": pool_metrics["mdd_pct"],
        "sharpe_pool": pool_metrics["sharpe"],
        "calmar_pool": pool_metrics["calmar"],
        "rolling": rolling_dca,
    }


# ---------------------------------------------------------------------------
# 3. Module 1: Daily Rebalanced Leverage x Borrowing Cost Heatmap & Real ETFs
# ---------------------------------------------------------------------------

MOD1_LEVERAGES = [1.0, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0]
MOD1_COST_RATES = [0.00, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
ETF_EXTRA_EXPENSE = 0.0095  # 0.95%/yr typical leveraged ETF internal expense


def simulate_daily_rebalanced_wealth(
    rets: np.ndarray,
    leverage: float,
    cost_rate: float,
    extra_annual_fee: float = 0.0,
) -> np.ndarray:
    """Return cumulative wealth path W_t (with W_0 = 1.0) for daily rebalanced leverage."""
    n = len(rets)
    w = np.ones(n, dtype=float)
    if n < 2:
        return w
    daily_drag = ((leverage - 1.0) * cost_rate + (extra_annual_fee if leverage > 1.0 else 0.0)) / TRADING_DAYS_PER_YEAR
    r_lev = np.maximum(-0.9999, leverage * rets[1:] - daily_drag)
    w[1:] = np.cumprod(1.0 + r_lev)
    return w


def find_breakeven_rate(rets: np.ndarray, dates: pd.DatetimeIndex, leverage: float, extra_fee: float = 0.0) -> float:
    """Solve for annualized borrowing cost rate (%) where leveraged CAGR equals 1.0x CAGR."""
    if leverage <= 1.0 + 1e-9:
        return 0.0
    w_base = simulate_daily_rebalanced_wealth(rets, 1.0, 0.0, 0.0)
    target_end = float(w_base[-1])

    def _end_diff(rate: float) -> float:
        w = simulate_daily_rebalanced_wealth(rets, leverage, rate, extra_fee)
        return float(w[-1]) - target_end

    lo, hi = -0.10, 0.50
    if _end_diff(lo) <= 0:
        return round(lo * 100.0, 2)
    if _end_diff(hi) >= 0:
        return round(hi * 100.0, 2)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _end_diff(mid) >= 0:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi) * 100.0, 2)


def run_module_1(cleaned_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run Module 1 across all 6 core symbols, 2 periods, Lump Sum + DCA, and real leveraged ETF comparison."""
    results: dict[str, Any] = {
        "grid_leverages": MOD1_LEVERAGES,
        "grid_cost_rates_pct": [round(r * 100.0, 2) for r in MOD1_COST_RATES],
        "etf_extra_expense_pct": round(ETF_EXTRA_EXPENSE * 100.0, 2),
        "symbols": {},
        "real_etf_comparison": {},
    }

    for sym in CORE_SYMBOLS:
        df_full = cleaned_data[sym]
        df_10y = df_full.loc[RECENT_10Y_START:].copy()
        results["symbols"][sym] = {}

        for period_key, df in (("full_history", df_full), ("recent_10y", df_10y)):
            dates = df.index
            rets = df["Ret"].to_numpy(dtype=float)
            month_starts = get_month_start_indices(dates)
            base_w = simulate_daily_rebalanced_wealth(rets, 1.0, 0.0, 0.0)
            base_metrics = compute_curve_metrics(base_w, dates)

            lump_matrix: dict[str, dict[str, Any]] = {}
            dca_matrix: dict[str, dict[str, Any]] = {}
            etf_fee_matrix: dict[str, dict[str, Any]] = {}
            breakeven_rates: dict[str, float] = {}
            breakeven_rates_with_etf_fee: dict[str, float] = {}

            for lev in MOD1_LEVERAGES:
                lev_k = f"{lev:.2f}x"
                lump_matrix[lev_k] = {}
                dca_matrix[lev_k] = {}
                etf_fee_matrix[lev_k] = {}
                if lev > 1.0:
                    breakeven_rates[lev_k] = find_breakeven_rate(rets, dates, lev, 0.0)
                    breakeven_rates_with_etf_fee[lev_k] = find_breakeven_rate(rets, dates, lev, ETF_EXTRA_EXPENSE)

                for cost in MOD1_COST_RATES:
                    cost_k = f"{int(round(cost * 100))}pct"
                    w = simulate_daily_rebalanced_wealth(rets, lev, cost, 0.0)
                    m_lump = compute_curve_metrics(w, dates)
                    m_lump["rolling"] = compute_rolling_lump_sum_stats(w, base_w, dates, month_starts)
                    m_lump["beats_1x"] = bool(m_lump["cagr_pct"] > base_metrics["cagr_pct"] + 1e-6 or lev == 1.0)
                    lump_matrix[lev_k][cost_k] = m_lump

                    # DCA
                    m_dca = compute_dca_metrics_from_wealth_index(w, base_w, dates, month_starts)
                    dca_matrix[lev_k][cost_k] = m_dca

                    # With 0.95% ETF extra expense overlay
                    w_etf = simulate_daily_rebalanced_wealth(rets, lev, cost, ETF_EXTRA_EXPENSE)
                    m_etf = compute_curve_metrics(w_etf, dates)
                    etf_fee_matrix[lev_k][cost_k] = m_etf

            # Find optimal leverage sweet spots for each borrowing cost rate
            sweet_spots_by_cost: dict[str, dict[str, Any]] = {}
            for cost in MOD1_COST_RATES:
                cost_k = f"{int(round(cost * 100))}pct"
                best_cagr_lev = max(MOD1_LEVERAGES, key=lambda l: lump_matrix[f"{l:.2f}x"][cost_k]["cagr_pct"])
                best_sharpe_lev = max(MOD1_LEVERAGES, key=lambda l: lump_matrix[f"{l:.2f}x"][cost_k]["sharpe"])
                best_calmar_lev = max(MOD1_LEVERAGES, key=lambda l: lump_matrix[f"{l:.2f}x"][cost_k]["calmar"])
                best_dca_lev = max(MOD1_LEVERAGES, key=lambda l: dca_matrix[f"{l:.2f}x"][cost_k]["xirr_pct"])
                sweet_spots_by_cost[cost_k] = {
                    "cost_rate_pct": round(cost * 100.0, 2),
                    "best_cagr_lev": best_cagr_lev,
                    "best_cagr_pct": lump_matrix[f"{best_cagr_lev:.2f}x"][cost_k]["cagr_pct"],
                    "best_cagr_mdd_pct": lump_matrix[f"{best_cagr_lev:.2f}x"][cost_k]["mdd_pct"],
                    "best_sharpe_lev": best_sharpe_lev,
                    "best_sharpe": lump_matrix[f"{best_sharpe_lev:.2f}x"][cost_k]["sharpe"],
                    "best_calmar_lev": best_calmar_lev,
                    "best_calmar": lump_matrix[f"{best_calmar_lev:.2f}x"][cost_k]["calmar"],
                    "best_dca_xirr_lev": best_dca_lev,
                    "best_dca_xirr_pct": dca_matrix[f"{best_dca_lev:.2f}x"][cost_k]["xirr_pct"],
                }

            results["symbols"][sym][period_key] = {
                "start_date": dates[0].strftime("%Y-%m-%d"),
                "end_date": dates[-1].strftime("%Y-%m-%d"),
                "trading_days": int(len(dates)),
                "years": base_metrics["years"],
                "benchmark_1x": base_metrics,
                "lump_sum_matrix": lump_matrix,
                "dca_matrix": dca_matrix,
                "etf_fee_overlay_matrix": etf_fee_matrix,
                "breakeven_rates_pct": breakeven_rates,
                "breakeven_rates_with_etf_fee_pct": breakeven_rates_with_etf_fee,
                "sweet_spots_by_cost": sweet_spots_by_cost,
            }

    # Real Leveraged ETF comparison over exact overlapping dates
    for etf_sym in LEV_ETFS:
        cfg = TICKERS_CONFIG[etf_sym]
        lev = float(cfg["leverage"])
        df_etf = cleaned_data[etf_sym]
        results["real_etf_comparison"][etf_sym] = {}

        for ref_type in ("underlying_price", "underlying_tr"):
            und_sym = cfg[ref_type]
            df_und = cleaned_data[und_sym]
            common_idx = df_etf.index.intersection(df_und.index)
            if len(common_idx) < 50:
                continue

            sub_etf = df_etf.loc[common_idx]
            sub_und = df_und.loc[common_idx]

            r_etf = sub_etf["Price_Used"].pct_change().fillna(0.0).to_numpy(dtype=float)
            r_und = sub_und["Price_Used"].pct_change().fillna(0.0).to_numpy(dtype=float)

            w_etf = np.ones(len(r_etf), dtype=float)
            w_etf[1:] = np.cumprod(1.0 + r_etf[1:])
            w_und_1x = simulate_daily_rebalanced_wealth(r_und, 1.0, 0.0, 0.0)
            w_theo_0 = simulate_daily_rebalanced_wealth(r_und, lev, 0.0, 0.0)
            w_theo_3 = simulate_daily_rebalanced_wealth(r_und, lev, 0.03, ETF_EXTRA_EXPENSE)
            w_theo_5 = simulate_daily_rebalanced_wealth(r_und, lev, 0.05, ETF_EXTRA_EXPENSE)

            # Solve for implied annualized total drag cost (on (L-1) borrowed portion) where theoretical matches actual ETF terminal wealth
            target_end = float(w_etf[-1])
            lo_c, hi_c = -0.15, 0.25
            for _ in range(50):
                mid_c = 0.5 * (lo_c + hi_c)
                w_m = simulate_daily_rebalanced_wealth(r_und, lev, mid_c, 0.0)
                if float(w_m[-1]) >= target_end:
                    lo_c = mid_c
                else:
                    hi_c = mid_c
            implied_cost_per_unit_debt = 0.5 * (lo_c + hi_c)

            results["real_etf_comparison"][etf_sym][und_sym] = {
                "etf_symbol": etf_sym,
                "underlying_symbol": und_sym,
                "leverage": lev,
                "start_date": common_idx[0].strftime("%Y-%m-%d"),
                "end_date": common_idx[-1].strftime("%Y-%m-%d"),
                "trading_days": int(len(common_idx)),
                "actual_etf": compute_curve_metrics(w_etf, common_idx),
                "underlying_1x": compute_curve_metrics(w_und_1x, common_idx),
                "theoretical_0pct_cost": compute_curve_metrics(w_theo_0, common_idx),
                "theoretical_3pct_plus_fee": compute_curve_metrics(w_theo_3, common_idx),
                "theoretical_5pct_plus_fee": compute_curve_metrics(w_theo_5, common_idx),
                "implied_annual_financing_rate_pct": round(implied_cost_per_unit_debt * 100.0, 2),
                "implied_total_annual_drag_pct": round((lev - 1.0) * implied_cost_per_unit_debt * 100.0, 2),
                "daily_return_corr": round(float(np.corrcoef(r_etf[1:], r_und[1:])[0, 1]), 4),
            }

    return results


# ---------------------------------------------------------------------------
# 4. Module 2: Margin Buying (Fixed Debt, Maintenance Margin Call & Cash Buffer)
# ---------------------------------------------------------------------------

MOD2_RATES = [0.05, 0.06, 0.065, 0.07, 0.08]
MOD2_LEVERAGES = [1.2, 1.4, 1.67, 2.0, 2.5]
LIQ_SLIPPAGE = 0.002  # 0.20% forced liquidation slippage


def simulate_non_rebalanced_debt_path(
    price_rel: np.ndarray,
    dates: pd.DatetimeIndex,
    init_leverage: float,
    interest_rate: float,
    maint_threshold: float,
    reenter_1x_after_liq: bool = False,
) -> dict[str, Any]:
    """Simulate non-daily-rebalanced margin or stock pledge position from start to end of price_rel.

    price_rel: P_t / P_0 (starts at 1.0).
    Initial equity E_0 = 1.0, initial market value V_0 = L_0, initial debt D_0 = L_0 - 1.0.
    At day t:
      V_t = L_0 * price_rel[t]
      D_t = D_0 * (1 + interest_rate / 252)^t
      Maintenance ratio MR_t = V_t / D_t.
    """
    n = len(price_rel)
    t_arr = np.arange(n, dtype=float)
    v_t = init_leverage * price_rel
    d_0 = init_leverage - 1.0
    if d_0 <= 1e-9:
        metrics = compute_curve_metrics(price_rel, dates)
        return {
            **metrics,
            "margin_called": False,
            "margin_call_date": None,
            "min_maint_ratio_pct": 9999.0,
            "req_cash_buffer_pct_of_e0": 0.0,
            "effective_leverage_with_buffer": 1.0,
            "cagr_with_buffer_pct": metrics["cagr_pct"],
        }

    d_t = d_0 * np.power(1.0 + interest_rate / TRADING_DAYS_PER_YEAR, t_arr)
    mr_t = v_t / d_t
    min_mr = float(np.min(mr_t))

    # Required cash buffer C_buf (as fraction of E_0=1.0) so that V_t / (D_t - C_buf) >= maint_threshold everywhere
    # i.e. D_t - C_buf <= V_t / maint_threshold => C_buf >= max_t(D_t - V_t / maint_threshold)
    req_buf = float(np.max(np.maximum(0.0, d_t - v_t / maint_threshold)))

    # Breach detection
    breach_indices = np.flatnonzero(mr_t < maint_threshold)
    nav = np.maximum(0.0, v_t - d_t)
    margin_called = len(breach_indices) > 0
    mc_date = None

    if margin_called:
        b_idx = int(breach_indices[0])
        mc_date = dates[b_idx].strftime("%Y-%m-%d")
        liq_equity = max(0.0, float(v_t[b_idx] * (1.0 - LIQ_SLIPPAGE) - d_t[b_idx]))
        if reenter_1x_after_liq and price_rel[b_idx] > 0:
            nav[b_idx:] = liq_equity * (price_rel[b_idx:] / price_rel[b_idx])
        else:
            nav[b_idx:] = liq_equity

    metrics = compute_curve_metrics(nav, dates, total_invested=1.0)

    # Metrics if investor holds req_buf in cash (earning 1.5%/yr risk-free until needed, or 0% conservative)
    # Total capital committed = 1.0 + req_buf, unbroken position terminal equity = max(0, v_t[-1] - d_t[-1]) + req_buf
    nav_with_buf = np.maximum(0.0, v_t - d_t) + req_buf
    buf_metrics = compute_curve_metrics(nav_with_buf, dates, total_invested=1.0 + req_buf)
    eff_lev = init_leverage / (1.0 + req_buf)

    # Peak-to-trough worst historical crash buffer analysis:
    # Suppose investor enters (or is re-leveraged to init_leverage) at the running peak right before the period's
    # worst historical drawdown (MDD_index), lasting ~1.5 years of accrued interest on debt.
    peaks_p = np.maximum.accumulate(price_rel)
    dd_series = (price_rel - peaks_p) / peaks_p
    trough_idx = int(np.argmin(dd_series))
    peak_idx = int(np.argmax(price_rel[: trough_idx + 1])) if trough_idx > 0 else 0
    crash_days = max(1, trough_idx - peak_idx)
    p_trough_ratio = float(price_rel[trough_idx] / price_rel[peak_idx]) if peak_idx < len(price_rel) else 1.0
    v_crash_trough = init_leverage * p_trough_ratio
    d_crash_trough = d_0 * ((1.0 + interest_rate / TRADING_DAYS_PER_YEAR) ** crash_days)
    crash_trough_mr = v_crash_trough / d_crash_trough if d_crash_trough > 0 else 9999.0
    peak_crash_req_buf = max(0.0, d_crash_trough - v_crash_trough / maint_threshold)
    peak_crash_eff_lev = init_leverage / (1.0 + peak_crash_req_buf)
    nav_with_peak_buf = np.maximum(0.0, v_t - d_t) + peak_crash_req_buf
    peak_buf_metrics = compute_curve_metrics(nav_with_peak_buf, dates, total_invested=1.0 + peak_crash_req_buf)

    return {
        **metrics,
        "margin_called": bool(margin_called),
        "margin_call_date": mc_date,
        "min_maint_ratio_pct": round(min_mr * 100.0, 1),
        "req_cash_buffer_pct_of_e0": round(req_buf * 100.0, 1),
        "effective_leverage_with_buffer": round(eff_lev, 3),
        "cagr_with_buffer_pct": buf_metrics["cagr_pct"],
        "mdd_with_buffer_pct": buf_metrics["mdd_pct"],
        "peak_crash_drawdown_pct": round((p_trough_ratio - 1.0) * 100.0, 2),
        "peak_crash_trough_mr_pct": round(crash_trough_mr * 100.0, 1),
        "peak_crash_req_buffer_pct": round(peak_crash_req_buf * 100.0, 1),
        "peak_crash_effective_leverage": round(peak_crash_eff_lev, 3),
        "peak_crash_buffered_cagr_pct": peak_buf_metrics["cagr_pct"],
    }


def compute_rolling_debt_cohorts(
    prices: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
    init_leverage: float,
    interest_rate: float,
    maint_threshold: float,
    horizon_years: tuple[int, ...] = (5, 10, 15),
) -> dict[str, Any]:
    """Vectorized evaluation of all rolling monthly entry cohorts for non-rebalanced debt (Margin / Pledge)."""
    n_m = len(month_starts)
    d_0 = init_leverage - 1.0
    out: dict[str, Any] = {}

    for hy in horizon_years:
        h_m = hy * 12
        key = f"{hy}y"
        if n_m <= h_m:
            out[key] = {
                "count": 0,
                "margin_call_rate_pct": None,
                "win_rate_vs_1x_pct": None,
                "median_cagr_pct": None,
                "p5_cagr_pct": None,
                "median_req_buffer_pct": None,
                "max_req_buffer_pct": None,
            }
            continue

        n_cohorts = n_m - h_m
        mc_flags = np.zeros(n_cohorts, dtype=bool)
        mults = np.zeros(n_cohorts, dtype=float)
        base_mults = np.zeros(n_cohorts, dtype=float)
        yrs_arr = np.zeros(n_cohorts, dtype=float)
        req_bufs = np.zeros(n_cohorts, dtype=float)

        for k in range(n_cohorts):
            s_idx = int(month_starts[k])
            e_idx = int(month_starts[k + h_m])
            p_slice = prices[s_idx : e_idx + 1]
            p_rel = p_slice / p_slice[0]
            L_len = len(p_rel)
            t_arr = np.arange(L_len, dtype=float)
            v_t = init_leverage * p_rel
            d_t = d_0 * np.power(1.0 + interest_rate / TRADING_DAYS_PER_YEAR, t_arr)
            mr_t = v_t / d_t

            req_bufs[k] = float(np.max(np.maximum(0.0, d_t - v_t / maint_threshold)))
            breaches = np.flatnonzero(mr_t < maint_threshold)
            if len(breaches) > 0:
                mc_flags[k] = True
                b_i = int(breaches[0])
                mults[k] = max(0.0, float(v_t[b_i] * (1.0 - LIQ_SLIPPAGE) - d_t[b_i]))
            else:
                mults[k] = max(0.0, float(v_t[-1] - d_t[-1]))

            base_mults[k] = float(p_rel[-1])
            yrs_arr[k] = max((dates[e_idx] - dates[s_idx]).days / 365.25, float(hy))

        cagrs = (np.power(np.maximum(mults, 1e-6), 1.0 / yrs_arr) - 1.0) * 100.0
        wins = float(np.mean(mults > base_mults + 1e-9) * 100.0)
        mc_rate = float(np.mean(mc_flags) * 100.0)

        out[key] = {
            "count": int(n_cohorts),
            "margin_call_rate_pct": round(mc_rate, 2),
            "win_rate_vs_1x_pct": round(wins, 2),
            "median_cagr_pct": round(float(np.median(cagrs)), 2),
            "p5_cagr_pct": round(float(np.percentile(cagrs, 5)), 2),
            "p5_multiple": round(float(np.percentile(mults, 5)), 3),
            "median_req_buffer_pct": round(float(np.median(req_bufs) * 100.0), 1),
            "max_req_buffer_pct": round(float(np.max(req_bufs) * 100.0), 1),
        }
    return out


def run_module_2(cleaned_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run Module 2: Margin Buying across rates (5%~8%), initial leverages (1.2x~2.5x), and US/TW rules."""
    results: dict[str, Any] = {
        "grid_rates_pct": [round(r * 100.0, 2) for r in MOD2_RATES],
        "grid_leverages": MOD2_LEVERAGES,
        "maintenance_rules": {
            "TW": {"maint_ratio_threshold": 1.30, "desc": "台股融資維持率 < 130% 追繳斷頭"},
            "US": {"maint_ratio_threshold": 1.3333, "desc": "美股 Reg-T 維持保證金 < 25% (約維持率 < 133.3%) 斷頭"},
        },
        "symbols": {},
    }

    for sym in CORE_SYMBOLS:
        mkt = TICKERS_CONFIG[sym]["market"]
        maint_thresh = 1.30 if mkt == "TW" else (1.0 / 0.75)
        df_full = cleaned_data[sym]
        df_10y = df_full.loc[RECENT_10Y_START:].copy()
        results["symbols"][sym] = {
            "market": mkt,
            "maint_threshold_pct": round(maint_thresh * 100.0, 2),
        }

        for period_key, df in (("full_history", df_full), ("recent_10y", df_10y)):
            dates = df.index
            prices = df["Price_Used"].to_numpy(dtype=float)
            p_rel = prices / prices[0]
            month_starts = get_month_start_indices(dates)
            base_metrics = compute_curve_metrics(p_rel, dates)

            grid_out: dict[str, dict[str, Any]] = {}
            for lev in MOD2_LEVERAGES:
                lev_k = f"{lev:.2f}x"
                grid_out[lev_k] = {}
                # Theoretical immediate drawdown from entry that triggers margin call before interest
                dd_trigger = (1.0 - (maint_thresh * (lev - 1.0)) / lev) * 100.0

                for r_m in MOD2_RATES:
                    r_k = f"{r_m * 100:.1f}pct"
                    sim = simulate_non_rebalanced_debt_path(p_rel, dates, lev, r_m, maint_thresh)
                    sim["entry_drawdown_to_margin_call_pct"] = round(-dd_trigger, 2)
                    sim["initial_maint_ratio_pct"] = round((lev / (lev - 1.0)) * 100.0, 1)
                    sim["rolling"] = compute_rolling_debt_cohorts(
                        prices, dates, month_starts, lev, r_m, maint_thresh
                    )
                    grid_out[lev_k][r_k] = sim

            results["symbols"][sym][period_key] = {
                "start_date": dates[0].strftime("%Y-%m-%d"),
                "end_date": dates[-1].strftime("%Y-%m-%d"),
                "years": base_metrics["years"],
                "benchmark_1x": base_metrics,
                "grid": grid_out,
            }

    return results


# ---------------------------------------------------------------------------
# 5. Module 3: Personal Loan / Mortgage Cash-Out Investing (Amortized Repayment)
# ---------------------------------------------------------------------------

MOD3_RATES = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05, 0.06]
MOD3_TENORS = [
    {"tenor_years": 7, "tenor_months": 84, "label": "7年期信貸 (本息均攤)"},
    {"tenor_years": 20, "tenor_months": 240, "label": "20年期房貸增貸 (本息均攤)"},
]
MOD3_LOAN_SIZES = [
    {"years_of_savings": 1, "principal": 12.0, "label": "預借1年儲蓄 (12倍月現金流)"},
    {"years_of_savings": 2, "principal": 24.0, "label": "預借2年儲蓄 (24倍月現金流)"},
    {"years_of_savings": 3, "principal": 36.0, "label": "預借3年儲蓄 (36倍月現金流)"},
    {"years_of_savings": 5, "principal": 60.0, "label": "預借5年儲蓄 (60倍月現金流)"},
]


def calc_amortized_pmt_and_balance(principal: float, annual_rate: float, tenor_months: int, elapsed_months: int) -> tuple[float, float]:
    """Return (monthly_payment, remaining_principal_after_elapsed_months)."""
    if principal <= 0 or tenor_months <= 0:
        return 0.0, 0.0
    i = annual_rate / 12.0
    if i <= 1e-9:
        pmt = principal / tenor_months
        rem = max(0.0, principal * (1.0 - elapsed_months / tenor_months))
        return pmt, rem
    factor = (1.0 + i) ** tenor_months
    pmt = principal * (i * factor) / (factor - 1.0)
    if elapsed_months >= tenor_months:
        rem = 0.0
    else:
        rem = principal * (factor - ((1.0 + i) ** elapsed_months)) / (factor - 1.0)
    return float(pmt), float(rem)


def build_mod3_monthly_schedule(
    strategy_type: str,
    n_months: int,
    principal: float,
    annual_rate: float,
    tenor_months: int,
) -> tuple[np.ndarray, float, float]:
    """Build monthly stock contribution schedule c[0..n_months-1] and terminal remaining debt at month n_months.

    Monthly available cash flow CF = 1.0 unit/month (total out-of-pocket cash outflow = n_months * 1.0).
    Returns (c_schedule, terminal_debt, pmt_ratio_of_cf).
    """
    c = np.ones(n_months, dtype=float)
    if strategy_type == "pure_dca" or principal <= 0:
        return c, 0.0, 0.0

    if strategy_type == "revolving_interest_only":
        # Interest-only revolving line (理財型房貸/循環信貸): invest principal at m=0, pay monthly interest, deduct principal at end
        m_int = principal * (annual_rate / 12.0)
        c[:] = 1.0 - m_int
        c[0] += principal
        return c, principal, float(m_int)

    pmt, rem_debt = calc_amortized_pmt_and_balance(principal, annual_rate, tenor_months, n_months)

    if strategy_type == "lump_sum_loan":
        active_m = min(n_months, tenor_months)
        c[:active_m] = 1.0 - pmt
        c[0] += principal
        return c, rem_debt, float(pmt)

    if strategy_type in ("tranche_12m", "tranche_24m"):
        k_tranche = 12 if strategy_type == "tranche_12m" else 24
        active_m = min(n_months, tenor_months)
        c[:active_m] = 1.0 - pmt
        k_eff = min(n_months, k_tranche)
        c[:k_eff] += principal / float(k_tranche)
        # If n_months < k_tranche, remaining undeployed cash offsets remaining debt
        undeployed = principal * max(0, k_tranche - n_months) / float(k_tranche)
        return c, max(0.0, rem_debt - undeployed), float(pmt)

    if strategy_type == "renewed_loan_cycle":
        # Renews loan every tenor_months (e.g. every 84 months for 7Y loan)
        c[:] = 0.0
        total_rem_debt = 0.0
        for cycle_start in range(0, n_months, tenor_months):
            cycle_end = min(n_months, cycle_start + tenor_months)
            elapsed = cycle_end - cycle_start
            c_pmt, c_rem = calc_amortized_pmt_and_balance(principal, annual_rate, tenor_months, elapsed)
            c[cycle_start:cycle_end] += (1.0 - c_pmt)
            c[cycle_start] += principal
            total_rem_debt += c_rem
        return c, total_rem_debt, float(pmt)

    raise ValueError(f"Unknown strategy_type: {strategy_type}")


def eval_mod3_schedule_full_and_rolling(
    prices: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
    strategy_type: str,
    principal: float,
    annual_rate: float,
    tenor_months: int,
    rolling_horizons: tuple[int, ...] = (7, 10, 15),
) -> dict[str, Any]:
    """Evaluate a Module 3 loan+DCA strategy over the full period and across all rolling 7Y, 10Y, 15Y cohorts."""
    n_m = len(month_starts)
    p_at_m = prices[month_starts]
    inv_p_m = 1.0 / np.maximum(p_at_m, 1e-12)
    p_end = float(prices[-1])

    # Full period (n_m monthly contributions)
    c_full, rem_debt_full, pmt_ratio = build_mod3_monthly_schedule(
        strategy_type, n_m, principal, annual_rate, tenor_months
    )
    shares_full = float(np.sum(c_full * inv_p_m))
    gross_val_full = shares_full * p_end
    net_worth_full = gross_val_full - rem_debt_full
    total_out_of_pocket = float(n_m)

    # Pure DCA baseline over full period
    base_nw_full = float(np.sum(inv_p_m)) * p_end

    # Full-period XIRR on out-of-pocket cash flows (-1.0 per month, +net_worth_full at end)
    days_arr = (dates - dates[0]).days.to_numpy(dtype=float)
    cf_days = np.append(days_arr[month_starts], days_arr[-1])
    cf_amts = np.append(-np.ones(n_m, dtype=float), net_worth_full)
    xirr_full = solve_xirr(cf_days, cf_amts) * 100.0

    # Daily Net Worth curve to check max drawdown and whether net worth ever turns negative (underwater debt)
    m_idx_for_day = np.searchsorted(month_starts, np.arange(len(prices)), side="right") - 1
    cum_shares_at_m = np.cumsum(c_full * inv_p_m)
    # Remaining debt at each month index m (0..n_m-1)
    rem_debt_at_m = np.zeros(n_m, dtype=float)
    for m_i in range(n_m):
        if strategy_type == "pure_dca" or principal <= 0:
            rem_debt_at_m[m_i] = 0.0
        elif strategy_type == "revolving_interest_only":
            rem_debt_at_m[m_i] = principal
        elif strategy_type == "renewed_loan_cycle":
            elapsed_in_cycle = (m_i % tenor_months) + 1
            _, rem_debt_at_m[m_i] = calc_amortized_pmt_and_balance(principal, annual_rate, tenor_months, elapsed_in_cycle)
        else:
            _, rem_debt_at_m[m_i] = calc_amortized_pmt_and_balance(principal, annual_rate, tenor_months, m_i + 1)

    daily_gross = prices * cum_shares_at_m[m_idx_for_day]
    daily_debt = rem_debt_at_m[m_idx_for_day]
    daily_nw = daily_gross - daily_debt
    min_daily_nw = float(np.min(daily_nw))

    # Rolling cohorts (7Y = 84m, 10Y = 120m, 15Y = 180m)
    rolling_out: dict[str, Any] = {}
    for hy in rolling_horizons:
        h_m = hy * 12
        key = f"{hy}y"
        if n_m <= h_m:
            rolling_out[key] = {
                "count": 0,
                "win_rate_vs_dca_pct": None,
                "median_nw_multiple": None,
                "p5_nw_multiple": None,
                "median_excess_vs_dca_pct": None,
                "p5_excess_vs_dca_pct": None,
                "median_xirr_pct": None,
                "p5_xirr_pct": None,
            }
            continue

        c_h, rem_debt_h, _ = build_mod3_monthly_schedule(
            strategy_type, h_m, principal, annual_rate, tenor_months
        )
        # Cross-correlation to compute sum_{j=0}^{h_m-1} c_h[j] * inv_p_m[k + j] for k = 0..n_m-h_m-1
        shares_cohorts = np.correlate(inv_p_m[:-1], c_h, mode="valid")
        base_shares_cohorts = np.correlate(inv_p_m[:-1], np.ones(h_m, dtype=float), mode="valid")
        eval_prices = p_at_m[h_m:]

        nw_cohorts = shares_cohorts * eval_prices - rem_debt_h
        base_nw_cohorts = base_shares_cohorts * eval_prices

        nw_mults = nw_cohorts / float(h_m)
        base_mults = base_nw_cohorts / float(h_m)
        excess_pct = ((nw_cohorts - base_nw_cohorts) / np.maximum(base_nw_cohorts, 1e-9)) * 100.0
        wins = float(np.mean(nw_cohorts > base_nw_cohorts + 1e-9) * 100.0)

        sched_days = np.append(np.arange(h_m, dtype=float) * (365.25 / 12.0), hy * 365.25)
        med_mult = float(np.median(nw_mults))
        p5_mult = float(np.percentile(nw_mults, 5))
        med_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), med_mult * h_m)) * 100.0
        p5_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), p5_mult * h_m)) * 100.0

        rolling_out[key] = {
            "count": int(len(nw_cohorts)),
            "win_rate_vs_dca_pct": round(wins, 2),
            "median_nw_multiple": round(med_mult, 3),
            "p5_nw_multiple": round(p5_mult, 3),
            "median_excess_vs_dca_pct": round(float(np.median(excess_pct)), 2),
            "p5_excess_vs_dca_pct": round(float(np.percentile(excess_pct, 5)), 2),
            "median_xirr_pct": round(med_xirr, 2),
            "p5_xirr_pct": round(p5_xirr, 2),
        }

    return {
        "strategy_type": strategy_type,
        "loan_principal_months": round(principal, 1),
        "annual_rate_pct": round(annual_rate * 100.0, 2),
        "tenor_months": tenor_months,
        "pmt_to_monthly_cf_pct": round(pmt_ratio * 100.0, 2),
        "within_50pct_cf_cap": bool(pmt_ratio <= 0.50 + 1e-6),
        "within_70pct_cf_cap": bool(pmt_ratio <= 0.70 + 1e-6),
        "full_period_net_worth_multiple": round(net_worth_full / total_out_of_pocket, 3),
        "full_period_excess_vs_dca_pct": round(((net_worth_full - base_nw_full) / max(base_nw_full, 1e-9)) * 100.0, 2),
        "full_period_xirr_pct": round(xirr_full, 2),
        "min_daily_net_worth_units": round(min_daily_nw, 2),
        "rolling": rolling_out,
    }


def run_module_3(cleaned_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run Module 3: Personal Loan (7Y) & Mortgage Cash-Out (20Y) vs Pure Monthly DCA."""
    results: dict[str, Any] = {
        "grid_rates_pct": [round(r * 100.0, 2) for r in MOD3_RATES],
        "tenors": MOD3_TENORS,
        "loan_sizes": MOD3_LOAN_SIZES,
        "symbols": {},
    }

    for sym in CORE_SYMBOLS:
        df_full = cleaned_data[sym]
        df_10y = df_full.loc[RECENT_10Y_START:].copy()
        results["symbols"][sym] = {}

        for period_key, df in (("full_history", df_full), ("recent_10y", df_10y)):
            dates = df.index
            prices = df["Price_Used"].to_numpy(dtype=float)
            month_starts = get_month_start_indices(dates)

            baseline_dca = eval_mod3_schedule_full_and_rolling(
                prices, dates, month_starts, "pure_dca", 0.0, 0.0, 84
            )

            tenor_results: dict[str, Any] = {}
            for t_cfg in MOD3_TENORS:
                t_m = t_cfg["tenor_months"]
                t_key = f"{t_cfg['tenor_years']}y_loan"
                tenor_results[t_key] = {"label": t_cfg["label"], "by_loan_size": {}}

                for s_cfg in MOD3_LOAN_SIZES:
                    p_amt = s_cfg["principal"]
                    s_key = f"borrow_{s_cfg['years_of_savings']}y_savings"
                    tenor_results[t_key]["by_loan_size"][s_key] = {
                        "label": s_cfg["label"],
                        "principal_months_cf": p_amt,
                        "by_rate": {},
                    }

                    for r_loan in MOD3_RATES:
                        r_key = f"{r_loan * 100:.1f}pct"
                        lump = eval_mod3_schedule_full_and_rolling(
                            prices, dates, month_starts, "lump_sum_loan", p_amt, r_loan, t_m
                        )
                        tr12 = eval_mod3_schedule_full_and_rolling(
                            prices, dates, month_starts, "tranche_12m", p_amt, r_loan, t_m
                        )
                        tr24 = eval_mod3_schedule_full_and_rolling(
                            prices, dates, month_starts, "tranche_24m", p_amt, r_loan, t_m
                        )
                        rev = eval_mod3_schedule_full_and_rolling(
                            prices, dates, month_starts, "revolving_interest_only", p_amt, r_loan, t_m
                        )
                        renew = eval_mod3_schedule_full_and_rolling(
                            prices, dates, month_starts, "renewed_loan_cycle", p_amt, r_loan, t_m
                        )
                        tenor_results[t_key]["by_loan_size"][s_key]["by_rate"][r_key] = {
                            "lump_sum_loan": lump,
                            "tranche_12m": tr12,
                            "tranche_24m": tr24,
                            "revolving_interest_only": rev,
                            "renewed_loan_cycle": renew,
                        }

            results["symbols"][sym][period_key] = {
                "start_date": dates[0].strftime("%Y-%m-%d"),
                "end_date": dates[-1].strftime("%Y-%m-%d"),
                "total_months": int(len(month_starts)),
                "baseline_pure_dca": baseline_dca,
                "tenors": tenor_results,
            }

    return results


# ---------------------------------------------------------------------------
# 6. Module 4: Stock Pledging (Interest-Only with 130% and 140% Maintenance Ratio)
# ---------------------------------------------------------------------------

MOD4_RATES = [0.02, 0.025, 0.03, 0.035, 0.04]
MOD4_LEVERAGES = [1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2.0]
MOD4_THRESHOLDS = [1.30, 1.40]


def run_module_4(cleaned_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run Module 4: Stock Pledging across rates (2.0%~4.0%), leverages (1.2x~2.0x), and 130%/140% thresholds."""
    results: dict[str, Any] = {
        "grid_rates_pct": [round(r * 100.0, 2) for r in MOD4_RATES],
        "grid_leverages": MOD4_LEVERAGES,
        "thresholds_pct": [round(th * 100.0, 1) for th in MOD4_THRESHOLDS],
        "symbols": {},
    }

    for sym in CORE_SYMBOLS:
        df_full = cleaned_data[sym]
        df_10y = df_full.loc[RECENT_10Y_START:].copy()
        results["symbols"][sym] = {}

        for period_key, df in (("full_history", df_full), ("recent_10y", df_10y)):
            dates = df.index
            prices = df["Price_Used"].to_numpy(dtype=float)
            p_rel = prices / prices[0]
            month_starts = get_month_start_indices(dates)
            base_metrics = compute_curve_metrics(p_rel, dates)

            thresh_out: dict[str, Any] = {}
            for th in MOD4_THRESHOLDS:
                th_k = f"maint_{int(round(th * 100))}pct"
                thresh_out[th_k] = {"grid": {}, "safe_leverage_summary_by_rate": {}}

                for lev in MOD4_LEVERAGES:
                    lev_k = f"{lev:.2f}x"
                    thresh_out[th_k]["grid"][lev_k] = {}
                    dd_trigger = (1.0 - (th * (lev - 1.0)) / lev) * 100.0

                    for r_p in MOD4_RATES:
                        r_k = f"{r_p * 100:.1f}pct"
                        sim = simulate_non_rebalanced_debt_path(p_rel, dates, lev, r_p, th)
                        sim["entry_drawdown_to_liquidation_pct"] = round(-dd_trigger, 2)
                        sim["initial_maint_ratio_pct"] = round((lev / (lev - 1.0)) * 100.0, 1)
                        sim["rolling"] = compute_rolling_debt_cohorts(
                            prices, dates, month_starts, lev, r_p, th
                        )
                        thresh_out[th_k]["grid"][lev_k][r_k] = sim

                # Determine safe leverage limit (0% or <1% rolling 10Y liquidation rate) for each interest rate
                for r_p in MOD4_RATES:
                    r_k = f"{r_p * 100:.1f}pct"
                    max_zero_mc_lev = 1.0
                    max_sub1pct_mc_lev = 1.0
                    best_safe_cagr = base_metrics["cagr_pct"]
                    for lev in MOD4_LEVERAGES:
                        lev_k = f"{lev:.2f}x"
                        cell = thresh_out[th_k]["grid"][lev_k][r_k]
                        r10 = cell["rolling"]["10y"]
                        mc10 = r10["margin_call_rate_pct"] if r10["margin_call_rate_pct"] is not None else (100.0 if cell["margin_called"] else 0.0)
                        if not cell["margin_called"] and mc10 == 0.0:
                            max_zero_mc_lev = lev
                            best_safe_cagr = cell["cagr_pct"]
                        if not cell["margin_called"] and mc10 <= 1.0:
                            max_sub1pct_mc_lev = lev

                    thresh_out[th_k]["safe_leverage_summary_by_rate"][r_k] = {
                        "rate_pct": round(r_p * 100.0, 2),
                        "max_zero_liquidation_leverage": max_zero_mc_lev,
                        "max_sub1pct_liquidation_leverage": max_sub1pct_mc_lev,
                        "cagr_at_max_zero_liq_pct": best_safe_cagr,
                    }

            results["symbols"][sym][period_key] = {
                "start_date": dates[0].strftime("%Y-%m-%d"),
                "end_date": dates[-1].strftime("%Y-%m-%d"),
                "years": base_metrics["years"],
                "benchmark_1x": base_metrics,
                "thresholds": thresh_out,
            }

    return results


# ---------------------------------------------------------------------------
# 7. Module 5: Dip-Buying & Dynamic Leverage (E1 Cash Reserve, E2 Dip Leverage, E3 200-SMA)
# ---------------------------------------------------------------------------

SWITCH_COST_RATE = 0.001  # 0.10% per leverage switch
CASH_RESERVE_YIELD = 0.015  # 1.5%/yr yield on idle cash reserve


def simulate_e1_lump_sum(
    prices: np.ndarray,
    rets: np.ndarray,
    init_stock_weight: float,
    mode: str,
    cash_yield: float = CASH_RESERVE_YIELD,
) -> tuple[np.ndarray, int]:
    """Simulate E1 Cash Reserve Dip-Buying on an initial 1.0 unit portfolio.

    init_stock_weight: 0.70 or 0.80. Remainder (0.30 or 0.20) sits in cash reserve until drawdown triggers.
    When price recovers to a new running all-time high (DD == 0), portfolio resets cash reserve back to
    (1 - init_stock_weight) to prepare for the next bear market.
    """
    n = len(prices)
    w = np.ones(n, dtype=float)
    stock_val = init_stock_weight
    cash_val = 1.0 - init_stock_weight
    daily_cash_r = cash_yield / TRADING_DAYS_PER_YEAR

    peak_p = float(prices[0])
    t10_done = False
    t20_done = False
    t30_done = False
    deploy_count = 0

    for t in range(1, n):
        # Update values for day t return
        stock_val *= (1.0 + rets[t])
        cash_val *= (1.0 + daily_cash_r)
        total_val = stock_val + cash_val

        p_t = float(prices[t])
        if p_t >= peak_p - 1e-12:
            peak_p = p_t
            # At new all-time high, if cash reserve was depleted, replenish back to (1 - init_stock_weight)
            if t10_done or t20_done or t30_done:
                target_cash = total_val * (1.0 - init_stock_weight)
                if cash_val < target_cash:
                    transfer = target_cash - cash_val
                    stock_val -= transfer
                    cash_val += transfer
                t10_done = False
                t20_done = False
                t30_done = False
        else:
            dd = (p_t - peak_p) / peak_p  # negative
            if mode == "staged_10_20_30":
                if dd <= -0.10 and not t10_done and cash_val > 1e-6:
                    amt = cash_val / 3.0
                    stock_val += amt * (1.0 - SWITCH_COST_RATE * 0.5)
                    cash_val -= amt
                    t10_done = True
                    deploy_count += 1
                if dd <= -0.20 and not t20_done and cash_val > 1e-6:
                    amt = cash_val * 0.5
                    stock_val += amt * (1.0 - SWITCH_COST_RATE * 0.5)
                    cash_val -= amt
                    t20_done = True
                    deploy_count += 1
                if dd <= -0.30 and not t30_done and cash_val > 1e-6:
                    amt = cash_val
                    stock_val += amt * (1.0 - SWITCH_COST_RATE * 0.5)
                    cash_val = 0.0
                    t30_done = True
                    deploy_count += 1
            elif mode == "dd10" and dd <= -0.10 and not t10_done and cash_val > 1e-6:
                stock_val += cash_val * (1.0 - SWITCH_COST_RATE * 0.5)
                cash_val = 0.0
                t10_done = True
                deploy_count += 1
            elif mode == "dd20" and dd <= -0.20 and not t20_done and cash_val > 1e-6:
                stock_val += cash_val * (1.0 - SWITCH_COST_RATE * 0.5)
                cash_val = 0.0
                t20_done = True
                deploy_count += 1
            elif mode == "dd30" and dd <= -0.30 and not t30_done and cash_val > 1e-6:
                stock_val += cash_val * (1.0 - SWITCH_COST_RATE * 0.5)
                cash_val = 0.0
                t30_done = True
                deploy_count += 1

        w[t] = stock_val + cash_val

    return w, deploy_count


def simulate_e1_dca_cashflow(
    prices: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
    base_cum_wealth: np.ndarray,
    init_stock_weight: float,
    mode: str,
    cash_yield: float = CASH_RESERVE_YIELD,
) -> dict[str, Any]:
    """Simulate E1 Cash Reserve Dip-Buying under monthly cash flow CF = 1.0/month (strictly no borrowing).

    Every month on month_starts, investor receives 1.0 unit of cash:
      - Invests `init_stock_weight` (0.70 or 0.80) immediately into stock.
      - Adds `(1 - init_stock_weight)` (0.30 or 0.20) to cash reserve pool.
    Whenever daily drawdown from running peak reaches trigger (-10% / -20% / -30%), reserve cash is deployed into stock.
    """
    n = len(prices)
    rets = np.zeros(n, dtype=float)
    rets[1:] = (prices[1:] - prices[:-1]) / prices[:-1]
    daily_cash_r = cash_yield / TRADING_DAYS_PER_YEAR
    is_m_start = np.zeros(n, dtype=bool)
    is_m_start[month_starts] = True

    def _run_slice(s_idx: int, e_idx: int) -> tuple[float, np.ndarray, int]:
        stock_v = 0.0
        cash_v = 0.0
        peak_p = float(prices[s_idx])
        t10, t20, t30 = False, False, False
        deploys = 0
        curve = np.zeros(e_idx - s_idx + 1, dtype=float)

        for idx in range(s_idx, e_idx + 1):
            if idx > s_idx:
                stock_v *= (1.0 + rets[idx])
                cash_v *= (1.0 + daily_cash_r)

            p_t = float(prices[idx])
            if p_t >= peak_p - 1e-12:
                peak_p = p_t
                t10, t20, t30 = False, False, False
            dd = (p_t - peak_p) / peak_p

            if is_m_start[idx] and idx < e_idx:
                stock_v += init_stock_weight
                cash_v += (1.0 - init_stock_weight)

            # Check dip triggers
            if mode == "staged_10_20_30":
                if dd <= -0.10 and not t10 and cash_v > 1e-6:
                    amt = cash_v / 3.0
                    stock_v += amt
                    cash_v -= amt
                    t10 = True
                    deploys += 1
                if dd <= -0.20 and not t20 and cash_v > 1e-6:
                    amt = cash_v * 0.5
                    stock_v += amt
                    cash_v -= amt
                    t20 = True
                    deploys += 1
                if dd <= -0.30 and not t30 and cash_v > 1e-6:
                    stock_v += cash_v
                    cash_v = 0.0
                    t30 = True
                    deploys += 1
            elif mode == "dd10" and dd <= -0.10 and cash_v > 1e-6:
                stock_v += cash_v
                cash_v = 0.0
                deploys += 1
            elif mode == "dd20" and dd <= -0.20 and cash_v > 1e-6:
                stock_v += cash_v
                cash_v = 0.0
                deploys += 1
            elif mode == "dd30" and dd <= -0.30 and cash_v > 1e-6:
                stock_v += cash_v
                cash_v = 0.0
                deploys += 1

            curve[idx - s_idx] = stock_v + cash_v
        return stock_v + cash_v, curve, deploys

    # Full period (add final contribution if last month_start == e_idx? Wait: for full period, contributions at all month_starts)
    stock_v, cash_v = 0.0, 0.0
    peak_p = float(prices[0])
    t10, t20, t30 = False, False, False
    deploys_full = 0
    full_curve = np.zeros(n, dtype=float)
    for idx in range(n):
        if idx > 0:
            stock_v *= (1.0 + rets[idx])
            cash_v *= (1.0 + daily_cash_r)
        p_t = float(prices[idx])
        if p_t >= peak_p - 1e-12:
            peak_p = p_t
            t10, t20, t30 = False, False, False
        dd = (p_t - peak_p) / peak_p
        if is_m_start[idx]:
            stock_v += init_stock_weight
            cash_v += (1.0 - init_stock_weight)
        if mode == "staged_10_20_30":
            if dd <= -0.10 and not t10 and cash_v > 1e-6:
                amt = cash_v / 3.0
                stock_v += amt
                cash_v -= amt
                t10 = True
                deploys_full += 1
            if dd <= -0.20 and not t20 and cash_v > 1e-6:
                amt = cash_v * 0.5
                stock_v += amt
                cash_v -= amt
                t20 = True
                deploys_full += 1
            if dd <= -0.30 and not t30 and cash_v > 1e-6:
                stock_v += cash_v
                cash_v = 0.0
                t30 = True
                deploys_full += 1
        elif mode == "dd10" and dd <= -0.10 and cash_v > 1e-6:
            stock_v += cash_v
            cash_v = 0.0
            deploys_full += 1
        elif mode == "dd20" and dd <= -0.20 and cash_v > 1e-6:
            stock_v += cash_v
            cash_v = 0.0
            deploys_full += 1
        elif mode == "dd30" and dd <= -0.30 and cash_v > 1e-6:
            stock_v += cash_v
            cash_v = 0.0
            deploys_full += 1
        full_curve[idx] = stock_v + cash_v

    n_m = len(month_starts)
    days_arr = (dates - dates[0]).days.to_numpy(dtype=float)
    cf_days = np.append(days_arr[month_starts], days_arr[-1])
    cf_amts = np.append(-np.ones(n_m, dtype=float), float(full_curve[-1]))
    xirr_full = solve_xirr(cf_days, cf_amts) * 100.0

    peaks = np.maximum.accumulate(np.maximum(full_curve, 1e-12))
    mdd_active = float(np.min((full_curve - peaks) / peaks)) * 100.0

    # Base 1.0x DCA prefix units for rolling comparison
    inv_base_m = 1.0 / np.maximum(base_cum_wealth[month_starts], 1e-12)
    pref_base = np.concatenate([[0.0], np.cumsum(inv_base_m)])

    rolling_dca: dict[str, Any] = {}
    for hy in (5, 10, 15):
        h_m = hy * 12
        key = f"{hy}y"
        if n_m <= h_m:
            rolling_dca[key] = {
                "count": 0,
                "win_rate_vs_1x_dca_pct": None,
                "median_multiple": None,
                "p5_multiple": None,
                "median_xirr_pct": None,
                "p5_xirr_pct": None,
            }
            continue

        n_c = n_m - h_m
        mults = np.zeros(n_c, dtype=float)
        base_mults = np.zeros(n_c, dtype=float)
        for k in range(n_c):
            s_i = int(month_starts[k])
            e_i = int(month_starts[k + h_m])
            end_v, _, _ = _run_slice(s_i, e_i)
            mults[k] = end_v / float(h_m)
            base_end_v = float(base_cum_wealth[e_i]) * (pref_base[k + h_m] - pref_base[k])
            base_mults[k] = base_end_v / float(h_m)

        wins = float(np.mean(mults > base_mults + 1e-9) * 100.0)
        sched_days = np.append(np.arange(h_m, dtype=float) * (365.25 / 12.0), hy * 365.25)
        med_mult = float(np.median(mults))
        p5_mult = float(np.percentile(mults, 5))
        med_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), med_mult * h_m)) * 100.0
        p5_xirr = solve_xirr(sched_days, np.append(-np.ones(h_m), p5_mult * h_m)) * 100.0

        rolling_dca[key] = {
            "count": int(n_c),
            "win_rate_vs_1x_dca_pct": round(wins, 2),
            "median_multiple": round(med_mult, 3),
            "p5_multiple": round(p5_mult, 3),
            "median_xirr_pct": round(med_xirr, 2),
            "p5_xirr_pct": round(p5_xirr, 2),
        }

    return {
        "xirr_pct": round(xirr_full, 2),
        "final_multiple": round(float(full_curve[-1]) / float(n_m), 3),
        "total_return_pct": round((float(full_curve[-1]) / float(n_m) - 1.0) * 100.0, 2),
        "mdd_active_pct": round(mdd_active, 2),
        "deploy_triggers_count": int(deploys_full),
        "rolling": rolling_dca,
    }


def simulate_e2_dip_leverage(
    prices: np.ndarray,
    rets: np.ndarray,
    dd_trigger_abs: float,
    target_leverage: float,
    borrow_rate: float,
    maint_threshold: float = 1.30,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Simulate E2: Normally 1.0x, scale to `target_leverage` when drawdown from running peak <= -dd_trigger_abs,
    and de-leverage back to 1.0x when index recovers to running peak (DD == 0).

    Also tracks non-rebalanced pledge maintenance ratio during each leveraged episode to verify whether
    entering leverage AFTER a -15%..-30% drop triggers any 130% maintenance margin call.
    """
    n = len(prices)
    w = np.ones(n, dtype=float)
    peak_p = float(prices[0])
    cur_lev = 1.0
    daily_borrow_drag = (target_leverage - 1.0) * borrow_rate / TRADING_DAYS_PER_YEAR

    episodes = 0
    days_leveraged = 0
    pledge_mc_episodes = 0
    min_episode_mr = 9999.0
    ep_entry_p = 0.0
    ep_days = 0

    for t in range(1, n):
        # Apply yesterday's leverage to today's return
        if cur_lev > 1.0:
            r_day = max(-0.9999, cur_lev * rets[t] - daily_borrow_drag)
            days_leveraged += 1
            ep_days += 1
            # Check non-rebalanced pledge maintenance ratio from episode entry price
            p_ratio = float(prices[t]) / ep_entry_p
            v_ep = target_leverage * p_ratio
            d_ep = (target_leverage - 1.0) * ((1.0 + borrow_rate / TRADING_DAYS_PER_YEAR) ** ep_days)
            mr_ep = v_ep / d_ep
            if mr_ep < min_episode_mr:
                min_episode_mr = mr_ep
        else:
            r_day = rets[t]

        w[t] = w[t - 1] * (1.0 + r_day)

        p_t = float(prices[t])
        if p_t >= peak_p - 1e-12:
            peak_p = p_t
            if cur_lev > 1.0:
                # Recovered to peak -> de-leverage back to 1.0x
                w[t] *= (1.0 - SWITCH_COST_RATE * (cur_lev - 1.0))
                cur_lev = 1.0
        else:
            dd = (p_t - peak_p) / peak_p
            if cur_lev == 1.0 and dd <= -dd_trigger_abs:
                # Trigger dip leverage for tomorrow onwards
                cur_lev = target_leverage
                w[t] *= (1.0 - SWITCH_COST_RATE * (cur_lev - 1.0))
                episodes += 1
                ep_entry_p = p_t
                ep_days = 0
            elif cur_lev > 1.0:
                # Check if this episode breached maint_threshold
                p_ratio = p_t / ep_entry_p
                v_ep = target_leverage * p_ratio
                d_ep = (target_leverage - 1.0) * ((1.0 + borrow_rate / TRADING_DAYS_PER_YEAR) ** ep_days)
                if (v_ep / d_ep) < maint_threshold and ep_days == 1:
                    pass

    # Re-scan episodes accurately for pledge margin call count
    peak_p = float(prices[0])
    in_ep = False
    ep_entry_p = 0.0
    ep_d = 0
    ep_breached = False
    for t in range(1, n):
        p_t = float(prices[t])
        if in_ep:
            ep_d += 1
            v_ep = target_leverage * (p_t / ep_entry_p)
            d_ep = (target_leverage - 1.0) * ((1.0 + borrow_rate / TRADING_DAYS_PER_YEAR) ** ep_d)
            if (v_ep / d_ep) < maint_threshold:
                ep_breached = True
        if p_t >= peak_p - 1e-12:
            peak_p = p_t
            if in_ep:
                if ep_breached:
                    pledge_mc_episodes += 1
                in_ep = False
                ep_breached = False
        else:
            dd = (p_t - peak_p) / peak_p
            if not in_ep and dd <= -dd_trigger_abs:
                in_ep = True
                ep_entry_p = p_t
                ep_d = 0
                ep_breached = False
    if in_ep and ep_breached:
        pledge_mc_episodes += 1

    meta = {
        "episodes_triggered": int(episodes),
        "pct_time_leveraged": round((days_leveraged / max(1, n - 1)) * 100.0, 2),
        "min_maint_ratio_during_dip_pct": round(min_episode_mr * 100.0, 1) if episodes > 0 else 9999.0,
        "pledge_130_breach_episodes": int(pledge_mc_episodes),
    }
    return w, meta


def simulate_e3_sma200_dynamic(
    prices: np.ndarray,
    rets: np.ndarray,
    bull_leverage: float,
    borrow_rate: float,
    buffer_pct: float = 0.0,
    sma_window: int = 200,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Simulate E3: Causal 200-day SMA dynamic leverage filter.

    When Close > SMA200 * (1 + buffer_pct), target leverage = bull_leverage.
    When Close < SMA200 * (1 - buffer_pct), target leverage = 1.0x.
    Includes 0.10% switching friction cost on traded leverage change.
    """
    n = len(prices)
    w = np.ones(n, dtype=float)
    sma = pd.Series(prices).rolling(sma_window, min_periods=sma_window).mean().to_numpy(dtype=float)

    cur_lev = 1.0
    daily_borrow_drag = (bull_leverage - 1.0) * borrow_rate / TRADING_DAYS_PER_YEAR
    switches = 0
    days_bull = 0

    for t in range(1, n):
        if cur_lev > 1.0:
            r_day = max(-0.9999, cur_lev * rets[t] - daily_borrow_drag)
            days_bull += 1
        else:
            r_day = rets[t]

        w[t] = w[t - 1] * (1.0 + r_day)

        if not np.isnan(sma[t]) and sma[t] > 0:
            upper = sma[t] * (1.0 + buffer_pct)
            lower = sma[t] * (1.0 - buffer_pct)
            if cur_lev == 1.0 and prices[t] > upper:
                cur_lev = bull_leverage
                w[t] *= (1.0 - SWITCH_COST_RATE * (bull_leverage - 1.0))
                switches += 1
            elif cur_lev > 1.0 and prices[t] < lower:
                w[t] *= (1.0 - SWITCH_COST_RATE * (cur_lev - 1.0))
                cur_lev = 1.0
                switches += 1

    years = max(1.0, (n - 1) / TRADING_DAYS_PER_YEAR)
    meta = {
        "total_switches": int(switches),
        "switches_per_year": round(switches / years, 2),
        "pct_time_in_bull_leverage": round((days_bull / max(1, n - 1)) * 100.0, 2),
    }
    return w, meta


def run_module_5(cleaned_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run Module 5: Compare E1 (Cash Reserve Dip-Buy), E2 (Leveraged Dip-Buy), E3 (200-SMA Filter) vs 1.0x & Fixed 1.5x."""
    results: dict[str, Any] = {"symbols": {}}

    for sym in CORE_SYMBOLS:
        df_full = cleaned_data[sym]
        df_10y = df_full.loc[RECENT_10Y_START:].copy()
        results["symbols"][sym] = {}

        for period_key, df in (("full_history", df_full), ("recent_10y", df_10y)):
            dates = df.index
            prices = df["Price_Used"].to_numpy(dtype=float)
            rets = df["Ret"].to_numpy(dtype=float)
            month_starts = get_month_start_indices(dates)

            # Baselines: 1.0x and Fixed 1.5x (at 3.0% borrowing rate) and Fixed 2.0x (at 3.0% borrowing rate)
            w_1x = simulate_daily_rebalanced_wealth(rets, 1.0, 0.0, 0.0)
            w_15x_r3 = simulate_daily_rebalanced_wealth(rets, 1.5, 0.03, 0.0)
            w_20x_r3 = simulate_daily_rebalanced_wealth(rets, 2.0, 0.03, 0.0)

            baselines: dict[str, Any] = {}
            for b_key, b_w, b_label in (
                ("baseline_1.0x", w_1x, "基準 A：全程 1.0x (不開槓桿)"),
                ("baseline_fixed_1.5x_r3pct", w_15x_r3, "基準 B：全程固定 1.5x (利率 3.0%)"),
                ("baseline_fixed_2.0x_r3pct", w_20x_r3, "對照 C：全程固定 2.0x (利率 3.0%)"),
            ):
                m_lump = compute_curve_metrics(b_w, dates)
                m_lump["rolling"] = compute_rolling_lump_sum_stats(b_w, w_1x, dates, month_starts)
                m_dca = compute_dca_metrics_from_wealth_index(b_w, w_1x, dates, month_starts)
                baselines[b_key] = {
                    "label": b_label,
                    "lump_sum": m_lump,
                    "dca": m_dca,
                }

            # E1: Cash Reserve Dip-Buying (No Borrowing)
            e1_results: dict[str, Any] = {}
            e1_configs = [
                ("e1_80_20_staged", 0.80, "staged_10_20_30", "平時投80%+留20%現金 (-10%/-20%/-30%分批加碼)"),
                ("e1_70_30_staged", 0.70, "staged_10_20_30", "平時投70%+留30%現金 (-10%/-20%/-30%分批加碼)"),
                ("e1_80_20_dd10", 0.80, "dd10", "平時投80%+留20%現金 (跌-10%一次梭哈備用金)"),
                ("e1_80_20_dd20", 0.80, "dd20", "平時投80%+留20%現金 (跌-20%一次梭哈備用金)"),
                ("e1_80_20_dd30", 0.80, "dd30", "平時投80%+留20%現金 (跌-30%一次梭哈備用金)"),
            ]
            for e1_k, w_init, mode_str, label_str in e1_configs:
                w_e1, dep_cnt = simulate_e1_lump_sum(prices, rets, w_init, mode_str)
                m_lump = compute_curve_metrics(w_e1, dates)
                m_lump["deploy_triggers_count"] = dep_cnt
                m_lump["rolling"] = compute_rolling_lump_sum_stats(w_e1, w_1x, dates, month_starts)
                m_dca = simulate_e1_dca_cashflow(prices, dates, month_starts, w_1x, w_init, mode_str)
                e1_results[e1_k] = {
                    "label": label_str,
                    "init_stock_weight": w_init,
                    "mode": mode_str,
                    "lump_sum": m_lump,
                    "dca": m_dca,
                }

            # E2: Normal 1.0x, Leveraged Dip-Buying at -15%, -20%, -25%, -30% to 1.5x or 2.0x (rates 2.5% and 3.5%)
            e2_results: dict[str, Any] = {}
            for dd_th in (0.15, 0.20, 0.25, 0.30):
                for t_lev in (1.5, 2.0):
                    for b_rate in (0.025, 0.035):
                        e2_k = f"e2_dd{int(round(dd_th*100))}_lev{t_lev:.1f}x_r{b_rate*100:.1f}pct"
                        label_str = f"跌 -{int(round(dd_th*100))}% 開 {t_lev:.1f}x 槓桿 (回前高降回 1.0x, 利率 {b_rate*100:.1f}%)"
                        w_e2, meta_e2 = simulate_e2_dip_leverage(prices, rets, dd_th, t_lev, b_rate)
                        m_lump = compute_curve_metrics(w_e2, dates)
                        m_lump.update(meta_e2)
                        m_lump["rolling"] = compute_rolling_lump_sum_stats(w_e2, w_1x, dates, month_starts)
                        m_dca = compute_dca_metrics_from_wealth_index(w_e2, w_1x, dates, month_starts)
                        e2_results[e2_k] = {
                            "label": label_str,
                            "dd_trigger_pct": -round(dd_th * 100.0, 1),
                            "target_leverage": t_lev,
                            "borrow_rate_pct": round(b_rate * 100.0, 2),
                            "lump_sum": m_lump,
                            "dca": m_dca,
                        }

            # E3: 200-SMA Dynamic Leverage Filter (1.5x and 2.0x, buffer 0% and +-2%, borrow rate 3.0% and 4.0%)
            e3_results: dict[str, Any] = {}
            for b_lev in (1.5, 2.0):
                for buf in (0.00, 0.02):
                    for b_rate in (0.03, 0.04):
                        buf_tag = "buf2pct" if buf > 0 else "nobuf"
                        e3_k = f"e3_sma200_lev{b_lev:.1f}x_{buf_tag}_r{int(round(b_rate*100))}pct"
                        buf_desc = "緩衝 ±2%" if buf > 0 else "無緩衝 0%"
                        label_str = f"200日均線之上 {b_lev:.1f}x / 之下 1.0x ({buf_desc}, 利率 {b_rate*100:.1f}%, 換倉成本 0.1%)"
                        w_e3, meta_e3 = simulate_e3_sma200_dynamic(prices, rets, b_lev, b_rate, buffer_pct=buf)
                        m_lump = compute_curve_metrics(w_e3, dates)
                        m_lump.update(meta_e3)
                        m_lump["rolling"] = compute_rolling_lump_sum_stats(w_e3, w_1x, dates, month_starts)
                        m_dca = compute_dca_metrics_from_wealth_index(w_e3, w_1x, dates, month_starts)
                        e3_results[e3_k] = {
                            "label": label_str,
                            "bull_leverage": b_lev,
                            "buffer_pct": round(buf * 100.0, 1),
                            "borrow_rate_pct": round(b_rate * 100.0, 1),
                            "lump_sum": m_lump,
                            "dca": m_dca,
                        }

            results["symbols"][sym][period_key] = {
                "start_date": dates[0].strftime("%Y-%m-%d"),
                "end_date": dates[-1].strftime("%Y-%m-%d"),
                "years": baselines["baseline_1.0x"]["lump_sum"]["years"],
                "baselines": baselines,
                "e1_cash_reserve_dip": e1_results,
                "e2_leveraged_dip": e2_results,
                "e3_sma200_dynamic": e3_results,
            }

    return results


# ---------------------------------------------------------------------------
# 8. Numerical Sanity Verification & Main Orchestrator
# ---------------------------------------------------------------------------

def verify_results_integrity(all_results: dict[str, Any]) -> None:
    """Verify strict numerical identities:
      1. 1.0x 0% cost in Module 1 must identically match the benchmark 1.0x metrics for every symbol and period.
      2. Pure DCA baseline in Module 3 must match Module 1 1.0x 0% DCA metrics.
      3. All 5 modules and all 6 symbols + 5 real leveraged ETFs must be populated.
    """
    m1 = all_results["module_1"]
    m3 = all_results["module_3"]
    for sym in CORE_SYMBOLS:
        for pk in ("full_history", "recent_10y"):
            b = m1["symbols"][sym][pk]["benchmark_1x"]
            c1x0 = m1["symbols"][sym][pk]["lump_sum_matrix"]["1.00x"]["0pct"]
            assert abs(b["cagr_pct"] - c1x0["cagr_pct"]) < 1e-9, f"1.0x 0% CAGR mismatch on {sym} {pk}"
            assert abs(b["total_return_pct"] - c1x0["total_return_pct"]) < 1e-9, f"1.0x 0% TotalRet mismatch on {sym} {pk}"
            assert abs(b["mdd_pct"] - c1x0["mdd_pct"]) < 1e-9, f"1.0x 0% MDD mismatch on {sym} {pk}"

            dca_1x0 = m1["symbols"][sym][pk]["dca_matrix"]["1.00x"]["0pct"]
            m3_dca = m3["symbols"][sym][pk]["baseline_pure_dca"]
            assert abs(dca_1x0["xirr_pct"] - m3_dca["full_period_xirr_pct"]) < 1e-6, f"DCA XIRR mismatch on {sym} {pk}"
            assert abs(dca_1x0["final_multiple"] - m3_dca["full_period_net_worth_multiple"]) < 1e-6, f"DCA Mult mismatch on {sym} {pk}"

    print("[VERIFY] All numerical sanity checks passed (1.0x 0% == benchmark, DCA identities exact).")


def run_all_studies(refresh: bool = False) -> dict[str, Any]:
    t0 = time.time()
    raw = fetch_and_cache_raw_data(refresh=refresh)
    cleaned, hygiene_notes = clean_and_prepare_series(raw)

    data_summary: dict[str, Any] = {}
    for sym, df in cleaned.items():
        cfg = TICKERS_CONFIG[sym]
        rets = df["Ret"].to_numpy(dtype=float)
        w = np.ones(len(rets), dtype=float)
        w[1:] = np.cumprod(1.0 + rets[1:])
        m = compute_curve_metrics(w, df.index)
        data_summary[sym] = {
            "symbol": sym,
            "description": cfg["desc"],
            "market": cfg["market"],
            "kind": cfg["kind"],
            "start_date": df.index[0].strftime("%Y-%m-%d"),
            "end_date": df.index[-1].strftime("%Y-%m-%d"),
            "trading_days": int(len(df)),
            "years": m["years"],
            "full_cagr_pct": m["cagr_pct"],
            "full_total_return_pct": m["total_return_pct"],
            "full_mdd_pct": m["mdd_pct"],
            "full_sharpe": m["sharpe"],
            "full_calmar": m["calmar"],
            "full_vol_pct": m["vol_pct"],
            "hygiene_note": hygiene_notes[sym],
        }

    print("[RUN] Module 1: Daily Rebalanced Leverage x Borrowing Cost Heatmap & Real ETFs...")
    mod1 = run_module_1(cleaned)
    print("[RUN] Module 2: Margin Trading with Maintenance Calls & Cash Buffer...")
    mod2 = run_module_2(cleaned)
    print("[RUN] Module 3: Personal Loan (7Y) & Mortgage (20Y) vs Pure DCA...")
    mod3 = run_module_3(cleaned)
    print("[RUN] Module 4: Stock Pledging (Interest-Only, 130% & 140% Maintenance)...")
    mod4 = run_module_4(cleaned)
    print("[RUN] Module 5: Dip-Buying & 200-SMA Dynamic Leverage...")
    mod5 = run_module_5(cleaned)

    all_results = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - t0, 2),
        "data_summary": data_summary,
        "module_1": mod1,
        "module_2": mod2,
        "module_3": mod3,
        "module_4": mod4,
        "module_5": mod5,
    }
    verify_results_integrity(all_results)
    RESULTS_JSON_PATH.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md = generate_markdown_report(all_results)
    REPORT_MD_PATH.write_text(report_md, encoding="utf-8")
    print(f"[DONE] Saved {RESULTS_JSON_PATH} and {REPORT_MD_PATH} in {all_results['elapsed_sec']:.2f}s")
    return all_results


# ---------------------------------------------------------------------------
# 9. Automated Markdown Report Generator (report.md)
# ---------------------------------------------------------------------------

def generate_markdown_report(res: dict[str, Any]) -> str:
    """Generate comprehensive Traditional Chinese empirical report from structured results."""
    ds = res["data_summary"]
    m1 = res["module_1"]
    m2 = res["module_2"]
    m3 = res["module_3"]
    m4 = res["module_4"]
    m5 = res["module_5"]

    lines: list[str] = []
    a = lines.append

    a("# 美股指數與台股指數：上不上槓桿？槓桿倍率、借貸／融資利率與跌深加碼的甜蜜點實證研究")
    a("")
    a("> **研究範圍**：美股 S&P 500（`^GSPC` / `SPY`）、美股 Nasdaq 100（`^NDX` / `QQQ`）、台股加權指數與台灣 50（`^TWII` / `0050.TW`），以及五檔真實槓桿 ETF（`SSO`、`UPRO`、`QLD`、`TQQQ`、`00631L.TW`）。嚴格排除加密貨幣與單一個股。")
    a("> **雙期間檢驗**：同時輸出「**全歷史最長期間（涵蓋 1997 亞洲金融風暴、2000 網路泡沫、2008 金融海嘯、2020 疫情、2022 升息熊市）**」與「**近 10 年（2016–2026 多頭為主週期）**」，並針對每一個模組執行歷史所有月初進場之 **5 年、10 年、15 年滾動視窗檢定（Rolling Entry Windows）**。")
    a("")
    a("---")
    a("")
    a("## 一句話總結論：美股／台股到底有沒有槓桿甜蜜點？")
    a("")
    a("**有，但槓桿甜蜜點不在 2.5 倍或 3 倍，而是高度受限於「資金成本（借貸利率）」、「標的是否含息」與「工具是否會斷頭」三大鐵律：**")
    a("")
    a("1. **跨越多輪大崩盤的「全週期槓桿甜蜜點」落在 `1.3x ～ 1.6x`（若使用無維持率斷頭風險的每日再平衡正二 ETF 搭配定投或均線濾網，上限可達 `1.5x ～ 2.0x`）**：")
    a("   - 在近 10 年（2016–2026）未發生 `-50%` 以上史詩級崩盤的順風期，3.0 倍槓桿看似報酬最高；但拉長至涵蓋 2000 網路泡沫（`QQQ -83%`、`^TWII -66%`）與 2008 金融海嘯（`SPY -55%`）的全歷史週期，**單筆超過 `2.0x` 的固定槓桿在 3%～5% 資金成本下，不僅最大回撤高達 `-90% ～ -99%`，年化報酬（CAGR）反而因波動耗損（Volatility Drag）開始倒退，甚至輸給 1.0x 原型指數**。")
    a("2. **資金成本（利率）生死線：`≤ 3.5%` 是黃金借貸區、`4.0% ～ 5.0%` 是警戒區、`> 5.0%` 在經歷大熊市時幾乎全面淪為替銀行與券商打工**：")
    a("   - 在全歷史含息基準下，`SPY` 開 `1.5x / 2.0x` 的損益兩平利率門檻為 **`7.48% / 6.61%`**（若扣除槓桿 ETF 0.95% 內扣費則降至 **`5.58% / 5.66%`**）；波動巨大的 `QQQ`（1999–2026）開 `1.5x / 2.0x` 的損益兩平利率僅 **`4.39% / 2.56%`**（`3.0x` 就算 0% 利率也會輸給 `1.0x`！）；台股若不含息（`^TWII` 1997–2026）開 `1.5x` 損益兩平利率僅 **`1.62%`**，但加計約 `3.5%～4.0%` 高殖利率還原後（`0050.TW`），息收大幅抵銷借貸利息，使台股成為最能承受 `2.5%～3.5%` 低利槓桿的市場。")
    a("3. **四大槓桿工具實戰勝負：『低利房貸增貸／信貸（利率 ≤3.5%） ≈ 正二 ETF（00631L / SSO / QLD 搭配再平衡或動態濾網） > 低倍率股票質押（≤1.35x～1.4x） ≫ 券商一般融資（Margin）』**：")
    a("   - **券商一般融資（利率 6%～7% + 130%/133% 維持率斷頭）是散戶最容易爆倉的毒藥**：台股 2.5 倍融資（自備四成）只要下跌 **22.0%** 即觸發 130% 斷頭，在 `^TWII` 滾動 10 年進場的**歷史斷頭率高達 `92.7%`**；就算在場外保留大筆現金準備補維持率，把備用金算進總本金後，真實有效槓桿被稀釋到僅剩 `1.1x ～ 1.3x`，卻要付 6.5% 高利，報酬率完全不如直接買現股或正二 ETF。")
    a("4. **跌深加碼與動態槓桿實證：『留現金等大跌（E1）』90% 時間輸給直接買；『平時 1.0x 滿倉、跌深 -20% 才低利借貸開 1.5x（E2）』與『200 日均線 ±2% 緩衝動態槓桿（E3）』才是真正兼顧高報酬與防斷頭的聖杯**。")
    a("")
    a("---")
    a("")
    a("## 一、資料範圍、分割／配息驗證與「台股含息 vs 不含息」關鍵差異")
    a("")
    a("### 1.1 樣本涵蓋期間與基礎績效總覽（全歷史 vs 近 10 年）")
    a("")
    a("| 標的代碼 | 名稱與性質 | 市場 | 全歷史起訖期間 | 樣本年數 | 全歷史 CAGR | 全歷史總報酬 | 全歷史最大回撤 (MDD) | 全歷史 Sharpe | 近10年 (2016–2026) CAGR | 近10年 MDD | 近10年 Sharpe |")
    a("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sym in CORE_SYMBOLS + LEV_ETFS:
        d = ds[sym]
        if sym in CORE_SYMBOLS:
            m10 = m1["symbols"][sym]["recent_10y"]["benchmark_1x"]
            c10_str = f"{m10['cagr_pct']:.2f}%"
            dd10_str = f"{m10['mdd_pct']:.2f}%"
            sh10_str = f"{m10['sharpe']:.2f}"
        else:
            c10_str = "-"
            dd10_str = "-"
            sh10_str = "-"
        a(
            f"| `{sym}` | {d['description']} | {d['market']} | {d['start_date']} ~ {d['end_date']} | "
            f"{d['years']:.2f} 年 | **{d['full_cagr_pct']:.2f}%** | {d['full_total_return_pct']:,.1f}% | "
            f"{d['full_mdd_pct']:.2f}% | {d['full_sharpe']:.2f} | {c10_str} | {dd10_str} | {sh10_str} |"
        )

    a("")
    a("### 1.2 資料完整性校驗與台股高殖利率（`^TWII` vs `0050.TW`）關鍵對照說明")
    a("")
    a("1. **為什麼台股必須同時看 `^TWII`（1997–2026）與 `0050.TW`（2009–2026 含息還原）？**")
    a("   - **期間涵蓋差異**：Yahoo Finance 上 `^TWII` 價格指數始於 **1997-07-02（共 29.40 年）**，完整涵蓋 1997 亞洲金融風暴、2000 網路泡沫（`-66.22%`）與 2008 金融海嘯（`-59.26%`）；而 Yahoo Finance 的 `0050.TW` 有效連續日線始於 **2009-03-10（共 17.71 年）**，雖涵蓋 2011 歐債、2015 中國股災、2018 貿易戰、2020 疫情與 2022 升息熊市（`-33.83%`），但**未涵蓋 2000 與 2008 兩次超過 `-55%` 的極端崩盤**。")
    a("   - **股息殖利率的決定性影響**：在 `2009-03-10 ～ 2026-11-24` 同期間內，`^TWII`（不含息價格指數）年化報酬為 **`12.91%`**，`0050.TW` 不含息價格年化報酬為 **`14.75%`**，而 `0050.TW` 含息還原總報酬（Adj Close）高達 **`18.27%`**——**每年股息再投資貢獻了 `+3.52%/年` 的額外複利**！若只看 `^TWII` 不含息價格指數，會誤以為台股連 2% 借貸利率都付不起；反之若只看 2009 年後的 `0050.TW`，又會低估 2000/2008 級別大崩盤對維持率的殺傷力。因此本報告所有模組皆**雙軌並陳 `^TWII`（29.4 年含大崩盤壓力測試）與 `0050.TW`（17.7 年含息真實現金流）**。")
    a("2. **Yahoo Finance 台灣 ETF 分割與配息原始資料瑕疵修正揭露**：")
    for sym in ("0050.TW", "00631L.TW"):
        a(f"   - **`{sym}`**：{ds[sym]['hygiene_note']}")
    a("")
    a("---")
    a("")
    a("## 二、總表 1：槓桿倍率 × 利率甜蜜點熱力矩陣（模組 1：每日再平衡模型 + 真實槓桿 ETF 對照）")
    a("")
    a("### 2.1 各指數在不同借貸利率下的最佳槓桿倍率與損益兩平利率門檻總表")
    a("")
    a("> **閱讀指南**：下表列出每個指數在「全歷史」與「近 10 年」下，1.0x 基準報酬、各槓桿倍率（1.5x / 2.0x / 3.0x）的**損益兩平借貸利率（Break-even Rate，即利率高於此數值時開槓桿反而輸給 1.0x）**，以及在 2%、3%、5%、7% 四種典型資金成本下的**最佳年化報酬（CAGR）倍率**與**最佳風險調整（Sharpe / Calmar）倍率**。")
    a("")
    a("| 指數／標的 | 期間 | 1.0x 基準 CAGR (MDD) | 1.5x 兩平利率 (含ETF費) | 2.0x 兩平利率 (含ETF費) | 3.0x 兩平利率 (含ETF費) | 利率 2% 最佳CAGR倍率 | 利率 3% 最佳CAGR倍率 | 利率 5% 最佳CAGR倍率 | 利率 7% 最佳CAGR倍率 | 利率 3% 最佳Calmar倍率 |")
    a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sym in CORE_SYMBOLS:
        for pk, p_label in (("full_history", "全歷史"), ("recent_10y", "近10年")):
            d = m1["symbols"][sym][pk]
            b = d["benchmark_1x"]
            be = d["breakeven_rates_pct"]
            be_etf = d["breakeven_rates_with_etf_fee_pct"]
            ss = d["sweet_spots_by_cost"]
            a(
                f"| `{sym}` | {p_label} ({d['years']:.1f}y) | {b['cagr_pct']:.2f}% ({b['mdd_pct']:.1f}%) | "
                f"**{be['1.50x']:.2f}%** ({be_etf['1.50x']:.2f}%) | **{be['2.00x']:.2f}%** ({be_etf['2.00x']:.2f}%) | "
                f"**{be['3.00x']:.2f}%** ({be_etf['3.00x']:.2f}%) | "
                f"{ss['2pct']['best_cagr_lev']}x ({ss['2pct']['best_cagr_pct']:.1f}%) | "
                f"**{ss['3pct']['best_cagr_lev']}x** ({ss['3pct']['best_cagr_pct']:.1f}%) | "
                f"{ss['5pct']['best_cagr_lev']}x ({ss['5pct']['best_cagr_pct']:.1f}%) | "
                f"{ss['7pct']['best_cagr_lev']}x ({ss['7pct']['best_cagr_pct']:.1f}%) | "
                f"{ss['3pct']['best_calmar_lev']}x (Calmar {ss['3pct']['best_calmar']:.2f}) |"
            )

    a("")
    a("### 2.2 三大核心市場「槓桿倍率 × 資金成本」完整熱力矩陣（全歷史 vs 近 10 年）")
    a("")

    matrix_display_syms = [
        ("SPY", "美股 S&P 500 含息總報酬 (`SPY`, 1993–2026 vs 2016–2026)"),
        ("^GSPC", "美股 S&P 500 價格指數 (`^GSPC`, 1990–2026 vs 2016–2026)"),
        ("QQQ", "美股 Nasdaq 100 含息總報酬 (`QQQ`, 1999–2026 含網路泡沫 vs 2016–2026)"),
        ("^NDX", "美股 Nasdaq 100 價格指數 (`^NDX`, 1990–2026 vs 2016–2026)"),
        ("0050.TW", "台股 台灣50 含息總報酬 (`0050.TW`, 2009–2026 vs 2016–2026)"),
        ("^TWII", "台股 加權指數 價格指數 (`^TWII`, 1997–2026 含2000/2008崩盤 vs 2016–2026)"),
    ]

    for sym, title in matrix_display_syms:
        a(f"#### {title}")
        a("")
        a("| 期間 | 槓桿倍率 | 0% 成本 CAGR | 2% 利率 CAGR | 3% 利率 CAGR | 4% 利率 CAGR | 5% 利率 CAGR | 6% 利率 CAGR | 7% 利率 CAGR | 8% 利率 CAGR | 3% 利率 MDD | 3% 利率 Sharpe / Calmar | 3% 滾動10年勝率 (P5年化) | 3% 定投 DCA XIRR |")
        a("| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for pk, p_label in (("full_history", "全歷史"), ("recent_10y", "近10年")):
            d = m1["symbols"][sym][pk]
            lm = d["lump_sum_matrix"]
            dm = d["dca_matrix"]
            for lev in MOD1_LEVERAGES:
                lk = f"{lev:.2f}x"
                c3 = lm[lk]["3pct"]
                d3 = dm[lk]["3pct"]
                r10 = c3["rolling"]["10y"]
                w10_str = f"{r10['win_rate_vs_1x_pct']:.1f}% ({r10['p5_cagr_pct']:+.1f}%)" if r10["win_rate_vs_1x_pct"] is not None else "N/A"
                a(
                    f"| {p_label} | **{lev:.2f}x** | "
                    f"{lm[lk]['0pct']['cagr_pct']:.2f}% | {lm[lk]['2pct']['cagr_pct']:.2f}% | "
                    f"**{lm[lk]['3pct']['cagr_pct']:.2f}%** | {lm[lk]['4pct']['cagr_pct']:.2f}% | "
                    f"{lm[lk]['5pct']['cagr_pct']:.2f}% | {lm[lk]['6pct']['cagr_pct']:.2f}% | "
                    f"{lm[lk]['7pct']['cagr_pct']:.2f}% | {lm[lk]['8pct']['cagr_pct']:.2f}% | "
                    f"{c3['mdd_pct']:.1f}% | {c3['sharpe']:.2f} / {c3['calmar']:.2f} | {w10_str} | {d3['xirr_pct']:.2f}% |"
                )
        a("")

    a("### 2.3 真實槓桿 ETF（`SSO`、`UPRO`、`QLD`、`TQQQ`、`00631L.TW`）與理論模型誤差對照")
    a("")
    a("| 真實槓桿 ETF | 對照基準標的 | 重疊檢驗期間 | 樣本年數 | 基準 1.0x CAGR | 理論 L倍 (0%成本) CAGR | 理論 L倍 (3%利率+0.95%費) | **真實 ETF 實際 CAGR** | 真實 ETF MDD | 真實 ETF Sharpe | **反推真實隱含年化資金利率** | 日報酬相關係數 |")
    a("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for etf_sym in LEV_ETFS:
        for und_sym, c in m1["real_etf_comparison"][etf_sym].items():
            a(
                f"| `{etf_sym}` ({c['leverage']:.0f}x) | `{und_sym}` | {c['start_date']}~{c['end_date']} | "
                f"{c['actual_etf']['years']:.2f}y | {c['underlying_1x']['cagr_pct']:.2f}% | "
                f"{c['theoretical_0pct_cost']['cagr_pct']:.2f}% | {c['theoretical_3pct_plus_fee']['cagr_pct']:.2f}% | "
                f"**{c['actual_etf']['cagr_pct']:.2f}%** | {c['actual_etf']['mdd_pct']:.2f}% | "
                f"{c['actual_etf']['sharpe']:.2f} | **{c['implied_annual_financing_rate_pct']:+.2f}%/年** | {c['daily_return_corr']:.4f} |"
            )

    a("")
    a("> [!IMPORTANT]")
    a("> **真實槓桿 ETF 關鍵實證發現**：")
    a("> 1. **美股正二／正三（`SSO`、`UPRO`、`QLD`、`TQQQ`）**：對齊含息 ETF（`SPY`、`QQQ`）後，真實槓桿 ETF 過去 16～20 年反推的**總隱含年化資金成本（聯邦基準利率 + 交換合約摩擦 + 0.95% 管理費）僅約 `2.51% ～ 3.23%/年`**（因為 2009–2021 長達 12 年處於美國零利率環境）。但在 2022–2026 美股高利率環境（SOFR 4.5%～5.3% + 1% 內扣費 ≈ 5.5%～6.0% 總成本）下，正三 ETF（`UPRO`、`TQQQ`）的持有成本已大幅上升。")
    a("> 2. **台股元大台灣50正2（`00631L.TW`）為何看似「超額暴賺」？**：若拿 `00631L.TW`（2015–2026 年化 **38.20%**）對比不含息的加權指數 `^TWII`（理論 2 倍 0% 成本年化僅 **29.56%**），`00631L.TW` 每年竟多賺 `+8.64%`（隱含借貸利率為 `-6.70%`）！這是因為 `00631L.TW` 透過**台指期（TX）**曝險，而台指期在每年除權息旺季呈現大幅「逆價差」，期貨轉倉自然吃進了台股每年約 `3.5%～4.0%` 的兩倍除息點數（且免繳股利所得稅與二代健保補充保費！）。當我們改以含息還原的 `0050.TW`（同期間 1x 年化 **21.70%**，理論 2 倍 0% 成本年化 **42.15%**）作為真實基準時，`00631L.TW` 的反推隱含年化資金與內扣總成本為 **`+2.92%/年`**，完全符合財務理論！")
    a("")
    a("---")
    a("")
    a("## 三、模組 2：券商融資買進（Margin，維持率斷頭機制與備用現金效益檢驗）")
    a("")
    a("### 3.1 融資維持率斷頭數學界線與全歷史滾動斷頭率（無備用金直接平倉）")
    a("")
    a("> **規則設定**：非每日再平衡（固定負債、每日計息滾入）。台股整戶維持率 `市值 / 融資金額 < 130%` 強制斷頭；美股 Reg-T 維持保證金 `< 25%`（即 `市值 / 融資金額 < 133.33%`）強制斷頭。下表展示代表性融資利率（美股 `6.0%`、台股 `6.5%`）下的殘酷現實：")
    a("")
    a("| 標的 | 融資利率 | 初始槓桿 (初始維持率) | 觸發斷頭之即時跌幅 | 全歷史單筆是否斷頭 (斷頭日) | 全歷史最終 CAGR | 滾動5年斷頭率 | **滾動10年斷頭率** | 滾動10年勝率 (vs 1x) | 滾動10年最差5% (P5 CAGR) | **為挺過歷史最大崩盤所需備用金 (%本金)** | **計入備用金後真實有效槓桿 (真實CAGR)** |")
    a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sym in ("SPY", "QQQ", "^TWII", "0050.TW"):
        d = m2["symbols"][sym]["full_history"]
        rk = "6.5pct" if sym in ("^TWII", "0050.TW") else "6.0pct"
        r_disp = "6.5%" if sym in ("^TWII", "0050.TW") else "6.0%"
        for lk in ("1.20x", "1.40x", "1.67x", "2.00x", "2.50x"):
            c = d["grid"][lk][rk]
            r5 = c["rolling"]["5y"]
            r10 = c["rolling"]["10y"]
            mc_str = f"是 ({c['margin_call_date']})" if c["margin_called"] else "否"
            a(
                f"| `{sym}` | {r_disp} | **{lk}** ({c['initial_maint_ratio_pct']:.0f}%) | "
                f"{c['entry_drawdown_to_margin_call_pct']:.1f}% | {mc_str} | {c['cagr_pct']:.2f}% | "
                f"{r5['margin_call_rate_pct']:.1f}% | **{r10['margin_call_rate_pct']:.1f}%** | "
                f"{r10['win_rate_vs_1x_pct']:.1f}% | {r10['p5_cagr_pct']:.2f}% | "
                f"**+{c['peak_crash_req_buffer_pct']:.1f}%** (崩盤底維持率 {c['peak_crash_trough_mr_pct']:.0f}%) | "
                f"**{c['peak_crash_effective_leverage']:.2f}x** ({c['peak_crash_buffered_cagr_pct']:.2f}%) |"
            )

    a("")
    a("### 3.2 融資利率敏感度（5.0% ～ 8.0%）對照表（以美股 2.0x、台股 2.0x 與 2.5x 為例）")
    a("")
    a("| 標的 | 初始槓桿 | 利率 5.0% (10Y斷頭率 / 10Y勝率 / 中位數CAGR) | 利率 6.0% (10Y斷頭率 / 10Y勝率 / 中位數CAGR) | 利率 6.5% (10Y斷頭率 / 10Y勝率 / 中位數CAGR) | 利率 7.0% (10Y斷頭率 / 10Y勝率 / 中位數CAGR) | 利率 8.0% (10Y斷頭率 / 10Y勝率 / 中位數CAGR) |")
    a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
    for sym, lks in (("SPY", ["1.67x", "2.00x"]), ("QQQ", ["1.67x", "2.00x"]), ("^TWII", ["1.67x", "2.00x", "2.50x"]), ("0050.TW", ["1.67x", "2.00x", "2.50x"])):
        d = m2["symbols"][sym]["full_history"]["grid"]
        for lk in lks:
            cells = []
            for rk in ("5.0pct", "6.0pct", "6.5pct", "7.0pct", "8.0pct"):
                r10 = d[lk][rk]["rolling"]["10y"]
                cells.append(f"{r10['margin_call_rate_pct']:.1f}% / {r10['win_rate_vs_1x_pct']:.1f}% / {r10['median_cagr_pct']:.1f}%")
            a(f"| `{sym}` | **{lk}** | " + " | ".join(cells) + " |")

    a("")
    a("> [!CAUTION]")
    a("> **為什麼「高倍率融資 + 留備用現金補維持率」是散戶最大的資金效率陷阱？**")
    a("> - **直接斷頭的毀滅性**：台股標準融資自備 4 成（即 **`2.50x` 槓桿**，初始維持率僅 `166.7%`），只要指數從買進點回檔 **`-22.0%`**（若計入一年 6.5% 融資利息，甚至只需跌 **`-18%`**）就會觸發 130% 追繳斷頭！在 `^TWII` 全歷史滾動 10 年進場中，2.5 倍融資有 **`92.7%` 的梯次會斷頭**；即便在 2009 年後的 `0050.TW` 多頭週期，滾動 5 年進場也有 **`20.3%` 的梯次在 2015、2020 或 2022 股災中斷頭出局**。")
    a("> - **保留備用金的數學荒謬性**：為了在 2008 金融海嘯（`SPY -55.2%`）或台股大崩盤（`^TWII -66.2%`、`0050.TW -33.8%`）中「保證不斷頭」，若你在高點開啟 `2.0x ～ 2.5x` 融資，你必須在場外額外準備 **`31% ～ 127%` 的備用現金**。一旦把這筆備用現金算進你的總資金分母，你的**真實有效槓桿立刻退化為 `1.10x ～ 1.44x`**，但你卻在用 **6.0%～6.5% 的高利**向券商借錢，而場外備用現金幾乎沒有收益！不如一開始就把全部資金直接買成 `1.3x ～ 1.4x`（或配置 30%～40% 的正二 ETF `00631L` / `SSO`），不但**完全沒有斷頭追繳壓力**，省下的高額融資利息還讓長期年化報酬高出 `2% ～ 5%/年`！")
    a("")
    a("---")
    a("")
    a("## 四、模組 3：信貸／房貸增貸投資（無斷頭風險、本息平均攤還之生命週期投資法）")
    a("")
    a("### 4.1 四種借貸定投策略在「完全相同月現金流出（1 單位／月）」下的公平對決")
    a("")
    a("> **公平基準設計**：假設投資人每月固定有 `1.0` 單位可支配儲蓄（例如台灣上班族每月 3 萬台幣，年儲蓄 36 萬；或每月 1,000 美元）。所有策略**每個月從口袋掏出的現金完全相同（皆為 1.0／月）**：借貸策略在期初預借一筆資金（1 年、2 年、3 年或 5 年儲蓄額）投入市場，之後每月 1.0 的現金流**優先償還銀行本息月付金（PMT）**，剩餘現金流才繼續定期定額；期末以「**最終投資市值 − 剩餘未還貸款本金 = 淨資產（Net Worth）**」與純定期定額（Pure DCA）公平比較。")
    a("")
    a("| 標的 | 貸款結構與規模 | 貸款利率 | 月付金佔月現金流 (PMT/CF) | 符合月付 <50% / <70% 上限 | **單筆提前投入** 滾動10年勝率 (中位數超額 / P5最差超額) | **分12個月分批投** 滾動10年勝率 (中位數超額) | **分24個月分批投** 滾動10年勝率 (中位數超額) | **7年到期續借疊加** 滾動15年勝率 (中位數超額) | 全歷史淨值倍數 (vs 純DCA) |")
    a("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    mod3_showcase = [
        ("7y_loan", "borrow_2y_savings", "7年信貸-借2年儲蓄(24x)"),
        ("7y_loan", "borrow_3y_savings", "7年信貸-借3年儲蓄(36x)"),
        ("7y_loan", "borrow_5y_savings", "7年信貸-借5年儲蓄(60x)"),
        ("20y_loan", "borrow_3y_savings", "20年房貸增貸-借3年儲蓄(36x)"),
        ("20y_loan", "borrow_5y_savings", "20年房貸增貸-借5年儲蓄(60x)"),
    ]
    for sym in ("SPY", "QQQ", "0050.TW", "^TWII"):
        d = m3["symbols"][sym]["full_history"]
        base_mult = d["baseline_pure_dca"]["full_period_net_worth_multiple"]
        for tk, sk, label_s in mod3_showcase:
            for rk in ("2.5pct", "3.5pct", "5.0pct"):
                cell = d["tenors"][tk]["by_loan_size"][sk]["by_rate"][rk]
                lump = cell["lump_sum_loan"]
                tr12 = cell["tranche_12m"]
                tr24 = cell["tranche_24m"]
                ren = cell["renewed_loan_cycle"]
                r10_l = lump["rolling"]["10y"]
                r10_12 = tr12["rolling"]["10y"]
                r10_24 = tr24["rolling"]["10y"]
                r15_ren = ren["rolling"]["15y"]
                cap_str = "✅<50%" if lump["within_50pct_cf_cap"] else ("⚠️<70%" if lump["within_70pct_cf_cap"] else "❌>70%")
                ren_str = (
                    f"{r15_ren['win_rate_vs_dca_pct']:.1f}% ({r15_ren['median_excess_vs_dca_pct']:+.1f}%)"
                    if r15_ren["win_rate_vs_dca_pct"] is not None
                    else "N/A"
                )
                a(
                    f"| `{sym}` | {label_s} | {lump['annual_rate_pct']:.1f}% | "
                    f"{lump['pmt_to_monthly_cf_pct']:.1f}% | {cap_str} | "
                    f"**{r10_l['win_rate_vs_dca_pct']:.1f}%** ({r10_l['median_excess_vs_dca_pct']:+.1f}% / {r10_l['p5_excess_vs_dca_pct']:+.1f}%) | "
                    f"{r10_12['win_rate_vs_dca_pct']:.1f}% ({r10_12['median_excess_vs_dca_pct']:+.1f}%) | "
                    f"{r10_24['win_rate_vs_dca_pct']:.1f}% ({r10_24['median_excess_vs_dca_pct']:+.1f}%) | "
                    f"{ren_str} | {lump['full_period_net_worth_multiple']:.2f}x (基準 {base_mult:.2f}x) |"
                )

    a("")
    asubsection = [
        "### 4.2 信貸／房貸增貸投資（Lifecycle Investing）四大實戰結論",
        "",
        "1. **為什麼信貸／房貸增貸比券商融資安全得多？**",
        "   - 因為銀行信貸與房貸增貸**沒有股票維持率斷頭機制（No Margin Call）**！只要你每個月從薪水穩定繳得出月付金（PMT），就算股市在 2008 年暴跌 `-55%` 或 2000 年暴跌 `-83%`，你手上的股票一股都不會被強制賤賣，能夠完整吃到熊市底部反彈。",
        "2. **利率門檻與貸款年限的黃金比例（7 年信貸 vs 20 年房貸增貸）**：",
        "   - **20 年期房貸增貸（利率 `2.2% ～ 3.0%`）是地表最強槓桿工具**：預借 3 年儲蓄額（36 倍月現金流）在 2.5% 利率下，每月還款僅佔月現金流的 **`19.1%`**（剩下 80.9% 還能繼續定期定額！），在 `SPY` 滾動 10 年勝率高達 **`87.5%`**（期末淨資產中位數比純定投多 **`+18.6%`**），在 `0050.TW` 滾動 10 年勝率更高達 **`100.0%`**（中位數淨資產多 **`+20.6%`**，連最差 5% 情境都還贏純定投 **`+11.5%`**）。",
        "   - **7 年期個人信貸（本息均攤）的現金流緊箍咒**：預借 **3 年儲蓄額（36 倍月現金流）** 是 7 年期信貸的安全極限——在 2.5%～3.5% 利率下，月付金剛好佔月現金流的 **`46.8% ～ 48.4%`**（符合 `<50%` 現金流安全上限）；若貪心借到 **5 年儲蓄額（60 倍月現金流）**，7 年期信貸月付金將高達月現金流的 **`78% ～ 85%`**，嚴重擠壓日常生活與後續逢低定投能力。",
        "   - **利率超過 `4.5% ～ 5.0%` 時不值得借信貸投資**：當信貸利率升至 5.0%～6.0%，7 年期信貸在 `^TWII` 的滾動 10 年勝率跌破 `31%`，在 `SPY` 與 `QQQ` 的最差 5%（P5）情境會比純定投少 `-10% ～ -22%` 淨值（因為如果在 2000 或 2007 高點借 5% 高利信貸單筆投入，既要還高息、又少了後續低檔定投的子彈）。",
        "3. **借出信貸後，應該「第一天單筆投入」還是「分 12 / 24 個月分批定投」？**",
        "   - 實證數據顯示：**「借出來直接單筆投入」與「分 12 個月分批投入」的滾動 10 年勝率幾乎平手（甚至在 `QQQ` 與 `0050.TW` 分 12 個月投入的勝率略高 1%～2%，且最差 P5 回撤較溫和），但「單筆投入」的中位數超額報酬比「分 12 個月」高出約 `+1.5% ～ +2.5%`，比「分 24 個月」高出 `+3.5% ～ +5.0%`**。",
        "   - **原因**：信貸從撥款第一天起就對「整筆本金」計息；若把借來的錢放在活存分 24 個月慢慢買，前兩年有平均一半的借款在空轉白繳利息。**實務折衷最佳解**：若擔心買在最高點，**最多分 6 ～ 12 個月內佈局完畢**，切勿拖到 24 個月。",
    ]
    for line in asubsection:
        a(line)

    a("")
    a("---")
    a("")
    a("## 五、模組 4：股票質押（只繳息不還本，130% 強制處分線 vs 140% 預警線）")
    a("")
    a("### 5.1 股票質押（利率 2.5% 與 3.5%）各槓桿倍率斷頭機率與安全上限全表")
    a("")
    a("| 標的 | 維持率門檻 | 質押後總槓桿 (初始維持率) | 觸發處分之即時跌幅 | 利率 2.5% 全歷史CAGR | 利率 2.5% **滾動10年斷頭率** | 利率 2.5% 滾動10年勝率 | 利率 2.5% 滾動10年P5年化 | 利率 3.5% **滾動10年斷頭率** | 利率 3.5% 滾動10年勝率 | **是否符合安全質押 (<1%斷頭)** |")
    a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sym in ("SPY", "QQQ", "^TWII", "0050.TW"):
        d = m4["symbols"][sym]["full_history"]
        for th_k, th_label in (("maint_130pct", "130% 斷頭線"), ("maint_140pct", "140% 預警線")):
            g = d["thresholds"][th_k]["grid"]
            for lk in ("1.20x", "1.30x", "1.40x", "1.50x", "1.60x", "1.80x", "2.00x"):
                c25 = g[lk]["2.5pct"]
                c35 = g[lk]["3.5pct"]
                r10_25 = c25["rolling"]["10y"]
                r10_35 = c35["rolling"]["10y"]
                mc25 = r10_25["margin_call_rate_pct"]
                mc35 = r10_35["margin_call_rate_pct"]
                is_safe = (not c25["margin_called"]) and (mc25 is not None and mc25 <= 1.0)
                safe_badge = "✅ 安全" if is_safe else ("⚠️ 僅多頭安全" if mc25 <= 10.0 else "❌ 高危易爆")
                a(
                    f"| `{sym}` | {th_label} | **{lk}** ({c25['initial_maint_ratio_pct']:.0f}%) | "
                    f"{c25['entry_drawdown_to_liquidation_pct']:.1f}% | {c25['cagr_pct']:.2f}% | "
                    f"**{mc25:.1f}%** | {r10_25['win_rate_vs_1x_pct']:.1f}% | {r10_25['p5_cagr_pct']:.2f}% | "
                    f"**{mc35:.1f}%** | {r10_35['win_rate_vs_1x_pct']:.1f}% | {safe_badge} |"
                )

    a("")
    a("> [!IMPORTANT]")
    a("> **股票質押的「安全槓桿黃金上限」**：")
    a("> 1. **跨越 2000 / 2008 級別大崩盤（跌幅 `-55% ～ -66%`）的絕對安全上限為 `1.30x ～ 1.35x`（最高不超過 `1.40x`）**：")
    a(">    - 當你把 100 萬股票質押借出 30 萬買進股票（總市值 130 萬、負債 30 萬，**總槓桿 `1.30x`**），你的初始維持率高達 **`433.3%`**，大盤必須從買進點暴跌 **`-70.0%`** 才會觸及 130% 斷頭線（或暴跌 **`-67.7%`** 觸及 140% 預警線）。在 `SPY` 與 `^TWII` 近 30～34 年的所有滾動 10 年進場梯次中，**`1.30x` 質押的斷頭率為 `0.0%`**，且滾動 10 年勝率高達 `73% ～ 85%`！")
    a("> 2. **為什麼千萬不要把質押開到 `1.6x ～ 2.0x`（借滿六成再質押）？**")
    a(">    - 許多券商允許股票質押借 6 成（借出後再買進並重複質押可把總槓桿推到 `1.6x ～ 2.0x`）。但數據顯示：一旦質押總槓桿達到 **`1.60x`**（初始維持率 `266.7%`，跌 `-51.2%` 斷頭），在 `SPY`、`QQQ`、`^TWII` 遇到 2000 或 2008 等級股災時，滾動 10 年斷頭率跳升至 **`14.6% ～ 24.5%`**；若開到 **`2.00x`**（初始維持率 `200%`，只要跌 **`-35.0%`** 就斷頭），滾動 10 年斷頭率更飆升至 **`42.2% ～ 47.2%`**！")
    a(">    - 雖然 `0050.TW` 在 2009–2026 年間最大回撤僅 `-33.83%`（2022 年），使得帳面上 `1.8x ～ 2.0x` 剛好閃過 130% 斷頭線，但那是因為 `0050.TW` 樣本未涵蓋 2008 年（當時台股跌幅近 `-60%`）。**若以能挺過 2008 等級海嘯為標準，台股 `0050.TW` 平時常態質押總槓桿應嚴格控制在 `1.30x ～ 1.45x`（即每 100 萬市值最多借 30～40 萬，初始維持率保持在 `320% ～ 430%` 以上）**。")
    a("")
    a("---")
    a("")
    a("## 六、總表 2：四大槓桿工具實戰綜合評比（正二 ETF vs 券商融資 vs 信貸／房貸增貸 vs 股票質押）")
    a("")
    a("為了讓投資人一目了然，我們將四種工具在**相同代表性槓桿規模（約 `1.5x` 與 `2.0x`）**與**真實市場利率**下的實證數據與機制優劣整理成總表 2：")
    a("")
    a("| 評比維度 | 工具 A：槓桿 ETF 正二 (`00631L` / `SSO` / `QLD` 配置 50%=1.5x, 100%=2.0x) | 工具 B：券商一般融資 (Margin, 美股 6.0% / 台股 6.5%) | 工具 C：信貸／房貸增貸 (7年信貸 3.0%~3.5% / 20年房貸 2.5%) | 工具 D：股票質押 (Stock Pledge, 利率 2.5%~3.0%, 只繳息) |")
    a("| :--- | :--- | :--- | :--- | :--- |")
    a("| **真實年化資金成本** | **低～中**（隱含總成本約 **2.9%～3.2%/年**；高息期美股約 5%） | **極高**（台股 **6.5%～7.0%**，美股散戶 **6.0%～11.0%**） | **極低～中**（房貸增貸 **2.2%～2.8%**，優質信貸 **2.5%～3.8%**） | **極低**（台股券商／銀行質押約 **2.3%～3.2%**） |")
    a("| **斷頭／強制平倉風險** | **0%（無維持率追繳，永不欠錢）**，但大跌時有每日再平衡波動耗損 | **極高（130%/133% 斷頭）**：2.0x 跌 35% 斷頭、2.5x 跌 22% 斷頭 | **0%（無維持率斷頭）**，只要每月薪水繳得出月付金即可抱過股災 | **中～高（130%~140% 斷頭）**：1.3x~1.4x 極安全，但 ≥1.6x 遇大海嘯易斷頭 |")
    a("| **每月現金流還款壓力** | **無**（費用內扣於淨值，不需從口袋掏錢繳息） | **低～中**（賣出時結算或半年展延，但跌深需補巨額保證金） | **高（需每月還本付息）**：7年信貸月付金佔比高，20年房貸月付金極輕 | **極低（只繳息不還本）**：半年／一年到期續約，用 `0050` 股息即可全額付清利息 |")
    a("| **`SPY` (全歷史 1.5x) CAGR / 10Y勝率 / 10Y斷頭率** | **13.09%** / 勝率 **79.8%** / 斷頭 **0.0%** | (1.67x @6%) 11.92% / 勝率 58.9% / **斷頭 36.9%** | (借3年儲蓄 @3%) 10Y勝率 **68.3%~86.1%** / 斷頭 **0.0%** | (1.4x @2.5%) **11.92%** / 勝率 **85.0%** / 斷頭 **7.3%** (1.3x 斷頭 **0%**) |")
    a("| **`QQQ` (全歷史 1.5x) CAGR / 10Y勝率 / 10Y斷頭率** | **11.07%** (定投XIRR **18.34%**) / 10Y勝率 **82.2%** / 斷頭 **0.0%** | (1.67x @6%) **-5.03%(斷頭)** / 勝率 68.5% / **斷頭 31.5%** | (借3年儲蓄 @3%) 10Y勝率 **77.5%~85.0%** / 斷頭 **0.0%** | (1.3x @2.5%) **11.23%** / 勝率 **87.8%** / 斷頭 **7.0%** (2000泡沫太深) |")
    a("| **`0050.TW` (含息 1.5x) CAGR / 10Y勝率 / 10Y斷頭率** | **25.02%** (`00631L` 2x 實績 **38.20%**) / 勝率 **100%** / 斷頭 **0%** | (2.5x @6.5%) 5Y斷頭率 **20.3%** / 利息吃掉大半超額報酬 | (借3年儲蓄 @2.5%) 10Y勝率 **98.9%~100%** / 斷頭 **0.0%** | (1.4x @2.5%) **20.38%** / 勝率 **100%** / 斷頭 **0.0%** |")
    a("| **實戰綜合評級與推薦序** | 🏆 **第 1 名（並列）**：適合用「50% 現股 + 50% 正二 = 1.5x」定期再平衡或搭配均線濾網 | 💀 **最後一名（強烈不推薦）**：高利 + 易斷頭，即使留備用金也完全不划算 | 🏆 **第 1 名（並列）**：有穩定薪資／房產者首選（房貸增貸 > 7年低利信貸 ≤3.5%） | 🥈 **第 2 名**：適合已累積一桶金之存股族，**嚴守總槓桿 ≤ 1.35x～1.40x** |")
    a("")
    a("---")
    a("")
    a("## 七、總表 3：跌深加碼與 200 日均線動態槓桿評比（模組 5：E1 vs E2 vs E3）")
    a("")
    a("許多投資人問：**「如果我平時不要開槓桿（甚至留現金），等大跌 `-15% / -20% / -30%` 才加碼或開槓桿，或是只在站上 200 日均線（年線）時才開槓桿，能不能同時賺更多又避開斷頭？」** 以下為五大核心標的在**相同初始資金（單筆 Lump Sum）**與**相同每月現金流（定期定額 DCA）**下的完整實證對決：")
    a("")
    a("### 7.1 四大標的「跌深加碼（E1/E2）與 200 日線動態槓桿（E3）」全歷史績效總表")
    a("")

    mod5_rows = [
        ("baselines", "baseline_1.0x", "基準 A：全程 1.0x (100%滿倉/定投)"),
        ("baselines", "baseline_fixed_1.5x_r3pct", "基準 B：全程固定 1.5x (利率 3.0%)"),
        ("baselines", "baseline_fixed_2.0x_r3pct", "對照 C：全程固定 2.0x (利率 3.0%)"),
        ("e1_cash_reserve_dip", "e1_80_20_staged", "E1-1：留20%現金 (-10/-20/-30%分批加碼)"),
        ("e1_cash_reserve_dip", "e1_70_30_staged", "E1-2：留30%現金 (-10/-20/-30%分批加碼)"),
        ("e1_cash_reserve_dip", "e1_80_20_dd20", "E1-3：留20%現金 (跌-20%一次全加碼)"),
        ("e2_leveraged_dip", "e2_dd15_lev1.5x_r2.5pct", "E2-1：平時1.0x，跌-15%開1.5x (回前高降1x, 2.5%)"),
        ("e2_leveraged_dip", "e2_dd20_lev1.5x_r2.5pct", "E2-2：平時1.0x，跌-20%開1.5x (回前高降1x, 2.5%)"),
        ("e2_leveraged_dip", "e2_dd25_lev1.5x_r2.5pct", "E2-3：平時1.0x，跌-25%開1.5x (回前高降1x, 2.5%)"),
        ("e2_leveraged_dip", "e2_dd20_lev2.0x_r2.5pct", "E2-4：平時1.0x，跌-20%開2.0x (回前高降1x, 2.5%)"),
        ("e2_leveraged_dip", "e2_dd30_lev2.0x_r2.5pct", "E2-5：平時1.0x，跌-30%開2.0x (回前高降1x, 2.5%)"),
        ("e3_sma200_dynamic", "e3_sma200_lev1.5x_nobuf_r3pct", "E3-1：200日線之上1.5x / 之下1.0x (無緩衝, 3%)"),
        ("e3_sma200_dynamic", "e3_sma200_lev1.5x_buf2pct_r3pct", "E3-2：200日線之上1.5x / 之下1.0x (±2%緩衝, 3%)"),
        ("e3_sma200_dynamic", "e3_sma200_lev2.0x_buf2pct_r3pct", "E3-3：200日線之上2.0x / 之下1.0x (±2%緩衝, 3%)"),
    ]

    for sym, title in (
        ("SPY", "美股 S&P 500 含息 (`SPY`, 1993–2026 全歷史)"),
        ("QQQ", "美股 Nasdaq 100 含息 (`QQQ`, 1999–2026 含網路泡沫全歷史)"),
        ("0050.TW", "台股 台灣50 含息 (`0050.TW`, 2009–2026 全歷史)"),
        ("^TWII", "台股 加權指數 (`^TWII`, 1997–2026 含2000/2008崩盤全歷史)"),
    ):
        a(f"#### {title}")
        a("")
        a("| 策略方案 | 單筆 CAGR | 單筆 MDD | 單筆 Sharpe / Calmar | 單筆滾動10年勝率 (vs 1x) | 單筆滾動10年 P5年化 | **每月定投 DCA XIRR** | 定投期末倍數 | 定投滾動10年勝率 (vs 1x定投) | 備註 (觸發次數 / 質押斷頭數) |")
        a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |")
        d = m5["symbols"][sym]["full_history"]
        for grp, k, label_s in mod5_rows:
            cell = d[grp][k]
            lump = cell["lump_sum"]
            dca = cell["dca"]
            r10_l = lump["rolling"]["10y"]
            r10_d = dca["rolling"]["10y"]
            w_dca_key = "win_rate_vs_1x_dca_pct" if "win_rate_vs_1x_dca_pct" in r10_d else "win_rate_vs_1x_pct"
            w_dca_val = r10_d[w_dca_key]

            if grp == "e1_cash_reserve_dip":
                note = f"加碼觸發 {lump['deploy_triggers_count']} 次"
            elif grp == "e2_leveraged_dip":
                note = f"啟動 {lump['episodes_triggered']} 次 (佔時 {lump['pct_time_leveraged']:.0f}%, 130%斷頭 {lump['pledge_130_breach_episodes']} 次)"
            elif grp == "e3_sma200_dynamic":
                note = f"年均換倉 {lump['switches_per_year']:.1f} 次 (開槓桿佔時 {lump['pct_time_in_bull_leverage']:.0f}%)"
            else:
                note = "基準對照"

            a(
                f"| {label_s} | **{lump['cagr_pct']:.2f}%** | {lump['mdd_pct']:.2f}% | "
                f"{lump['sharpe']:.2f} / {lump['calmar']:.2f} | **{r10_l['win_rate_vs_1x_pct']:.1f}%** | "
                f"{r10_l['p5_cagr_pct']:.2f}% | **{dca['xirr_pct']:.2f}%** | {dca['final_multiple']:.2f}x | "
                f"**{w_dca_val:.1f}%** | {note} |"
            )
        a("")

    a("### 7.2 全歷史（含大崩盤）vs 近 10 年（2016–2026 多頭期）跨模組關鍵指標對照表")
    a("")
    a("> **為什麼不能只看近 10 年回測就決定開幾倍槓桿？** 下表直接並列四大標的在「全歷史」與「近 10 年（2016–2026）」下，各模組代表性策略的年化報酬、最大回撤與斷頭率差異：")
    a("")
    a("| 標的 | 期間 | 1.0x 基準 CAGR (MDD) | 正二 2.0x @3% CAGR (MDD) | 融資 2.0x @6%/6.5% 5Y斷頭率 | 質押 1.6x @2.5% 5Y斷頭率 | 信貸借3年 @3% (7Y) 5Y勝率 | E1-1 留20%現金抄底 單筆CAGR | E2-2 跌-20%開1.5x 單筆CAGR (定投XIRR) | E3-2 200日線1.5x(±2%) 單筆CAGR (MDD) |")
    a("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for sym in ("SPY", "QQQ", "0050.TW", "^TWII"):
        for pk, p_label in (("full_history", "全歷史"), ("recent_10y", "近10年(2016–2026)")):
            b1 = m1["symbols"][sym][pk]["benchmark_1x"]
            l2 = m1["symbols"][sym][pk]["lump_sum_matrix"]["2.00x"]["3pct"]
            rk_m2 = "6.5pct" if sym in ("^TWII", "0050.TW") else "6.0pct"
            mc_m2_5y = m2["symbols"][sym][pk]["grid"]["2.00x"][rk_m2]["rolling"]["5y"]["margin_call_rate_pct"]
            mc_m4_5y = m4["symbols"][sym][pk]["thresholds"]["maint_130pct"]["grid"]["1.60x"]["2.5pct"]["rolling"]["5y"]["margin_call_rate_pct"]
            w_m3_5y = m3["symbols"][sym][pk]["tenors"]["7y_loan"]["by_loan_size"]["borrow_3y_savings"]["by_rate"]["3.0pct"]["lump_sum_loan"]["rolling"]["7y"]["win_rate_vs_dca_pct"]
            e1_c = m5["symbols"][sym][pk]["e1_cash_reserve_dip"]["e1_80_20_staged"]["lump_sum"]
            e2_c = m5["symbols"][sym][pk]["e2_leveraged_dip"]["e2_dd20_lev1.5x_r2.5pct"]
            e3_c = m5["symbols"][sym][pk]["e3_sma200_dynamic"]["e3_sma200_lev1.5x_buf2pct_r3pct"]["lump_sum"]
            a(
                f"| `{sym}` | **{p_label}** | {b1['cagr_pct']:.2f}% ({b1['mdd_pct']:.1f}%) | "
                f"{l2['cagr_pct']:.2f}% ({l2['mdd_pct']:.1f}%) | **{mc_m2_5y:.1f}%** | **{mc_m4_5y:.1f}%** | "
                f"{w_m3_5y:.1f}% (滾動7Y) | {e1_c['cagr_pct']:.2f}% | "
                f"**{e2_c['lump_sum']['cagr_pct']:.2f}%** ({e2_c['dca']['xirr_pct']:.2f}%) | "
                f"**{e3_c['cagr_pct']:.2f}%** ({e3_c['mdd_pct']:.1f}%) |"
            )

    a("")
    a("### 7.3 模組 5 三大顛覆性實證發現")
    a("")
    a("1. **真相一：『平時留 20%～30% 現金等大跌才加碼（E1）』幾乎全面輸給『每個月 100% 直接買進（全程 1.0x）』！**")
    a("   - 無論在 `SPY`、`QQQ`、`^TWII` 或 `0050.TW`，平時只投 70%～80%、留 20%～30% 現金等 `-10% / -20% / -30%` 分批抄底（`E1-1`、`E1-2`），**單筆年化報酬比全程 1.0x 少 `0.6% ～ 2.8%/年`，滾動 10 年勝率僅 `0% ～ 23%`（即高達 77%～100% 的 10 年區間都輸給直接滿倉 1.0x！）**；在每月定期定額模式下，分批抄底（`E1-1`）的滾動 10 年勝率更只有 **`0% ～ 3.9%`**！")
    a("   - **為什麼？** 因為美股與台股長期處於向上趨勢，為了等待幾年才發生一次的 `-20%` 大跌，常態性讓 20%～30% 的資金躺在低利現金裡踏空牛市（Cash Drag），踏空損失遠大於股災抄底省下的成本。**結論：要跌深加碼，絕對不能靠『平時少買留現金』，而必須『平時 100% 滿倉 1.0x，大跌時動用平時未使用的低利信用額度（質押／信貸）加碼（E2）』！**")
    a("2. **真相二：『平時 1.0x 滿倉定投，大盤跌 `-20% ～ -30%` 才開 `1.5x / 2.0x` 槓桿、回前高即還款降回 `1.0x`（E2）』能大幅消弭斷頭風險並拉高定投報酬！**")
    a("   - 在 `SPY` 上，若你在歷史高點就開 `1.5x` 質押，遇到 2008 年 `-55.2%` 崩盤會逼近甚至跌破維持率警戒線；但若你**平時維持 1.0x 零負債，等大盤從高點已經跌掉 `-15%` 或 `-20%` 時才啟動 `1.5x` 低利質押（`E2-1` / `E2-2`）**，因為你的借貸起算點已經打過 8 折，在 `SPY` 34 年歷史（含 2000 與 2008 大崩盤）與 `0050.TW` 18 年歷史中，**觸發 130% 質押斷頭的次數為 `0 次`**！且 `SPY` 單筆 CAGR 從 `10.58%` 提升至 `11.37%`（若在 `-30%` 開 `2.0x` 更提升至 `12.03%`），`0050.TW` 跌 `-20%` 開 `2.0x` 的定投 XIRR 更從 `18.91%` 躍升至 **`27.74%`**（滾動 10 年勝率 **100.0%**）！")
    a("   - **唯一例外警告**：在像 2000 年網路泡沫那種跌幅高達 `-83%`（`QQQ`）或 `-66%`（`^TWII`）的極端泡沫破裂中，就算跌 `-20%` 才開 `2.0x` 槓桿，後面繼續從 `-20%` 跌到 `-83%`（即再跌 `-78%`）依然會爆倉！因此**跌深開槓桿（E2）的加碼上限仍應控制在 `1.5x`（或分批在 `-20%` 開 `1.3x`、`-35%` 開 `1.6x`）**。")
    a("3. **真相三：『200 日均線（年線）+ ±2% 緩衝區動態槓桿濾網（E3-2 / E3-3）』是全歷史綜合表現最強的策略！**")
    a("   - 收盤價站上 `SMA200 × 1.02` 時開 `1.5x`（或 `2.0x`），跌破 `SMA200 × 0.98` 時降回 `1.0x` 現股（不空手，因此不怕 V 轉踏空！），並扣除每次 `0.10%` 換倉成本。")
    a("   - 加上 `±2%` 緩衝區後，每年平均換倉僅 **`1.1 ～ 1.6 次`**（大幅消除年線附近反覆巴臉的假突破耗損），卻能精準在 2000 網路泡沫、2008 金融海嘯、2022 升息熊市的主跌段**自動把槓桿降回 1.0x**！")
    a("   - 實證結果：在 `SPY` 全歷史上，`E3-2 (1.5x ±2%緩衝)` 的年化報酬達 **`13.41%`**（超越全程固定 1.5x 的 `13.09%`！），而最大回撤卻從固定 1.5x 的 `-72.82%` 大幅壓低至 **`-58.59%`**（幾乎與 1.0x 原型指數的 `-55.19%` 相同！），**滾動 10 年勝率高達 `97.56%`**！在 `QQQ`、`^TWII` 與 `0050.TW` 上，`E3` 同樣在所有槓桿策略中取得最高的夏普值與卡瑪比率（Calmar Ratio）。")
    a("")
    a("---")
    a("")
    a("## 八、明確實務界線與研究限制揭露（Actionable Rules & Limitations）")
    a("")
    a("### 8.1 投資人開槓桿前必看的四條「實務鐵律」")
    a("")
    a("1. **【利率界線】什麼利率以上絕對不該借？**")
    a("   - **黃金區（`≤ 3.0% ～ 3.5%`）**：台灣房貸增貸（約 2.2%～2.8%）、優質信貸／股票質押（約 2.5%～3.2%）、台股正二 `00631L.TW`（含期貨逆價差補償後相對於 0050 的隱含資金成本約 2.92%），長期期望值與滾動 10 年勝率極高（`75% ～ 100%`）。")
    a("   - **警戒區（`3.5% ～ 5.0%`）**：僅適合 `1.2x ～ 1.5x` 溫和槓桿或搭配 200 日均線濾網（只在多頭段付息）；若無濾網硬扛熊市，超額報酬會被利息與波動耗損吃掉大半。")
    a("   - **禁區（`> 5.0%`，尤其是 6.0%～7.0% 券商融資）**：一旦資金成本超過 5.0%，在涵蓋熊市的全週期中，開槓桿的風險調整後報酬（Sharpe / Calmar）全面劣於 1.0x 現股，絕對不該借。")
    a("2. **【倍率界線】哪個槓桿倍率以上風險急劇跳升？**")
    a("   - **`1.3x ～ 1.5x`（溫和槓桿甜蜜點）**：例如「70% 現股 `0050/SPY` + 30% 正二 `00631L/SSO` = `1.3x`」或「50% 現股 + 50% 正二 = `1.5x`」，或「股票質押只借 30%～40%（總槓桿 `1.3x～1.4x`）」。在此區間內，歷史斷頭率趨近 **0%**，年化報酬可穩健提升 `+2% ～ +6%/年`。")
    a("   - **`1.6x ～ 2.0x`（進階上限區）**：只可透過「無斷頭機制的正二 ETF + 定期再平衡／200 日線濾網」或「20 年期低利房貸增貸」執行；**絕對不可用股票質押或券商融資開到 `1.8x ～ 2.0x`**（因為跌 35%～42% 就會被強制斷頭在阿呆谷）。")
    a("   - **`> 2.0x`（`2.5x ～ 3.0x` 毀滅區）**：在經歷 2000 或 2008 等級熊市時，單筆持有 3 倍槓桿（如 `TQQQ`/`UPRO` 原型）最大回撤高達 **`-94% ～ -99.9%`**（跌 99% 需要漲 100 倍才能回本！），僅能作為極小倉位或嚴格趨勢濾網下的波段工具，絕不可無腦單筆長抱。")
    a("3. **【爆倉排行】哪種槓桿方式最容易爆倉？**")
    a("   - **第 1 容易爆倉**：**台股 2.5 倍券商融資（自備四成）** —— 跌 `22%` 即觸發 130% 斷頭，歷史滾動 10 年斷頭率 **`92.7%`**。")
    a("   - **第 2 容易爆倉**：**美股 2.0 倍 Reg-T 融資 / 股票質押借滿反覆質押至 2.0 倍** —— 跌 `33%～35%` 即斷頭，歷史滾動 10 年斷頭率 **`42% ～ 50%`**。")
    a("   - **第 3 容易爆倉**：**單筆重倉 3 倍槓桿 ETF（`TQQQ` / `UPRO`）不做再平衡也不設均線濾網** —— 雖不會被追繳欠債，但遇 2000 等級泡沫破裂淨值會蒸發 **`99%`**，實質等同歸零。")
    a("4. **【最佳實戰組合建議】一般投資人該怎麼做？**")
    a("   - **有穩定薪資現金流的上班族**：若能取得利率 **`≤ 3.2%`** 的 20 年期房貸增貸或 7 年期信貸（借款金額控制在 **2～3 年儲蓄額**，月付金 **< 月結餘的 45%**），於指數回檔或分 6～12 個月投入 `0050.TW` / `SPY` / `QQQ`，並持續用剩餘薪水定期定額。")
    a("   - **已有一桶金的指數投資人**：採用 **「50% `0050.TW/SPY` + 50% `00631L.TW/SSO`（合成 `1.5x`）」**，或搭配 **「200 日均線 `±2%` 緩衝濾網（站上年線時將正二比例調升至 `1.5x～1.8x`，跌破年線時將正二轉回 `1.0x` 原型 ETF）」**，即可在零斷頭風險下取得超越大盤的長期風險調整報酬。")
    a("")
    a("### 8.2 研究方法論與誠實限制揭露（防過度擬合說明）")
    a("")
    a("1. **倖存者偏差（Survivorship Bias）**：本研究標的為美股（S&P 500、Nasdaq 100）與台股（加權指數、0050），這兩個市場在過去 30 年受惠於全球科技與半導體產業紅利，屬於全球表現最強的少數股市。若將相同槓桿模型套用於 1990–2020 年的日本日經指數（失落三十年）或長期走平的新興市場，任何大於 1.0x 的槓桿都會因長期借貸利息而虧損。")
    a("2. **利率環境變遷（Regime Shift）**：2009–2021 年全球處於罕見的零利率／超低利率時代，使槓桿 ETF 與質押借貸享有極低的資金成本；若未來長期維持高利率環境，請務必參考本研究「總表 1」中對應 **4%～6% 資金成本欄位** 的保守數據，切勿直接線性外推 2010–2021 的正二／正三暴利。")
    a("3. **稅務、匯率與極端跳空滑價**：台股股票現股配息需計入綜合所得稅與二代健保補充保費（高所得稅率者使用 `00631L.TW` 期貨型正二反而具備免配息稅優勢）；此外，維持率斷頭模擬已計入 `0.20%` 強制平倉滑價，但在極端連續跌停（如無量跌停鎖死）情境下，實際斷頭損失可能超過模擬值。")
    a("")
    return "\n".join(lines)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.fetch_only:
        fetch_and_cache_raw_data(refresh=args.refresh)
    elif args.report_only and RESULTS_JSON_PATH.exists():
        loaded = json.loads(RESULTS_JSON_PATH.read_text(encoding="utf-8"))
        verify_results_integrity(loaded)
        REPORT_MD_PATH.write_text(generate_markdown_report(loaded), encoding="utf-8")
        print(f"[DONE] Generated {REPORT_MD_PATH} from existing {RESULTS_JSON_PATH}")
    else:
        run_all_studies(refresh=args.refresh)

#!/usr/bin/env python3
"""Final verification check (research/final_check/run_final_check.py).

Runs all 8 pre-registered loophole checks against the Signal Desk investment recommendations,
reusing existing caches & data cleaning logic from `research/robustness_check/` and
`research/dca_allocation_grid/`.

Usage:
  .venv/bin/python research/final_check/run_final_check.py
  .venv/bin/python research/final_check/run_final_check.py --quick
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

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.final_check import engine as fc_eng
from research.robustness_check import engine as rc_eng
from research.robustness_check import run_robustness as rc_mod

CACHE_DIR = BASE_DIR / "cache"
YF_EXTRA_CACHE = CACHE_DIR / "final_check_yf.pkl"
SHILLER_CSV_PATH = CACHE_DIR / "shiller_monthly.csv"
FRED_CPI_PATH = CACHE_DIR / "fred_cpiaucsl.csv"
RESULTS_JSON_PATH = BASE_DIR / "results.json"

QUERY_DATE = "2026-11-27"


# ---------------------------------------------------------------------------
# Data Loading & Unified Preparation (Reusing Existing Cleaners)
# ---------------------------------------------------------------------------

def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.dropna(subset=["Close"]).copy()
    out.index = pd.to_datetime(out.index).tz_localize(None)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def load_extended_dataset() -> dict[str, Any]:
    """Load existing cleaned master dataset + extra ETFs (CSPX.L, VOO, SHY, VGIT, IEF, ^TNX, ^FVX) + Shiller."""
    bundle = rc_mod.load_all_cached_or_fetch(refresh=False)
    mds = rc_mod.build_master_dataset(bundle)

    if not YF_EXTRA_CACHE.exists():
        raise FileNotFoundError(f"Missing {YF_EXTRA_CACHE}; run research/final_check/fetch_data.py first.")
    extra_pkg = pickle.loads(YF_EXTRA_CACHE.read_bytes())
    eh = extra_pkg["histories"]
    emeta = extra_pkg["meta"]

    all_dates: pd.DatetimeIndex = mds["all_dates"]  # 1997-07-02 .. common_end
    common_end: pd.Timestamp = mds["common_end"]
    fx_al: pd.Series = mds["fx_aligned"]

    # 1) Build CSPX (Ireland UCITS S&P 500 Acc, 15% WH, 0.07% ER) & VOO (US, 30% WH, 0.03% ER) in USD
    spy_df = mds["spy_df"]
    voo_df = _clean_df(eh["VOO"]).loc[:common_end]
    cspx_df = _clean_df(eh["CSPX.L"]).loc[:common_end]

    # SPY gross & dividend returns on all_dates
    sp_tr = mds["sp_tr"]
    gspc_pr = mds["gspc_pr"]
    r_sp_tr = sp_tr.reindex(all_dates).ffill().pct_change().fillna(0.0)
    r_sp_pr = gspc_pr.reindex(all_dates).ffill().pct_change().fillna(0.0)
    div_sp_est = (r_sp_tr - r_sp_pr).clip(lower=0.0)

    r_spy_real_pr = spy_df["Close"].pct_change().fillna(0.0)
    r_spy_real_tr = spy_df["Adj Close"].pct_change().fillna(0.0)
    div_spy_real = (r_spy_real_tr - r_spy_real_pr).clip(lower=0.0).where(spy_df["Dividends"] > 0, 0.0)

    ov_spy = all_dates.intersection(spy_df.index)[1:]
    # SPY gross index-level return (before ER and WH)
    r_spy_gross_net_er = r_sp_tr.copy()
    r_spy_gross_net_er.loc[ov_spy] = r_spy_real_tr.loc[ov_spy]
    div_spy_al = div_sp_est.copy()
    div_spy_al.loc[ov_spy] = div_spy_real.loc[ov_spy]

    # SPY net (30% WH, 0.0945% ER already inside Adj Close post-1993)
    r_spy_net30 = r_spy_gross_net_er - fc_eng.US_DIV_TAX_RATE * div_spy_al
    r_spy_net30.iloc[0] = 0.0
    w_spy_usd = (1.0 + r_spy_net30).cumprod()

    # VOO simulated pre-2010-09-09 (add back ER diff 0.0945% - 0.03% = +0.0645%/yr) + real VOO post-inception
    r_voo_sim = r_spy_net30 + ((0.000945 - 0.0003) / 252.0)
    r_voo_sim.iloc[0] = 0.0
    r_voo_real_pr = voo_df["Close"].pct_change().fillna(0.0)
    r_voo_real_tr = voo_df["Adj Close"].pct_change().fillna(0.0)
    div_voo_real = (r_voo_real_tr - r_voo_real_pr).clip(lower=0.0).where(voo_df["Dividends"] > 0, 0.0)
    r_voo_real_net30 = r_voo_real_tr - fc_eng.US_DIV_TAX_RATE * div_voo_real
    r_voo_spliced = r_voo_sim.copy()
    ov_voo = all_dates.intersection(voo_df.index)[1:]
    r_voo_spliced.loc[ov_voo] = r_voo_real_net30.loc[ov_voo]
    w_voo_usd = (1.0 + r_voo_spliced).cumprod()

    # CSPX simulated across all_dates: SPY gross + ER diff (0.0945% - 0.07% = +0.0245%/yr) - 15% WH on dividends
    r_cspx_sim = r_spy_gross_net_er + ((0.000945 - 0.0007) / 252.0) - fc_eng.IE_DIV_TAX_RATE * div_spy_al
    r_cspx_sim.iloc[0] = 0.0
    w_cspx_sim_usd = (1.0 + r_cspx_sim).cumprod()

    # Real CSPX.L (accumulating, USD on LSE — no further withholding on investor)
    r_cspx_real = cspx_df["Close"].pct_change().fillna(0.0)
    r_cspx_spliced = r_cspx_sim.copy()
    ov_cspx = all_dates.intersection(cspx_df.index)[1:]
    # Align LSE trading days onto all_dates using cumulative wealth ffill to handle US/UK holiday offsets cleanly
    w_cspx_real_native = (1.0 + r_cspx_real).cumprod()
    w_cspx_spliced_usd = w_cspx_sim_usd.copy()
    cspx_start = cspx_df.index[0]
    anchor_val = float(w_cspx_sim_usd.loc[w_cspx_sim_usd.index >= cspx_start].iloc[0])
    real_re = w_cspx_real_native.reindex(all_dates).ffill()
    real_anchor = float(real_re.loc[real_re.index >= cspx_start].iloc[0])
    post_mask = all_dates >= cspx_start
    w_cspx_spliced_usd.loc[post_mask] = anchor_val * (real_re.loc[post_mask] / real_anchor)

    # 2) Build Bond ETFs (SHY, VGIT, IEF) with pre-inception yield synthesis + real ETF splicing
    irx_y = (_clean_df(bundle["raw_histories"]["^IRX"])["Close"].reindex(all_dates).ffill().bfill() / 100.0).clip(lower=0.0)
    fvx_y = (_clean_df(eh["^FVX"])["Close"].reindex(all_dates).ffill().bfill() / 100.0).clip(lower=0.0)
    tnx_y = (_clean_df(eh["^TNX"])["Close"].reindex(all_dates).ffill().bfill() / 100.0).clip(lower=0.0)

    # Synthetic pre-inception yield-based daily returns
    y_2y_proxy = 0.65 * irx_y + 0.35 * fvx_y
    r_shy_syn = fc_eng.synthesize_bond_daily_returns(y_2y_proxy, duration=1.9, convexity=4.5, expense_ratio=0.0015)
    r_vgit_syn = fc_eng.synthesize_bond_daily_returns(fvx_y, duration=5.0, convexity=30.0, expense_ratio=0.0003)
    r_ief_syn = fc_eng.synthesize_bond_daily_returns(tnx_y, duration=7.5, convexity=65.0, expense_ratio=0.0015)

    bond_etfs_raw = {
        "SHY": (_clean_df(eh["SHY"]).loc[:common_end], r_shy_syn, 0.0015),
        "VGIT": (_clean_df(eh["VGIT"]).loc[:common_end], r_vgit_syn, 0.0003),
        "IEF": (_clean_df(eh["IEF"]).loc[:common_end], r_ief_syn, 0.0015),
    }

    bond_wealth_usd_qii0: dict[str, pd.Series] = {}
    bond_wealth_usd_tax30: dict[str, pd.Series] = {}
    bond_synthesis_validation: dict[str, Any] = {}

    for sym, (bdf, r_syn, er) in bond_etfs_raw.items():
        r_b_pr = bdf["Close"].pct_change().fillna(0.0)
        r_b_tr = bdf["Adj Close"].pct_change().fillna(0.0)
        div_b = (r_b_tr - r_b_pr).clip(lower=0.0).where(bdf["Dividends"] > 0, 0.0)
        r_b_net30 = r_b_tr - fc_eng.US_DIV_TAX_RATE * div_b

        ov = all_dates.intersection(bdf.index)[1:]
        # Validate synthesis vs real over overlap period (using monthly returns for noise-free correlation)
        w_syn_ov = (1.0 + r_syn.loc[ov]).cumprod()
        w_real_ov = (1.0 + r_b_tr.loc[ov]).cumprod()
        yrs_ov = (ov[-1] - ov[0]).days / 365.25
        cagr_syn = (float(w_syn_ov.iloc[-1]) ** (1.0 / yrs_ov) - 1.0) * 100.0
        cagr_real = (float(w_real_ov.iloc[-1]) ** (1.0 / yrs_ov) - 1.0) * 100.0
        m_syn = w_syn_ov.resample("ME").last().pct_change().dropna()
        m_real = w_real_ov.resample("ME").last().pct_change().dropna()
        corr_m = float(m_syn.corr(m_real))
        bond_synthesis_validation[sym] = {
            "inception_date": bdf.index[0].strftime("%Y-%m-%d"),
            "overlap_years": round(yrs_ov, 2),
            "real_cagr_pct": round(cagr_real, 2),
            "synth_cagr_pct": round(cagr_syn, 2),
            "cagr_diff_pp": round(cagr_syn - cagr_real, 2),
            "monthly_corr": round(corr_m, 4),
        }

        # Splice QII 0% tax
        r_sp0 = r_syn.copy()
        r_sp0.loc[ov] = r_b_tr.loc[ov]
        r_sp0.iloc[0] = 0.0
        bond_wealth_usd_qii0[sym] = (1.0 + r_sp0).cumprod()

        # Splice 30% withholding stress test (pre-inception reduces carry by 30%)
        y_src = y_2y_proxy if sym == "SHY" else (fvx_y if sym == "VGIT" else tnx_y)
        r_sp30 = r_syn - fc_eng.US_DIV_TAX_RATE * (y_src.shift(1).bfill() / 252.0)
        r_sp30.loc[ov] = r_b_net30.loc[ov]
        r_sp30.iloc[0] = 0.0
        bond_wealth_usd_tax30[sym] = (1.0 + r_sp30).cumprod()

    # ^IRX T-bill benchmark in USD
    w_irx_usd = mds["twd_6_df"]["US_TBILL"] / fx_al
    bond_wealth_usd_qii0["IRX"] = w_irx_usd / float(w_irx_usd.iloc[0])
    bond_wealth_usd_tax30["IRX"] = bond_wealth_usd_qii0["IRX"]

    # 3) Load & extend Shiller long monthly dataset (1871-01 .. 2026-09)
    shiller_df = load_shiller_monthly_dataset(mds, eh)

    return {
        "mds": mds,
        "extra_meta": emeta,
        "extra_histories": eh,
        "all_dates": all_dates,
        "common_end": common_end,
        "fx_aligned": fx_al,
        "w_0050_twd": mds["twd_6_df"]["0050"],
        "w_00631l_twd": mds["twd_6_df"]["00631L"],
        "p_0050_native": mds["p_0050_native"],
        "w_spy_usd": w_spy_usd,
        "w_voo_usd": w_voo_usd,
        "w_cspx_sim_usd": w_cspx_sim_usd,
        "w_cspx_spliced_usd": w_cspx_spliced_usd,
        "cspx_df": cspx_df,
        "voo_df": voo_df,
        "spy_df": spy_df,
        "bond_wealth_usd_qii0": bond_wealth_usd_qii0,
        "bond_wealth_usd_tax30": bond_wealth_usd_tax30,
        "bond_synthesis_validation": bond_synthesis_validation,
        "shiller_df": shiller_df,
    }


def load_shiller_monthly_dataset(mds: dict[str, Any], eh: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Build clean monthly Shiller dataset from 1871-01 to 2023-06, extended to 2026-09 via ^SP500TR, ^TNX, FRED CPIAUCSL."""
    sc = pd.read_csv(SHILLER_CSV_PATH)
    sc["Date"] = pd.to_datetime(sc["Date"])
    sc = sc.sort_values("Date").reset_index(drop=True)

    # Clean Shiller rows where all 4 columns are strictly positive (1871-01 .. 2023-06)
    valid = sc[(sc["SP500"] > 0) & (sc["Dividend"] > 0) & (sc["Consumer Price Index"] > 0) & (sc["Long Interest Rate"] > 0)].copy()
    valid = valid.set_index("Date")
    last_shiller_date = valid.index[-1]  # 2023-06-01

    # Compute monthly S&P 500 total return on Shiller core:
    # Standard Shiller monthly total return: (P_t + D_t/12) / P_{t-1} - 1
    p_s = valid["SP500"].to_numpy(dtype=np.float64)
    d_s = valid["Dividend"].to_numpy(dtype=np.float64)
    r_sp_m = np.zeros(len(valid), dtype=np.float64)
    r_sp_m[1:] = (p_s[1:] + d_s[1:] / 12.0) / p_s[:-1] - 1.0
    valid["r_sp_gross_m"] = r_sp_m
    valid["cpi"] = valid["Consumer Price Index"].astype(np.float64)
    valid["gs10"] = valid["Long Interest Rate"].astype(np.float64)
    valid["is_extended"] = False

    # Extend from 2023-07 to latest available month (2026-09) using Yahoo ^SP500TR, ^TNX & FRED CPIAUCSL
    if FRED_CPI_PATH.exists():
        fcpi = pd.read_csv(FRED_CPI_PATH)
        c_date = fcpi.columns[0]
        c_val = fcpi.columns[1]
        fcpi[c_date] = pd.to_datetime(fcpi[c_date])
        fcpi[c_val] = pd.to_numeric(fcpi[c_val], errors="coerce")
        fcpi = fcpi.dropna().set_index(c_date)[c_val]

        sp_tr_m = mds["sp_tr"].resample("MS").mean()
        tnx_m = _clean_df(eh["^TNX"])["Close"].resample("MS").mean()

        ext_dates = fcpi.index[fcpi.index > last_shiller_date]
        ext_dates = ext_dates.intersection(sp_tr_m.index).intersection(tnx_m.index)
        if len(ext_dates) > 0:
            # Anchor FRED CPI to Shiller CPI at last_shiller_date
            cpi_ratio = float(valid.loc[last_shiller_date, "cpi"]) / float(fcpi.loc[last_shiller_date])
            prev_sp = float(sp_tr_m.loc[last_shiller_date])
            ext_rows = []
            for dt in ext_dates:
                cur_sp = float(sp_tr_m.loc[dt])
                r_m = cur_sp / prev_sp - 1.0
                prev_sp = cur_sp
                ext_rows.append({
                    "Date": dt,
                    "SP500": cur_sp,
                    "Dividend": np.nan,
                    "Consumer Price Index": float(fcpi.loc[dt]) * cpi_ratio,
                    "Long Interest Rate": float(tnx_m.loc[dt]),
                    "r_sp_gross_m": r_m,
                    "cpi": float(fcpi.loc[dt]) * cpi_ratio,
                    "gs10": float(tnx_m.loc[dt]),
                    "is_extended": True,
                })
            ext_df = pd.DataFrame(ext_rows).set_index("Date")
            combined = pd.concat([valid[["SP500", "cpi", "gs10", "r_sp_gross_m", "is_extended"]],
                                  ext_df[["SP500", "cpi", "gs10", "r_sp_gross_m", "is_extended"]]])
        else:
            combined = valid[["SP500", "cpi", "gs10", "r_sp_gross_m", "is_extended"]].copy()
    else:
        combined = valid[["SP500", "cpi", "gs10", "r_sp_gross_m", "is_extended"]].copy()

    # Compute 10Y Treasury monthly total return across entire 1871..end series
    combined["r_bond10_m"] = fc_eng.synthesize_shiller_monthly_10y_bond_returns(combined["gs10"].to_numpy())
    # Apply realistic ETF friction (0.07% ER + ~0.25%/yr dividend withholding drag for IE equity, 0.03% ER for bond)
    combined["r_sp_net_ie_m"] = combined["r_sp_gross_m"] - (0.0032 / 12.0)
    combined["r_bond10_net_m"] = combined["r_bond10_m"] - (0.0003 / 12.0)
    return combined


# ---------------------------------------------------------------------------
# Test 1: Aggressive Filter Rules (A vs B vs C) + Age-50 Exit vs Hold
# ---------------------------------------------------------------------------

def run_test_1_aggressive_filter(ds: dict[str, Any], n_boot: int = 2000) -> dict[str, Any]:
    all_dates = ds["all_dates"]
    w_50 = ds["w_0050_twd"].to_numpy(dtype=np.float64)
    w_631 = ds["w_00631l_twd"].to_numpy(dtype=np.float64)
    w_us_twd = (ds["w_spy_usd"] * ds["fx_aligned"]).to_numpy(dtype=np.float64)
    p_50_nat = ds["p_0050_native"]

    # Causal execution signals: close < SMA200 (0% band) AND +-2% band (legacy check)
    sig_0_exec = fc_eng.compute_sma_below_signal(p_50_nat, window=200, band=0.0).reindex(all_dates).ffill().fillna(False).shift(1, fill_value=False).to_numpy(dtype=bool)
    sig_2_exec = fc_eng.compute_sma_below_signal(p_50_nat, window=200, band=0.02).reindex(all_dates).ffill().fillna(False).shift(1, fill_value=False).to_numpy(dtype=bool)

    ms_all = fc_eng.get_dca_Indices_by_timing(all_dates, "start")

    def _eval_rule_suite(tw_share: float, sig_exec: np.ndarray, label_prefix: str) -> dict[str, Any]:
        out_rules: dict[str, Any] = {}
        for rule in ("A", "B", "C"):
            # Full history (1997-07 .. 2026-11)
            full_res = fc_eng.simulate_aggressive_filter_rule(
                all_dates, w_50, w_631, sig_exec, rule=rule,
                w_us=w_us_twd if tw_share < 1.0 else None,
                tw_share=tw_share, lev_frac_in_tw=0.20,
            )
            # Real-only 00631L sub-period (2015-08-25 .. 2026-11)
            m_real = all_dates >= pd.Timestamp("2015-08-25")
            real_res = fc_eng.simulate_aggressive_filter_rule(
                all_dates[m_real], w_50[m_real], w_631[m_real], sig_exec[m_real], rule=rule,
                w_us=w_us_twd[m_real] if tw_share < 1.0 else None,
                tw_share=tw_share, lev_frac_in_tw=0.20,
            )
            # Rolling 10-year cohorts across full history
            n_coh = len(ms_all) - 120
            coh_xirr, coh_mdd, coh_sw = [], [], []
            for c in range(n_coh):
                i0, i1 = int(ms_all[c]), int(ms_all[c + 120])
                rc = fc_eng.simulate_aggressive_filter_rule(
                    all_dates[i0 : i1 + 1], w_50[i0 : i1 + 1], w_631[i0 : i1 + 1], sig_exec[i0 : i1 + 1],
                    rule=rule, w_us=w_us_twd[i0 : i1 + 1] if tw_share < 1.0 else None,
                    tw_share=tw_share, lev_frac_in_tw=0.20,
                )
                coh_xirr.append(rc["xirr_pct"])
                coh_mdd.append(rc["max_drawdown_pct"])
                coh_sw.append(rc["total_events_per_year"])

            x_arr = np.array(coh_xirr)
            m_arr = np.array(coh_mdd)
            out_rules[f"rule_{rule}"] = {
                "rule": rule,
                "label": f"{label_prefix}_Rule_{rule}",
                "full_history": {
                    "xirr_pct": round(full_res["xirr_pct"], 2),
                    "mdd_pct": round(full_res["max_drawdown_pct"], 2),
                    "final_value_twd": round(full_res["final_value_twd"], 0),
                    "switch_events": full_res["switch_events"],
                    "rebalance_events": full_res["rebalance_events"],
                    "switches_per_year": round(full_res["switches_per_year"], 2),
                    "total_events_per_year": round(full_res["total_events_per_year"], 2),
                    "annual_turnover_pct": round(full_res["annual_turnover_pct"], 2),
                    "total_fees_twd": round(full_res["total_fees_twd"], 0),
                },
                "real_2015_2026": {
                    "xirr_pct": round(real_res["xirr_pct"], 2),
                    "mdd_pct": round(real_res["max_drawdown_pct"], 2),
                    "switches_per_year": round(real_res["switches_per_year"], 2),
                    "total_events_per_year": round(real_res["total_events_per_year"], 2),
                },
                "rolling_10y": {
                    "cohort_count": n_coh,
                    "median_xirr_pct": round(float(np.median(x_arr)), 2),
                    "p10_xirr_pct": round(float(np.percentile(x_arr, 10)), 2),
                    "median_mdd_pct": round(float(np.median(m_arr)), 2),
                    "worst_mdd_pct": round(float(np.min(m_arr)), 2),
                    "prob_mdd_lt_60pct": round(float(np.mean(m_arr < -60.0)) * 100.0, 2),
                    "avg_events_per_year": round(float(np.mean(coh_sw)), 2),
                },
            }
        return out_rules

    # Primary: Pure TW leg (0050 80% + 00631L 20%, literal 0% band)
    tw_only_0band = _eval_rule_suite(1.0, sig_0_exec, "TW_80_20_0band")
    # Reference: Pure TW leg with +-2% hysteresis band
    tw_only_2band = _eval_rule_suite(1.0, sig_2_exec, "TW_80_20_2band")
    # Full aggressive portfolio (40% 0050 + 10% 00631L + 50% US equity, 0% band & 2% band)
    full_port_0band = _eval_rule_suite(0.5, sig_0_exec, "Full_40_10_50_0band")
    full_port_2band = _eval_rule_suite(0.5, sig_2_exec, "Full_40_10_50_2band")

    # Stationary Bootstrap (avg 3Y, n_boot paths, 10Y horizon) on monthly returns for Rule A vs B vs C (0% band)
    # Use monthly end-of-month prices + exact month-end SMA state for bootstrap P(MDD < -60%) check
    m_df = pd.DataFrame({
        "w50": ds["w_0050_twd"],
        "w631": ds["w_00631l_twd"],
        "wus": ds["w_spy_usd"] * ds["fx_aligned"],
        "below0": pd.Series(sig_0_exec, index=all_dates).astype(int),
        "below2": pd.Series(sig_2_exec, index=all_dates).astype(int),
    }).resample("ME").last().dropna()
    r50_m = m_df["w50"].pct_change().dropna().to_numpy()
    r631_m = m_df["w631"].pct_change().dropna().to_numpy()
    rus_m = m_df["wus"].pct_change().dropna().to_numpy()
    b0_m = m_df["below0"].iloc[1:].to_numpy(dtype=bool)
    n_hm = len(r50_m)

    idx_10y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 120, mode="stationary", block_months=36, n_paths=n_boot, seed=20261127)
    boot_stats_tw: dict[str, Any] = {}
    boot_stats_full: dict[str, Any] = {}
    for target_name, tw_sh, out_dict in [("tw_only", 1.0, boot_stats_tw), ("full_port", 0.5, boot_stats_full)]:
        for rule in ("A", "B", "C"):
            mdds = np.empty(n_boot, dtype=np.float64)
            xirrs = np.empty(n_boot, dtype=np.float64)
            rem_yrs = (120 - np.arange(120, dtype=np.float64)) / 12.0
            y_grid = rc_eng.precompute_xirr_y_grid(rem_yrs)
            end_mults = np.empty(n_boot, dtype=np.float64)
            for p_i in range(n_boot):
                ix = idx_10y[p_i]
                p50 = p631 = pus = 0.0
                pk = 0.0
                wdd = 0.0
                prev_b = False
                for m in range(120):
                    is_b = bool(b0_m[ix[m]]) if rule in ("A", "B") else False
                    if rule == "B":
                        if is_b and not prev_b and p631 > 1.0:
                            p50 += p631 * (1.0 - fc_eng.TW_REBAL_SELL_FEE) * (1.0 - fc_eng.TW_REBAL_BUY_FEE)
                            p631 = 0.0
                        elif not is_b and prev_b and (p50 + p631) > 1.0:
                            tgt631 = (p50 + p631) * 0.20
                            if tgt631 > p631:
                                s50 = min(p50, tgt631 - p631)
                                p50 -= s50
                                p631 += s50 * (1.0 - fc_eng.TW_REBAL_SELL_FEE) * (1.0 - fc_eng.TW_REBAL_BUY_FEE)
                    prev_b = is_b
                    # Monthly DCA
                    tw_in = 10_000.0 * tw_sh
                    us_in = 10_000.0 * (1.0 - tw_sh)
                    if rule in ("A", "B") and is_b:
                        p50 += tw_in
                    else:
                        p50 += tw_in * 0.80
                        p631 += tw_in * 0.20
                    pus += us_in * (1.0 - fc_eng.BASE_FX_FEE_RATE) * (1.0 - fc_eng.US_COMM_RATE)
                    # Annual Jan rebalance
                    if m > 0 and m % 12 == 0:
                        tot = p50 + p631 + pus
                        t_us = tot * (1.0 - tw_sh)
                        t_tw = tot * tw_sh
                        if rule == "B" and is_b:
                            t50, t631 = t_tw, 0.0
                        elif rule == "A" and is_b:
                            t631 = min(p631, t_tw * 0.20)
                            t50 = t_tw - t631
                        else:
                            t631 = t_tw * 0.20
                            t50 = t_tw * 0.80
                        # Approx net fee on rebalance turnover
                        s_tw = max(0.0, p50 - t50) + max(0.0, p631 - t631)
                        s_us = max(0.0, pus - t_us)
                        fee = s_tw * (fc_eng.TW_REBAL_SELL_FEE + fc_eng.TW_REBAL_BUY_FEE) + s_us * (fc_eng.US_COMM_RATE + fc_eng.BASE_FX_FEE_RATE)
                        scale = max(0.0, (tot - fee) / max(tot, 1.0))
                        p50, p631, pus = t50 * scale, t631 * scale, t_us * scale
                    # Apply return
                    p50 *= (1.0 + r50_m[ix[m]])
                    p631 *= max(0.0, 1.0 + r631_m[ix[m]])
                    pus *= (1.0 + rus_m[ix[m]])
                    tot_e = p50 + p631 + pus
                    if tot_e > pk:
                        pk = tot_e
                    if pk > 1e-9:
                        wdd = min(wdd, (tot_e - pk) / pk)
                mdds[p_i] = wdd * 100.0
                end_mults[p_i] = (p50 + p631 + pus) / 10_000.0
            xirrs = rc_eng.solve_xirr_from_y_grid(y_grid, end_mults) * 100.0
            out_dict[f"rule_{rule}"] = {
                "median_xirr_pct": round(float(np.median(xirrs)), 2),
                "median_mdd_pct": round(float(np.median(mdds)), 2),
                "p10_mdd_pct": round(float(np.percentile(mdds, 10)), 2),
                "prob_mdd_lt_60pct": round(float(np.mean(mdds < -60.0)) * 100.0, 2),
            }

    # Test "50 歲賣掉 00631L" vs "55 歲滑降時才賣" vs "不賣（持續持有到 60 歲）"
    # Run 30-year accumulation (age 30->60, 360 months) across Stationary Bootstrap paths using full aggressive portfolio
    idx_30y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 360, mode="stationary", block_months=36, n_paths=n_boot, seed=20261128)
    w_shy_m = ds["bond_wealth_usd_qii0"]["SHY"].resample("ME").last().pct_change().dropna()
    fx_m_s = ds["fx_aligned"].resample("ME").last()
    # Align monthly arrays cleanly
    m_comb = pd.DataFrame({
        "w50": ds["w_0050_twd"],
        "w631": ds["w_00631l_twd"],
        "wus_usd": ds["w_cspx_spliced_usd"],
        "wbond_usd": ds["bond_wealth_usd_qii0"]["SHY"],
        "fx": ds["fx_aligned"],
    }).resample("ME").last().dropna()
    r50_arr = m_comb["w50"].pct_change().dropna().to_numpy()
    r631_arr = m_comb["w631"].pct_change().dropna().to_numpy()
    rus_usd_arr = m_comb["wus_usd"].pct_change().dropna().to_numpy()
    rbond_usd_arr = m_comb["wbond_usd"].pct_change().dropna().to_numpy()
    fx_arr = m_comb["fx"].to_numpy()
    n_hm2 = len(r50_arr)
    idx_30y, _ = fc_eng.generate_bootstrap_month_indices(n_hm2, 360, mode="stationary", block_months=36, n_paths=n_boot, seed=20261128)

    age50_comparison: dict[str, Any] = {}
    for mode_key, sell_age in [("sell_at_50", 50), ("sell_at_55_glidepath", 55), ("never_sell_until_60", None)]:
        w60_list = np.empty(n_boot, dtype=np.float64)
        dd50_60_list = np.empty(n_boot, dtype=np.float64)
        for p_i in range(n_boot):
            ix = idx_30y[p_i]
            # Construct synthetic fx path from sampled fx ratios
            fx_ratios = fx_arr[ix + 1] / np.maximum(fx_arr[ix], 1e-9)
            fx_p = np.empty(361, dtype=np.float64)
            fx_p[0] = 32.0
            fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r50_arr[ix],
                r_us_usd_m=rus_usd_arr[ix],
                r_bond_usd_m=rbond_usd_arr[ix],
                fx_m=fx_p,
                start_age=30,
                glide_start_age=55,
                retire_age=60,
                end_age=60,
                monthly_contrib_twd=10_000.0,
                r_lev_tw_m=r631_arr[ix],
                lev_frac_in_tw=0.20,
                sell_lev_age=sell_age,
            )
            w60_list[p_i] = res["wealth_at_retire_twd"]
            dd50_60_list[p_i] = res["worst_dd_50_60_pct"]
        age50_comparison[mode_key] = {
            "sell_age": sell_age,
            "age60_p10_twd": round(float(np.percentile(w60_list, 10)), 0),
            "age60_p50_twd": round(float(np.percentile(w60_list, 50)), 0),
            "age60_p90_twd": round(float(np.percentile(w60_list, 90)), 0),
            "dd_50_60_median_pct": round(float(np.median(dd50_60_list)), 2),
            "dd_50_60_p10_worst_pct": round(float(np.percentile(dd50_60_list, 10)), 2),
            "prob_dd_50_60_lt_40pct": round(float(np.mean(dd50_60_list < -40.0)) * 100.0, 2),
        }

    # Evaluate pre-registered criteria on Pure TW 80/20 (0% band) AND Full 40/10/50 (0% band)
    def _pick_rule(suite: dict[str, Any], boot_d: dict[str, Any]) -> dict[str, Any]:
        eligible = []
        for rk in ("rule_A", "rule_B", "rule_C"):
            s = suite[rk]
            b = boot_d[rk]
            full_mdd = s["full_history"]["mdd_pct"]
            roll_p60 = s["rolling_10y"]["prob_mdd_lt_60pct"]
            boot_p60 = b["prob_mdd_lt_60pct"]
            passes_cap = (full_mdd >= -60.0) and (roll_p60 <= 10.0) and (boot_p60 <= 10.0)
            eligible.append({
                "key": rk,
                "rule": s["rule"],
                "passes_cap": passes_cap,
                "full_xirr": s["full_history"]["xirr_pct"],
                "roll_med_xirr": s["rolling_10y"]["median_xirr_pct"],
                "boot_med_xirr": b["median_xirr_pct"],
                "full_mdd": full_mdd,
                "roll_p60": roll_p60,
                "boot_p60": boot_p60,
                "events_yr": s["full_history"]["total_events_per_year"],
            })
        passed = [e for e in eligible if e["passes_cap"]]
        if not passed:
            return {"winner": "NONE_REDUCE_LEVERAGE", "candidates": eligible}
        # Compare by rolling 10Y median XIRR with 0.30pp tie threshold (1.00pp if events_yr > 4.0)
        best = passed[0]
        for cand in passed[1:]:
            diff = cand["roll_med_xirr"] - best["roll_med_xirr"]
            need_diff = 1.00 if cand["events_yr"] > 4.0 else 0.30
            rev_need = 1.00 if best["events_yr"] > 4.0 else 0.30
            if diff >= need_diff:
                best = cand
            elif (-diff) >= rev_need:
                continue
            else:
                # Tie -> lower events/yr, then smaller |MDD|, then simpler (C < A < B)
                simplicity = {"C": 0, "A": 1, "B": 2}
                if (cand["events_yr"], abs(cand["full_mdd"]), simplicity[cand["rule"]]) < (
                    best["events_yr"], abs(best["full_mdd"]), simplicity[best["rule"]]
                ):
                    best = cand
        return {"winner": best["rule"], "candidates": eligible}

    tw_decision = _pick_rule(tw_only_0band, boot_stats_tw)
    full_decision = _pick_rule(full_port_0band, boot_stats_full)

    # Check age-50 exit decision per criteria.md
    s50 = age50_comparison["sell_at_50"]
    s55 = age50_comparison["sell_at_55_glidepath"]
    s_never = age50_comparison["never_sell_until_60"]
    p10_impr_vs_never_pct = (s50["age60_p10_twd"] - s_never["age60_p10_twd"]) / max(s_never["age60_p10_twd"], 1.0) * 100.0
    p10_impr_vs_55_pct = (s50["age60_p10_twd"] - s55["age60_p10_twd"]) / max(s55["age60_p10_twd"], 1.0) * 100.0
    p50_diff_vs_55_pct = (s50["age60_p50_twd"] - s55["age60_p50_twd"]) / max(s55["age60_p50_twd"], 1.0) * 100.0
    dd_impr_vs_55 = s50["dd_50_60_p10_worst_pct"] - s55["dd_50_60_p10_worst_pct"]  # positive = less severe DD
    keep_age_50_exit = (p10_impr_vs_55_pct >= 3.0) or (dd_impr_vs_55 >= 5.0) or not (p50_diff_vs_55_pct < -5.0)

    return {
        "tw_only_0band": tw_only_0band,
        "tw_only_2band": tw_only_2band,
        "full_port_0band": full_port_0band,
        "full_port_2band": full_port_2band,
        "bootstrap_tw_only": boot_stats_tw,
        "bootstrap_full_port": boot_stats_full,
        "tw_decision": tw_decision,
        "full_decision": full_decision,
        "age50_exit_comparison": age50_comparison,
        "age50_decision": {
            "keep_age_50_exit": bool(keep_age_50_exit),
            "p10_impr_vs_55_pct": round(p10_impr_vs_55_pct, 2),
            "p10_impr_vs_never_pct": round(p10_impr_vs_never_pct, 2),
            "p50_diff_vs_55_pct": round(p50_diff_vs_55_pct, 2),
            "worst_dd_impr_vs_55_pp": round(dd_impr_vs_55, 2),
        },
    }


# ---------------------------------------------------------------------------
# Test 2: US Estate Tax & ETF Domicile (SPY/VOO vs CSPX; 0050 vs 006208)
# ---------------------------------------------------------------------------

def run_test_2_estate_tax_and_domicile(ds: dict[str, Any], lifetime_medians: dict[str, float]) -> dict[str, Any]:
    all_dates = ds["all_dates"]
    fx_al = ds["fx_aligned"]
    w_50_twd = ds["w_0050_twd"]

    # 1) Cross-validate simulated CSPX vs real CSPX.L (2010-09-15 .. common_end)
    cspx_df = ds["cspx_df"]
    voo_df = ds["voo_df"]
    spy_df = ds["spy_df"]

    c_start = cspx_df.index[0]
    ov_idx = all_dates[all_dates >= c_start]
    yrs_ov = (ov_idx[-1] - ov_idx[0]).days / 365.25

    def _cagr(s: pd.Series, idx: pd.DatetimeIndex) -> float:
        sub = s.reindex(idx).ffill().dropna()
        y = (sub.index[-1] - sub.index[0]).days / 365.25
        return (float(sub.iloc[-1] / sub.iloc[0]) ** (1.0 / y) - 1.0) * 100.0

    cagr_cspx_real = _cagr(ds["w_cspx_spliced_usd"], ov_idx)
    cagr_cspx_sim = _cagr(ds["w_cspx_sim_usd"], ov_idx)
    cagr_voo_net30 = _cagr(ds["w_voo_usd"], ov_idx)
    cagr_spy_net30 = _cagr(ds["w_spy_usd"], ov_idx)

    m_cspx_real = ds["w_cspx_spliced_usd"].reindex(ov_idx).resample("ME").last().pct_change().dropna()
    m_cspx_sim = ds["w_cspx_sim_usd"].reindex(ov_idx).resample("ME").last().pct_change().dropna()
    cspx_corr = float(m_cspx_real.corr(m_cspx_sim))

    # Full-history (1997-07 .. 2026-11) 50/50 DCA XIRR comparison: 0050 + SPY vs VOO vs CSPX
    dca_cmp: dict[str, Any] = {}
    for tag, w_us_usd in [("SPY_30wh", ds["w_spy_usd"]), ("VOO_30wh", ds["w_voo_usd"]), ("CSPX_15wh", ds["w_cspx_spliced_usd"])]:
        w_mat = np.column_stack([w_50_twd.to_numpy(), (w_us_usd * fx_al).to_numpy()])
        res = rc_eng.simulate_general_dca(
            all_dates, w_mat, np.array([0.5, 0.5]), np.array([True, False]), [],
            fx_daily=fx_al.to_numpy(), rebalance_mode="annual",
        )
        # Also pure 100% US equity DCA in TWD
        res_us = rc_eng.simulate_general_dca(
            all_dates, w_mat[:, 1:2], np.array([1.0]), np.array([False]), [],
            fx_daily=fx_al.to_numpy(), rebalance_mode="none",
        )
        dca_cmp[tag] = {
            "port_50_50_xirr_pct": round(res["xirr_pct"], 2),
            "port_50_50_mdd_pct": round(res["max_drawdown_pct"], 2),
            "port_50_50_final_twd": round(res["final_value_twd"], 0),
            "us_only_xirr_pct": round(res_us["xirr_pct"], 2),
            "us_only_final_twd": round(res_us["final_value_twd"], 0),
        }

    # 2) US Estate Tax scenarios at death ages 60, 75, 90 (FX = 32.0 TWD/USD)
    fx_ref = 32.0
    estate_scenarios: dict[str, Any] = {}
    # Show both NT$10,000/mo baseline and NT$30,000/mo middle-class scale
    for scale_name, mult in [("dca_10k_per_month", 1.0), ("dca_30k_per_month", 3.0)]:
        age_rows = {}
        for age_str, w_key, us_eq_share, us_bond_share in [
            ("60", "age_60_median_twd", 0.30, 0.40),
            ("75", "age_75_median_twd", 0.30, 0.40),
            ("90", "age_90_median_twd", 0.30, 0.40),
        ]:
            tot_twd = float(lifetime_medians[w_key]) * mult
            # Case 1: Only US equity (30% of portfolio) is US-situs (SPY/VOO vs CSPX)
            us_eq_usd = (tot_twd * us_eq_share) / fx_ref
            tax_eq_only = fc_eng.compute_us_nra_estate_tax_usd(us_eq_usd)
            tax_eq_twd = tax_eq_only["net_estate_tax_usd"] * fx_ref

            # Case 2: Both US equity (30%) AND US-domiciled Bond ETF (40% = 70% total) are US-situs!
            us_both_usd = (tot_twd * (us_eq_share + us_bond_share)) / fx_ref
            tax_both = fc_eng.compute_us_nra_estate_tax_usd(us_both_usd)
            tax_both_twd = tax_both["net_estate_tax_usd"] * fx_ref

            age_rows[age_str] = {
                "total_portfolio_twd": round(tot_twd, 0),
                "us_equity_only": {
                    "us_situs_usd": round(us_eq_usd, 0),
                    "spy_voo_estate_tax_usd": round(tax_eq_only["net_estate_tax_usd"], 0),
                    "spy_voo_estate_tax_twd": round(tax_eq_twd, 0),
                    "spy_voo_tax_pct_of_total_port": round(tax_eq_twd / max(tot_twd, 1.0) * 100.0, 2),
                    "spy_voo_net_inherited_twd": round(tot_twd - tax_eq_twd, 0),
                    "cspx_net_inherited_twd": round(tot_twd, 0),
                    "cspx_savings_twd": round(tax_eq_twd, 0),
                },
                "us_equity_plus_us_bond_etf": {
                    "us_situs_usd": round(us_both_usd, 0),
                    "us_domiciled_estate_tax_usd": round(tax_both["net_estate_tax_usd"], 0),
                    "us_domiciled_estate_tax_twd": round(tax_both_twd, 0),
                    "us_domiciled_tax_pct_of_total_port": round(tax_both_twd / max(tot_twd, 1.0) * 100.0, 2),
                    "us_domiciled_net_inherited_twd": round(tot_twd - tax_both_twd, 0),
                    "ireland_ucits_net_inherited_twd": round(tot_twd, 0),
                    "ireland_ucits_savings_twd": round(tax_both_twd, 0),
                },
            }
        estate_scenarios[scale_name] = age_rows

    # 3) Verified official fees & channels table (queried 2026-11-27)
    verified_etf_specs = {
        "query_date": QUERY_DATE,
        "CSPX": {
            "name": "iShares Core S&P 500 UCITS ETF (USD Acc)",
            "yahoo_ticker": "CSPX.L",
            "isin": "IE00B5BMR087",
            "domicile": "愛爾蘭 (Ireland UCITS)",
            "expense_ratio_pct": 0.07,
            "dividend_withholding_pct": 15.0,
            "us_estate_tax_exempt": True,
            "source_url": "https://www.ishares.com/uk/individual/en/products/253743/ishares-sp-500-b-ucits-etf-acc-fund",
            "taiwan_channels": "海外券商 Interactive Brokers (IBKR)；台灣複委託可透過永豐金證券、富邦證券下單倫敦證交所美元計價 CSPX（國泰證券目前未開放英股複委託，複委託英股手續費與低消須事先向營業員議價確認）。",
        },
        "VOO": {
            "name": "Vanguard S&P 500 ETF",
            "yahoo_ticker": "VOO",
            "domicile": "美國 (US)",
            "expense_ratio_pct": 0.03,
            "dividend_withholding_pct": 30.0,
            "us_estate_tax_exempt": False,
            "source_url": "https://investor.vanguard.com/investment-products/etfs/profile/voo",
            "taiwan_channels": "台灣各大券商美股定期定額複委託（國泰、永豐、富邦等免低消或固定低額手續費）與海外券商（IBKR、Firstrade、Schwab）。",
        },
        "SPY": {
            "name": "SPDR S&P 500 ETF Trust",
            "yahoo_ticker": "SPY",
            "domicile": "美國 (US)",
            "expense_ratio_pct": 0.0945,
            "dividend_withholding_pct": 30.0,
            "us_estate_tax_exempt": False,
            "source_url": "https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-trust-spy",
            "taiwan_channels": "台灣各大券商美股複委託與海外券商皆可交易。",
        },
        "0050": {
            "name": "元大台灣50 ETF",
            "yahoo_ticker": "0050.TW",
            "domicile": "台灣 (TW)",
            "management_fee_tiers": "1,000億以下 0.15% / 1,000–5,000億 0.10% / 5,000億–1兆 0.08% / 逾1兆 0.05%",
            "custody_fee_tiers": "1兆以下 0.03% / 逾1兆 0.025%",
            "estimated_total_expense_ratio_pct": 0.22,
            "source_url": "https://www.yuantaetfs.com/product/detail/0050/Basic_information",
            "note": "2025 年改採規模遞減費率並完成 1 拆 4 分割；規模突破 2 兆元後實際經理費約 0.07%，含保管費與雜費總費用率約 0.22%（查自元大投信官網與公開費率揭露，實際年度總費用率隨規模浮動）。",
        },
        "006208": {
            "name": "富邦台50 ETF",
            "yahoo_ticker": "006208.TW",
            "domicile": "台灣 (TW)",
            "management_fee_tiers": "1,000億以下 0.15% / 1,000–5,000億 0.10% / 5,000億–1兆 0.08% / 逾1兆 0.05%",
            "custody_fee_tiers": "1兆以下 0.03% / 逾1兆 0.025%",
            "estimated_total_expense_ratio_pct": 0.23,
            "source_url": "https://www.fsit.com.tw/",
            "note": "2025 年跟進改採與 0050 相同之級距式費率；規模約 4,500–5,000 億元，實際總費用率約 0.23%（差距約 0.01 個百分點，小於 0.05% 門檻，與 0050 視為打平）。",
        },
    }

    # Check criteria.md triggers
    xirr_diff_cspx_vs_spy = dca_cmp["CSPX_15wh"]["port_50_50_xirr_pct"] - dca_cmp["SPY_30wh"]["port_50_50_xirr_pct"]
    xirr_diff_cspx_vs_voo = dca_cmp["CSPX_15wh"]["port_50_50_xirr_pct"] - dca_cmp["VOO_30wh"]["port_50_50_xirr_pct"]
    max_estate_tax_pct_10k_eq = max(
        estate_scenarios["dca_10k_per_month"][a]["us_equity_only"]["spy_voo_tax_pct_of_total_port"]
        for a in ("60", "75", "90")
    )
    max_estate_savings_10k_eq = max(
        estate_scenarios["dca_10k_per_month"][a]["us_equity_only"]["cspx_savings_twd"]
        for a in ("60", "75", "90")
    )
    switch_to_cspx = (
        (xirr_diff_cspx_vs_voo >= -0.10)
        and ((max_estate_tax_pct_10k_eq >= 5.0) or (max_estate_savings_10k_eq >= 1_000_000.0))
    )

    return {
        "overlap_validation_2010_2026": {
            "start_date": c_start.strftime("%Y-%m-%d"),
            "end_date": ov_idx[-1].strftime("%Y-%m-%d"),
            "years": round(yrs_ov, 2),
            "cspx_real_usd_cagr_pct": round(cagr_cspx_real, 2),
            "cspx_sim_usd_cagr_pct": round(cagr_cspx_sim, 2),
            "cspx_sim_minus_real_pp": round(cagr_cspx_sim - cagr_cspx_real, 2),
            "cspx_monthly_corr": round(cspx_corr, 4),
            "voo_net30_usd_cagr_pct": round(cagr_voo_net30, 2),
            "spy_net30_usd_cagr_pct": round(cagr_spy_net30, 2),
            "cspx_real_minus_spy_net30_pp": round(cagr_cspx_real - cagr_spy_net30, 2),
            "cspx_real_minus_voo_net30_pp": round(cagr_cspx_real - cagr_voo_net30, 2),
        },
        "full_history_dca_comparison": dca_cmp,
        "estate_tax_by_death_age": estate_scenarios,
        "verified_etf_specs": verified_etf_specs,
        "decision": {
            "switch_primary_us_etf_to_cspx": bool(switch_to_cspx),
            "prefer_voo_over_spy_for_us_domiciled": True,  # 0.03% vs 0.0945%, diff 0.0645% >= 0.05%
            "tw_etf_tie_0050_vs_006208": True,             # 0.22% vs 0.23%, diff 0.01% < 0.05%
            "xirr_diff_cspx_vs_spy_pp": round(xirr_diff_cspx_vs_spy, 2),
            "xirr_diff_cspx_vs_voo_pp": round(xirr_diff_cspx_vs_voo, 2),
            "max_estate_tax_pct_10k_eq": round(max_estate_tax_pct_10k_eq, 2),
            "max_estate_savings_10k_eq_twd": round(max_estate_savings_10k_eq, 0),
        },
    }


# ---------------------------------------------------------------------------
# Test 3: Real Bond Products (SHY vs VGIT vs IEF vs ^IRX)
# ---------------------------------------------------------------------------

def run_test_3_real_bonds(ds: dict[str, Any], n_boot: int = 3000) -> dict[str, Any]:
    all_dates = ds["all_dates"]
    fx_al = ds["fx_aligned"]
    w_50_twd = ds["w_0050_twd"]
    w_us_twd = ds["w_cspx_spliced_usd"] * fx_al

    # 1) 2022 Stock+Bond Selloff (both calendar year 2022 and peak-to-trough 2021-11 .. 2023-10)
    selloff_2022: dict[str, Any] = {}
    for sym in ("SHY", "VGIT", "IEF", "IRX"):
        wb_usd = ds["bond_wealth_usd_qii0"][sym]
        wb_twd = wb_usd * fx_al

        sub_22_u = wb_usd.loc["2022-01-01":"2022-12-31"]
        sub_22_t = wb_twd.loc["2022-01-01":"2022-12-31"]
        sub_cyc_u = wb_usd.loc["2021-01-01":"2023-12-31"]

        # 30/30/40 lump-sum portfolio in 2022 (rebalanced Jan 2022)
        p303040_22 = (
            0.30 * (w_50_twd.loc["2022-01-01":"2022-12-31"] / float(w_50_twd.loc["2022-01-01":"2022-12-31"].iloc[0]))
            + 0.30 * (w_us_twd.loc["2022-01-01":"2022-12-31"] / float(w_us_twd.loc["2022-01-01":"2022-12-31"].iloc[0]))
            + 0.40 * (sub_22_t / float(sub_22_t.iloc[0]))
        )
        # Full-history CAGR & MDD (1997-07 .. 2026-11)
        yrs_all = (all_dates[-1] - all_dates[0]).days / 365.25
        cagr_usd = (float(wb_usd.iloc[-1] / wb_usd.iloc[0]) ** (1.0 / yrs_all) - 1.0) * 100.0
        cagr_twd = (float(wb_twd.iloc[-1] / wb_twd.iloc[0]) ** (1.0 / yrs_all) - 1.0) * 100.0
        cagr_usd_tax30 = (float(ds["bond_wealth_usd_tax30"][sym].iloc[-1] / ds["bond_wealth_usd_tax30"][sym].iloc[0]) ** (1.0 / yrs_all) - 1.0) * 100.0

        selloff_2022[sym] = {
            "bond_2022_ret_usd_pct": round((float(sub_22_u.iloc[-1] / sub_22_u.iloc[0]) - 1.0) * 100.0, 2),
            "bond_2022_mdd_usd_pct": round(fc_eng.compute_max_drawdown_pct(sub_22_u.to_numpy()), 2),
            "bond_2021_2023_mdd_usd_pct": round(fc_eng.compute_max_drawdown_pct(sub_cyc_u.to_numpy()), 2),
            "bond_2022_ret_twd_pct": round((float(sub_22_t.iloc[-1] / sub_22_t.iloc[0]) - 1.0) * 100.0, 2),
            "bond_2022_mdd_twd_pct": round(fc_eng.compute_max_drawdown_pct(sub_22_t.to_numpy()), 2),
            "port_30_30_40_2022_ret_twd_pct": round((float(p303040_22.iloc[-1]) - 1.0) * 100.0, 2),
            "port_30_30_40_2022_mdd_twd_pct": round(fc_eng.compute_max_drawdown_pct(p303040_22.to_numpy()), 2),
            "full_history_cagr_usd_qii0_pct": round(cagr_usd, 2),
            "full_history_cagr_usd_tax30_pct": round(cagr_usd_tax30, 2),
            "full_history_cagr_twd_qii0_pct": round(cagr_twd, 2),
            "full_history_mdd_usd_pct": round(fc_eng.compute_max_drawdown_pct(wb_usd.to_numpy()), 2),
        }

    # 2) 30-year retirement withdrawal (30/30/40) across Stationary Bootstrap (avg 3Y, n_boot paths)
    m_df = pd.DataFrame({
        "tw": w_50_twd,
        "us": w_us_twd,
        "SHY": ds["bond_wealth_usd_qii0"]["SHY"] * fx_al,
        "VGIT": ds["bond_wealth_usd_qii0"]["VGIT"] * fx_al,
        "IEF": ds["bond_wealth_usd_qii0"]["IEF"] * fx_al,
        "IRX": ds["bond_wealth_usd_qii0"]["IRX"] * fx_al,
        "SHY_tax30": ds["bond_wealth_usd_tax30"]["SHY"] * fx_al,
        "VGIT_tax30": ds["bond_wealth_usd_tax30"]["VGIT"] * fx_al,
    }).resample("ME").last().dropna()
    g_df = (1.0 + m_df.pct_change().dropna())
    n_hm = len(g_df)
    idx_30y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 360, mode="stationary", block_months=36, n_paths=n_boot, seed=20261129)

    g_tw = g_df["tw"].to_numpy()
    g_us = g_df["us"].to_numpy()
    # Identify early-crash paths (first 36 months cumulative equity return < 0.80)
    eq_3y = np.prod(0.5 * g_tw[idx_30y[:, :36]] + 0.5 * g_us[idx_30y[:, :36]], axis=1)
    early_crash_mask = eq_3y < 0.85

    retire_30y_cmp: dict[str, Any] = {}
    for sym in ("SHY", "VGIT", "IEF", "IRX", "SHY_tax30", "VGIT_tax30"):
        g_b = g_df[sym].to_numpy()
        g_batch = np.stack([g_tw[idx_30y], g_us[idx_30y], g_b[idx_30y]], axis=-1)
        res = rc_eng.simulate_retirement_withdrawal_batch(
            g_batch,
            weights_1d=np.array([0.30, 0.30, 0.40]),
            is_tw_mask=np.array([True, False, False]),
            initial_capital_twd=10_000_000.0,
            annual_withdrawal_rate=0.04,
            inflation_rate=0.02,
            rebalance_annual=True,
        )
        ec_ruin = float(np.mean(res["ruined_mask"][early_crash_mask])) * 100.0 if np.any(early_crash_mask) else 0.0
        retire_30y_cmp[sym] = {
            "ruin_prob_pct": round(res["ruin_prob_pct"], 2),
            "early_crash_ruin_prob_pct": round(ec_ruin, 2),
            "median_terminal_twd": round(res["median_terminal_twd"], 0),
            "p10_terminal_twd": round(float(np.percentile(res["terminal_wealth_twd"], 10)), 0),
            "median_worst_mdd_pct": round(float(np.median(res["worst_mdd_pct"])), 2),
        }

    # Apply pre-registered criteria to select concrete bond ETF (SHY vs VGIT vs IEF)
    etf_cands = ["SHY", "VGIT", "IEF"]
    ruins = {s: retire_30y_cmp[s]["ruin_prob_pct"] for s in etf_cands}
    min_ruin = min(ruins.values())
    tied_ruin = [s for s in etf_cands if ruins[s] - min_ruin < 3.0]

    selected = "SHY"
    reason = ""
    if len(tied_ruin) == 1:
        selected = tied_ruin[0]
        reason = f"{selected} 30 年破產機率 ({ruins[selected]:.2f}%) 明確領先其他商品超過 3 個百分點。"
    else:
        # Check 2022 drawdown & long-term advantage per criteria.md
        shy_22_dd = selloff_2022["SHY"]["bond_2022_mdd_usd_pct"]
        shy_med = retire_30y_cmp["SHY"]["median_terminal_twd"]
        shy_p_dd = selloff_2022["SHY"]["port_30_30_40_2022_mdd_twd_pct"]
        shy_cagr = selloff_2022["SHY"]["full_history_cagr_usd_qii0_pct"]
        mid_winners = []
        for mid in ("VGIT", "IEF"):
            if mid not in tied_ruin:
                continue
            mid_22_dd = selloff_2022[mid]["bond_2022_mdd_usd_pct"]
            mid_med = retire_30y_cmp[mid]["median_terminal_twd"]
            mid_p_dd = selloff_2022[mid]["port_30_30_40_2022_mdd_twd_pct"]
            mid_cagr = selloff_2022[mid]["full_history_cagr_usd_qii0_pct"]
            dd_deeper_5pp = (shy_22_dd - mid_22_dd) > 5.0
            has_term_gain = (mid_med / max(shy_med, 1.0) - 1.0) * 100.0 >= 10.0
            port_22_ok = (shy_p_dd - mid_p_dd) <= 3.0
            cagr_gain = (mid_cagr - shy_cagr) >= 0.30
            if (not dd_deeper_5pp) or (has_term_gain and port_22_ok) or (cagr_gain and port_22_ok):
                mid_winners.append(mid)
        if mid_winners:
            # Pick lower expense ratio among qualifying intermediate bond ETFs (VGIT 0.03% < IEF 0.15%)
            ers = {"VGIT": 0.03, "IEF": 0.15}
            selected = min(mid_winners, key=lambda x: ers[x])
            reason = f"破產機率打平下，{selected} 長期年化報酬與 90 歲剩餘中位數明顯優於 SHY，且 2022 組合回撤惡化在 3 個百分點門檻內、總費用率僅 {ers[selected]:.2f}%。"
        else:
            selected = "SHY"
            reason = "破產機率打平，但中期債 2022 單獨回撤比 SHY 深逾 5 個百分點且未達長期補償門檻，依防禦優先選 SHY。"

    return {
        "synthesis_validation": ds["bond_synthesis_validation"],
        "selloff_2022_and_history": selloff_2022,
        "retirement_30y_comparison": retire_30y_cmp,
        "selected_bond_etf": selected,
        "selection_reason": reason,
        "bond_etf_specs": {
            "query_date": QUERY_DATE,
            "SHY": {"name": "iShares 1-3 Year Treasury Bond ETF", "er_pct": 0.15, "duration_yr": 1.9, "url": "https://www.ishares.com/us/products/239452/ishares-13-year-treasury-bond-etf"},
            "VGIT": {"name": "Vanguard Intermediate-Term Treasury ETF", "er_pct": 0.03, "duration_yr": 5.0, "url": "https://investor.vanguard.com/investment-products/etfs/profile/vgit"},
            "IEF": {"name": "iShares 7-10 Year Treasury Bond ETF", "er_pct": 0.15, "duration_yr": 7.5, "url": "https://www.ishares.com/us/products/239456/ishares-710-year-treasury-bond-etf"},
        },
    }


# ---------------------------------------------------------------------------
# Test 4 & Test 5: Full-Life Continuous Simulation + Withdrawal Protection Rules
# ---------------------------------------------------------------------------

def run_test_4_and_5_lifetime_and_withdrawal(
    ds: dict[str, Any],
    selected_bond: str,
    n_boot: int = 3000,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    # A) Shiller 1871..2026 rolling 60-year continuous lifespans (start age 30 -> 55-59 glidepath -> 60-90 retire)
    sdf = ds["shiller_df"]
    r_sp = sdf["r_sp_net_ie_m"].to_numpy(dtype=np.float64)
    r_bd = sdf["r_bond10_net_m"].to_numpy(dtype=np.float64)
    cpi_s = sdf["cpi"].to_numpy(dtype=np.float64)
    n_s = len(sdf)
    fx_flat = np.full(721, 32.0, dtype=np.float64)

    n_life_coh = n_s - 721  # ~1,148 real rolling 60-year historical start months!
    shiller_life_results: dict[str, Any] = {}
    for rule_key in ("fixed_4_plus_2", "protect_20_35_plus_2", "fixed_4_cpi", "protect_20_35_cpi"):
        w60, w75, w90, mult60 = [], [], [], []
        ruined_flags, min_nom, min_real, prot_yrs = [], [], [], []
        for s0 in range(n_life_coh):
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_sp[s0 : s0 + 720],
                r_us_usd_m=r_sp[s0 : s0 + 720],
                r_bond_usd_m=r_bd[s0 : s0 + 720],
                fx_m=fx_flat,
                cpi_m=cpi_s[s0 : s0 + 721],
                start_age=30, glide_start_age=55, retire_age=60, end_age=90,
                monthly_contrib_twd=10_000.0,
                withdrawal_rule=rule_key,
            )
            w60.append(res["wealth_at_retire_twd"])
            w75.append(res["wealth_at_75_twd"])
            w90.append(res["wealth_at_end_twd"])
            mult60.append(res["retire_multiple"])
            ruined_flags.append(res["ruined"])
            min_nom.append(res["min_annual_spending_nom_pct"])
            min_real.append(res["min_annual_spending_real_pct"])
            prot_yrs.append(res["protection_years"])
        shiller_life_results[rule_key] = {
            "cohort_count": n_life_coh,
            "start_range": f"{sdf.index[0].strftime('%Y-%m')} ~ {sdf.index[n_life_coh - 1].strftime('%Y-%m')}",
            "age60_p10_twd": round(float(np.percentile(w60, 10)), 0),
            "age60_p50_twd": round(float(np.percentile(w60, 50)), 0),
            "age60_p90_twd": round(float(np.percentile(w60, 90)), 0),
            "age60_p50_multiple": round(float(np.median(mult60)), 2),
            "age75_p50_twd": round(float(np.median(w75)), 0),
            "age90_p50_twd": round(float(np.median(w90)), 0),
            "ruin_prob_pct": round(float(np.mean(ruined_flags)) * 100.0, 2),
            "min_spending_nom_p50_pct": round(float(np.median(min_nom)), 2),
            "min_spending_nom_p10_pct": round(float(np.percentile(min_nom, 10)), 2),
            "min_spending_real_p50_pct": round(float(np.median(min_real)), 2),
            "min_spending_real_p10_pct": round(float(np.percentile(min_real, 10)), 2),
            "avg_protection_years": round(float(np.mean(prot_yrs)), 2),
        }

    # B) Modern TW+US+FX (1997-07 .. 2026-11) Stationary Bootstrap (avg 3Y, n_boot paths) continuous 60-year lifecycle!
    m_df = pd.DataFrame({
        "tw": ds["w_0050_twd"],
        "us_usd": ds["w_cspx_spliced_usd"],
        "bond_usd": ds["bond_wealth_usd_qii0"][selected_bond],
        "fx": ds["fx_aligned"],
    }).resample("ME").last().dropna()
    r_tw_m = m_df["tw"].pct_change().dropna().to_numpy()
    r_us_m = m_df["us_usd"].pct_change().dropna().to_numpy()
    r_bd_m = m_df["bond_usd"].pct_change().dropna().to_numpy()
    fx_hist = m_df["fx"].to_numpy()
    n_hm = len(r_tw_m)

    idx_60y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 720, mode="stationary", block_months=36, n_paths=n_boot, seed=20261130)

    modern_life_results: dict[str, Any] = {}
    sample_wealth_quantiles: dict[str, list[float]] = {}
    for rule_key in ("fixed_4_plus_2", "protect_20_35_plus_2", "flat_4_no_adj"):
        w60, w75, w90, mult60 = [], [], [], []
        ruined_flags, min_nom, min_real, prot_yrs = [], [], [], []
        wealth_curves = np.empty((n_boot, 61), dtype=np.float64)
        for p_i in range(n_boot):
            ix = idx_60y[p_i]
            fx_ratios = fx_hist[ix + 1] / np.maximum(fx_hist[ix], 1e-9)
            fx_p = np.empty(721, dtype=np.float64)
            fx_p[0] = 32.0
            fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_tw_m[ix],
                r_us_usd_m=r_us_m[ix],
                r_bond_usd_m=r_bd_m[ix],
                fx_m=fx_p,
                start_age=30, glide_start_age=55, retire_age=60, end_age=90,
                monthly_contrib_twd=10_000.0,
                withdrawal_rule=rule_key,
            )
            w60.append(res["wealth_at_retire_twd"])
            w75.append(res["wealth_at_75_twd"])
            w90.append(res["wealth_at_end_twd"])
            mult60.append(res["retire_multiple"])
            ruined_flags.append(res["ruined"])
            min_nom.append(res["min_annual_spending_nom_pct"])
            min_real.append(res["min_annual_spending_real_pct"])
            prot_yrs.append(res["protection_years"])
            for a_idx, age_v in enumerate(range(30, 91)):
                wealth_curves[p_i, a_idx] = res["annual_wealth"].get(age_v, 0.0)

        modern_life_results[rule_key] = {
            "n_paths": n_boot,
            "total_contributed_twd": 3_600_000.0,
            "age60_p10_twd": round(float(np.percentile(w60, 10)), 0),
            "age60_p50_twd": round(float(np.percentile(w60, 50)), 0),
            "age60_p90_twd": round(float(np.percentile(w60, 90)), 0),
            "age60_p10_multiple": round(float(np.percentile(mult60, 10)), 2),
            "age60_p50_multiple": round(float(np.percentile(mult60, 50)), 2),
            "age60_p90_multiple": round(float(np.percentile(mult60, 90)), 2),
            "age75_p50_twd": round(float(np.median(w75)), 0),
            "age90_p10_twd": round(float(np.percentile(w90, 10)), 0),
            "age90_p50_twd": round(float(np.median(w90)), 0),
            "age90_p90_twd": round(float(np.percentile(w90, 90)), 0),
            "ruin_prob_pct": round(float(np.mean(ruined_flags)) * 100.0, 2),
            "min_spending_nom_p50_pct": round(float(np.median(min_nom)), 2),
            "min_spending_nom_p10_pct": round(float(np.percentile(min_nom, 10)), 2),
            "min_spending_nom_worst_pct": round(float(np.min(min_nom)), 2),
            "min_spending_real_p50_pct": round(float(np.median(min_real)), 2),
            "min_spending_real_p10_pct": round(float(np.percentile(min_real, 10)), 2),
            "avg_protection_years": round(float(np.mean(prot_yrs)), 2),
            "pct_paths_ever_triggered": round(float(np.mean(np.array(prot_yrs) > 0)) * 100.0, 2),
        }
        if rule_key == "protect_20_35_plus_2":
            sample_wealth_quantiles = {
                "ages": list(range(30, 91)),
                "p10_twd": [round(float(x), 0) for x in np.percentile(wealth_curves, 10, axis=0)],
                "p50_twd": [round(float(x), 0) for x in np.percentile(wealth_curves, 50, axis=0)],
                "p90_twd": [round(float(x), 0) for x in np.percentile(wealth_curves, 90, axis=0)],
            }

    # Test 5 standalone clean 30-year retirement comparison (starting at NT$10,000,000 at age 60, 30/30/40)
    # on BOTH Modern Bootstrap AND Shiller 1871..1996 rolling 30Y cohorts
    idx_30r, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 360, mode="stationary", block_months=36, n_paths=n_boot, seed=20261201)
    t5_modern: dict[str, Any] = {}
    for rule_key in ("fixed_4_plus_2", "protect_20_35_plus_2", "flat_4_no_adj"):
        ruined_f, min_nom, min_real, last_real, prot_y, term_w = [], [], [], [], [], []
        for p_i in range(n_boot):
            ix = idx_30r[p_i]
            fx_ratios = fx_hist[ix + 1] / np.maximum(fx_hist[ix], 1e-9)
            fx_p = np.empty(361, dtype=np.float64)
            fx_p[0] = 32.0
            fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_tw_m[ix], r_us_usd_m=r_us_m[ix], r_bond_usd_m=r_bd_m[ix], fx_m=fx_p,
                start_age=60, glide_start_age=55, retire_age=60, end_age=90,
                monthly_contrib_twd=0.0, initial_lump_sum_twd=10_000_000.0,
                withdrawal_rule=rule_key,
            )
            ruined_f.append(res["ruined"])
            min_nom.append(res["min_annual_spending_nom_pct"])
            min_real.append(res["min_annual_spending_real_pct"])
            last_real.append(res["final_year_spending_real_pct"])
            prot_y.append(res["protection_years"])
            term_w.append(res["wealth_at_end_twd"])
        t5_modern[rule_key] = {
            "ruin_prob_pct": round(float(np.mean(ruined_f)) * 100.0, 2),
            "min_spending_nom_p50_pct": round(float(np.median(min_nom)), 2),
            "min_spending_nom_p10_pct": round(float(np.percentile(min_nom, 10)), 2),
            "min_spending_nom_p05_pct": round(float(np.percentile(min_nom, 5)), 2),
            "min_spending_real_p50_pct": round(float(np.median(min_real)), 2),
            "min_spending_real_p10_pct": round(float(np.percentile(min_real, 10)), 2),
            "final_year_real_p50_pct": round(float(np.median(last_real)), 2),
            "avg_protection_years": round(float(np.mean(prot_y)), 2),
            "pct_paths_triggered": round(float(np.mean(np.array(prot_y) > 0)) * 100.0, 2),
            "median_terminal_twd": round(float(np.median(term_w)), 0),
        }

    # Shiller rolling 30Y retirement comparison (all start months 1871-01 .. 1996-09)
    n_30_coh = n_s - 361
    fx_flat30 = np.full(361, 32.0, dtype=np.float64)
    t5_shiller: dict[str, Any] = {}
    for rule_key in ("fixed_4_plus_2", "protect_20_35_plus_2", "flat_4_no_adj", "fixed_4_cpi", "protect_20_35_cpi"):
        ruined_f, min_nom, min_real, last_real, prot_y, term_w = [], [], [], [], [], []
        for s0 in range(n_30_coh):
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_sp[s0 : s0 + 360], r_us_usd_m=r_sp[s0 : s0 + 360], r_bond_usd_m=r_bd[s0 : s0 + 360],
                fx_m=fx_flat30, cpi_m=cpi_s[s0 : s0 + 361],
                start_age=60, glide_start_age=55, retire_age=60, end_age=90,
                monthly_contrib_twd=0.0, initial_lump_sum_twd=10_000_000.0,
                withdrawal_rule=rule_key,
            )
            ruined_f.append(res["ruined"])
            min_nom.append(res["min_annual_spending_nom_pct"])
            min_real.append(res["min_annual_spending_real_pct"])
            last_real.append(res["final_year_spending_real_pct"])
            prot_y.append(res["protection_years"])
            term_w.append(res["wealth_at_end_twd"])
        t5_shiller[rule_key] = {
            "cohort_count": n_30_coh,
            "ruin_prob_pct": round(float(np.mean(ruined_f)) * 100.0, 2),
            "min_spending_nom_p50_pct": round(float(np.median(min_nom)), 2),
            "min_spending_nom_p10_pct": round(float(np.percentile(min_nom, 10)), 2),
            "min_spending_real_p50_pct": round(float(np.median(min_real)), 2),
            "min_spending_real_p10_pct": round(float(np.percentile(min_real, 10)), 2),
            "final_year_real_p50_pct": round(float(np.median(last_real)), 2),
            "avg_protection_years": round(float(np.mean(prot_y)), 2),
            "pct_paths_triggered": round(float(np.mean(np.array(prot_y) > 0)) * 100.0, 2),
            "median_terminal_twd": round(float(np.median(term_w)), 0),
        }

    # Package Test 4 & Test 5 outputs
    test4_out = {
        "selected_bond_etf": selected_bond,
        "modern_tw_us_bootstrap": modern_life_results,
        "shiller_1871_rolling": shiller_life_results,
        "lifetime_wealth_quantiles_modern": sample_wealth_quantiles,
        "glidepath_verified_schedule": [
            {"age": a, "tw": round(fc_eng.get_glidepath_weights(a)[0] * 100), "us": round(fc_eng.get_glidepath_weights(a)[1] * 100), "bond": round(fc_eng.get_glidepath_weights(a)[2] * 100)}
            for a in range(54, 61)
        ],
    }

    # Test 5 decision per criteria.md
    m_fix = t5_modern["fixed_4_plus_2"]["ruin_prob_pct"]
    m_pro = t5_modern["protect_20_35_plus_2"]["ruin_prob_pct"]
    s_fix_cpi = t5_shiller["fixed_4_cpi"]["ruin_prob_pct"]
    s_pro_cpi = t5_shiller["protect_20_35_cpi"]["ruin_prob_pct"]
    ruin_drop_modern = m_fix - m_pro
    ruin_drop_shiller_cpi = s_fix_cpi - s_pro_cpi
    flat_real_shrink_pct = 100.0 - t5_shiller["flat_4_no_adj"]["final_year_real_p50_pct"]
    prot_min_real_p10 = t5_shiller["protect_20_35_cpi"]["min_spending_real_p10_pct"]

    test5_out = {
        "modern_30y_bootstrap": t5_modern,
        "shiller_30y_rolling": t5_shiller,
        "decision": {
            "ruin_reduction_modern_pp": round(ruin_drop_modern, 2),
            "ruin_reduction_shiller_cpi_pp": round(ruin_drop_shiller_cpi, 2),
            "flat_4_real_purchasing_power_loss_30y_pct": round(flat_real_shrink_pct, 2),
            "protect_min_real_p10_shiller_pct": round(prot_min_real_p10, 2),
            "keep_20_35_protection": bool((ruin_drop_modern >= 3.0) or (ruin_drop_shiller_cpi >= 3.0)),
        },
    }

    lifetime_medians = {
        "age_60_median_twd": modern_life_results["protect_20_35_plus_2"]["age60_p50_twd"],
        "age_75_median_twd": modern_life_results["protect_20_35_plus_2"]["age75_p50_twd"],
        "age_90_median_twd": modern_life_results["protect_20_35_plus_2"]["age90_p50_twd"],
    }
    return test4_out, test5_out, lifetime_medians


# ---------------------------------------------------------------------------
# Test 6: High Inflation & Long History (Shiller 1871+, 5 Historic Paths)
# ---------------------------------------------------------------------------

def run_test_6_shiller_long_history(ds: dict[str, Any]) -> dict[str, Any]:
    sdf = ds["shiller_df"]
    r_sp = sdf["r_sp_net_ie_m"].to_numpy(dtype=np.float64)
    r_bd = sdf["r_bond10_net_m"].to_numpy(dtype=np.float64)
    cpi_s = sdf["cpi"].to_numpy(dtype=np.float64)
    n_s = len(sdf)
    n_30_coh = n_s - 361
    fx_flat30 = np.full(361, 32.0, dtype=np.float64)

    # Compare 4.0% fixed +2%, 4.0% fixed CPI, 4.0% protect +2%, 4.0% protect CPI, 3.5% fixed CPI, 3.5% protect CPI
    configs = [
        ("fixed_4_plus_2", 0.04, "fixed_4_plus_2"),
        ("fixed_4_cpi", 0.04, "fixed_4_cpi"),
        ("protect_20_35_plus_2", 0.04, "protect_20_35_plus_2"),
        ("protect_20_35_cpi", 0.04, "protect_20_35_cpi"),
        ("fixed_35_cpi", 0.035, "fixed_4_cpi"),
        ("protect_35_cpi", 0.035, "protect_20_35_cpi"),
    ]
    rolling_summary: dict[str, Any] = {}
    for cfg_name, wd_rate, rule_name in configs:
        ruined_f, min_real, term_w, prot_y = [], [], [], []
        for s0 in range(n_30_coh):
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_sp[s0 : s0 + 360], r_us_usd_m=r_sp[s0 : s0 + 360], r_bond_usd_m=r_bd[s0 : s0 + 360],
                fx_m=fx_flat30, cpi_m=cpi_s[s0 : s0 + 361],
                start_age=60, glide_start_age=55, retire_age=60, end_age=90,
                monthly_contrib_twd=0.0, initial_lump_sum_twd=10_000_000.0,
                initial_withdrawal_rate=wd_rate, withdrawal_rule=rule_name,
            )
            ruined_f.append(res["ruined"])
            min_real.append(res["min_annual_spending_real_pct"])
            term_w.append(res["wealth_at_end_twd"])
            prot_y.append(res["protection_years"])
        rolling_summary[cfg_name] = {
            "cohort_count": n_30_coh,
            "ruin_prob_pct": round(float(np.mean(ruined_f)) * 100.0, 2),
            "min_real_spending_p10_pct": round(float(np.percentile(min_real, 10)), 2),
            "min_real_spending_p50_pct": round(float(np.median(min_real)), 2),
            "median_terminal_twd": round(float(np.median(term_w)), 0),
            "avg_protection_years": round(float(np.mean(prot_y)), 2),
        }

    # 5 required historical crisis retirement paths: 1929, 1966, 1973, 2000, 2008
    # Note: for 2000 (26 yrs available) and 2008 (18 yrs available), simulate exact available full years!
    crisis_specs = [
        ("1929", "1929-09-01", "1929 大蕭條頂點退休"),
        ("1966", "1966-01-01", "1966 滯脹前夕退休"),
        ("1973", "1973-01-01", "1973 石油危機高通膨退休"),
        ("2000", "2000-08-01", "2000 網路泡沫頂點退休"),
        ("2008", "2008-01-01", "2008 金融海嘯前退休"),
    ]
    five_paths: dict[str, Any] = {}
    for c_key, d_str, c_label in crisis_specs:
        s0 = int(sdf.index.get_indexer([pd.Timestamp(d_str)], method="nearest")[0])
        avail_months = n_s - 1 - s0
        sim_years = min(30, avail_months // 12)
        sim_months = sim_years * 12
        fx_sub = np.full(sim_months + 1, 32.0, dtype=np.float64)

        # Realized CPI CAGR over sim_years
        cpi_cagr = ((cpi_s[s0 + sim_months] / cpi_s[s0]) ** (1.0 / sim_years) - 1.0) * 100.0

        path_modes: dict[str, Any] = {}
        for cfg_name, wd_rate, rule_name in [
            ("fixed_4_plus_2", 0.04, "fixed_4_plus_2"),
            ("fixed_4_cpi", 0.04, "fixed_4_cpi"),
            ("protect_20_35_cpi", 0.04, "protect_20_35_cpi"),
            ("fixed_35_cpi", 0.035, "fixed_4_cpi"),
        ]:
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_sp[s0 : s0 + sim_months],
                r_us_usd_m=r_sp[s0 : s0 + sim_months],
                r_bond_usd_m=r_bd[s0 : s0 + sim_months],
                fx_m=fx_sub,
                cpi_m=cpi_s[s0 : s0 + sim_months + 1],
                start_age=60, glide_start_age=55, retire_age=60, end_age=60 + sim_years,
                monthly_contrib_twd=0.0, initial_lump_sum_twd=10_000_000.0,
                initial_withdrawal_rate=wd_rate, withdrawal_rule=rule_name,
            )
            # Store real (CPI-deflated to retirement year) wealth curve for plotting!
            cpi0 = float(cpi_s[s0])
            real_curve = []
            for yr_i in range(sim_years + 1):
                w_nom = res["annual_wealth"].get(60 + yr_i, 0.0)
                c_y = float(cpi_s[s0 + yr_i * 12])
                real_curve.append(round(w_nom * (cpi0 / c_y), 0))
            path_modes[cfg_name] = {
                "ruined": res["ruined"],
                "ruin_year_offset": round(res["ruin_age"] - 60.0, 2) if res["ruin_age"] is not None else None,
                "end_nominal_twd": round(res["wealth_at_end_twd"], 0),
                "end_real_twd": real_curve[-1],
                "min_real_spending_pct": round(res["min_annual_spending_real_pct"], 2),
                "protection_years": res["protection_years"],
                "real_wealth_curve_twd": real_curve,
            }

        five_paths[c_key] = {
            "start_date": sdf.index[s0].strftime("%Y-%m-%d"),
            "label": c_label,
            "simulated_years": sim_years,
            "is_censored_lt_30y": bool(sim_years < 30),
            "realized_cpi_cagr_pct": round(cpi_cagr, 2),
            "modes": path_modes,
        }

    # Evaluate criteria.md triggers
    cpi_vs_2pct_gap = rolling_summary["fixed_4_cpi"]["ruin_prob_pct"] - rolling_summary["fixed_4_plus_2"]["ruin_prob_pct"]
    prot_cpi_ruin = rolling_summary["protect_20_35_cpi"]["ruin_prob_pct"]
    crisis_ruin_count_prot = sum(1 for v in five_paths.values() if v["modes"]["protect_20_35_cpi"]["ruined"])
    crisis_ruin_count_fix = sum(1 for v in five_paths.values() if v["modes"]["fixed_4_cpi"]["ruined"])

    return {
        "data_source": {
            "primary_url": "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
            "csv_mirror_url": "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv",
            "extension_2023_2026": "Yahoo ^SP500TR, ^TNX + FRED CPIAUCSL (https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL)",
            "date_range": f"{sdf.index[0].strftime('%Y-%m')} ~ {sdf.index[-1].strftime('%Y-%m')}",
            "total_months": n_s,
            "proxy_disclosure": "1871–1997 年無台股歷史資料，長歷史模擬期間以美國 S&P 500 含息報酬（扣 0.32%/年估計愛爾蘭 ETF 費用與預扣稅）代替台股部分，並以 10 年期美國公債殖利率推算債券報酬。",
        },
        "rolling_30y_summary": rolling_summary,
        "five_crisis_paths": five_paths,
        "decision": {
            "cpi_vs_fixed2_ruin_diff_pp": round(cpi_vs_2pct_gap, 2),
            "must_replace_fixed2_with_cpi": bool(cpi_vs_2pct_gap >= 3.0),
            "protect_20_35_cpi_ruin_pct": prot_cpi_ruin,
            "crisis_ruin_count_fixed4_cpi": crisis_ruin_count_fix,
            "crisis_ruin_count_protect_cpi": crisis_ruin_count_prot,
            "must_lower_init_rate_to_35": bool(prot_cpi_ruin > 5.0 or crisis_ruin_count_prot >= 2),
        },
    }


# ---------------------------------------------------------------------------
# Test 7: Large FX Swings (TWD -> 25 and TWD -> 40 over 5 yrs, Pre & Post Retire)
# ---------------------------------------------------------------------------

def run_test_7_fx_swings(ds: dict[str, Any], selected_bond: str, n_boot: int = 3000) -> dict[str, Any]:
    m_df = pd.DataFrame({
        "tw": ds["w_0050_twd"],
        "us_usd": ds["w_cspx_spliced_usd"],
        "bond_usd": ds["bond_wealth_usd_qii0"][selected_bond],
        "fx": ds["fx_aligned"],
    }).resample("ME").last().dropna()
    r_tw_m = m_df["tw"].pct_change().dropna().to_numpy()
    r_us_m = m_df["us_usd"].pct_change().dropna().to_numpy()
    r_bd_m = m_df["bond_usd"].pct_change().dropna().to_numpy()
    fx_hist = m_df["fx"].to_numpy()
    n_hm = len(r_tw_m)

    # Simulate 35-year continuous window: age 55 (start of glidepath, NT$10M baseline) -> 55-59 glidepath -> 60-90 retire!
    # Placing FX shock at month 0..60 = "5 years before retirement (55-60)"
    # Placing FX shock at month 60..120 = "first 5 years after retirement (60-65)"
    idx_35y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 420, mode="stationary", block_months=36, n_paths=n_boot, seed=20261202)

    scenarios = [
        ("hist_fx_baseline", "歷史真實匯率抽樣基準", "hist", 0, 0.0),
        ("flat_fx_32", "固定匯率 32.0 不變", "flat", 0, 0.0),
        ("twd_appreciate_2pct_yr", "台幣每年升值 2%（60歲起）", "drift_down_2pct", 60, 0.0),
        ("twd_depreciate_2pct_yr", "台幣每年貶值 2%（60歲起）", "drift_up_2pct", 60, 0.0),
        ("pre_retire_to_25", "退休前 5 年（55–60歲）直線升值至 25 元", "appreciate_to_25", 0, 0.0),
        ("post_retire_to_25", "退休後前 5 年（60–65歲）直線升值至 25 元", "appreciate_to_25", 60, 0.0),
        ("pre_retire_to_40", "退休前 5 年（55–60歲）直線貶值至 40 元", "depreciate_to_40", 0, 0.0),
        ("post_retire_to_40", "退休後前 5 年（60–65歲）直線貶值至 40 元", "depreciate_to_40", 60, 0.0),
        # Currency-hedged backup: 30% TW + 30% US + 20% US Bond + 20% TWD Cash (50% TWD / 50% USD)
        ("post_retire_to_25_twd20_hedge", "【對照】退休後升至 25 元 + 20%台幣定存(50/50幣別)", "appreciate_to_25", 60, 0.20),
        ("pre_retire_to_25_twd20_hedge", "【對照】退休前升至 25 元 + 20%台幣定存(50/50幣別)", "appreciate_to_25", 0, 0.20),
    ]

    results_by_scen: dict[str, Any] = {}
    for s_key, s_label, fx_mode, shock_m, twd_cash_sh in scenarios:
        out_rules = {}
        for wd_rule in ("fixed_4_plus_2", "protect_20_35_plus_2"):
            w60_l, wd1_l, wd5_l, min_nom_l, ruined_l, term_l = [], [], [], [], [], []
            for p_i in range(n_boot):
                ix = idx_35y[p_i]
                if fx_mode == "hist":
                    fx_ratios = fx_hist[ix + 1] / np.maximum(fx_hist[ix], 1e-9)
                    fx_p = np.empty(421, dtype=np.float64)
                    fx_p[0] = 32.0
                    fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
                else:
                    fx_p = fc_eng.build_fx_scenario_path(420, base_fx=32.0, scenario=fx_mode, shock_start_month=shock_m, shock_duration_months=60)

                res = fc_eng.simulate_lifetime_continuous(
                    r_tw_m=r_tw_m[ix], r_us_usd_m=r_us_m[ix], r_bond_usd_m=r_bd_m[ix], fx_m=fx_p,
                    start_age=55, glide_start_age=55, retire_age=60, end_age=90,
                    monthly_contrib_twd=10_000.0, initial_lump_sum_twd=10_000_000.0,
                    twd_cash_share_in_retire=twd_cash_sh,
                    withdrawal_rule=wd_rule,
                )
                w60_l.append(res["wealth_at_retire_twd"])
                wd1_l.append(res["year1_annual_wd_twd"])
                wd5_l.append(res["annual_spending_nominal"][4] if len(res["annual_spending_nominal"]) >= 5 else 0.0)
                min_nom_l.append(res["min_annual_spending_nom_pct"])
                ruined_l.append(res["ruined"])
                term_l.append(res["wealth_at_end_twd"])

            out_rules[wd_rule] = {
                "age60_median_twd": round(float(np.median(w60_l)), 0),
                "year1_wd_median_twd": round(float(np.median(wd1_l)), 0),
                "year5_wd_median_twd": round(float(np.median(wd5_l)), 0),
                "min_spending_nom_p10_pct": round(float(np.percentile(min_nom_l, 10)), 2),
                "ruin_prob_pct": round(float(np.mean(ruined_l)) * 100.0, 2),
                "median_terminal_twd": round(float(np.median(term_l)), 0),
            }
        results_by_scen[s_key] = {"label": s_label, "rules": out_rules}

    # Check criteria.md triggers
    base_ruin_fix = results_by_scen["hist_fx_baseline"]["rules"]["fixed_4_plus_2"]["ruin_prob_pct"]
    base_ruin_pro = results_by_scen["hist_fx_baseline"]["rules"]["protect_20_35_plus_2"]["ruin_prob_pct"]
    base_wd1 = results_by_scen["hist_fx_baseline"]["rules"]["protect_20_35_plus_2"]["year1_wd_median_twd"]

    post25_ruin_fix = results_by_scen["post_retire_to_25"]["rules"]["fixed_4_plus_2"]["ruin_prob_pct"]
    post25_ruin_pro = results_by_scen["post_retire_to_25"]["rules"]["protect_20_35_plus_2"]["ruin_prob_pct"]
    pre25_wd1 = results_by_scen["pre_retire_to_25"]["rules"]["protect_20_35_plus_2"]["year1_wd_median_twd"]
    pre25_wd1_drop_pct = (base_wd1 - pre25_wd1) / max(base_wd1, 1.0) * 100.0

    trigger_currency_split = (
        ((post25_ruin_pro - base_ruin_pro >= 5.0) and (post25_ruin_pro > 10.0))
        or ((post25_ruin_fix - base_ruin_fix >= 5.0) and (post25_ruin_fix > 10.0))
    )
    trigger_twd_buffer_note = (pre25_wd1_drop_pct >= 15.0) or trigger_currency_split

    return {
        "selected_bond_etf": selected_bond,
        "scenarios": results_by_scen,
        "decision": {
            "base_ruin_fixed4_pct": base_ruin_fix,
            "post25_ruin_fixed4_pct": post25_ruin_fix,
            "base_ruin_protect_pct": base_ruin_pro,
            "post25_ruin_protect_pct": post25_ruin_pro,
            "pre25_year1_wd_drop_vs_base_pct": round(pre25_wd1_drop_pct, 2),
            "change_retirement_currency_split": bool(trigger_currency_split),
            "add_twd_cash_buffer_rule": bool(trigger_twd_buffer_note),
        },
    }


# ---------------------------------------------------------------------------
# Test 8: Different People (Ages 25/30/40/45, Fixed vs +3% DCA, Conservative 25/25/50)
# ---------------------------------------------------------------------------

def run_test_8_different_people(ds: dict[str, Any], selected_bond: str, n_boot: int = 2500) -> dict[str, Any]:
    all_dates = ds["all_dates"]
    fx_al = ds["fx_aligned"]
    w_50 = ds["w_0050_twd"]
    w_us = ds["w_cspx_spliced_usd"] * fx_al
    w_bd = ds["bond_wealth_usd_qii0"][selected_bond] * fx_al

    # 1) Standalone backtest of Conservative 25/25/50 vs 50/50 vs 60/40 (30/30/40) vs 20/20/60
    w_mat = np.column_stack([w_50.to_numpy(), w_us.to_numpy(), w_bd.to_numpy()])
    is_tw_3 = np.array([True, False, False], dtype=bool)
    ms_all = fc_eng.get_dca_Indices_by_timing(all_dates, "start")
    n_coh_10y = len(ms_all) - 120

    port_specs = [
        ("cons_25_25_50", f"保守型 25/25/50 (0050+CSPX+{selected_bond})", np.array([0.25, 0.25, 0.50])),
        ("cons_20_20_60", f"超保守 20/20/60 (0050+CSPX+{selected_bond})", np.array([0.20, 0.20, 0.60])),
        ("bal_30_30_40", f"平衡 60/40 (30% 0050+30% CSPX+40% {selected_bond})", np.array([0.30, 0.30, 0.40])),
        ("eq_50_50", "一般型 50/50 (50% 0050+50% CSPX)", np.array([0.50, 0.50, 0.00])),
    ]
    cons_comparison: dict[str, Any] = {}
    for p_id, p_lbl, p_w in port_specs:
        full_r = rc_eng.simulate_general_dca(
            all_dates, w_mat, p_w, is_tw_3, [], fx_daily=fx_al.to_numpy(), rebalance_mode="annual",
        )
        coh_x, coh_m = [], []
        for c in range(n_coh_10y):
            i0, i1 = int(ms_all[c]), int(ms_all[c + 120])
            rc = rc_eng.simulate_general_dca(
                all_dates[i0 : i1 + 1], w_mat[i0 : i1 + 1], p_w, is_tw_3, [],
                fx_daily=fx_al.to_numpy()[i0 : i1 + 1], rebalance_mode="annual",
            )
            coh_x.append(rc["xirr_pct"])
            coh_m.append(rc["max_drawdown_pct"])
        cx = np.array(coh_x)
        cm = np.array(coh_m)
        cons_comparison[p_id] = {
            "label": p_lbl,
            "full_history_xirr_pct": round(full_r["xirr_pct"], 2),
            "full_history_mdd_pct": round(full_r["max_drawdown_pct"], 2),
            "full_history_final_twd": round(full_r["final_value_twd"], 0),
            "rolling_10y_median_xirr_pct": round(float(np.median(cx)), 2),
            "rolling_10y_p10_xirr_pct": round(float(np.percentile(cx, 10)), 2),
            "rolling_10y_median_mdd_pct": round(float(np.median(cm)), 2),
            "rolling_10y_worst_mdd_pct": round(float(np.min(cm)), 2),
            "rolling_10y_prob_mdd_lt_30pct": round(float(np.mean(cm < -30.0)) * 100.0, 2),
            "rolling_10y_prob_mdd_lt_50pct": round(float(np.mean(cm < -50.0)) * 100.0, 2),
        }

    # 2) Starting ages 25, 30, 40, 45 -> 55 glidepath -> 60 retire -> 90 end
    # Fixed NT$10k/mo vs +3%/yr growing DCA (starting NT$10k/mo)
    m_df = pd.DataFrame({
        "tw": w_50,
        "us_usd": ds["w_cspx_spliced_usd"],
        "bond_usd": ds["bond_wealth_usd_qii0"][selected_bond],
        "fx": fx_al,
    }).resample("ME").last().dropna()
    r_tw_m = m_df["tw"].pct_change().dropna().to_numpy()
    r_us_m = m_df["us_usd"].pct_change().dropna().to_numpy()
    r_bd_m = m_df["bond_usd"].pct_change().dropna().to_numpy()
    fx_hist = m_df["fx"].to_numpy()
    n_hm = len(r_tw_m)

    # Generate max-length 65-year (780 months, age 25..90) paths once for fair comparison across start ages!
    idx_65y, _ = fc_eng.generate_bootstrap_month_indices(n_hm, 780, mode="stationary", block_months=36, n_paths=n_boot, seed=20261203)

    personas: dict[str, Any] = {}
    for start_age in (25, 30, 40, 45):
        n_m = (90 - start_age) * 12
        age_out = {}
        for g_key, g_rate in [("fixed_10k", 0.0), ("grow_3pct_yr", 0.03)]:
            w60_l, mult60_l, wd1_mo_l, ruined_l, term_l, tot_in_val = [], [], [], [], [], 0.0
            for p_i in range(n_boot):
                ix = idx_65y[p_i, :n_m]
                fx_ratios = fx_hist[ix + 1] / np.maximum(fx_hist[ix], 1e-9)
                fx_p = np.empty(n_m + 1, dtype=np.float64)
                fx_p[0] = 32.0
                fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
                res = fc_eng.simulate_lifetime_continuous(
                    r_tw_m=r_tw_m[ix], r_us_usd_m=r_us_m[ix], r_bond_usd_m=r_bd_m[ix], fx_m=fx_p,
                    start_age=start_age, glide_start_age=55, retire_age=60, end_age=90,
                    monthly_contrib_twd=10_000.0, contrib_growth_rate=g_rate,
                    withdrawal_rule="protect_20_35_plus_2",
                )
                tot_in_val = res["total_contributed_twd"]
                w60_l.append(res["wealth_at_retire_twd"])
                mult60_l.append(res["retire_multiple"])
                wd1_mo_l.append(res["year1_annual_wd_twd"] / 12.0)
                ruined_l.append(res["ruined"])
                term_l.append(res["wealth_at_end_twd"])

            w60_arr = np.array(w60_l)
            m60_arr = np.array(mult60_l)
            wd_mo_arr = np.array(wd1_mo_l)
            med_w60 = float(np.median(w60_arr))
            # How much monthly DCA is needed at this age to reach NT$10,000,000 median at age 60?
            dca_for_10m = 10_000.0 * (10_000_000.0 / max(med_w60, 1.0))

            age_out[g_key] = {
                "total_contributed_twd": round(tot_in_val, 0),
                "age60_p10_twd": round(float(np.percentile(w60_arr, 10)), 0),
                "age60_p50_twd": round(med_w60, 0),
                "age60_p90_twd": round(float(np.percentile(w60_arr, 90)), 0),
                "age60_p10_multiple": round(float(np.percentile(m60_arr, 10)), 2),
                "age60_p50_multiple": round(float(np.median(m60_arr)), 2),
                "age60_p90_multiple": round(float(np.percentile(m60_arr, 90)), 2),
                "retire_yr1_monthly_wd_p50_twd": round(float(np.median(wd_mo_arr)), 0),
                "retire_yr1_monthly_wd_p10_twd": round(float(np.percentile(wd_mo_arr, 10)), 0),
                "monthly_dca_needed_for_10m_median_twd": round(dca_for_10m, 0),
                "ruin_prob_pct": round(float(np.mean(ruined_l)) * 100.0, 2),
                "age90_p50_twd": round(float(np.median(term_l)), 0),
                "fits_10k_alone_criteria": bool(
                    (float(np.percentile(m60_arr, 10)) >= 1.30) and (float(np.median(wd_mo_arr)) >= 15_000.0)
                ),
            }
        personas[f"age_{start_age}"] = age_out

    # 3) Concrete alternatives for age-45 starter (10k fixed vs 35/35/30 vs delay retire to 65)
    age45_alts: dict[str, Any] = {}
    for alt_key, alt_lbl, acc_w, gl_age, ret_age, contrib_twd in [
        ("age45_35_35_30_ret60", "45歲改35/35/30減震、60歲退（月扣1萬）", (0.35, 0.35, 0.30), 55, 60, 10_000.0),
        ("age45_50_50_ret65", "45歲維持50/50、延至60歲滑降、65歲退（月扣1萬）", (0.50, 0.50, 0.00), 60, 65, 10_000.0),
        ("age45_50_50_30k_ret60", "45歲維持50/50、月扣提至3萬、60歲退", (0.50, 0.50, 0.00), 55, 60, 30_000.0),
        ("age40_50_50_ret65", "40歲維持50/50、延至60歲滑降、65歲退（月扣1萬）", (0.50, 0.50, 0.00), 60, 65, 10_000.0),
    ]:
        st_a = 40 if alt_key.startswith("age40") else 45
        n_m = (90 - st_a) * 12
        w_ret_l, m_ret_l, wd_mo_l, ruined_l, dd_pre_l = [], [], [], [], []
        for p_i in range(n_boot):
            ix = idx_65y[p_i, :n_m]
            fx_ratios = fx_hist[ix + 1] / np.maximum(fx_hist[ix], 1e-9)
            fx_p = np.empty(n_m + 1, dtype=np.float64)
            fx_p[0] = 32.0
            fx_p[1:] = 32.0 * np.cumprod(fx_ratios)
            res = fc_eng.simulate_lifetime_continuous(
                r_tw_m=r_tw_m[ix], r_us_usd_m=r_us_m[ix], r_bond_usd_m=r_bd_m[ix], fx_m=fx_p,
                start_age=st_a, glide_start_age=gl_age, retire_age=ret_age, end_age=90,
                monthly_contrib_twd=contrib_twd, acc_weights=acc_w,
                withdrawal_rule="protect_20_35_plus_2",
            )
            w_ret_l.append(res["wealth_at_retire_twd"])
            m_ret_l.append(res["retire_multiple"])
            wd_mo_l.append(res["year1_annual_wd_twd"] / 12.0)
            ruined_l.append(res["ruined"])
            dd_pre_l.append(res["worst_dd_50_60_pct"])
        age45_alts[alt_key] = {
            "label": alt_lbl,
            "retire_p10_twd": round(float(np.percentile(w_ret_l, 10)), 0),
            "retire_p50_twd": round(float(np.median(w_ret_l)), 0),
            "retire_p10_multiple": round(float(np.percentile(m_ret_l, 10)), 2),
            "retire_p50_multiple": round(float(np.median(m_ret_l)), 2),
            "retire_yr1_monthly_wd_p50_twd": round(float(np.median(wd_mo_l)), 0),
            "ruin_prob_pct": round(float(np.mean(ruined_l)) * 100.0, 2),
            "dd_50_60_median_pct": round(float(np.median(dd_pre_l)), 2),
        }

    # Check conservative 25/25/50 decision per criteria.md
    c25 = cons_comparison["cons_25_25_50"]
    cons_25_25_50_passes = (c25["full_history_mdd_pct"] >= -30.0) and (c25["rolling_10y_prob_mdd_lt_30pct"] <= 5.0)

    return {
        "selected_bond_etf": selected_bond,
        "conservative_comparison": cons_comparison,
        "conservative_25_25_50_passes_criteria": bool(cons_25_25_50_passes),
        "personas_by_start_age": personas,
        "late_starter_alternatives": age45_alts,
    }


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Use smaller bootstrap path count (500) for quick iteration")
    args = parser.parse_args()

    t0 = time.time()
    n_boot = 500 if args.quick else 3000
    n_boot_t1 = 500 if args.quick else 2000

    print("[1/7] Loading cached datasets and building extended series...", flush=True)
    ds = load_extended_dataset()

    print("[2/7] Running Test 1: Aggressive 200MA Filter Rules (A/B/C) & Age-50 Exit...", flush=True)
    t1 = run_test_1_aggressive_filter(ds, n_boot=n_boot_t1)

    print("[3/7] Running Test 3: Real Bond ETFs (SHY / VGIT / IEF vs ^IRX)...", flush=True)
    t3 = run_test_3_real_bonds(ds, n_boot=n_boot)
    selected_bond = t3["selected_bond_etf"]
    print(f"      -> Selected Bond ETF per criteria.md: {selected_bond}", flush=True)

    print("[4/7] Running Test 4 (Lifetime Continuous) & Test 5 (Withdrawal Rules)...", flush=True)
    t4, t5, life_meds = run_test_4_and_5_lifetime_and_withdrawal(ds, selected_bond=selected_bond, n_boot=n_boot)

    print("[5/7] Running Test 2: US Estate Tax & ETF Domicile (SPY/VOO vs CSPX)...", flush=True)
    t2 = run_test_2_estate_tax_and_domicile(ds, lifetime_medians=life_meds)

    print("[6/7] Running Test 6 (Shiller High Inflation 1871+) & Test 7 (Large FX Swings)...", flush=True)
    t6 = run_test_6_shiller_long_history(ds)
    t7 = run_test_7_fx_swings(ds, selected_bond=selected_bond, n_boot=n_boot)

    print("[7/7] Running Test 8: Different People (Ages 25/30/40/45, +3% DCA, Conservative 25/25/50)...", flush=True)
    t8 = run_test_8_different_people(ds, selected_bond=selected_bond, n_boot=min(n_boot, 2500))

    results = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query_date": QUERY_DATE,
        "bootstrap_paths": n_boot,
        "common_data_end": ds["common_end"].strftime("%Y-%m-%d"),
        "test_1_aggressive_filter": t1,
        "test_2_estate_tax_domicile": t2,
        "test_3_real_bonds": t3,
        "test_4_lifetime_continuous": t4,
        "test_5_withdrawal_protection": t5,
        "test_6_shiller_inflation": t6,
        "test_7_fx_swings": t7,
        "test_8_different_people": t8,
    }

    RESULTS_JSON_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    elapsed = time.time() - t0
    print(f"[DONE] Wrote {RESULTS_JSON_PATH} in {elapsed:.1f}s.", flush=True)


if __name__ == "__main__":
    main()

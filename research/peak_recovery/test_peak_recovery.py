"""Independent verification & schema test suite for research/peak_recovery."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.dca_allocation_grid import run_dca_allocation_grid as gm
from research.peak_recovery import run_peak_recovery as pr

BASE_DIR = Path(__file__).resolve().parent
RESULTS_PATH = BASE_DIR / "results.json"

EXPECTED_TOP_KEYS = {
    "meta",
    "data_sources",
    "data_issues",
    "episodes",
    "episode_summary",
    "peak_start_tests",
    "rolling_10y_by_regime",
    "bootstrap_10y_by_regime",
    "ranking",
    "recommendations",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def results_data() -> dict:
    assert RESULTS_PATH.exists(), f"Missing {RESULTS_PATH}; run run_peak_recovery.py first."
    return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def study_dataset() -> dict:
    raw = pr.load_or_fetch_all_data(refresh=False)
    return pr.build_complete_study_dataset(raw)


def test_results_json_schema_size_and_rounding(results_data: dict) -> None:
    assert set(results_data.keys()) == EXPECTED_TOP_KEYS
    size_mb = RESULTS_PATH.stat().st_size / (1024.0 * 1024.0)
    assert size_mb < 5.0, f"results.json too large: {size_mb:.2f} MB"
    assert results_data["meta"]["peak_memory_mb"] < 1500.0

    # Check every float in results.json has at most 2 decimal places & dates match YYYY-MM-DD
    def _walk(node, key_name: str = "") -> None:
        if isinstance(node, float):
            assert abs(node - round(node, 2)) < 1e-9, f"Float {node} at {key_name} exceeds 2 decimals"
        elif isinstance(node, str) and key_name.endswith("_date"):
            if node != "至今未回":
                assert DATE_RE.match(node), f"Invalid date format {node} at {key_name}"
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k))
        elif isinstance(node, list):
            for item in node:
                _walk(item, key_name)

    _walk(results_data)


def test_png_charts_exist_and_valid(results_data: dict) -> None:
    charts = results_data["meta"]["charts"]
    for _, fname in charts.items():
        p = BASE_DIR / fname
        assert p.exists(), f"Missing chart {p}"
        raw = p.read_bytes()
        assert len(raw) > 20_000, f"Chart {p} unexpectedly small ({len(raw)} bytes)"
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"Invalid PNG signature in {p}"


def _deepest_in_year(ep_list: list[dict], yr: str) -> dict:
    cands = [e for e in ep_list if e["peak_date"].startswith(yr)]
    assert cands, f"No episode starting in {yr}"
    return min(cands, key=lambda x: float(x["max_drawdown_pct"]))


def test_known_historical_benchmarks(results_data: dict) -> None:
    eps = results_data["episodes"]

    # 1) US 2008 GFC: trough in 2009-03, drawdown around -55%
    sp_tr_2008 = _deepest_in_year(eps["SP500TR_USD_tr"]["episodes"], "2007")
    spy_2008 = _deepest_in_year(eps["SPY_USD_tr_net30"]["episodes"], "2007")
    gspc_2008 = _deepest_in_year(eps["GSPC_USD_price"]["episodes"], "2007")

    assert sp_tr_2008["trough_date"].startswith("2009-03")
    assert spy_2008["trough_date"].startswith("2009-03")
    assert gspc_2008["trough_date"].startswith("2009-03")
    assert -56.5 <= sp_tr_2008["max_drawdown_pct"] <= -54.0
    assert -57.5 <= gspc_2008["max_drawdown_pct"] <= -54.0

    # 2) Nasdaq 2000 bubble: recovered around 2015
    ixic_2000 = _deepest_in_year(eps["IXIC_USD_price"]["episodes"], "2000")
    ndx_2000 = _deepest_in_year(eps["NDX_USD_price"]["episodes"], "2000")
    qqq_2000 = _deepest_in_year(eps["QQQ_USD_tr_net30"]["episodes"], "2000")

    assert ixic_2000["recovery_date"].startswith("2015")
    assert ndx_2000["recovery_date"].startswith("2015")
    assert qqq_2000["recovery_date"].startswith("2015")


def test_manual_episode_calculations_match_engine(results_data: dict, study_dataset: dict) -> None:
    """Independently hand-calculate 4 episodes from raw series and compare with results.json."""
    idx_map = study_dataset["index_series_map"]
    eps = results_data["episodes"]

    targets = [
        ("GSPC_USD_price", "1929"),
        ("GSPC_USD_price", "1973"),
        ("NDX_USD_price", "2000"),
        ("0050_TWD_tr", "2000"),
    ]
    for key, yr in targets:
        ep = _deepest_in_year(eps[key]["episodes"], yr)
        ser = idx_map[key]["series"].dropna()

        pk_dt = pd.Timestamp(ep["peak_date"])
        rc_dt = pd.Timestamp(ep["recovery_date"])
        pk_val = float(ser.loc[pk_dt])

        # Verify pk_dt was truly the all-time high up to pk_dt
        assert abs(float(ser.loc[:pk_dt].max()) - pk_val) < 1e-9

        # Verify no day strictly between pk_dt and rc_dt reached pk_val, and rc_dt >= pk_val
        under_window = ser.loc[pk_dt:rc_dt]
        assert float(under_window.iloc[1:-1].max()) < pk_val
        assert float(ser.loc[rc_dt]) >= pk_val

        # Hand-calculate trough date, max drawdown, and durations
        manual_tr_dt = under_window.iloc[:-1].idxmin()
        manual_tr_val = float(ser.loc[manual_tr_dt])
        manual_dd = round((manual_tr_val - pk_val) / pk_val * 100.0, 2)
        manual_dec_m = round((manual_tr_dt - pk_dt).days / (365.25 / 12.0), 2)
        manual_rec_m = round((rc_dt - manual_tr_dt).days / (365.25 / 12.0), 2)
        manual_tot_m = round((rc_dt - pk_dt).days / (365.25 / 12.0), 2)

        assert manual_tr_dt.strftime("%Y-%m-%d") == ep["trough_date"]
        assert manual_dd == ep["max_drawdown_pct"]
        assert manual_dec_m == ep["decline_months"]
        assert manual_rec_m == ep["recovery_months"]
        assert manual_tot_m == ep["total_underwater_months"]


def test_independent_share_tracking_methods(results_data: dict, study_dataset: dict) -> None:
    """Recompute M1a, M2a, M2c, M6, M7, and M8 via literal share counts on US_2007 peak (10Y) with < 0.01% error."""
    all_dates = study_dataset["all_dates"]
    w_df = study_dataset["twd_wealth_df"]
    fx_s = study_dataset["fx_aligned"]
    irx_s = study_dataset["irx_aligned"]
    cons_df = study_dataset["cons_df"]

    w50_all = w_df["0050"].to_numpy(dtype=float)
    ath_all = np.maximum.accumulate(w50_all)
    dd_prev_all = np.concatenate([[0.0], (w50_all[:-1] - ath_all[:-1]) / ath_all[:-1]])

    pk_ts = pd.Timestamp("2007-10-09")
    s_pos = int(np.searchsorted(all_dates, pk_ts, side="left"))
    ms_all = gm.get_month_start_indices(all_dates[s_pos:])
    e_pos = s_pos + int(ms_all[120])

    d_win = all_dates[s_pos : e_pos + 1]
    ms_win = ms_all[:120]
    ms_set = set(int(x) for x in ms_win)

    p50 = w_df.loc[d_win, "0050"].to_numpy(dtype=float)
    fx = fx_s.loc[d_win].to_numpy(dtype=float)
    pspy_usd = w_df.loc[d_win, "SPY"].to_numpy(dtype=float) / fx
    ptb_usd = w_df.loc[d_win, "US_TBILL"].to_numpy(dtype=float) / fx
    irx = irx_s.loc[d_win].to_numpy(dtype=float)
    cons_tw = cons_df.loc[d_win, "00631L"].to_numpy(dtype=bool)
    dd_prev = dd_prev_all[s_pos : e_pos + 1]

    ev_us2007 = next(e for e in results_data["peak_start_tests"]["events"] if e["event_id"] == "US_2007")["methods"]

    # 1) M1a: Lump sum 1.2M into 0050
    sh_m1a = 1_200_000.0 / p50[0]
    val_m1a = sh_m1a * p50[-1]
    eng_m1a = float(ev_us2007["M1a"]["horizons"]["10y"]["value_twd"])
    assert abs(val_m1a - eng_m1a) / eng_m1a < 1e-4

    # 2) M2a & M2c: Pure share accumulation
    sh_2a = 0.0
    sh_2c_50 = 0.0
    sh_2c_spy = 0.0
    for t in range(len(d_win)):
        if t in ms_set:
            sh_2a += 10_000.0 / p50[t]
            sh_2c_50 += 5_000.0 / p50[t]
            sh_2c_spy += ((5_000.0 * (1.0 - gm.FX_FEE_RATE) / fx[t]) * (1.0 - gm.US_COMM_RATE)) / pspy_usd[t]
    val_m2a = sh_2a * p50[-1]
    val_m2c = sh_2c_50 * p50[-1] + (sh_2c_spy * pspy_usd[-1]) * fx[-1]

    for val_ind, m_id in [(val_m2a, "M2a"), (val_m2c, "M2c")]:
        eng_v = float(ev_us2007[m_id]["horizons"]["10y"]["value_twd"])
        assert abs(val_ind - eng_v) / eng_v < 1e-4, f"{m_id} mismatch: {val_ind} vs {eng_v}"

    # 3) M7: 100% 0050 + 200MA cash filter via literal share & cash ledger
    sh_7 = 0.0
    cash_7 = 0.0
    for t in range(len(d_win)):
        if t > 0:
            cash_7 *= (1.0 + irx[t] / 252.0)
        if t in ms_set:
            if cons_tw[t]:
                cash_7 += 10_000.0
            else:
                sh_7 += 10_000.0 / p50[t]
        if (not cons_tw[t]) and cash_7 > 0.0:
            sh_7 += (cash_7 * (1.0 - gm.TW_REBAL_BUY_FEE)) / p50[t]
            cash_7 = 0.0
    val_m7 = sh_7 * p50[-1] + cash_7
    eng_m7 = float(ev_us2007["M7"]["horizons"]["10y"]["value_twd"])
    assert abs(val_m7 - eng_m7) / eng_m7 < 1e-4

    # 4) M6: Dip-buy literal share & cash ledger
    sh_6 = 0.0
    cash_6 = 0.0
    stg = 2 if dd_prev[0] <= -0.30 else (1 if dd_prev[0] <= -0.20 else 0)
    for t in range(len(d_win)):
        if t > 0:
            cash_6 *= (1.0 + irx[t] / 252.0)
        if dd_prev[t] >= -1e-9:
            stg = 0
        if t in ms_set:
            if stg == 0:
                sh_6 += 8_000.0 / p50[t]
                cash_6 += 2_000.0
            else:
                sh_6 += 10_000.0 / p50[t]
        if stg == 0 and dd_prev[t] <= -0.20:
            if dd_prev[t] <= -0.30:
                sh_6 += (cash_6 * (1.0 - gm.TW_REBAL_BUY_FEE)) / p50[t]
                cash_6 = 0.0
                stg = 2
            else:
                dep = 0.5 * cash_6
                sh_6 += (dep * (1.0 - gm.TW_REBAL_BUY_FEE)) / p50[t]
                cash_6 -= dep
                stg = 1
        elif stg == 1 and dd_prev[t] <= -0.30:
            sh_6 += (cash_6 * (1.0 - gm.TW_REBAL_BUY_FEE)) / p50[t]
            cash_6 = 0.0
            stg = 2
    val_m6 = sh_6 * p50[-1] + cash_6
    eng_m6 = float(ev_us2007["M6"]["horizons"]["10y"]["value_twd"])
    assert abs(val_m6 - eng_m6) / eng_m6 < 1e-4

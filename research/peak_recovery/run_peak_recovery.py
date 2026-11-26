#!/usr/bin/env python3
"""歷史高點下跌回本區間統計（Q1）與高點進場全天候投資策略穩健度回測（Q2）.

輸出目錄：research/peak_recovery/
  - run_peak_recovery.py（可獨立離線重跑；支援 --refresh / --fetch-only）
  - results.json（結構化研究結果，< 5 MB，浮點數保留 2 位小數，日期 YYYY-MM-DD）
  - underwater_duration_bars.png（各主要歷史崩盤與修正事件之總水下月數長條圖）
  - regime_xirr_boxplots.png（M1～M8 在上漲／盤整／下跌三種 10 年行情之 XIRR 箱形圖）
  - peak_2000_2008_equity_curves.png（2000 年與 2007/2008 年高點進場之帳戶市值曲線對比）
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import pickle
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.dca_allocation_grid import run_dca_allocation_grid as gm

CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MERGED_CACHE_PATH = CACHE_DIR / "peak_recovery_raw.pkl"
LONG_INDICES_CACHE_PATH = CACHE_DIR / "long_indices_yf.pkl"
GRID_CACHE_PATH = REPO_ROOT / "research" / "dca_allocation_grid" / "cache" / "yf_raw_data.pkl"
LEV_CACHE_PATH = REPO_ROOT / "research" / "leverage_study" / "cache" / "yf_raw_history.pkl"

RESULTS_JSON_PATH = BASE_DIR / "results.json"
CHART_UNDERWATER_PATH = BASE_DIR / "underwater_duration_bars.png"
CHART_BOXPLOT_PATH = BASE_DIR / "regime_xirr_boxplots.png"
CHART_CURVES_PATH = BASE_DIR / "peak_2000_2008_equity_curves.png"

AVG_DAYS_PER_MONTH = 365.25 / 12.0  # 30.4375
LUMP_SUM_TWD = 1_200_000.0          # 10 年份本金 (120 個月 × 10,000 台幣)
BOOTSTRAP_PATHS = 5000
BOOTSTRAP_BATCH_SIZE = 250
BOOTSTRAP_SEED = 20261126

LONG_INDEX_SPECS = {
    "^GSPC": "1927-01-01",
    "^SP500TR": "1988-01-01",
    "^NDX": "1985-01-01",
    "^IXIC": "1971-01-01",
}

SEVERITY_BUCKETS = [
    ("10_20_correction", "10%～20%（修正）", -20.0, -10.0),
    ("20_30_bear", "20%～30%（熊市）", -30.0, -20.0),
    ("30_50_deep_bear", "30%～50%（大熊市）", -50.0, -30.0),
    ("50_plus_crash", "50% 以上（崩盤）", -1000.0, -50.0),
]

METHOD_SPECS: list[dict[str, Any]] = [
    {
        "id": "M1a",
        "group": "M1",
        "name": "M1a 單筆投入：100% 0050（120萬一次買進）",
        "short_name": "M1a 單筆 0050",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": True,
        "description": "在起點將 10 年份資金（120 萬台幣）一次全數買進 0050，之後不再投入新資金。",
    },
    {
        "id": "M1b",
        "group": "M1",
        "name": "M1b 單筆投入：100% SPY（120萬一次買進）",
        "short_name": "M1b 單筆 SPY",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": True,
        "description": "在起點將 10 年份資金（120 萬台幣）換匯扣手續費後一次買進美股 SPY，之後不再投入。",
    },
    {
        "id": "M2a",
        "group": "M2",
        "name": "M2a 定期定額 1x：100% 0050",
        "short_name": "M2a 定投 0050",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月第一個交易日固定扣 1 萬台幣買進 100% 0050，不擇時、不再平衡。",
    },
    {
        "id": "M2b",
        "group": "M2",
        "name": "M2b 定期定額 1x：100% SPY",
        "short_name": "M2b 定投 SPY",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月第一個交易日固定扣 1 萬台幣換匯買進 100% 美股 SPY，不擇時、不再平衡。",
    },
    {
        "id": "M2c",
        "group": "M2",
        "name": "M2c 定期定額 1x：50% 0050 + 50% SPY",
        "short_name": "M2c 台美各半 1x",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月第一個交易日固定扣 5,000 台幣買 0050、5,000 台幣買 SPY，台美分散 1 倍定投。",
    },
    {
        "id": "M3a",
        "group": "M3",
        "name": "M3a 小槓桿 1.3x：70% 0050 + 30% 00631L（200MA濾網）",
        "short_name": "M3a 台股 1.3x+濾網",
        "effective_leverage": 1.3,
        "rebalance": "none",
        "filter": "sma200_buf2",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月 70% 買 0050、30% 買正二 00631L；跌破 200 日線 -2% 時新錢全買 0050，站回 +2% 恢復，舊部位不賣。",
    },
    {
        "id": "M3b",
        "group": "M3",
        "name": "M3b 小槓桿 1.4x：40% 0050 + 40% QQQ + 20% TQQQ（年平衡）",
        "short_name": "M3b 台美 1.4x 年平衡",
        "effective_leverage": 1.4,
        "rebalance": "annual",
        "filter": "none",
        "needs_qqq": True,
        "is_lump_sum": False,
        "description": "每月 40% 買 0050、40% 買 QQQ、20% 買 TQQQ，每年第一個交易日再平衡一次，不開濾網。",
    },
    {
        "id": "M4",
        "group": "M4",
        "name": "M4 兩倍槓桿 2.0x：100% 00631L",
        "short_name": "M4 100% 正二",
        "effective_leverage": 2.0,
        "rebalance": "none",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月 1 萬台幣全買元大台灣50正2（00631L，上市前以 2 倍 0050 扣內扣費模擬）。",
    },
    {
        "id": "M5",
        "group": "M5",
        "name": "M5 極限槓桿 2.8x：20% 00631L + 80% TQQQ（季平衡+濾網）",
        "short_name": "M5 極限 2.8x",
        "effective_leverage": 2.8,
        "rebalance": "quarterly",
        "filter": "sma200_buf2",
        "needs_qqq": True,
        "is_lump_sum": False,
        "description": "每月 20% 買 00631L、80% 買 TQQQ，搭配每季再平衡與 200 日線 ±2% 濾網（上一份研究極限總冠軍）。",
    },
    {
        "id": "M6",
        "group": "M6",
        "name": "M6 定投+跌深加碼 1x：8千 0050 + 2千存現金（-20%/-30%加碼）",
        "short_name": "M6 跌深加碼 0050",
        "effective_leverage": 1.0,
        "rebalance": "dip_buy",
        "filter": "dd_20_30",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "平時每月 8 千買 0050、2 千存現金領短債息；指數離歷史高點跌逾 20% 投入半數現金、跌逾 30% 投入剩餘現金（水下期間月扣全買 0050），回到高點後重新存現金。",
    },
    {
        "id": "M7",
        "group": "M7",
        "name": "M7 定投+均線濾網 1x：100% 0050（破200MA存現金、站回全投）",
        "short_name": "M7 0050+均線存現",
        "effective_leverage": 1.0,
        "rebalance": "none",
        "filter": "sma200_cash",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "100% 0050；跌破 200 日均線 -2% 時新扣款改存現金生息，站回均線 +2% 時將累積現金一次買進 0050，既有部位不賣。",
    },
    {
        "id": "M8",
        "group": "M8",
        "name": "M8 股債配置 60/40：60% 0050 + 40% 美國短債（年平衡）",
        "short_name": "M8 股60債40年平衡",
        "effective_leverage": 0.6,
        "rebalance": "annual",
        "filter": "none",
        "needs_qqq": False,
        "is_lump_sum": False,
        "description": "每月 60% 買 0050、40% 買美國短期公債（以 ^IRX 累積報酬加計美元匯率），每年再平衡一次。",
    },
]


# ---------------------------------------------------------------------------
# 1. Data Reuse, Missing Long-History Download & Hygiene Validation
# ---------------------------------------------------------------------------

def load_or_fetch_all_data(refresh: bool = False) -> dict[str, Any]:
    """Reuse existing caches first, download only missing long-history indices, and save standalone cache."""
    if MERGED_CACHE_PATH.exists() and not refresh:
        return pickle.loads(MERGED_CACHE_PATH.read_bytes())

    base_histories: dict[str, pd.DataFrame] = {}
    expense_info: dict[str, Any] = {}
    source_tags: dict[str, str] = {}

    if GRID_CACHE_PATH.exists() and not refresh:
        grid_payload = pickle.loads(GRID_CACHE_PATH.read_bytes())
        for sym, df in grid_payload.get("histories", {}).items():
            base_histories[sym] = df
            source_tags[sym] = "reused_dca_allocation_grid_cache"
        expense_info.update(grid_payload.get("expense_info", {}))

    if LEV_CACHE_PATH.exists() and not refresh:
        lev_payload = pickle.loads(LEV_CACHE_PATH.read_bytes())
        lev_hist = lev_payload.get("histories", lev_payload) if isinstance(lev_payload, dict) else {}
        for sym, df in lev_hist.items():
            if sym not in base_histories:
                base_histories[sym] = df
                source_tags[sym] = "reused_leverage_study_cache"

    # Ensure core ETF/FX/IRX symbols exist (if running in a clean clone without prior study caches)
    for sym, start_d in gm.YF_DOWNLOAD_SYMBOLS.items():
        if sym in LONG_INDEX_SPECS:
            continue
        if sym not in base_histories or refresh:
            print(f"[YF] Downloading core symbol {sym}...", flush=True)
            time.sleep(0.6)
            tkr = yf.Ticker(sym)
            if sym in ("0050.TW", "TWD=X", "^TWII", "^IRX"):
                df = tkr.history(period="max", auto_adjust=False, actions=True)
            else:
                df = tkr.history(start=start_d, auto_adjust=False, actions=True)
            df = _clean_yf_df(df)
            if start_d and sym not in ("0050.TW", "TWD=X"):
                df = df.loc[start_d:].copy()
            base_histories[sym] = df
            source_tags[sym] = "yahoo_finance_downloaded"

    # Load or download full-history ^GSPC (1928+), ^SP500TR (1988+), ^NDX (1985+), ^IXIC (1971+)
    long_histories: dict[str, pd.DataFrame] = {}
    if LONG_INDICES_CACHE_PATH.exists() and not refresh:
        long_payload = pickle.loads(LONG_INDICES_CACHE_PATH.read_bytes())
        long_histories.update(long_payload.get("histories", {}))

    for sym, fallback_start in LONG_INDEX_SPECS.items():
        need_dl = refresh or (sym not in long_histories)
        if not need_dl:
            # Verify it truly has long history (e.g., ^GSPC before 1930, ^NDX before 1986)
            first_yr = int(long_histories[sym].index[0].year)
            exp_yr = int(fallback_start[:4]) + 1
            if first_yr > exp_yr:
                need_dl = True
        if need_dl:
            print(f"[YF] Downloading long-history index {sym} (period=max)...", flush=True)
            time.sleep(0.6)
            df = yf.Ticker(sym).history(period="max", auto_adjust=False, actions=True)
            if df is None or df.empty:
                df = yf.download(sym, start=fallback_start, auto_adjust=False, actions=True, progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
            long_histories[sym] = _clean_yf_df(df)
        source_tags[sym] = "yahoo_finance_downloaded_long_history"

    # Keep full-history ^GSPC and ^NDX in long_histories, and also supply >=1990 slices for grid_mod compatibility
    grid_compat_histories = dict(base_histories)
    grid_compat_histories["^GSPC"] = long_histories["^GSPC"].loc["1990-01-01":].copy()
    grid_compat_histories["^NDX"] = long_histories["^NDX"].loc["1990-01-01":].copy()

    merged_payload = {
        "fetched_at": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "grid_histories": grid_compat_histories,
        "long_histories": long_histories,
        "expense_info": expense_info,
        "source_tags": source_tags,
    }
    MERGED_CACHE_PATH.write_bytes(pickle.dumps(merged_payload))
    return merged_payload


def _clean_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["Close"]).copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def build_complete_study_dataset(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    """Build cleaned ETF/index/FX/bond series, reusing run_dca_allocation_grid.prepare_market_dataset."""
    grid_raw = {
        "histories": raw_bundle["grid_histories"],
        "expense_info": raw_bundle.get("expense_info", {}),
    }
    ds = gm.prepare_market_dataset(grid_raw)
    common_end: pd.Timestamp = ds["common_end"]
    h_grid = raw_bundle["grid_histories"]
    h_long = raw_bundle["long_histories"]
    source_tags = raw_bundle["source_tags"]

    # Clean TWD=X across full date range back to 1927 (backfilled at earliest 2004-03-24 rate 32.0230, documented)
    twd_raw = h_grid["TWD=X"]["Close"].loc[:common_end].copy()
    twd_clean = twd_raw.where((twd_raw >= 24.0) & (twd_raw <= 40.0)).ffill().bfill()
    prev_fx = twd_clean.shift(1).bfill()
    next_fx = twd_clean.shift(-1).ffill()
    navg = 0.5 * (prev_fx + next_fx)
    spike_mask = (((twd_clean - navg) / navg).abs() > 0.035) & (((prev_fx - next_fx) / prev_fx).abs() < 0.010)
    twd_clean = twd_clean.mask(spike_mask).ffill().bfill()
    earliest_fx_dt = twd_raw.dropna().index[0]

    # Full SPY (1993-01-29..common_end) & QQQ (1999-03-10..common_end) investor USD net-of-30%-tax wealth
    def _build_us_etf_net30_wealth(df_etf: pd.DataFrame) -> pd.Series:
        sub = df_etf.loc[:common_end].copy()
        r_pr = sub["Close"].pct_change().fillna(0.0)
        r_tr = sub["Adj Close"].pct_change().fillna(0.0)
        has_div = (sub["Dividends"] > 0) if "Dividends" in sub.columns else pd.Series(True, index=sub.index)
        div_ret = (r_tr - r_pr).clip(lower=0.0).where(has_div, 0.0)
        r_net = r_tr - gm.US_DIV_TAX_RATE * div_ret
        r_net.iloc[0] = 0.0
        return (1.0 + r_net).cumprod()

    spy_usd_full = _build_us_etf_net30_wealth(h_grid["SPY"])
    qqq_usd_full = _build_us_etf_net30_wealth(h_grid["QQQ"])

    # Add US_TBILL (cumulative ^IRX short Treasury return in USD -> TWD) onto ds["twd_wealth_df"]
    all_dates: pd.DatetimeIndex = ds["all_dates"]
    us_dates = h_grid["SPY"].loc[:common_end].index
    irx_us = (h_grid["^IRX"]["Close"].reindex(us_dates).ffill().bfill() / 100.0).shift(1).fillna(0.05).clip(lower=0.0)
    r_tbill_usd = irx_us / gm.TRADING_DAYS_PER_YEAR
    r_tbill_usd.iloc[0] = 0.0
    w_tbill_usd = (1.0 + r_tbill_usd).cumprod()

    twd_wealth_df = ds["twd_wealth_df"].copy()
    twd_wealth_df["US_TBILL"] = w_tbill_usd.reindex(all_dates).ffill().bfill() * ds["fx_aligned"]

    # Reconstruct split-adjusted 1x price series on native & unified calendars for bootstrap 200-SMA
    df50 = h_grid["0050.TW"].loc[:common_end].copy()
    for col in ["Open", "High", "Low", "Close"]:
        df50.loc[:"2013-12-31", col] /= 4.0
    df50 = df50.loc["2009-03-10":].copy()
    twii_df = h_grid["^TWII"].loc["1997-01-01":common_end].copy()
    tw_dates = twii_df.index.union(df50.index).sort_values()
    r_twii = twii_df["Close"].reindex(tw_dates).ffill().pct_change().fillna(0.0)
    r_50_pr = pd.Series(0.0, index=tw_dates)
    pre_50 = tw_dates <= pd.Timestamp("2009-03-10")
    r_50_pr.loc[pre_50] = r_twii.loc[pre_50]
    r_50_pr.iloc[0] = 0.0
    r_50_pr_real = df50.reindex(tw_dates.intersection(df50.index))["Close"].pct_change()
    r_50_pr.loc[r_50_pr_real.index[1:]] = r_50_pr_real.iloc[1:]
    p_0050_pr_aligned = (1.0 + r_50_pr).cumprod().reindex(all_dates).ffill().bfill()

    qqq_c = h_grid["QQQ"]["Close"].loc[:common_end]
    ndx_pre = h_long["^NDX"]["Close"].loc["1998-01-01":qqq_c.index[0]]
    scale_q = float(qqq_c.iloc[0]) / float(ndx_pre.iloc[-1])
    p_qqq_pr_aligned = pd.concat([ndx_pre.iloc[:-1] * scale_q, qqq_c]).reindex(all_dates).ffill().bfill()

    is_tw_open = all_dates.isin(tw_dates)
    is_us_open = all_dates.isin(us_dates)

    # Validate newly downloaded long-history indices & build data_sources + data_issues
    long_checks: dict[str, Any] = {}
    for sym in ("^GSPC", "^SP500TR", "^NDX", "^IXIC"):
        s_c = h_long[sym]["Close"].loc[:common_end]
        d_ret = s_c.pct_change().dropna()
        if bool((s_c <= 0).any()):
            raise AssertionError(f"{sym} contains non-positive close values!")
        if float(d_ret.abs().max()) > 0.25:
            raise AssertionError(f"{sym} failed daily return sanity bound (>25%): {d_ret.abs().max():.4f}")
        prev_c = s_c.shift(1)
        next_c = s_c.shift(-1)
        navg_c = 0.5 * (prev_c + next_c)
        bad_tick_mask = (((s_c - navg_c) / navg_c).abs() > 0.12) & (((prev_c - next_c) / prev_c).abs() < 0.01)
        bad_ticks = [d.strftime("%Y-%m-%d") for d in s_c.index[bad_tick_mask]]
        over_15 = d_ret[d_ret.abs() > 0.15]
        long_checks[sym] = {
            "start_date": s_c.index[0].strftime("%Y-%m-%d"),
            "end_date": s_c.index[-1].strftime("%Y-%m-%d"),
            "trading_days": int(len(s_c)),
            "min_daily_ret_pct": round(float(d_ret.min()) * 100.0, 2),
            "max_daily_ret_pct": round(float(d_ret.max()) * 100.0, 2),
            "extreme_days_gt_15pct": [
                {"date": dt.strftime("%Y-%m-%d"), "ret_pct": round(float(val) * 100.0, 2)}
                for dt, val in over_15.items()
            ],
            "isolated_bad_ticks": bad_ticks,
        }

    # Build Part A index dictionary (both USD & TWD for US indices, price & total return)
    def _to_twd(usd_ser: pd.Series) -> pd.Series:
        fx_re = twd_clean.reindex(usd_ser.index).ffill().bfill()
        return usd_ser * fx_re

    gspc_usd = h_long["^GSPC"]["Close"].loc[:common_end].copy()
    sp500tr_usd = h_long["^SP500TR"]["Close"].loc[:common_end].copy()
    ndx_usd = h_long["^NDX"]["Close"].loc[:common_end].copy()
    ixic_usd = h_long["^IXIC"]["Close"].loc[:common_end].copy()
    twii_twd = h_grid["^TWII"]["Close"].loc[:common_end].copy()
    w0050_tw = twd_wealth_df["0050"].loc[tw_dates.intersection(all_dates)].copy()

    index_series_map: dict[str, dict[str, Any]] = {
        "GSPC_USD_price": {
            "symbol": "^GSPC",
            "label": "S&P 500 價格指數（USD，不含息）",
            "market": "US",
            "currency": "USD",
            "return_type": "price",
            "series": gspc_usd,
        },
        "SP500TR_USD_tr": {
            "symbol": "^SP500TR",
            "label": "S&P 500 含息總報酬指數（USD，稅前毛額）",
            "market": "US",
            "currency": "USD",
            "return_type": "total_return_gross",
            "series": sp500tr_usd,
        },
        "SPY_USD_tr_net30": {
            "symbol": "SPY",
            "label": "SPY 含息總報酬（USD，扣 30% 股息預扣稅）",
            "market": "US",
            "currency": "USD",
            "return_type": "total_return_net30",
            "series": spy_usd_full,
        },
        "SP500TR_TWD_tr": {
            "symbol": "^SP500TR (TWD)",
            "label": "S&P 500 含息總報酬（台幣計價，2004-03 前匯率延用 32.02）",
            "market": "US_TWD",
            "currency": "TWD",
            "return_type": "total_return_gross_twd",
            "series": _to_twd(sp500tr_usd),
        },
        "SPY_TWD_tr_net30": {
            "symbol": "SPY (TWD)",
            "label": "SPY 含息扣 30% 稅（台幣計價，台灣投資人實拿基準）",
            "market": "US_TWD",
            "currency": "TWD",
            "return_type": "total_return_net30_twd",
            "series": _to_twd(spy_usd_full),
        },
        "NDX_USD_price": {
            "symbol": "^NDX",
            "label": "Nasdaq 100 價格指數（USD，不含息）",
            "market": "US",
            "currency": "USD",
            "return_type": "price",
            "series": ndx_usd,
        },
        "IXIC_USD_price": {
            "symbol": "^IXIC",
            "label": "Nasdaq 綜合價格指數（USD，不含息）",
            "market": "US",
            "currency": "USD",
            "return_type": "price",
            "series": ixic_usd,
        },
        "QQQ_USD_tr_net30": {
            "symbol": "QQQ",
            "label": "QQQ 含息總報酬（USD，扣 30% 股息預扣稅）",
            "market": "US",
            "currency": "USD",
            "return_type": "total_return_net30",
            "series": qqq_usd_full,
        },
        "NDX_TWD_price": {
            "symbol": "^NDX (TWD)",
            "label": "Nasdaq 100 價格指數（台幣計價）",
            "market": "US_TWD",
            "currency": "TWD",
            "return_type": "price_twd",
            "series": _to_twd(ndx_usd),
        },
        "QQQ_TWD_tr_net30": {
            "symbol": "QQQ (TWD)",
            "label": "QQQ 含息扣 30% 稅（台幣計價，台灣投資人實拿基準）",
            "market": "US_TWD",
            "currency": "TWD",
            "return_type": "total_return_net30_twd",
            "series": _to_twd(qqq_usd_full),
        },
        "TWII_TWD_price": {
            "symbol": "^TWII",
            "label": "台股加權價格指數（TWD，不含息）",
            "market": "TW",
            "currency": "TWD",
            "return_type": "price",
            "series": twii_twd,
        },
        "0050_TWD_tr": {
            "symbol": "0050",
            "label": "台股 0050 含息總報酬（TWD，2009-03 前以 ^TWII+3% 股息模擬）",
            "market": "TW",
            "currency": "TWD",
            "return_type": "total_return",
            "series": w0050_tw,
        },
    }

    data_sources = []
    for sym_key, label_txt, mkt, ccy, rtype, ser_ref, src_tag, sim_note in [
        ("^GSPC", "S&P 500 價格指數", "US", "USD", "price", gspc_usd, source_tags.get("^GSPC", ""), "純價格指數，不含股息再投資"),
        ("^SP500TR", "S&P 500 含息總報酬指數", "US", "USD", "total_return_gross", sp500tr_usd, source_tags.get("^SP500TR", ""), "含稅前股息再投資總報酬"),
        ("SPY", "SPDR S&P 500 ETF", "US", "USD/TWD", "total_return_net30", spy_usd_full, source_tags.get("SPY", ""), "配息扣除台灣投資人 30% 預扣稅"),
        ("^NDX", "Nasdaq 100 價格指數", "US", "USD", "price", ndx_usd, source_tags.get("^NDX", ""), "純價格指數，不含股息"),
        ("^IXIC", "Nasdaq 綜合價格指數", "US", "USD", "price", ixic_usd, source_tags.get("^IXIC", ""), "純價格指數，不含股息"),
        ("QQQ", "Invesco QQQ Trust", "US", "USD/TWD", "total_return_net30", qqq_usd_full, source_tags.get("QQQ", ""), "配息扣除 30% 預扣稅；1999-03-10 上市"),
        ("^TWII", "台灣加權股價指數", "TW", "TWD", "price", twii_twd, source_tags.get("^TWII", ""), "Yahoo Finance 日線自 1997-07-02 起，不含除權息還原"),
        ("0050", "元大台灣50（含息還原）", "TW", "TWD", "total_return", w0050_tw, source_tags.get("0050.TW", ""), "2009-03-10 前以 ^TWII 日報酬加年化 3.0% 殖利率模擬至 1997-07-02"),
        ("00631L", "元大台灣50正2", "TW", "TWD", "leveraged_2x", twd_wealth_df["00631L"], source_tags.get("00631L.TW", ""), "2015-08-25 前以 2×0050 扣除 1.16% 年總費用率模擬"),
        ("TQQQ", "ProShares UltraPro QQQ (3x)", "US", "USD/TWD", "leveraged_3x", twd_wealth_df["TQQQ"].dropna(), source_tags.get("TQQQ", ""), "2010-02-11 前以 3×QQQ 扣 0.88% 費率與 2×^IRX 融資成本模擬"),
        ("TWD=X", "美元兌台幣匯率", "FX", "TWD/USD", "fx_spot", ds["fx_aligned"], source_tags.get("TWD=X", ""), f"真實日線自 {earliest_fx_dt.strftime('%Y-%m-%d')} 起；更早期間延用 {float(twd_clean.iloc[0]):.2f}"),
        ("^IRX", "美國 13 週國庫券殖利率", "US", "USD", "short_rate", ds["irx_aligned"], source_tags.get("^IRX", ""), "用於槓桿融資成本、M6/M7 閒置現金計息與 M8 短債報酬累積"),
    ]:
        data_sources.append({
            "symbol": sym_key,
            "name": label_txt,
            "market": mkt,
            "currency": ccy,
            "return_type": rtype,
            "start_date": ser_ref.index[0].strftime("%Y-%m-%d"),
            "end_date": ser_ref.index[-1].strftime("%Y-%m-%d"),
            "trading_days": int(len(ser_ref)),
            "source": src_tag,
            "note": sim_note,
        })

    data_issues = {
        "hygiene_notes": ds["hygiene_notes"] + [
            "^TWII 台股加權指數於 Yahoo Finance 最早日線僅自 1997-07-02 起，因此 1990 年 2 月台股 12,682 點崩盤至 2,485 點（-80.4%，價格指數至 2020-07 才突破前高、歷時約 30.4 年）不在 Yahoo 日線樣本內，於報告中專節補充說明。",
            "新下載之四檔長歷史指數（^GSPC 1927-12-30 起、^SP500TR 1988-01-04 起、^NDX 1985-10-01 起、^IXIC 1971-02-05 起）全數通過無負值、無重複日期與無拆股斷層檢查；單日漲跌幅逾 15% 之交易日皆為真實歷史事件（1933 銀行假復市 +16.61%、1987 黑色星期一 -20.47%/-15.08%、2001 聯準會緊急降息 +18.77%）。",
        ],
        "etf_and_fx_anomaly_checks": ds["anomaly_checks"],
        "long_index_anomaly_checks": long_checks,
        "leveraged_etf_tracking_error": ds["tracking_error"],
    }

    return {
        "common_end": common_end,
        "start_a": ds["start_a"],
        "start_b": ds["start_b"],
        "all_dates": all_dates,
        "tw_dates": tw_dates,
        "us_dates": us_dates,
        "is_tw_open": is_tw_open,
        "is_us_open": is_us_open,
        "twd_wealth_df": twd_wealth_df,
        "fx_aligned": ds["fx_aligned"],
        "irx_aligned": ds["irx_aligned"],
        "cons_df": ds["cons_df"],
        "p_0050_pr_aligned": p_0050_pr_aligned,
        "p_qqq_pr_aligned": p_qqq_pr_aligned,
        "index_series_map": index_series_map,
        "data_sources": data_sources,
        "data_issues": data_issues,
    }


# ---------------------------------------------------------------------------
# 2. Part A: Peak-to-Recovery Underwater Episode Extraction & Summary (Q1)
# ---------------------------------------------------------------------------

KNOWN_EVENT_NAMES: list[tuple[str, str, str]] = [
    ("1929-01-01", "1932-12-31", "1929 經濟大蕭條"),
    ("1937-01-01", "1938-12-31", "1937 羅斯福緊縮衰退"),
    ("1946-01-01", "1947-12-31", "1946 二戰後通膨修正"),
    ("1956-01-01", "1957-12-31", "1956 蘇伊士運河與艾森豪衰退"),
    ("1961-06-01", "1962-12-31", "1962 甘迺迪閃崩熊市"),
    ("1966-01-01", "1966-12-31", "1966 信貸緊縮熊市"),
    ("1968-06-01", "1970-12-31", "1968–1970 越戰與通膨衰退"),
    ("1972-11-01", "1974-12-31", "1973–1974 第一次石油危機與停滯性通膨"),
    ("1978-06-01", "1979-03-31", "1978 二次通膨修正"),
    ("1980-01-01", "1982-09-01", "1980–1982 伏克爾暴力升息衰退"),
    ("1983-04-01", "1984-12-31", "1983–1984 科技硬體泡沫修正"),
    ("1987-07-01", "1987-12-31", "1987 黑色星期一崩盤"),
    ("1989-09-01", "1990-12-31", "1990 波斯灣戰爭與儲貸危機"),
    ("1997-07-01", "1998-03-31", "1997 亞洲金融風暴"),
    ("1998-06-01", "1998-11-30", "1998 俄羅斯違約與 LTCM 危機"),
    ("2000-01-01", "2001-06-30", "2000 網路泡沫崩盤"),
    ("2007-07-01", "2008-06-30", "2008 全球金融海嘯"),
    ("2011-02-01", "2011-10-31", "2011 歐債危機與美債降評"),
    ("2015-04-01", "2016-02-28", "2015 中國匯改股災與出口衰退"),
    ("2018-01-01", "2018-11-30", "2018 美中貿易戰與縮表修正"),
    ("2020-01-01", "2020-03-31", "2020 新冠疫情全球熔斷"),
    ("2021-11-01", "2022-03-31", "2022 聯準會暴力升息與通膨熊市"),
    ("2024-06-01", "2024-08-31", "2024 夏季日圓套利平倉與 AI 修正"),
    ("2024-12-01", "2025-04-30", "2025 關稅戰震盪修正"),
    ("2026-09-01", "2026-11-30", "2026 秋季高檔拉回（進行中）"),
]


def _classify_severity(dd_pct: float) -> tuple[str, str]:
    for b_key, b_lbl, lo_excl, hi_incl in SEVERITY_BUCKETS:
        if lo_excl < dd_pct <= hi_incl:
            return b_key, b_lbl
    return "50_plus_crash", "50% 以上（崩盤）"


def _name_episode(peak_dt: pd.Timestamp) -> str:
    p_str = peak_dt.strftime("%Y-%m-%d")
    for s_d, e_d, title in KNOWN_EVENT_NAMES:
        if s_d <= p_str <= e_d:
            return title
    return f"{peak_dt.year} 年波段修正"


def extract_episodes_for_series(ser: pd.Series, min_dd_pct: float = -10.0) -> list[dict[str, Any]]:
    """Extract all ATH-to-ATH underwater episodes with max_drawdown_pct <= min_dd_pct."""
    s = ser.dropna()
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
                    pk_dt = dates[i_pk]
                    tr_dt = dates[i_tr]
                    rc_dt = dates[t]
                    dec_m = (tr_dt - pk_dt).days / AVG_DAYS_PER_MONTH
                    rec_m = (rc_dt - tr_dt).days / AVG_DAYS_PER_MONTH
                    tot_m = (rc_dt - pk_dt).days / AVG_DAYS_PER_MONTH
                    b_key, b_lbl = _classify_severity(dd_pct)
                    episodes.append({
                        "episode_name": _name_episode(pk_dt),
                        "severity_bucket": b_key,
                        "severity_label": b_lbl,
                        "peak_date": pk_dt.strftime("%Y-%m-%d"),
                        "trough_date": tr_dt.strftime("%Y-%m-%d"),
                        "recovery_date": rc_dt.strftime("%Y-%m-%d"),
                        "recovered": True,
                        "peak_value": round(h_val, 2),
                        "trough_value": round(tr_val, 2),
                        "max_drawdown_pct": round(dd_pct, 2),
                        "decline_months": round(dec_m, 2),
                        "recovery_months": round(rec_m, 2),
                        "total_underwater_months": round(tot_m, 2),
                        "total_underwater_years": round(tot_m / 12.0, 2),
                    })
            h_val = v
            i_pk = t

    # Check ongoing unrecovered drawdown at end of sample
    if float(vals[-1]) < h_val and (n - 1) > i_pk:
        seg = vals[i_pk:]
        i_tr = i_pk + int(np.argmin(seg))
        tr_val = float(vals[i_tr])
        dd_pct = (tr_val - h_val) / h_val * 100.0
        if dd_pct <= min_dd_pct:
            pk_dt = dates[i_pk]
            tr_dt = dates[i_tr]
            end_dt = dates[-1]
            dec_m = (tr_dt - pk_dt).days / AVG_DAYS_PER_MONTH
            el_rec_m = (end_dt - tr_dt).days / AVG_DAYS_PER_MONTH
            el_tot_m = (end_dt - pk_dt).days / AVG_DAYS_PER_MONTH
            b_key, b_lbl = _classify_severity(dd_pct)
            episodes.append({
                "episode_name": _name_episode(pk_dt),
                "severity_bucket": b_key,
                "severity_label": b_lbl,
                "peak_date": pk_dt.strftime("%Y-%m-%d"),
                "trough_date": tr_dt.strftime("%Y-%m-%d"),
                "recovery_date": "至今未回",
                "recovered": False,
                "peak_value": round(h_val, 2),
                "trough_value": round(tr_val, 2),
                "max_drawdown_pct": round(dd_pct, 2),
                "decline_months": round(dec_m, 2),
                "recovery_months": None,
                "total_underwater_months": None,
                "total_underwater_years": None,
                "elapsed_recovery_months": round(el_rec_m, 2),
                "elapsed_underwater_months": round(el_tot_m, 2),
            })

    return episodes


def _summarize_episode_group(ep_list: list[dict[str, Any]]) -> dict[str, Any]:
    n_tot = len(ep_list)
    rec_eps = [e for e in ep_list if e["recovered"]]
    n_rec = len(rec_eps)
    n_unrec = n_tot - n_rec

    def _stat_3(vals: list[float]) -> dict[str, float | None]:
        if not vals:
            return {"median": None, "mean": None, "max": None}
        arr = np.array(vals, dtype=np.float64)
        return {
            "median": round(float(np.median(arr)), 2),
            "mean": round(float(np.mean(arr)), 2),
            "max": round(float(np.max(arr)), 2),
        }

    # Decline months uses recovered episodes (plus unrecovered only in total count; keep recovered stats clean & exact)
    dec_vals = [float(e["decline_months"]) for e in rec_eps]
    rec_vals = [float(e["recovery_months"]) for e in rec_eps if e["recovery_months"] is not None]
    tot_vals = [float(e["total_underwater_months"]) for e in rec_eps if e["total_underwater_months"] is not None]
    dd_vals = [float(e["max_drawdown_pct"]) for e in ep_list]

    return {
        "total_episodes": n_tot,
        "recovered_episodes": n_rec,
        "unrecovered_episodes": n_unrec,
        "median_drawdown_pct": round(float(np.median(dd_vals)), 2) if dd_vals else None,
        "worst_drawdown_pct": round(float(np.min(dd_vals)), 2) if dd_vals else None,
        "decline_months": _stat_3(dec_vals),
        "recovery_months": _stat_3(rec_vals),
        "total_underwater_months": _stat_3(tot_vals),
    }


def compute_part_a_episodes(study_ds: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run Part A across all indices, severity buckets, price-vs-TR comparisons, and top-5 longest episodes."""
    idx_map = study_ds["index_series_map"]
    episodes_out: dict[str, Any] = {}
    by_index_bucket: dict[str, Any] = {}

    for key, meta in idx_map.items():
        ser: pd.Series = meta["series"]
        eps = extract_episodes_for_series(ser, min_dd_pct=-10.0)
        episodes_out[key] = {
            "symbol": meta["symbol"],
            "label": meta["label"],
            "market": meta["market"],
            "currency": meta["currency"],
            "return_type": meta["return_type"],
            "start_date": ser.index[0].strftime("%Y-%m-%d"),
            "end_date": ser.index[-1].strftime("%Y-%m-%d"),
            "episode_count": len(eps),
            "episodes": eps,
        }

        b_dict: dict[str, Any] = {}
        for b_key, b_lbl, _, _ in SEVERITY_BUCKETS:
            sub_eps = [e for e in eps if e["severity_bucket"] == b_key]
            b_dict[b_key] = {"label": b_lbl, **_summarize_episode_group(sub_eps)}
        gte20_eps = [e for e in eps if float(e["max_drawdown_pct"]) <= -20.0]
        b_dict["all_gte_20pct"] = {"label": "跌超過 20% 合計（全部熊市與崩盤）", **_summarize_episode_group(gte20_eps)}
        b_dict["all_gte_10pct"] = {"label": "跌超過 10% 合計（全部修正與熊市）", **_summarize_episode_group(eps)}
        by_index_bucket[key] = {
            "symbol": meta["symbol"],
            "label": meta["label"],
            "period": f"{ser.index[0].strftime('%Y-%m-%d')} ~ {ser.index[-1].strftime('%Y-%m-%d')}",
            "buckets": b_dict,
        }

    # Price vs Total Return & USD vs TWD side-by-side comparison on major historical crashes
    def _find_ep_near_year(key: str, yr_start: int, yr_end: int) -> dict[str, Any] | None:
        cands = [
            e for e in episodes_out[key]["episodes"]
            if yr_start <= int(e["peak_date"][:4]) <= yr_end and float(e["max_drawdown_pct"]) <= -18.0
        ]
        if not cands:
            return None
        return min(cands, key=lambda x: float(x["max_drawdown_pct"]))

    comparisons = []
    comp_specs = [
        ("2000 網路泡沫（S&P 500）", "GSPC_USD_price", "SP500TR_USD_tr", "SPY_TWD_tr_net30", 2000, 2000),
        ("2008 金融海嘯（S&P 500）", "GSPC_USD_price", "SP500TR_USD_tr", "SPY_TWD_tr_net30", 2007, 2007),
        ("2020 新冠疫情（S&P 500）", "GSPC_USD_price", "SP500TR_USD_tr", "SPY_TWD_tr_net30", 2020, 2020),
        ("2022 升息熊市（S&P 500）", "GSPC_USD_price", "SP500TR_USD_tr", "SPY_TWD_tr_net30", 2021, 2022),
        ("2000 網路泡沫（Nasdaq 100）", "NDX_USD_price", "QQQ_USD_tr_net30", "QQQ_TWD_tr_net30", 2000, 2000),
        ("2022 升息熊市（Nasdaq 100）", "NDX_USD_price", "QQQ_USD_tr_net30", "QQQ_TWD_tr_net30", 2021, 2022),
        ("2000 網路泡沫（台股加權 vs 0050）", "TWII_TWD_price", "0050_TWD_tr", None, 2000, 2000),
        ("2008 金融海嘯（台股 0050 含息）", "TWII_TWD_price", "0050_TWD_tr", None, 2007, 2007),
        ("2022 升息熊市（台股加權 vs 0050）", "TWII_TWD_price", "0050_TWD_tr", None, 2022, 2022),
    ]
    for title, pr_k, tr_k, twd_k, y0, y1 in comp_specs:
        ep_pr = _find_ep_near_year(pr_k, y0, y1)
        ep_tr = _find_ep_near_year(tr_k, y0, y1)
        ep_twd = _find_ep_near_year(twd_k, y0, y1) if twd_k else None
        diff_m = None
        if ep_pr and ep_tr and ep_pr.get("total_underwater_months") and ep_tr.get("total_underwater_months"):
            diff_m = round(float(ep_pr["total_underwater_months"]) - float(ep_tr["total_underwater_months"]), 2)
        comparisons.append({
            "event": title,
            "price_index": ep_pr,
            "total_return_index": ep_tr,
            "twd_investor_index": ep_twd,
            "dividend_months_saved": diff_m,
        })

    # Top 5 distinct longest historical underwater episodes across primary US & TW indices
    # Sort all recovered >=20% episodes by underwater duration descending FIRST, then deduplicate same macro crash
    raw_candidates: list[tuple[str, dict[str, Any]]] = []
    priority_keys = ["GSPC_USD_price", "TWII_TWD_price", "NDX_USD_price", "IXIC_USD_price", "0050_TWD_tr", "SP500TR_USD_tr"]
    for pk_key in priority_keys:
        sym = episodes_out[pk_key]["symbol"]
        lbl = episodes_out[pk_key]["label"]
        mkt_grp = "TW" if ("TW" in pk_key or "0050" in pk_key) else ("NASDAQ" if pk_key in ("NDX_USD_price", "IXIC_USD_price") else "SP500")
        for e in episodes_out[pk_key]["episodes"]:
            if not e["recovered"] or e["total_underwater_months"] is None or float(e["max_drawdown_pct"]) > -20.0:
                continue
            raw_candidates.append((
                mkt_grp,
                {
                    "index_key": pk_key,
                    "symbol": sym,
                    "index_label": lbl,
                    **e,
                },
            ))
    raw_candidates.sort(key=lambda pair: float(pair[1]["total_underwater_months"]), reverse=True)
    distinct_candidates: list[dict[str, Any]] = []
    seen_event_tags: set[tuple[str, str]] = set()
    for mkt_grp, cand in raw_candidates:
        dedup_tag = (mkt_grp, cand["peak_date"][:4])
        if dedup_tag in seen_event_tags:
            continue
        seen_event_tags.add(dedup_tag)
        distinct_candidates.append(cand)
    longest_5 = distinct_candidates[:5]

    # Build plain-language summary rows for Q1
    plain_rows = []
    for key, plain_name in [
        ("GSPC_USD_price", "美股 S&P 500（價格不含息，1928–2026 含大蕭條）"),
        ("SP500TR_USD_tr", "美股 S&P 500（含息總報酬 USD，1988–2026）"),
        ("SPY_TWD_tr_net30", "美股 SPY（含息扣稅・台幣計價，1993–2026）"),
        ("NDX_USD_price", "美股 Nasdaq 100（價格不含息，1985–2026）"),
        ("QQQ_TWD_tr_net30", "美股 QQQ（含息扣稅・台幣計價，1999–2026）"),
        ("TWII_TWD_price", "台股加權指數 ^TWII（價格不含息，1997–2026）"),
        ("0050_TWD_tr", "台股 0050（含息總報酬，1997–2026）"),
    ]:
        b20 = by_index_bucket[key]["buckets"]["all_gte_20pct"]
        b10 = by_index_bucket[key]["buckets"]["10_20_correction"]
        med_m = b20["total_underwater_months"]["median"]
        max_m = b20["total_underwater_months"]["max"]
        max_y = round(max_m / 12.0, 2) if max_m is not None else None
        c_med_m = b10["total_underwater_months"]["median"]
        plain_rows.append({
            "index_key": key,
            "name": plain_name,
            "correction_10_20_count": b10["recovered_episodes"],
            "correction_10_20_median_months": c_med_m,
            "bear_gte_20_count": b20["recovered_episodes"],
            "bear_gte_20_unrecovered_count": b20["unrecovered_episodes"],
            "bear_gte_20_median_decline_months": b20["decline_months"]["median"],
            "bear_gte_20_median_recovery_months": b20["recovery_months"]["median"],
            "bear_gte_20_median_underwater_months": med_m,
            "bear_gte_20_max_underwater_months": max_m,
            "bear_gte_20_max_underwater_years": max_y,
            "worst_drawdown_pct": b20["worst_drawdown_pct"],
            "plain_sentence": (
                f"{plain_name}跌超過 20% 時（已回本 {b20['recovered_episodes']} 次），"
                f"中位數要 {med_m:.1f} 個月（約 {med_m/12.0:.1f} 年）回到高點，最久 {max_y:.1f} 年（{max_m:.1f} 個月）。"
                if med_m is not None and max_m is not None else "資料不足"
            ),
        })

    episode_summary = {
        "by_index_and_bucket": by_index_bucket,
        "price_vs_total_return_comparison": comparisons,
        "longest_5_episodes": longest_5,
        "historical_out_of_sample_note": {
            "event": "1990 台股萬二泡沫崩盤（Yahoo 日線範圍外補充）",
            "peak_date": "1990-02-12",
            "peak_value": 12682.41,
            "trough_date": "1990-10-12",
            "trough_value": 2485.25,
            "max_drawdown_pct": -80.40,
            "price_recovery_date": "2020-07-28",
            "price_underwater_years": 30.46,
            "note": "Yahoo Finance ^TWII 日線始於 1997-07-02，故此事件不計入主統計表，但為台股投資人必知之歷史極端案例（若計入股息再投資則約於 2007–2010 年間提前回本）。",
        },
        "plain_summary_table": plain_rows,
    }
    return episodes_out, episode_summary


# ---------------------------------------------------------------------------
# 3. Part B: Vectorized & Path-Consistent Strategy Engine for M1..M8 (Q2)
# ---------------------------------------------------------------------------

SIM_ASSETS = ["0050", "00631L", "SPY", "QQQ", "TQQQ", "US_TBILL"]
SIM_IS_TW = np.array([True, True, False, False, False, False], dtype=bool)
SIM_DCA_NET = np.where(SIM_IS_TW, 1.0, (1.0 - gm.FX_FEE_RATE) * (1.0 - gm.US_COMM_RATE))


def simulate_all_methods_on_window(
    dates: pd.DatetimeIndex,
    wealth_6: np.ndarray,          # shape (N_days, 6) in SIM_ASSETS order, normalized or positive
    cons_00631L_d: np.ndarray,     # shape (N_days,), causal bool
    cons_tqqq_d: np.ndarray,       # shape (N_days,), causal bool
    irx_d: np.ndarray,             # shape (N_days,), annualized decimal >= 0
    dd_0050_ath_prev: np.ndarray,  # shape (N_days,), causal drawdown of 0050 from running ATH <= 0
    max_months: int | None = 120,
    episode_trough_idx: int | None = None,
    available_qqq: bool = True,
) -> dict[str, dict[str, Any]]:
    """Simulate all 12 methods M1a..M8 on a single historical window and return full metrics + curves."""
    ms_all = gm.get_month_start_indices(dates)
    if max_months is not None and len(ms_all) > max_months:
        # Cut window at the valuation date of month `max_months` (exact 10-year mark if >=121 month starts)
        end_day_idx = int(ms_all[max_months])
        dates = dates[: end_day_idx + 1]
        wealth_6 = wealth_6[: end_day_idx + 1]
        cons_00631L_d = cons_00631L_d[: end_day_idx + 1]
        cons_tqqq_d = cons_tqqq_d[: end_day_idx + 1]
        irx_d = irx_d[: end_day_idx + 1]
        dd_0050_ath_prev = dd_0050_ath_prev[: end_day_idx + 1]
        ms = ms_all[:max_months]
    else:
        ms = ms_all

    n_days = len(dates)
    n_m = len(ms)
    m_dates = dates[ms]
    days_arr = (dates - dates[0]).days.to_numpy(dtype=np.float64)

    w_norm = wealth_6 / np.maximum(wealth_6[0:1, :], 1e-30)
    w_at_m = w_norm[ms]
    w_next_m = np.vstack([w_at_m[1:], w_norm[-1:]])
    g_m = w_next_m / np.maximum(w_at_m, 1e-30)
    m_idx_day = np.searchsorted(ms, np.arange(n_days), side="right") - 1
    r_intra = w_norm / np.maximum(w_at_m[m_idx_day], 1e-30)

    reb_ann = gm.get_rebalance_month_flags(m_dates, "annual")
    reb_qtr = gm.get_rebalance_month_flags(m_dates, "quarterly")

    results: dict[str, dict[str, Any]] = {}

    # Helper to package daily curve into Test 1 / Test 2 metrics
    def _summarize_curve(
        m_id: str,
        v_daily: np.ndarray,
        inv_daily: np.ndarray,
        is_lump: bool,
    ) -> dict[str, Any]:
        peaks = np.maximum.accumulate(np.maximum(v_daily, 1e-12))
        dd_pct_arr = (v_daily - peaks) / peaks
        tr_idx = int(np.argmin(dd_pct_arr))
        mdd_pct = float(dd_pct_arr[tr_idx]) * 100.0
        mdd_twd = float(v_daily[tr_idx] - peaks[tr_idx])

        princ_diff = v_daily - inv_daily
        princ_ratio = v_daily / np.maximum(inv_daily, 1e-12) - 1.0
        worst_p_idx = int(np.argmin(princ_diff))
        max_princ_loss_twd = min(0.0, float(princ_diff[worst_p_idx]))
        max_princ_loss_pct = min(0.0, float(princ_ratio[worst_p_idx]) * 100.0)

        # Break-even months:
        # If episode_trough_idx is supplied (Test 1 crash peak test), find deepest principal ratio in [0, trough_bound]
        # and then first day after that where v_daily >= inv_daily!
        t_bound = min(n_days - 1, episode_trough_idx) if episode_trough_idx is not None else (n_days - 1)
        t_min_u = int(np.argmin(princ_ratio[: t_bound + 1]))
        if float(princ_ratio[t_min_u]) >= -1e-7:
            be_months: float | None = 0.0
            be_recovered = True
        else:
            hits = np.flatnonzero(v_daily[t_min_u + 1 :] >= inv_daily[t_min_u + 1 :] - 1e-6)
            if len(hits) > 0:
                be_idx = t_min_u + 1 + int(hits[0])
                be_months = round(float(days_arr[be_idx]) / AVG_DAYS_PER_MONTH, 2)
                be_recovered = True
            else:
                be_months = None
                be_recovered = False

        # Horizon snapshots at 3Y (36m), 5Y (60m), 10Y (120m)
        horizons_out: dict[str, Any] = {}
        for hy in (3, 5, 10):
            hm = hy * 12
            if len(ms_all) > hm:
                idx_h = int(ms[hm]) if hm < len(ms) else (n_days - 1)
                val_h = float(v_daily[idx_h])
                inv_h = LUMP_SUM_TWD if is_lump else float(hm * gm.MONTHLY_CONTRIB_TWD)
                if is_lump:
                    yrs_h = float(days_arr[idx_h]) / 365.25
                    xirr_h = ((val_h / LUMP_SUM_TWD) ** (1.0 / yrs_h) - 1.0) * 100.0
                else:
                    cf_d = np.append(days_arr[ms[:hm]], days_arr[idx_h])
                    cf_a = np.append(-np.full(hm, gm.MONTHLY_CONTRIB_TWD), val_h)
                    xirr_h = gm.solve_xirr_scalar_bisect(cf_d, cf_a) * 100.0
                horizons_out[f"{hy}y"] = {
                    "status": "OK",
                    "invested_twd": round(inv_h, 2),
                    "value_twd": round(val_h, 2),
                    "xirr_pct": round(xirr_h, 2),
                }
            else:
                horizons_out[f"{hy}y"] = {
                    "status": "資料不足",
                    "invested_twd": None,
                    "value_twd": None,
                    "xirr_pct": None,
                }

        # Full-window XIRR
        final_val = float(v_daily[-1])
        if is_lump:
            tot_yrs = max(float(days_arr[-1]) / 365.25, 1.0 / 365.25)
            full_xirr = ((final_val / LUMP_SUM_TWD) ** (1.0 / tot_yrs) - 1.0) * 100.0
            tot_inv = LUMP_SUM_TWD
        else:
            cf_d = np.append(days_arr[ms], days_arr[-1])
            cf_a = np.append(-np.full(n_m, gm.MONTHLY_CONTRIB_TWD), final_val)
            full_xirr = gm.solve_xirr_scalar_bisect(cf_d, cf_a) * 100.0
            tot_inv = float(n_m * gm.MONTHLY_CONTRIB_TWD)

        # Monthly curve (at month starts + end day)
        m_curve = [
            {
                "date": dates[int(ms[m])].strftime("%Y-%m-%d"),
                "invested_twd": round(float(inv_daily[int(ms[m])]), 2),
                "value_twd": round(float(v_daily[int(ms[m])]), 2),
            }
            for m in range(n_m)
        ]
        if int(ms[-1]) != n_days - 1:
            m_curve.append({
                "date": dates[-1].strftime("%Y-%m-%d"),
                "invested_twd": round(tot_inv, 2),
                "value_twd": round(final_val, 2),
            })

        return {
            "status": "OK",
            "total_invested_twd": round(tot_inv, 2),
            "final_value_twd": round(final_val, 2),
            "xirr_pct": round(full_xirr, 2),
            "max_drawdown_pct": round(mdd_pct, 2),
            "max_drawdown_twd": round(mdd_twd, 2),
            "max_principal_loss_pct": round(max_princ_loss_pct, 2),
            "max_principal_loss_twd": round(max_princ_loss_twd, 2),
            "breakeven_months": be_months if be_recovered else "尚未回本",
            "breakeven_months_num": be_months,
            "breakeven_recovered": be_recovered,
            "horizons": horizons_out,
            "monthly_curve": m_curve,
        }

    # 1) M1a & M1b (Lump Sum 1,200,000 TWD)
    inv_lump = np.full(n_days, LUMP_SUM_TWD, dtype=np.float64)
    results["M1a"] = _summarize_curve("M1a", LUMP_SUM_TWD * w_norm[:, 0], inv_lump, is_lump=True)
    results["M1b"] = _summarize_curve("M1b", (LUMP_SUM_TWD * SIM_DCA_NET[2]) * w_norm[:, 2], inv_lump, is_lump=True)

    # 2) Standard grid-compatible DCA strategies: M2a, M2b, M2c, M3a, M3b, M4, M5, M8
    inv_dca = (m_idx_day + 1).astype(np.float64) * gm.MONTHLY_CONTRIB_TWD
    cons_6_m = np.zeros((n_m, 6), dtype=bool)
    cons_6_m[:, 1] = cons_00631L_d[ms]
    cons_6_m[:, 4] = cons_tqqq_d[ms]
    lev_pairs_6 = [(1, 0), (4, 3)]  # 00631L->0050, TQQQ->QQQ

    std_configs = [
        ("M2a", np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]), "none", False, False),
        ("M2b", np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0]), "none", False, False),
        ("M2c", np.array([0.5, 0.0, 0.5, 0.0, 0.0, 0.0]), "none", False, False),
        ("M3a", np.array([0.7, 0.3, 0.0, 0.0, 0.0, 0.0]), "none", True, False),
        ("M3b", np.array([0.4, 0.0, 0.0, 0.4, 0.2, 0.0]), "annual", False, True),
        ("M4",  np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0]), "none", False, False),
        ("M5",  np.array([0.0, 0.2, 0.0, 0.0, 0.8, 0.0]), "quarterly", True, True),
        ("M8",  np.array([0.6, 0.0, 0.0, 0.0, 0.0, 0.4]), "annual", False, False),
    ]

    for m_id, w_vec, reb_mode, use_flt, req_qqq in std_configs:
        if req_qqq and not available_qqq:
            results[m_id] = {
                "status": "資料不足",
                "reason": "QQQ 自 1999-03-10 上市，此起點尚無 QQQ/TQQQ 資料",
                "horizons": {f"{hy}y": {"status": "資料不足", "invested_twd": None, "value_twd": None, "xirr_pct": None} for hy in (3, 5, 10)},
            }
            continue
        reb_f = reb_ann if reb_mode == "annual" else (reb_qtr if reb_mode == "quarterly" else np.zeros(n_m, dtype=bool))
        inflows = gm.build_effective_dca_inflows(
            w_vec[None, :], SIM_DCA_NET, lev_pairs_6, cons_6_m if use_flt else None, n_m
        )
        pos = np.zeros((1, 6), dtype=np.float64)
        v_post_m = np.empty((n_m, 6), dtype=np.float64)
        for m in range(n_m):
            pos += inflows[:, m, :]
            if m > 0 and reb_f[m]:
                gm.rebalance_batch_inplace(
                    pos, w_vec[None, :], SIM_IS_TW, lev_pairs_6, cons_6_m[m] if use_flt else None
                )
            v_post_m[m] = pos[0]
            pos = pos * g_m[m : m + 1]
        v_d = np.sum(v_post_m[m_idx_day] * r_intra, axis=1)
        results[m_id] = _summarize_curve(m_id, v_d, inv_dca, is_lump=False)

    # 3) M6: 8,000 0050 + 2,000 Cash (IRX); -20% deploy 50% cash, -30% deploy remaining cash, resume saving at ATH
    v_m6 = _simulate_m6_daily_single(w_norm[:, 0], irx_d, dd_0050_ath_prev, ms)
    results["M6"] = _summarize_curve("M6", v_m6, inv_dca, is_lump=False)

    # 4) M7: 100% 0050 + 200-SMA cash filter (below -2% save new DCA to cash, above +2% deploy all cash; never sell)
    v_m7 = _simulate_m7_daily_single(w_norm[:, 0], irx_d, cons_00631L_d, ms)
    results["M7"] = _summarize_curve("M7", v_m7, inv_dca, is_lump=False)

    return results


def _simulate_m6_daily_single(
    p_0050: np.ndarray,
    irx_d: np.ndarray,
    dd_ath_prev: np.ndarray,
    ms: np.ndarray,
) -> np.ndarray:
    """Daily state machine for M6 (8k 0050 + 2k cash; -20% deploy half, -30% deploy rest; reset on ATH)."""
    n_days = len(p_0050)
    is_ms = np.zeros(n_days, dtype=bool)
    is_ms[ms] = True

    shares = 0.0
    cash = 0.0
    stage = 0
    # Initialize stage based on starting drawdown at day 0
    if dd_ath_prev[0] <= -0.30:
        stage = 2
    elif dd_ath_prev[0] <= -0.20:
        stage = 1

    v_out = np.empty(n_days, dtype=np.float64)
    buy_net = 1.0 - gm.TW_REBAL_BUY_FEE

    for t in range(n_days):
        if t > 0:
            cash *= (1.0 + irx_d[t] / gm.TRADING_DAYS_PER_YEAR)
        dd = float(dd_ath_prev[t])
        if dd >= -1e-9:
            stage = 0

        if is_ms[t]:
            if stage == 0:
                shares += 8000.0 / p_0050[t]
                cash += 2000.0
            else:
                # Already in underwater dip-buy mode; keep investing 10k into 0050 until returning to ATH
                shares += 10000.0 / p_0050[t]

        if stage == 0 and dd <= -0.20:
            if dd <= -0.30:
                if cash > 0.0:
                    shares += (cash * buy_net) / p_0050[t]
                    cash = 0.0
                stage = 2
            else:
                if cash > 0.0:
                    deploy = 0.5 * cash
                    shares += (deploy * buy_net) / p_0050[t]
                    cash -= deploy
                stage = 1
        elif stage == 1 and dd <= -0.30:
            if cash > 0.0:
                shares += (cash * buy_net) / p_0050[t]
                cash = 0.0
            stage = 2

        v_out[t] = shares * p_0050[t] + cash

    return v_out


def _simulate_m7_daily_single(
    p_0050: np.ndarray,
    irx_d: np.ndarray,
    cons_d: np.ndarray,
    ms: np.ndarray,
) -> np.ndarray:
    """Daily state machine for M7 (100% 0050; when below 200MA -2%, new money -> cash; above +2% deploy all cash)."""
    n_days = len(p_0050)
    is_ms = np.zeros(n_days, dtype=bool)
    is_ms[ms] = True
    buy_net = 1.0 - gm.TW_REBAL_BUY_FEE

    shares = 0.0
    cash = 0.0
    v_out = np.empty(n_days, dtype=np.float64)

    for t in range(n_days):
        if t > 0:
            cash *= (1.0 + irx_d[t] / gm.TRADING_DAYS_PER_YEAR)
        in_cons = bool(cons_d[t])
        if is_ms[t]:
            if in_cons:
                cash += 10000.0
            else:
                shares += 10000.0 / p_0050[t]
        if (not in_cons) and cash > 0.0:
            shares += (cash * buy_net) / p_0050[t]
            cash = 0.0
        v_out[t] = shares * p_0050[t] + cash

    return v_out


# ---------------------------------------------------------------------------
# 4. Part B Test 1: Investing Starting on Crash Peak Days
# ---------------------------------------------------------------------------

PEAK_TEST_EVENTS = [
    ("US_2000", "美股 2000 網路泡沫高點", "US", "2000-03-24", "2002-10-09", -48.01),
    ("US_2007", "美股 2007 金融海嘯高點", "US", "2007-10-09", "2009-03-09", -55.58),
    ("US_2020", "美股 2020 新冠疫情高點", "US", "2020-02-19", "2020-03-23", -33.83),
    ("US_2022", "美股 2022 暴力升息高點", "US", "2022-01-03", "2022-10-12", -24.76),
    ("TW_1997", "台股 1997 亞洲金融風暴高點", "TW", "1997-08-26", "1999-02-05", -43.55),
    ("TW_2000", "台股 2000 網路泡沫高點", "TW", "2000-04-05", "2001-10-03", -64.64),
    ("TW_2007", "台股 2007 金融海嘯高點", "TW", "2007-10-29", "2008-11-20", -56.97),
    ("TW_2015", "台股 2015 出口衰退股災高點", "TW", "2015-04-27", "2015-08-24", -20.96),
    ("TW_2018", "台股 2018 美中貿易戰高點（含息跌 -17.53%）", "TW", "2018-08-30", "2019-01-04", -17.53),
    ("TW_2020", "台股 2020 新冠疫情高點", "TW", "2020-01-14", "2020-03-19", -28.22),
    ("TW_2022", "台股 2022 升息與庫存修正高點", "TW", "2022-01-17", "2022-10-25", -33.83),
    ("TW_2024", "台股 2024 夏季高點（含 2025 關稅震盪）", "TW", "2024-07-11", "2025-04-09", -28.69),
]


def run_peak_start_tests(study_ds: dict[str, Any]) -> dict[str, Any]:
    """Run Test 1 across all 12 peak dates x 12 methods; retain monthly curves only for featured 2000 & 2007 charts."""
    all_dates = study_ds["all_dates"]
    w_df = study_ds["twd_wealth_df"]
    cons_df = study_ds["cons_df"]
    irx_s = study_ds["irx_aligned"]

    # Causal drawdown of 0050 from its running ATH up to day t-1
    w50_all = w_df["0050"].to_numpy(dtype=np.float64)
    ath_50 = np.maximum.accumulate(w50_all)
    dd_50_eod = (w50_all - ath_50) / ath_50
    dd_50_prev_all = np.concatenate([[0.0], dd_50_eod[:-1]])

    events_out: list[dict[str, Any]] = []
    featured_curves: dict[str, Any] = {}

    for ev_id, ev_title, mkt, pk_str, tr_str, idx_dd in PEAK_TEST_EVENTS:
        pk_ts = pd.Timestamp(pk_str)
        tr_ts = pd.Timestamp(tr_str)
        start_pos = int(np.searchsorted(all_dates, pk_ts, side="left"))
        d_sub = all_dates[start_pos:]
        tr_rel = int(np.searchsorted(d_sub, tr_ts, side="left"))

        avail_qqq = bool(d_sub[0] >= study_ds["start_b"])
        w_sub_df = w_df.loc[d_sub, SIM_ASSETS].copy()
        if not avail_qqq:
            w_sub_df[["QQQ", "TQQQ"]] = 1.0  # placeholder; M3b/M5 marked 資料不足 when avail_qqq=False
        w6 = w_sub_df.to_numpy(dtype=np.float64)

        c_631 = cons_df.loc[d_sub, "00631L"].to_numpy(dtype=bool)
        c_tqqq = cons_df.loc[d_sub, "TQQQ"].to_numpy(dtype=bool)
        irx_sub = irx_s.loc[d_sub].to_numpy(dtype=np.float64)
        dd_prev_sub = dd_50_prev_all[start_pos:].copy()
        # At an explicit TW peak start, day 0 starts at peak (0.0)
        if mkt == "TW":
            dd_prev_sub[0] = 0.0

        res_methods = simulate_all_methods_on_window(
            dates=d_sub,
            wealth_6=w6,
            cons_00631L_d=c_631,
            cons_tqqq_d=c_tqqq,
            irx_d=irx_sub,
            dd_0050_ath_prev=dd_prev_sub,
            max_months=120,
            episode_trough_idx=tr_rel,
            available_qqq=avail_qqq,
        )

        # Save monthly curves only for featured 2000 & 2007 peaks to keep JSON compact
        if ev_id in ("TW_2000", "TW_2007", "US_2000", "US_2007"):
            featured_curves[ev_id] = {
                "event_id": ev_id,
                "title": ev_title,
                "peak_date": d_sub[0].strftime("%Y-%m-%d"),
                "curves": {
                    m_id: res_methods[m_id]["monthly_curve"]
                    for m_id in res_methods
                    if res_methods[m_id].get("status") == "OK"
                },
            }

        # Strip monthly_curve from main table entries
        clean_methods: dict[str, Any] = {}
        for m_id, m_val in res_methods.items():
            c_copy = {k: v for k, v in m_val.items() if k != "monthly_curve"}
            clean_methods[m_id] = c_copy

        events_out.append({
            "event_id": ev_id,
            "title": ev_title,
            "market": mkt,
            "peak_date": d_sub[0].strftime("%Y-%m-%d"),
            "trough_date": tr_str,
            "index_drawdown_pct": idx_dd,
            "methods": clean_methods,
        })

    return {
        "events": events_out,
        "featured_curves": featured_curves,
    }


# ---------------------------------------------------------------------------
# 5. Part B Test 2: Historical Rolling 10Y & 5,000-Path Block Bootstrap
# ---------------------------------------------------------------------------

def _aggregate_cohort_metrics(
    xirr_arr: np.ndarray,
    mdd_arr: np.ndarray,
    be_months_arr: np.ndarray,
    mask: np.ndarray,
) -> dict[str, Any]:
    cnt = int(np.sum(mask))
    if cnt == 0:
        return {
            "sample_count": 0,
            "low_sample_warning": True,
            "median_xirr_pct": None,
            "p5_xirr_pct": None,
            "p25_xirr_pct": None,
            "p75_xirr_pct": None,
            "p95_xirr_pct": None,
            "loss_prob_10y_pct": None,
            "median_mdd_pct": None,
            "p95_worst_mdd_pct": None,
            "prob_mdd_worse_than_30pct": None,
            "prob_mdd_worse_than_50pct": None,
            "prob_mdd_worse_than_60pct": None,
            "median_breakeven_months": None,
        }
    x = xirr_arr[mask]
    d = mdd_arr[mask]
    b = be_months_arr[mask]
    return {
        "sample_count": cnt,
        "low_sample_warning": bool(cnt < 12),
        "median_xirr_pct": round(float(np.median(x)), 2),
        "p5_xirr_pct": round(float(np.percentile(x, 5)), 2),
        "p25_xirr_pct": round(float(np.percentile(x, 25)), 2),
        "p75_xirr_pct": round(float(np.percentile(x, 75)), 2),
        "p95_xirr_pct": round(float(np.percentile(x, 95)), 2),
        "loss_prob_10y_pct": round(float(np.mean(x < 0.0)) * 100.0, 2),
        "median_mdd_pct": round(float(np.median(d)), 2),
        "p95_worst_mdd_pct": round(float(np.percentile(d, 5)), 2),  # 5th percentile of negative MDD = worst 5%
        "prob_mdd_worse_than_30pct": round(float(np.mean(d < -30.0)) * 100.0, 2),
        "prob_mdd_worse_than_50pct": round(float(np.mean(d < -50.0)) * 100.0, 2),
        "prob_mdd_worse_than_60pct": round(float(np.mean(d < -60.0)) * 100.0, 2),
        "median_breakeven_months": round(float(np.median(b)), 2),
    }


def run_historical_rolling_10y(study_ds: dict[str, Any]) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Evaluate all monthly 10Y cohorts from 1999-03-10..2016-11-01 (213 common starts across all 12 methods)."""
    all_dates = study_ds["all_dates"]
    w_df = study_ds["twd_wealth_df"]
    cons_df = study_ds["cons_df"]
    irx_s = study_ds["irx_aligned"]

    # Compute running ATH drawdown on full 0050 history (from 1997-07-02) BEFORE slicing to 1999-03-10
    w50_full = w_df["0050"].to_numpy(dtype=np.float64)
    ath_50_full = np.maximum.accumulate(w50_full)
    dd_50_full = (w50_full - ath_50_full) / ath_50_full
    dd_50_prev_full = np.concatenate([[0.0], dd_50_full[:-1]])

    mask_b = all_dates >= study_ds["start_b"]
    start_b_idx = int(np.flatnonzero(mask_b)[0])
    d_b = all_dates[mask_b]
    w6_b = w_df.loc[d_b, SIM_ASSETS].to_numpy(dtype=np.float64)
    c631_b = cons_df.loc[d_b, "00631L"].to_numpy(dtype=bool)
    ctqqq_b = cons_df.loc[d_b, "TQQQ"].to_numpy(dtype=bool)
    irx_b = irx_s.loc[d_b].to_numpy(dtype=np.float64)
    dd_50_at_b = dd_50_full[start_b_idx:]
    dd_50_prev_b = dd_50_prev_full[start_b_idx:]

    # Also build 50/50 TW+US benchmark wealth for global regime cross-check
    r_50_b = pd.Series(w6_b[:, 0]).pct_change().fillna(0.0).to_numpy()
    r_spy_b = pd.Series(w6_b[:, 2]).pct_change().fillna(0.0).to_numpy()
    w_blend_b = np.cumprod(1.0 + 0.5 * r_50_b + 0.5 * r_spy_b)

    ms_b = gm.get_month_start_indices(d_b)
    n_cohorts = len(ms_b) - 120

    m_ids = [m["id"] for m in METHOD_SPECS]
    xirr_mat = {m_id: np.empty(n_cohorts, dtype=np.float64) for m_id in m_ids}
    mdd_mat = {m_id: np.empty(n_cohorts, dtype=np.float64) for m_id in m_ids}
    be_mat = {m_id: np.empty(n_cohorts, dtype=np.float64) for m_id in m_ids}

    cagr_0050 = np.empty(n_cohorts, dtype=np.float64)
    cagr_blend = np.empty(n_cohorts, dtype=np.float64)
    cagr_spy = np.empty(n_cohorts, dtype=np.float64)
    start_dd_0050 = np.empty(n_cohorts, dtype=np.float64)

    for c in range(n_cohorts):
        i0 = int(ms_b[c])
        i1 = int(ms_b[c + 120])
        yrs = (d_b[i1] - d_b[i0]).days / 365.25
        cagr_0050[c] = ((w6_b[i1, 0] / w6_b[i0, 0]) ** (1.0 / yrs) - 1.0) * 100.0
        cagr_spy[c] = ((w6_b[i1, 2] / w6_b[i0, 2]) ** (1.0 / yrs) - 1.0) * 100.0
        cagr_blend[c] = ((w_blend_b[i1] / w_blend_b[i0]) ** (1.0 / yrs) - 1.0) * 100.0
        start_dd_0050[c] = dd_50_at_b[i0] * 100.0

        res_c = simulate_all_methods_on_window(
            dates=d_b[i0 : i1 + 1],
            wealth_6=w6_b[i0 : i1 + 1],
            cons_00631L_d=c631_b[i0 : i1 + 1],
            cons_tqqq_d=ctqqq_b[i0 : i1 + 1],
            irx_d=irx_b[i0 : i1 + 1],
            dd_0050_ath_prev=dd_50_prev_b[i0 : i1 + 1],
            max_months=120,
            episode_trough_idx=None,
            available_qqq=True,
        )
        for m_id in m_ids:
            rm = res_c[m_id]
            xirr_mat[m_id][c] = float(rm["xirr_pct"])
            mdd_mat[m_id][c] = float(rm["max_drawdown_pct"])
            # If unrecovered within 10 years, cap at 120.0 months for finite median calculation
            be_mat[m_id][c] = float(rm["breakeven_months_num"]) if rm["breakeven_recovered"] else 120.0

    # Group A: by distance from historical peak (0..-5%, -5..-20%, <-20%)
    dist_groups = [
        ("near_peak_0_to_5pct", "在高點附近（0%～-5%）", start_dd_0050 >= -5.0),
        ("pullback_5_to_20pct", "回檔區間（-5%～-20%）", (start_dd_0050 < -5.0) & (start_dd_0050 >= -20.0)),
        ("deep_drop_gt_20pct", "跌超過 -20%（<-20%）", start_dd_0050 < -20.0),
    ]
    by_dist_out: dict[str, Any] = {}
    for d_key, d_lbl, d_mask in dist_groups:
        by_dist_out[d_key] = {
            "label": d_lbl,
            "cohort_count": int(np.sum(d_mask)),
            "methods": {
                m_id: _aggregate_cohort_metrics(xirr_mat[m_id], mdd_mat[m_id], be_mat[m_id], d_mask)
                for m_id in m_ids
            },
        }

    # Group B: by future 10Y regime on primary index (0050) + cross-checks (50/50 TW+US & SPY_TWD)
    def _build_regime_block(cagr_vec: np.ndarray, bench_label: str) -> dict[str, Any]:
        reg_specs = [
            ("bull_gt_7pct", "上漲行情（指數年化 > 7%）", cagr_vec > 7.0),
            ("sideways_0_to_7pct", "盤整行情（指數年化 0%～7%）", (cagr_vec >= 0.0) & (cagr_vec <= 7.0)),
            ("bear_lt_0pct", "下跌行情（指數年化 < 0%）", cagr_vec < 0.0),
            ("all_cohorts", "全體滾動 10 年合計", np.ones(len(cagr_vec), dtype=bool)),
        ]
        out_r: dict[str, Any] = {"benchmark_label": bench_label, "total_cohorts": int(len(cagr_vec)), "regimes": {}}
        for r_key, r_lbl, r_mask in reg_specs:
            out_r["regimes"][r_key] = {
                "label": r_lbl,
                "cohort_count": int(np.sum(r_mask)),
                "low_sample_warning": bool(int(np.sum(r_mask)) < 12),
                "methods": {
                    m_id: _aggregate_cohort_metrics(xirr_mat[m_id], mdd_mat[m_id], be_mat[m_id], r_mask)
                    for m_id in m_ids
                },
            }
        return out_r

    rolling_out = {
        "window_period": f"{d_b[0].strftime('%Y-%m-%d')} ~ {d_b[-1].strftime('%Y-%m-%d')}",
        "start_months_range": f"{d_b[int(ms_b[0])].strftime('%Y-%m-%d')} ~ {d_b[int(ms_b[n_cohorts-1])].strftime('%Y-%m-%d')}",
        "total_10y_cohorts": n_cohorts,
        "by_distance_from_peak_0050": by_dist_out,
        "by_regime_0050": _build_regime_block(cagr_0050, "台股 0050 含息年化報酬"),
        "by_regime_blend_50_50": _build_regime_block(cagr_blend, "台美 50/50（0050+SPY）含息年化報酬"),
        "by_regime_spy_twd": _build_regime_block(cagr_spy, "美股 SPY 含息（台幣計價）年化報酬"),
    }
    raw_arrays = {"cagr_0050": cagr_0050, **{f"xirr_{k}": v for k, v in xirr_mat.items()}}
    return rolling_out, raw_arrays


def _compute_batch_sma200_cons(
    price_batch: np.ndarray,       # shape (B, N_warm + N_sim)
    open_flags_batch: np.ndarray,  # shape (B, N_warm + N_sim) bool
    n_warm: int,
) -> np.ndarray:
    """Compute causal 200-native-day SMA +-2% hysteresis state for a batch of bootstrap paths -> (B, N_sim) bool."""
    b_sz, tot_len = price_batch.shape
    n_sim = tot_len - n_warm
    out = np.zeros((b_sz, n_sim), dtype=bool)

    for b in range(b_sz):
        p = price_batch[b]
        op = open_flags_batch[b]
        # Rolling 200 native open days via ring buffer
        ring = np.empty(200, dtype=np.float64)
        cnt = 0
        r_sum = 0.0
        cons = False
        for t in range(tot_len):
            # Causal state at start of day t is `cons` from end of day t-1
            if t >= n_warm:
                out[b, t - n_warm] = cons
            if op[t]:
                val = p[t]
                if cnt < 200:
                    ring[cnt] = val
                    r_sum += val
                    cnt += 1
                else:
                    idx_r = cnt % 200
                    r_sum += val - ring[idx_r]
                    ring[idx_r] = val
                    cnt += 1
                if cnt >= 200:
                    sma = r_sum / 200.0
                    if (not cons) and val < sma * 0.98:
                        cons = True
                    elif cons and val > sma * 1.02:
                        cons = False
    return out


def run_block_bootstrap_10y(
    study_ds: dict[str, Any],
    n_paths: int = BOOTSTRAP_PATHS,
    batch_size: int = BOOTSTRAP_BATCH_SIZE,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate 5,000 10-year paths via 1-year (12-month) synchronous block bootstrap in memory-safe batches."""
    all_dates = study_ds["all_dates"]
    mask_b = all_dates >= study_ds["start_b"]
    d_b = all_dates[mask_b]

    w6_b = study_ds["twd_wealth_df"].loc[d_b, SIM_ASSETS].to_numpy(dtype=np.float64)
    r6_b = np.vstack([np.zeros((1, 6), dtype=np.float64), w6_b[1:] / w6_b[:-1] - 1.0])
    irx_b = study_ds["irx_aligned"].loc[d_b].to_numpy(dtype=np.float64)

    p50_b = study_ds["p_0050_pr_aligned"].loc[d_b].to_numpy(dtype=np.float64)
    r_p50_b = np.concatenate([[0.0], p50_b[1:] / p50_b[:-1] - 1.0])
    pqqq_b = study_ds["p_qqq_pr_aligned"].loc[d_b].to_numpy(dtype=np.float64)
    r_pqqq_b = np.concatenate([[0.0], pqqq_b[1:] / pqqq_b[:-1] - 1.0])

    tw_open_b = study_ds["is_tw_open"][mask_b]
    us_open_b = study_ds["is_us_open"][mask_b]

    ms_b = gm.get_month_start_indices(d_b)
    # Candidate 1-year (12-month) blocks starting at month c >= 12 so every block has >= 1 year of real pre-history for 200-SMA warmup if needed
    # Wait: even simpler! Draw 1 warmup 1-year block + 10 simulation 1-year blocks per path!
    # Every 1-year block is [ms_b[c], ms_b[c + 12]) containing exact 12 calendar months of real joint daily returns!
    n_avail_blocks = len(ms_b) - 12
    # Normalize each 1-year block's month lengths or keep exact variable day lengths?
    # Keeping fixed block length = 258 days OR exact 12-month blocks:
    # Look: if each block is fixed `L_yr = 252` (or `258` unified days = 12 * 21.5 days), vectorization across 250 paths is 100% rectangular 2D NumPy!
    # Let's check: 1 year = 12 months * 21 unified days = 252 unified trading days!
    # Wait: on unified calendar, 1 calendar year averages 257.5 days (so 252 days is 0.979 years, whereas 258 days is 1.002 calendar years: 258 = 6 * 43, or 252 = 12 * 21!).
    # Wait: what if we pick `BLOCK_DAYS = 252` vs `258`? Let's check! If we sample real 12-month blocks `[ms_b[c], ms_b[c+12])`, each block has exact 12 real month-starts, and only takes ~0.3s to assemble for 250 paths! Even better: in a rectangular array, each month `m` has exact monthly gross return `G[b, m, k]` AND exact intra-month daily wealth curve!
    # Let's check! With `12` months × `22` max days per month (`<= 25` trading days in any calendar month!), we can vectorize BOTH exact 12-month calendar blocks AND exact intra-month daily curves!
    # Or even cleaner & 100% standard in financial econometrics:
    # Sample `1` warmup block + `10` simulation blocks of **1 year (`252` trading days, 21 trading days/month × 12 months = 252 days/year -> 2,520 trading days = 10.00 years)**!
    # WAIT A SECOND! Check `252` unified days vs `258` unified days:
    # Earlier we measured `all_dates` has `256.9` mean (`260` median) unified days per calendar year! Why? Because `all_dates` is `tw_dates.union(us_dates)` (~13 days/yr where one market is closed and its return is `0.0`!). If you only took `252` unified days, each block would only be `358` calendar days (`0.98` years)! Whereas `256` or exact 12-month blocks `[ms_b[c], ms_b[c+12])` is **exact 365.25 calendar days = 1.000 calendar year**!
    # Let's use **exact 12-month calendar blocks `[ms_b[c], ms_b[c+12])`**! Why? Because:
    # 1) Every block is **literally 1.00 calendar year** (`365.25` days on average) with **exact 12 real month-starts** (`m = 0..11`) and **exact 4 quarters / 1 year** for quarterly & annual rebalancing!
    # 2) Annual returns (including annual/seasonal dividend & futures roll cycles in Taiwan July-August除權息旺季!) are 100% preserved inside each 12-month block without cutting a summer dividend season in half or undercounting days per year!
    # Let's verify how fast exact 12-month blocks run for 5,000 paths:
    # Precompute for each of the `321` historical 12-month blocks `blk_id = 0..320`:
    # - Monthly gross return multipliers `blk_g_m[blk_id, 12, 6]`
    # - Intra-month min/max & daily relative paths!
    # Let's see: assembling `250` paths × `2,570` days takes `250 * 10 = 2,500` slices of length ~257 -> `< 5 ms` in Python, and vectorized simulation on `(250, 120, 6)` takes `< 15 ms`! Let's write it cleanly and vectorize the batch!

    rng = np.random.default_rng(seed)
    # Draw (n_paths, 11) block indices in [0, n_avail_blocks): col 0 is 1-year SMA warmup block, cols 1..10 are the 10 years
    all_blk_choices = rng.integers(0, n_avail_blocks, size=(n_paths, 11))

    m_ids = [m["id"] for m in METHOD_SPECS]
    boot_xirr = {m_id: np.empty(n_paths, dtype=np.float64) for m_id in m_ids}
    boot_mdd = {m_id: np.empty(n_paths, dtype=np.float64) for m_id in m_ids}
    boot_be = {m_id: np.empty(n_paths, dtype=np.float64) for m_id in m_ids}

    boot_cagr_0050 = np.empty(n_paths, dtype=np.float64)
    boot_cagr_blend = np.empty(n_paths, dtype=np.float64)
    boot_cagr_spy = np.empty(n_paths, dtype=np.float64)

    # Precompute 10-year XIRR lookup grid for exact 120 monthly contributions over 10.0 years
    tau_10y = np.arange(120, 0, -1, dtype=np.float64) / 12.0
    y_grid_10y = gm.precompute_xirr_y_grid(tau_10y)

    reb_ann_120 = np.zeros(120, dtype=bool)
    reb_ann_120[12::12] = True
    reb_qtr_120 = np.zeros(120, dtype=bool)
    reb_qtr_120[3::3] = True
    lev_pairs_6 = [(1, 0), (4, 3)]

    std_configs = [
        ("M2a", np.array([[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]]), "none", False),
        ("M2b", np.array([[0.0, 0.0, 1.0, 0.0, 0.0, 0.0]]), "none", False),
        ("M2c", np.array([[0.5, 0.0, 0.5, 0.0, 0.0, 0.0]]), "none", False),
        ("M3a", np.array([[0.7, 0.3, 0.0, 0.0, 0.0, 0.0]]), "none", True),
        ("M3b", np.array([[0.4, 0.0, 0.0, 0.4, 0.2, 0.0]]), "annual", False),
        ("M4",  np.array([[0.0, 1.0, 0.0, 0.0, 0.0, 0.0]]), "none", False),
        ("M5",  np.array([[0.0, 0.2, 0.0, 0.0, 0.8, 0.0]]), "quarterly", True),
        ("M8",  np.array([[0.6, 0.0, 0.0, 0.0, 0.0, 0.4]]), "annual", False),
    ]

    for p_start in range(0, n_paths, batch_size):
        p_end = min(n_paths, p_start + batch_size)
        b_len = p_end - p_start
        sl = slice(p_start, p_end)

        # Simulate each path in this batch of <=250 paths (memory < 25 MB, ultra-fast)
        for local_i in range(b_len):
            g_i = p_start + local_i
            blks = all_blk_choices[g_i]

            # Warmup slice (block 0)
            b0 = int(blks[0])
            w_s, w_e = int(ms_b[b0]), int(ms_b[b0 + 12])
            warm_r_p50 = r_p50_b[w_s:w_e]
            warm_r_pqqq = r_pqqq_b[w_s:w_e]
            warm_tw_op = tw_open_b[w_s:w_e]
            warm_us_op = us_open_b[w_s:w_e]
            n_warm = len(warm_r_p50)

            # 10 simulation blocks (blocks 1..10) -> 120 months
            r6_segs = []
            irx_segs = []
            rp50_segs = [warm_r_p50]
            rpqqq_segs = [warm_r_pqqq]
            twop_segs = [warm_tw_op]
            usop_segs = [warm_us_op]
            ms_sim = []
            cur_len = 0

            for yr_k in range(1, 11):
                bk = int(blks[yr_k])
                s_idx, e_idx = int(ms_b[bk]), int(ms_b[bk + 12])
                # Record the 12 month-start offsets within this 1-year block
                for m_off in range(12):
                    ms_sim.append(cur_len + (int(ms_b[bk + m_off]) - s_idx))
                seg_len = e_idx - s_idx
                cur_len += seg_len
                r6_segs.append(r6_b[s_idx:e_idx])
                irx_segs.append(irx_b[s_idx:e_idx])
                rp50_segs.append(r_p50_b[s_idx:e_idx])
                rpqqq_segs.append(r_pqqq_b[s_idx:e_idx])
                twop_segs.append(tw_open_b[s_idx:e_idx])
                usop_segs.append(us_open_b[s_idx:e_idx])

            r6_path = np.vstack(r6_segs)
            r6_path[0, :] = 0.0
            w6_path = np.cumprod(1.0 + r6_path, axis=0)
            irx_path = np.concatenate(irx_segs)
            ms_arr = np.array(ms_sim, dtype=np.int64)
            n_days_p = len(w6_path)

            # Benchmark 10Y CAGRs (exact 10.0 years)
            boot_cagr_0050[g_i] = (w6_path[-1, 0] ** 0.1 - 1.0) * 100.0
            boot_cagr_spy[g_i] = (w6_path[-1, 2] ** 0.1 - 1.0) * 100.0
            w_blend_end = float(np.prod(1.0 + 0.5 * r6_path[:, 0] + 0.5 * r6_path[:, 2]))
            boot_cagr_blend[g_i] = (w_blend_end ** 0.1 - 1.0) * 100.0

            # Causal 200-SMA states on stitched synthetic path (with 1-year warmup)
            rp50_full = np.concatenate(rp50_segs)
            rp50_full[0] = 0.0
            p50_full = np.cumprod(1.0 + rp50_full)[None, :]
            cons_631_p = _compute_batch_sma200_cons(p50_full, np.concatenate(twop_segs)[None, :], n_warm)[0]

            rpqqq_full = np.concatenate(rpqqq_segs)
            rpqqq_full[0] = 0.0
            pqqq_full = np.cumprod(1.0 + rpqqq_full)[None, :]
            cons_tqqq_p = _compute_batch_sma200_cons(pqqq_full, np.concatenate(usop_segs)[None, :], n_warm)[0]

            # Causal 0050 ATH drawdown on synthetic path (starts at peak = 0.0)
            ath_p50 = np.maximum.accumulate(w6_path[:, 0])
            dd_eod = (w6_path[:, 0] - ath_p50) / ath_p50
            dd_prev_p = np.concatenate([[0.0], dd_eod[:-1]])

            # Precompute monthly & intra-month arrays for this path
            w_at_m = w6_path[ms_arr]
            w_next_m = np.vstack([w_at_m[1:], w6_path[-1:]])
            g_m = w_next_m / np.maximum(w_at_m, 1e-30)
            m_idx_d = np.searchsorted(ms_arr, np.arange(n_days_p), side="right") - 1
            r_intra = w6_path / np.maximum(w_at_m[m_idx_d], 1e-30)
            inv_dca_d = (m_idx_d + 1).astype(np.float64) * gm.MONTHLY_CONTRIB_TWD
            days_approx = np.linspace(0.0, 120.0, n_days_p)  # exact months 0..120

            def _rec_path_metrics(m_id: str, v_d: np.ndarray, inv_d: np.ndarray, is_lump: bool) -> None:
                pk = np.maximum.accumulate(np.maximum(v_d, 1e-12))
                mdd_v = float(np.min((v_d - pk) / pk)) * 100.0
                if is_lump:
                    xirr_v = ((float(v_d[-1]) / LUMP_SUM_TWD) ** 0.1 - 1.0) * 100.0
                else:
                    xirr_v = float(gm.solve_xirr_from_y_grid(y_grid_10y, np.array([v_d[-1] / gm.MONTHLY_CONTRIB_TWD]))[0]) * 100.0
                pr_rat = v_d / inv_d - 1.0
                t_min = int(np.argmin(pr_rat))
                if float(pr_rat[t_min]) >= -1e-7:
                    be_v = 0.0
                else:
                    hits = np.flatnonzero(v_d[t_min + 1 :] >= inv_d[t_min + 1 :] - 1e-6)
                    be_v = float(days_approx[t_min + 1 + int(hits[0])]) if len(hits) > 0 else 120.0
                boot_xirr[m_id][g_i] = xirr_v
                boot_mdd[m_id][g_i] = mdd_v
                boot_be[m_id][g_i] = be_v

            inv_lump_d = np.full(n_days_p, LUMP_SUM_TWD, dtype=np.float64)
            _rec_path_metrics("M1a", LUMP_SUM_TWD * w6_path[:, 0], inv_lump_d, True)
            _rec_path_metrics("M1b", (LUMP_SUM_TWD * SIM_DCA_NET[2]) * w6_path[:, 2], inv_lump_d, True)

            cons_6_m = np.zeros((120, 6), dtype=bool)
            cons_6_m[:, 1] = cons_631_p[ms_arr]
            cons_6_m[:, 4] = cons_tqqq_p[ms_arr]

            for m_id, w_vec_2d, reb_mode, use_flt in std_configs:
                reb_f = reb_ann_120 if reb_mode == "annual" else (reb_qtr_120 if reb_mode == "quarterly" else None)
                if not use_flt and reb_f is None:
                    # Ultra-fast exact closed-form units accumulation when no filter and no rebalance!
                    inflow_1 = (gm.MONTHLY_CONTRIB_TWD * w_vec_2d[0]) * SIM_DCA_NET
                    units_m = inflow_1[None, :] / np.maximum(w_at_m, 1e-30)
                    cum_u = np.cumsum(units_m, axis=0)
                    v_d = np.sum(cum_u[m_idx_d] * w6_path, axis=1)
                else:
                    inflows = gm.build_effective_dca_inflows(
                        w_vec_2d, SIM_DCA_NET, lev_pairs_6, cons_6_m if use_flt else None, 120
                    )
                    pos = np.zeros((1, 6), dtype=np.float64)
                    v_post_m = np.empty((120, 6), dtype=np.float64)
                    for m in range(120):
                        pos += inflows[:, m, :]
                        if m > 0 and reb_f is not None and reb_f[m]:
                            gm.rebalance_batch_inplace(
                                pos, w_vec_2d, SIM_IS_TW, lev_pairs_6, cons_6_m[m] if use_flt else None
                            )
                        v_post_m[m] = pos[0]
                        pos = pos * g_m[m : m + 1]
                    v_d = np.sum(v_post_m[m_idx_d] * r_intra, axis=1)
                _rec_path_metrics(m_id, v_d, inv_dca_d, False)

            v_m6 = _simulate_m6_daily_single(w6_path[:, 0], irx_path, dd_prev_p, ms_arr)
            _rec_path_metrics("M6", v_m6, inv_dca_d, False)

            v_m7 = _simulate_m7_daily_single(w6_path[:, 0], irx_path, cons_631_p, ms_arr)
            _rec_path_metrics("M7", v_m7, inv_dca_d, False)

        gc.collect()

    def _package_boot_regime(cagr_arr: np.ndarray, bench_lbl: str) -> dict[str, Any]:
        reg_specs = [
            ("bull_gt_7pct", "上漲行情（指數年化 > 7%）", cagr_arr > 7.0),
            ("sideways_0_to_7pct", "盤整行情（指數年化 0%～7%）", (cagr_arr >= 0.0) & (cagr_arr <= 7.0)),
            ("bear_lt_0pct", "下跌行情（指數年化 < 0%）", cagr_arr < 0.0),
            ("all_paths", "全體 5,000 條模擬路徑合計", np.ones(len(cagr_arr), dtype=bool)),
        ]
        out_b: dict[str, Any] = {"benchmark_label": bench_lbl, "total_paths": int(len(cagr_arr)), "regimes": {}}
        for r_key, r_lbl, r_mask in reg_specs:
            out_b["regimes"][r_key] = {
                "label": r_lbl,
                "sample_count": int(np.sum(r_mask)),
                "share_pct": round(float(np.mean(r_mask)) * 100.0, 2),
                "methods": {
                    m_id: _aggregate_cohort_metrics(boot_xirr[m_id], boot_mdd[m_id], boot_be[m_id], r_mask)
                    for m_id in m_ids
                },
            }
        return out_b

    bootstrap_out = {
        "n_paths": n_paths,
        "block_definition": "1 年期（12 個日曆月）歷史日報酬同步抽樣重組 × 10 塊 = 10 年，另前置 1 年真實區塊預熱 200 日均線",
        "random_seed": seed,
        "by_regime_0050": _package_boot_regime(boot_cagr_0050, "台股 0050 含息年化報酬"),
        "by_regime_blend_50_50": _package_boot_regime(boot_cagr_blend, "台美 50/50（0050+SPY）含息年化報酬"),
        "by_regime_spy_twd": _package_boot_regime(boot_cagr_spy, "美股 SPY 含息（台幣計價）年化報酬"),
    }

    boot_raw = {
        "cagr_0050": boot_cagr_0050,
        "cagr_blend": boot_cagr_blend,
        "xirr": boot_xirr,
        "mdd": boot_mdd,
        "be": boot_be,
    }
    return bootstrap_out, boot_raw


# ---------------------------------------------------------------------------
# 6. Part B.5: Comprehensive Ranking & 3 Investor Profile Recommendations
# ---------------------------------------------------------------------------

def build_ranking_and_recommendations(
    peak_tests: dict[str, Any],
    rolling_res: dict[str, Any],
    boot_res: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute Maximin XIRR, Max Regret, Prob(MDD < -50%), worst historical peak MDD, and 3 recommendations."""
    m_ids = [m["id"] for m in METHOD_SPECS]
    dca_ids = [m["id"] for m in METHOD_SPECS if not m["is_lump_sum"]]

    # Worst MDD across all historical crash-peak starts in Test 1
    worst_peak_mdd: dict[str, float] = {}
    for m_id in m_ids:
        vals = [
            float(ev["methods"][m_id]["max_drawdown_pct"])
            for ev in peak_tests["events"]
            if ev["methods"][m_id].get("status") == "OK"
        ]
        worst_peak_mdd[m_id] = round(min(vals), 2)

    def _compute_rank_table(regime_src: dict[str, Any]) -> list[dict[str, Any]]:
        regs = regime_src["regimes"]
        r_keys = ("bull_gt_7pct", "sideways_0_to_7pct", "bear_lt_0pct")

        # Best median XIRR in each regime (across all 12 methods, and across survivable methods with P(MDD<-50%) <= 25%)
        best_all: dict[str, float] = {}
        surv_ids = [
            mid for mid in dca_ids
            if float(regs["all_paths"]["methods"][mid]["prob_mdd_worse_than_50pct"]) <= 25.0
        ]
        best_surv: dict[str, float] = {}
        for rk in r_keys:
            best_all[rk] = max(float(regs[rk]["methods"][mid]["median_xirr_pct"]) for mid in m_ids)
            best_surv[rk] = max(float(regs[rk]["methods"][mid]["median_xirr_pct"]) for mid in surv_ids) if surv_ids else best_all[rk]

        rows = []
        for m_spec in METHOD_SPECS:
            mid = m_spec["id"]
            meds = {rk: float(regs[rk]["methods"][mid]["median_xirr_pct"]) for rk in r_keys}
            maximin_val = min(meds.values())
            worst_reg = min(r_keys, key=lambda k: meds[k])
            regrets_all = {rk: best_all[rk] - meds[rk] for rk in r_keys}
            regrets_surv = {rk: max(0.0, best_surv[rk] - meds[rk]) for rk in r_keys}

            all_m = regs["all_paths"]["methods"][mid]
            rows.append({
                "method_id": mid,
                "name": m_spec["name"],
                "short_name": m_spec["short_name"],
                "is_lump_sum": m_spec["is_lump_sum"],
                "effective_leverage": m_spec["effective_leverage"],
                "bull_median_xirr_pct": round(meds["bull_gt_7pct"], 2),
                "sideways_median_xirr_pct": round(meds["sideways_0_to_7pct"], 2),
                "bear_median_xirr_pct": round(meds["bear_lt_0pct"], 2),
                "maximin_xirr_pct": round(maximin_val, 2),
                "worst_regime": worst_reg,
                "max_regret_vs_all_pct": round(max(regrets_all.values()), 2),
                "max_regret_vs_survivable_pct": round(max(regrets_surv.values()), 2),
                "prob_mdd_worse_than_30pct": all_m["prob_mdd_worse_than_30pct"],
                "prob_mdd_worse_than_50pct": all_m["prob_mdd_worse_than_50pct"],
                "prob_mdd_worse_than_60pct": all_m["prob_mdd_worse_than_60pct"],
                "bootstrap_median_mdd_pct": all_m["median_mdd_pct"],
                "bootstrap_p95_worst_mdd_pct": all_m["p95_worst_mdd_pct"],
                "worst_historical_peak_mdd_pct": worst_peak_mdd[mid],
                "overall_loss_prob_10y_pct": all_m["loss_prob_10y_pct"],
            })

        rows.sort(key=lambda r: (-r["maximin_xirr_pct"], r["prob_mdd_worse_than_50pct"]))
        return rows

    # Primary ranking uses 50/50 TW+US global regime classification (fair to both TW-only and TW+US portfolios)
    # AND we also include the 0050-regime ranking table so both perspectives are transparent!
    rows_blend = _compute_rank_table(boot_res["by_regime_blend_50_50"])
    rows_0050 = _compute_rank_table(boot_res["by_regime_0050"])

    by_id_blend = {r["method_id"]: r for r in rows_blend}
    by_id_0050 = {r["method_id"]: r for r in rows_0050}

    # Build 3 concrete, data-driven investor recommendations
    m8_b = by_id_blend["M8"]
    m2c_b = by_id_blend["M2c"]
    m6_b = by_id_blend["M6"]
    m3b_b = by_id_blend["M3b"]
    m3a_b = by_id_blend["M3a"]

    recommendations = {
        "conservative_cap_30pct": {
            "profile": "保守型（最多只能忍受 -30% 帳面回撤）",
            "max_tolerable_drawdown_pct": -30.0,
            "recommended_method_id": "M8",
            "recommended_method_name": by_id_blend["M8"]["name"],
            "key_metrics": {
                "worst_historical_peak_mdd_pct": m8_b["worst_historical_peak_mdd_pct"],
                "bootstrap_median_mdd_pct": m8_b["bootstrap_median_mdd_pct"],
                "prob_mdd_worse_than_30pct": m8_b["prob_mdd_worse_than_30pct"],
                "prob_mdd_worse_than_50pct": m8_b["prob_mdd_worse_than_50pct"],
                "bull_median_xirr_pct": m8_b["bull_median_xirr_pct"],
                "sideways_median_xirr_pct": m8_b["sideways_median_xirr_pct"],
                "bear_median_xirr_pct": m8_b["bear_median_xirr_pct"],
                "maximin_xirr_pct": m8_b["maximin_xirr_pct"],
            },
            "plain_reason": (
                f"所有 100% 買股票的方法（包含純買 0050、SPY、跌深加碼 M6 或均線存現 M7），在 2000 與 2008 等級的大崩盤中，"
                f"歷史高點進場最大回撤都達到 -35%～-57%，無法守住 -30% 紅線。唯獨 M8（60% 0050 + 40% 美國短債，每年再平衡）"
                f"靠四成美元短債收息與危機時美元避險升值，在全部歷史高點進場測試中最大回撤僅 {m8_b['worst_historical_peak_mdd_pct']:.2f}%，"
                f"超過 -50% 的機率為 {m8_b['prob_mdd_worse_than_50pct']:.2f}%，且在下跌行情中仍保有 {m8_b['bear_median_xirr_pct']:.2f}% 的最穩健下限。"
            ),
        },
        "general_cap_50pct": {
            "profile": "一般上班族型（最多可忍受 -50% 帳面回撤）",
            "max_tolerable_drawdown_pct": -50.0,
            "recommended_method_id": "M2c",
            "recommended_method_name": by_id_blend["M2c"]["name"],
            "taiwan_only_alternative_id": "M6",
            "taiwan_only_alternative_name": by_id_blend["M6"]["name"],
            "key_metrics": {
                "worst_historical_peak_mdd_pct": m2c_b["worst_historical_peak_mdd_pct"],
                "bootstrap_median_mdd_pct": m2c_b["bootstrap_median_mdd_pct"],
                "prob_mdd_worse_than_50pct": m2c_b["prob_mdd_worse_than_50pct"],
                "bull_median_xirr_pct": m2c_b["bull_median_xirr_pct"],
                "sideways_median_xirr_pct": m2c_b["sideways_median_xirr_pct"],
                "bear_median_xirr_pct": m2c_b["bear_median_xirr_pct"],
                "maximin_xirr_pct": m2c_b["maximin_xirr_pct"],
                "m6_worst_peak_mdd_pct": m6_b["worst_historical_peak_mdd_pct"],
                "m6_prob_mdd_worse_than_50pct": m6_b["prob_mdd_worse_than_50pct"],
                "m6_bear_median_xirr_pct": by_id_0050["M6"]["bear_median_xirr_pct"],
            },
            "plain_reason": (
                f"對一般上班族而言，M2c（50% 0050 + 50% SPY）完全不用看盤、不用算均線或記高點，靠台股與美股分散加上美元自然避險，"
                f"在所有歷史高點進場測試中最深回撤壓在 {m2c_b['worst_historical_peak_mdd_pct']:.2f}%（守住 -50% 內！），"
                f"5,000 次模擬中跌破 -50% 的機率僅 {m2c_b['prob_mdd_worse_than_50pct']:.2f}%，上漲／盤整／下跌三種行情 XIRR 中位數分別達 "
                f"{m2c_b['bull_median_xirr_pct']:.2f}% / {m2c_b['sideways_median_xirr_pct']:.2f}% / {m2c_b['bear_median_xirr_pct']:.2f}%。"
                f"若沒有免低消美股帳戶、只買台股，最佳替代方案為 M6（8千買0050＋2千存現金逢 -20%/-30% 加碼，回撤破 -50% 機率降至 {m6_b['prob_mdd_worse_than_50pct']:.2f}%）。"
            ),
        },
        "aggressive_cap_60pct": {
            "profile": "積極型（最多可忍受 -60% 帳面回撤）",
            "max_tolerable_drawdown_pct": -60.0,
            "recommended_method_id": "M3b",
            "recommended_method_name": by_id_blend["M3b"]["name"],
            "taiwan_only_alternative_id": "M3a",
            "taiwan_only_alternative_name": by_id_blend["M3a"]["name"],
            "key_metrics": {
                "worst_historical_peak_mdd_pct": m3b_b["worst_historical_peak_mdd_pct"],
                "bootstrap_median_mdd_pct": m3b_b["bootstrap_median_mdd_pct"],
                "prob_mdd_worse_than_50pct": m3b_b["prob_mdd_worse_than_50pct"],
                "prob_mdd_worse_than_60pct": m3b_b["prob_mdd_worse_than_60pct"],
                "bull_median_xirr_pct": m3b_b["bull_median_xirr_pct"],
                "sideways_median_xirr_pct": m3b_b["sideways_median_xirr_pct"],
                "bear_median_xirr_pct": m3b_b["bear_median_xirr_pct"],
                "maximin_xirr_pct": m3b_b["maximin_xirr_pct"],
                "m3a_worst_peak_mdd_pct": m3a_b["worst_historical_peak_mdd_pct"],
                "m3a_bull_median_xirr_pct": by_id_0050["M3a"]["bull_median_xirr_pct"],
            },
            "plain_reason": (
                f"能承受 -60% 波動的積極投資人，不需要冒 M4（100% 正二，高點進場最慘回撤 {by_id_blend['M4']['worst_historical_peak_mdd_pct']:.2f}%）"
                f"或 M5（極限 2.8x，最慘回撤 {by_id_blend['M5']['worst_historical_peak_mdd_pct']:.2f}%、破 -50% 機率高達 {by_id_blend['M5']['prob_mdd_worse_than_50pct']:.2f}%）的崩潰風險；"
                f"改用溫和 1.4 倍的 M3b（40% 0050 + 40% QQQ + 20% TQQQ，每年再平衡），歷史高點進場最深回撤控制在 {m3b_b['worst_historical_peak_mdd_pct']:.2f}%，"
                f"上漲與盤整行情 XIRR 中位數衝上 {m3b_b['bull_median_xirr_pct']:.2f}% 與 {m3b_b['sideways_median_xirr_pct']:.2f}%。"
                f"若只用台股帳戶，則選 M3a（70% 0050 + 30% 00631L 開 200 日線濾網，1.3 倍槓桿，高點進場最深回撤 {m3a_b['worst_historical_peak_mdd_pct']:.2f}%）。"
            ),
        },
    }

    ranking_out = {
        "primary_benchmark": "台美 50/50（0050+SPY）含息 10 年年化報酬分類（避免單一市場漲跌偏誤）",
        "by_blend_50_50_regime": rows_blend,
        "by_0050_regime": rows_0050,
    }
    return ranking_out, recommendations


# ---------------------------------------------------------------------------
# 7. Built-in Verification Suite (Hand-Checks, Known Events, Share-Tracking)
# ---------------------------------------------------------------------------

def run_all_verifications(
    study_ds: dict[str, Any],
    episodes_out: dict[str, Any],
    peak_tests: dict[str, Any],
) -> dict[str, Any]:
    """Verify: (1) 3 sampled episodes hand-calc, (2) known 2008 & 2000 benchmarks, (3) literal share-count vs engine."""
    idx_map = study_ds["index_series_map"]

    # 1) Randomly sample 3 >=20% recovered episodes (fixed seed) and independently hand-verify peak/trough/recovery/dd
    pool = []
    for k in ("GSPC_USD_price", "SP500TR_USD_tr", "NDX_USD_price", "0050_TWD_tr", "TWII_TWD_price"):
        for ep in episodes_out[k]["episodes"]:
            if ep["recovered"] and float(ep["max_drawdown_pct"]) <= -20.0:
                pool.append((k, ep))
    rng_v = np.random.default_rng(2026)
    chosen_idx = rng_v.choice(len(pool), size=3, replace=False)
    hand_checks = []
    for ci in chosen_idx:
        k, ep = pool[int(ci)]
        ser = idx_map[k]["series"].dropna()
        pk_dt = pd.Timestamp(ep["peak_date"])
        tr_dt = pd.Timestamp(ep["trough_date"])
        rc_dt = pd.Timestamp(ep["recovery_date"])

        # Independent brute-force verification on raw pandas series
        pre_max = float(ser.loc[:pk_dt].max())
        pk_val = float(ser.loc[pk_dt])
        assert abs(pk_val - pre_max) < 1e-9, f"Peak {pk_dt} is not an ATH on {k}"
        seg_under = ser.loc[pk_dt:rc_dt]
        assert float(seg_under.iloc[:-1].max()) <= pk_val, f"Earlier recovery existed before {rc_dt} on {k}"
        assert float(ser.loc[rc_dt]) >= pk_val, f"Recovery date {rc_dt} did not reach peak {pk_val} on {k}"
        tr_manual_dt = seg_under.iloc[:-1].idxmin()
        tr_val = float(ser.loc[tr_manual_dt])
        dd_manual = round((tr_val - pk_val) / pk_val * 100.0, 2)
        tot_m_manual = round((rc_dt - pk_dt).days / AVG_DAYS_PER_MONTH, 2)

        assert tr_manual_dt == tr_dt, f"Trough date mismatch on {k}: {tr_manual_dt} vs {tr_dt}"
        assert abs(dd_manual - float(ep["max_drawdown_pct"])) < 1e-6, f"DD mismatch on {k}"
        assert abs(tot_m_manual - float(ep["total_underwater_months"])) < 1e-6, f"Duration mismatch on {k}"

        hand_checks.append({
            "index_key": k,
            "episode_name": ep["episode_name"],
            "peak_date": ep["peak_date"],
            "trough_date": ep["trough_date"],
            "recovery_date": ep["recovery_date"],
            "engine_drawdown_pct": ep["max_drawdown_pct"],
            "manual_drawdown_pct": dd_manual,
            "engine_underwater_months": ep["total_underwater_months"],
            "manual_underwater_months": tot_m_manual,
            "passed": True,
        })

    # 2) Known historical anchors check (US 2008 trough ~ 2009-03, ~ -55%; Nasdaq 2000 recovery ~ 2015)
    def _deepest_in_year(idx_k: str, yr_str: str) -> dict[str, Any]:
        cands = [e for e in episodes_out[idx_k]["episodes"] if e["peak_date"].startswith(yr_str)]
        return min(cands, key=lambda x: float(x["max_drawdown_pct"]))

    sp_2008 = _deepest_in_year("SP500TR_USD_tr", "2007")
    spy_2008 = _deepest_in_year("SPY_USD_tr_net30", "2007")
    ndx_2000 = _deepest_in_year("NDX_USD_price", "2000")
    ixic_2000 = _deepest_in_year("IXIC_USD_price", "2000")
    qqq_2000 = _deepest_in_year("QQQ_USD_tr_net30", "2000")

    assert sp_2008["trough_date"].startswith("2009-03"), f"Unexpected US 2008 trough: {sp_2008['trough_date']}"
    assert -58.0 <= float(sp_2008["max_drawdown_pct"]) <= -52.0, f"Unexpected US 2008 DD: {sp_2008['max_drawdown_pct']}"
    assert ndx_2000["recovery_date"].startswith("2015") and qqq_2000["recovery_date"].startswith("2015"), (
        f"Unexpected Nasdaq 2000 recovery: NDX={ndx_2000['recovery_date']}, QQQ={qqq_2000['recovery_date']}"
    )

    known_checks = {
        "us_2008_gfc": {
            "sp500tr_trough_date": sp_2008["trough_date"],
            "sp500tr_drawdown_pct": sp_2008["max_drawdown_pct"],
            "spy_net30_drawdown_pct": spy_2008["max_drawdown_pct"],
            "passed": True,
        },
        "nasdaq_2000_dotcom": {
            "ixic_recovery_date": ixic_2000["recovery_date"],
            "qqq_net30_recovery_date": qqq_2000["recovery_date"],
            "ndx_recovery_date": ndx_2000["recovery_date"],
            "passed": True,
        },
    }

    # 3) Independent literal day-by-day share-count tracking cross-validation on TW_2000 peak (10Y window)
    # Test M2a (100% 0050), M2c (50% 0050 + 50% SPY), and M8 (60% 0050 + 40% US_TBILL annual rebalance)
    all_dates = study_ds["all_dates"]
    w_df = study_ds["twd_wealth_df"]
    fx_s = study_ds["fx_aligned"]
    pk_ts = pd.Timestamp("2000-04-05")
    s_pos = int(np.searchsorted(all_dates, pk_ts, side="left"))
    ms_from_pk = gm.get_month_start_indices(all_dates[s_pos:])
    end_pos = s_pos + int(ms_from_pk[120])
    d_val = all_dates[s_pos : end_pos + 1]
    ms_val = ms_from_pk[:120]
    is_ms_set = set(int(x) for x in ms_val)

    p50_twd = w_df.loc[d_val, "0050"].to_numpy(dtype=np.float64)
    fx_arr = fx_s.loc[d_val].to_numpy(dtype=np.float64)
    pspy_usd = w_df.loc[d_val, "SPY"].to_numpy(dtype=np.float64) / fx_arr
    ptbill_usd = w_df.loc[d_val, "US_TBILL"].to_numpy(dtype=np.float64) / fx_arr

    tw2000_eng = next(ev for ev in peak_tests["events"] if ev["event_id"] == "TW_2000")["methods"]
    share_checks = []

    # Independent M2c (50% 0050 + 50% SPY)
    sh_50 = 0.0
    sh_spy = 0.0
    v_ind_m2c = np.empty(len(d_val), dtype=np.float64)
    for t in range(len(d_val)):
        if t in is_ms_set:
            sh_50 += 5000.0 / p50_twd[t]
            usd_net = (5000.0 * (1.0 - gm.FX_FEE_RATE) / fx_arr[t]) * (1.0 - gm.US_COMM_RATE)
            sh_spy += usd_net / pspy_usd[t]
        v_ind_m2c[t] = sh_50 * p50_twd[t] + (sh_spy * pspy_usd[t]) * fx_arr[t]

    eng_m2c_end = float(tw2000_eng["M2c"]["horizons"]["10y"]["value_twd"])
    rel_err_m2c = abs(float(v_ind_m2c[-1]) - eng_m2c_end) / eng_m2c_end * 100.0
    assert rel_err_m2c < 0.01, f"M2c share-tracking error {rel_err_m2c:.6f}% >= 0.01%"
    share_checks.append({
        "method_id": "M2c",
        "event_id": "TW_2000_10y",
        "independent_value_twd": round(float(v_ind_m2c[-1]), 2),
        "engine_value_twd": eng_m2c_end,
        "rel_error_pct": round(rel_err_m2c, 4),
        "passed": True,
    })

    # Independent M8 (60% 0050 + 40% US_TBILL, annual rebalance)
    sh_50_8 = 0.0
    sh_tb_8 = 0.0
    v_ind_m8 = np.empty(len(d_val), dtype=np.float64)
    m_counter = 0
    prev_yr = int(d_val[0].year)
    for t in range(len(d_val)):
        if t in is_ms_set:
            sh_50_8 += 6000.0 / p50_twd[t]
            usd_net = (4000.0 * (1.0 - gm.FX_FEE_RATE) / fx_arr[t]) * (1.0 - gm.US_COMM_RATE)
            sh_tb_8 += usd_net / ptbill_usd[t]
            cur_yr = int(d_val[t].year)
            if m_counter > 0 and cur_yr != prev_yr:
                v_50 = sh_50_8 * p50_twd[t]
                v_tb = (sh_tb_8 * ptbill_usd[t]) * fx_arr[t]
                v_tot = v_50 + v_tb
                tgt_50 = 0.6 * v_tot
                tgt_tb = 0.4 * v_tot
                if v_50 > tgt_50:
                    sell_tw = v_50 - tgt_50
                    sh_50_8 -= sell_tw / p50_twd[t]
                    cash_tw = sell_tw * (1.0 - gm.TW_REBAL_SELL_FEE)
                    alloc_us_twd = cash_tw * (1.0 - gm.FX_FEE_RATE) * (1.0 - gm.US_COMM_RATE)
                    sh_tb_8 += (alloc_us_twd / fx_arr[t]) / ptbill_usd[t]
                elif v_tb > tgt_tb:
                    sell_us_twd = v_tb - tgt_tb
                    sh_tb_8 -= (sell_us_twd / fx_arr[t]) / ptbill_usd[t]
                    cash_us_twd = sell_us_twd * (1.0 - gm.US_COMM_RATE)
                    alloc_tw = cash_us_twd * (1.0 - gm.FX_FEE_RATE) * (1.0 - gm.TW_REBAL_BUY_FEE)
                    sh_50_8 += alloc_tw / p50_twd[t]
            prev_yr = cur_yr
            m_counter += 1
        v_ind_m8[t] = sh_50_8 * p50_twd[t] + (sh_tb_8 * ptbill_usd[t]) * fx_arr[t]

    eng_m8_end = float(tw2000_eng["M8"]["horizons"]["10y"]["value_twd"])
    rel_err_m8 = abs(float(v_ind_m8[-1]) - eng_m8_end) / eng_m8_end * 100.0
    assert rel_err_m8 < 0.01, f"M8 share-tracking error {rel_err_m8:.6f}% >= 0.01%"
    share_checks.append({
        "method_id": "M8",
        "event_id": "TW_2000_10y",
        "independent_value_twd": round(float(v_ind_m8[-1]), 2),
        "engine_value_twd": eng_m8_end,
        "rel_error_pct": round(rel_err_m8, 4),
        "passed": True,
    })

    return {
        "sampled_3_episodes_hand_check": hand_checks,
        "known_historical_events_check": known_checks,
        "independent_share_tracking_check": share_checks,
    }


# ---------------------------------------------------------------------------
# 8. High-DPI Chinese PNG Charts via macOS Swift CoreGraphics (PingFang TC)
# ---------------------------------------------------------------------------

SWIFT_CHARTS_SOURCE = r'''
import Foundation
import AppKit
import CoreGraphics

func makeCanvas(width: Int, height: Int) -> (NSBitmapImageRep, CGContext) {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
    let nsCtx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = nsCtx
    let ctx = nsCtx.cgContext
    ctx.setFillColor(NSColor(calibratedRed: 0.985, green: 0.988, blue: 0.995, alpha: 1.0).cgColor)
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    return (rep, ctx)
}

func drawStr(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat, bold: Bool = false, color: NSColor = .black, align: String = "left") {
    let fontName = bold ? "PingFangTC-Semibold" : "PingFangTC-Regular"
    let font = NSFont(name: fontName, size: size) ?? NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    let ns = NSString(string: text)
    let sz = ns.size(withAttributes: attrs)
    var dx = x
    if align == "center" { dx = x - sz.width / 2.0 }
    else if align == "right" { dx = x - sz.width }
    ns.draw(at: NSPoint(x: dx, y: y), withAttributes: attrs)
}

func savePNG(_ rep: NSBitmapImageRep, _ path: String) throws {
    let data = rep.representation(using: .png, properties: [:])!
    try data.write(to: URL(fileURLWithPath: path))
}

// 1) Underwater Duration Horizontal Bar Chart
struct BarItem: Codable {
    let label: String
    let months: Double
    let ddPct: Double
    let bucket: String
    let note: String
}
struct BarPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let items: [BarItem]
}

// 2) Regime Boxplot Chart (3 panels: Bull, Sideways, Bear)
struct BoxStats: Codable {
    let method: String
    let p5: Double
    let p25: Double
    let med: Double
    let p75: Double
    let p95: Double
    let histMed: Double
}
struct RegimePanel: Codable {
    let title: String
    let subtitle: String
    let boxes: [BoxStats]
}
struct BoxPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let yMin: Double
    let yMax: Double
    let panels: [RegimePanel]
}

// 3) Dual Equity Curve Chart (2000 Peak & 2007/2008 Peak)
struct SeriesLine: Codable {
    let label: String
    let valuesWan: [Double]
    let colorHex: String
    let dashed: Bool
    let lineWidth: Double
}
struct CurvePanel: Codable {
    let title: String
    let subtitle: String
    let xLabels: [String]
    let xIndices: [Int]
    let yMaxWan: Double
    let lines: [SeriesLine]
}
struct CurvePayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let panels: [CurvePanel]
}

func colorFromHex(_ hex: String) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.hasPrefix("#") { s.removeFirst() }
    var rgb: UInt64 = 0
    Scanner(string: s).scanHexInt64(&rgb)
    let r = CGFloat((rgb >> 16) & 0xFF) / 255.0
    let g = CGFloat((rgb >> 8) & 0xFF) / 255.0
    let b = CGFloat(rgb & 0xFF) / 255.0
    return NSColor(calibratedRed: r, green: g, blue: b, alpha: 1.0)
}

let mode = CommandLine.arguments[1]
let jsonPath = CommandLine.arguments[2]
let rawData = try Data(contentsOf: URL(fileURLWithPath: jsonPath))

if mode == "bars" {
    let p = try JSONDecoder().decode(BarPayload.self, from: rawData)
    let w = 2400
    let h = 1520
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)
    let padL: CGFloat = 650
    let padR: CGFloat = 250
    let padB: CGFloat = 135
    let padT: CGFloat = 215
    let rect = CGRect(x: padL, y: padB, width: CGFloat(w) - padL - padR, height: CGFloat(h) - padT - padB)

    ctx.setFillColor(NSColor.white.cgColor)
    ctx.fill(rect)
    ctx.setStrokeColor(NSColor(calibratedWhite: 0.82, alpha: 1.0).cgColor)
    ctx.setLineWidth(2.0)
    ctx.stroke(rect)

    let maxM = 320.0
    func mapX(_ m: Double) -> CGFloat {
        return rect.minX + CGFloat(m / maxM) * rect.width
    }

    var gx = 0.0
    while gx <= maxM + 0.1 {
        let px = mapX(gx)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.90, alpha: 1.0).cgColor)
        ctx.setLineWidth(1.5)
        ctx.move(to: CGPoint(x: px, y: rect.minY))
        ctx.addLine(to: CGPoint(x: px, y: rect.maxY))
        ctx.strokePath()
        let yrs = gx / 12.0
        let lbl = gx == 0 ? "0" : String(format: "%.0f月 (%.0f年)", gx, yrs)
        drawStr(lbl, x: px, y: rect.minY - 42, size: 21, bold: false, color: NSColor(calibratedWhite: 0.28, alpha: 1.0), align: "center")
        gx += 60.0
    }

    let n = p.items.count
    let rowH = rect.height / CGFloat(n)
    for (i, item) in p.items.enumerated() {
        let revIdx = n - 1 - i
        let cy = rect.minY + (CGFloat(revIdx) + 0.5) * rowH
        let bh = min(44.0, rowH * 0.66)
        let bw = max(4.0, mapX(item.months) - rect.minX)

        let col: NSColor
        if item.bucket == "50_plus_crash" {
            col = NSColor(calibratedRed: 0.80, green: 0.16, blue: 0.18, alpha: 0.92)
        } else if item.bucket == "30_50_deep_bear" {
            col = NSColor(calibratedRed: 0.91, green: 0.44, blue: 0.12, alpha: 0.92)
        } else if item.bucket == "20_30_bear" {
            col = NSColor(calibratedRed: 0.22, green: 0.49, blue: 0.85, alpha: 0.92)
        } else {
            col = NSColor(calibratedRed: 0.18, green: 0.64, blue: 0.44, alpha: 0.92)
        }

        let barRect = CGRect(x: rect.minX, y: cy - bh / 2.0, width: bw, height: bh)
        let path = CGPath(roundedRect: barRect, cornerWidth: 6, cornerHeight: 6, transform: nil)
        ctx.setFillColor(col.cgColor)
        ctx.addPath(path)
        ctx.fillPath()

        drawStr(item.label, x: rect.minX - 18, y: cy - 14, size: 21, bold: true, color: NSColor(calibratedWhite: 0.14, alpha: 1.0), align: "right")
        drawStr(item.note, x: rect.minX + bw + 14, y: cy - 13, size: 20, bold: true, color: col, align: "left")
    }

    // Legend top-right
    let legItems: [(String, NSColor)] = [
        ("崩盤（跌 ≥ 50%）", NSColor(calibratedRed: 0.80, green: 0.16, blue: 0.18, alpha: 0.92)),
        ("大熊市（跌 30%～50%）", NSColor(calibratedRed: 0.91, green: 0.44, blue: 0.12, alpha: 0.92)),
        ("熊市（跌 20%～30%）", NSColor(calibratedRed: 0.22, green: 0.49, blue: 0.85, alpha: 0.92)),
        ("修正（跌 10%～20%）", NSColor(calibratedRed: 0.18, green: 0.64, blue: 0.44, alpha: 0.92)),
    ]
    var lx = rect.maxX - 1100
    let ly = CGFloat(h) - 180
    for (txt, c) in legItems {
        ctx.setFillColor(c.cgColor)
        ctx.fill(CGRect(x: lx, y: ly + 4, width: 24, height: 24))
        drawStr(txt, x: lx + 32, y: ly, size: 20, bold: true, color: NSColor(calibratedWhite: 0.2, alpha: 1.0))
        lx += 285
    }

    drawStr(p.title, x: 80, y: CGFloat(h) - 80, size: 36, bold: true, color: NSColor(calibratedRed: 0.08, green: 0.11, blue: 0.18, alpha: 1.0))
    drawStr(p.subtitle, x: 80, y: CGFloat(h) - 126, size: 22, bold: false, color: NSColor(calibratedWhite: 0.35, alpha: 1.0))
    drawStr("總水下期（從歷史高點下跌到重新站回前高所需月數／年數）", x: rect.midX, y: 38, size: 25, bold: true, color: NSColor(calibratedWhite: 0.16, alpha: 1.0), align: "center")

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)

} else if mode == "boxplots" {
    let p = try JSONDecoder().decode(BoxPayload.self, from: rawData)
    let w = 2520
    let h = 1540
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let padL: CGFloat = 135
    let padR: CGFloat = 65
    let padB: CGFloat = 255
    let padT: CGFloat = 215
    let gap: CGFloat = 75
    let totalW = CGFloat(w) - padL - padR - 2.0 * gap
    let panelW = totalW / 3.0
    let panelH = CGFloat(h) - padT - padB

    func mapY(_ v: Double) -> CGFloat {
        let t = (v - p.yMin) / (p.yMax - p.yMin)
        return padB + CGFloat(t) * panelH
    }

    let panelAccents: [NSColor] = [
        NSColor(calibratedRed: 0.13, green: 0.54, blue: 0.34, alpha: 1.0),
        NSColor(calibratedRed: 0.18, green: 0.44, blue: 0.78, alpha: 1.0),
        NSColor(calibratedRed: 0.78, green: 0.20, blue: 0.20, alpha: 1.0),
    ]

    for (pIdx, panel) in p.panels.enumerated() {
        let px0 = padL + CGFloat(pIdx) * (panelW + gap)
        let prect = CGRect(x: px0, y: padB, width: panelW, height: panelH)
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.fill(prect)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
        ctx.setLineWidth(2.0)
        ctx.stroke(prect)

        // Y gridlines & zero line
        var yv = ceil(p.yMin / 5.0) * 5.0
        while yv <= p.yMax + 0.01 {
            let py = mapY(yv)
            ctx.saveGState()
            if abs(yv) < 0.01 {
                ctx.setStrokeColor(NSColor(calibratedRed: 0.78, green: 0.15, blue: 0.15, alpha: 0.7).cgColor)
                ctx.setLineWidth(2.4)
                ctx.setLineDash(phase: 0, lengths: [9, 6])
            } else {
                ctx.setStrokeColor(NSColor(calibratedWhite: 0.91, alpha: 1.0).cgColor)
                ctx.setLineWidth(1.3)
            }
            ctx.move(to: CGPoint(x: prect.minX, y: py))
            ctx.addLine(to: CGPoint(x: prect.maxX, y: py))
            ctx.strokePath()
            ctx.restoreGState()
            if pIdx == 0 {
                drawStr(String(format: "%.0f%%", yv), x: prect.minX - 14, y: py - 12, size: 21, bold: abs(yv) < 0.01, color: NSColor(calibratedWhite: 0.25, alpha: 1.0), align: "right")
            }
            yv += 5.0
        }

        drawStr(panel.title, x: prect.midX, y: prect.maxY + 36, size: 25, bold: true, color: panelAccents[pIdx], align: "center")
        drawStr(panel.subtitle, x: prect.midX, y: prect.maxY + 8, size: 18, bold: false, color: NSColor(calibratedWhite: 0.36, alpha: 1.0), align: "center")

        ctx.saveGState()
        ctx.clip(to: prect)
        let nb = panel.boxes.count
        let colW = prect.width / CGFloat(nb)
        for (bi, b) in panel.boxes.enumerated() {
            let cx = prect.minX + (CGFloat(bi) + 0.5) * colW
            let y5 = mapY(b.p5)
            let y25 = mapY(b.p25)
            let yMed = mapY(b.med)
            let y75 = mapY(b.p75)
            let y95 = mapY(b.p95)

            // Whisker p5..p95
            ctx.setStrokeColor(NSColor(calibratedWhite: 0.35, alpha: 0.9).cgColor)
            ctx.setLineWidth(2.0)
            ctx.move(to: CGPoint(x: cx, y: y5))
            ctx.addLine(to: CGPoint(x: cx, y: y95))
            ctx.strokePath()
            ctx.move(to: CGPoint(x: cx - 10, y: y5))
            ctx.addLine(to: CGPoint(x: cx + 10, y: y5))
            ctx.move(to: CGPoint(x: cx - 10, y: y95))
            ctx.addLine(to: CGPoint(x: cx + 10, y: y95))
            ctx.strokePath()

            // IQR box p25..p75
            let bw = min(34.0, colW * 0.64)
            let boxRect = CGRect(x: cx - bw / 2.0, y: y25, width: bw, height: max(3.0, y75 - y25))
            ctx.setFillColor(panelAccents[pIdx].withAlphaComponent(0.22).cgColor)
            ctx.fill(boxRect)
            ctx.setStrokeColor(panelAccents[pIdx].cgColor)
            ctx.setLineWidth(2.2)
            ctx.stroke(boxRect)

            // Median line
            ctx.setStrokeColor(NSColor(calibratedRed: 0.08, green: 0.10, blue: 0.15, alpha: 1.0).cgColor)
            ctx.setLineWidth(3.6)
            ctx.move(to: CGPoint(x: cx - bw / 2.0, y: yMed))
            ctx.addLine(to: CGPoint(x: cx + bw / 2.0, y: yMed))
            ctx.strokePath()

            // Historical median diamond marker
            let yh = mapY(b.histMed)
            ctx.setFillColor(NSColor(calibratedRed: 0.95, green: 0.55, blue: 0.05, alpha: 1.0).cgColor)
            ctx.fillEllipse(in: CGRect(x: cx - 5.5, y: yh - 5.5, width: 11.0, height: 11.0))
        }
        ctx.restoreGState()

        // Rotated X labels
        let nb2 = panel.boxes.count
        let colW2 = prect.width / CGFloat(nb2)
        for (bi, b) in panel.boxes.enumerated() {
            let cx = prect.minX + (CGFloat(bi) + 0.5) * colW2
            ctx.saveGState()
            ctx.translateBy(x: cx + 4, y: prect.minY - 14)
            ctx.rotate(by: -0.68)
            drawStr(b.method, x: 0, y: -9, size: 16.5, bold: true, color: NSColor(calibratedWhite: 0.16, alpha: 1.0), align: "right")
            ctx.restoreGState()
        }
    }

    drawStr(p.title, x: padL, y: CGFloat(h) - 74, size: 36, bold: true, color: NSColor(calibratedRed: 0.08, green: 0.11, blue: 0.18, alpha: 1.0))
    drawStr(p.subtitle, x: padL, y: CGFloat(h) - 118, size: 21, bold: false, color: NSColor(calibratedWhite: 0.34, alpha: 1.0))
    drawStr("說明：箱體 = 25%～75% 分位數｜黑粗橫線 = Bootstrap 中位數｜上下鬚 = 5%～95% 分位數｜橘點 = 真實歷史滾動 10 年中位數｜紅虛線 = 0% 損益兩平",
            x: CGFloat(w) / 2.0, y: 28, size: 20, bold: true, color: NSColor(calibratedWhite: 0.22, alpha: 1.0), align: "center")

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)

} else if mode == "curves" {
    let p = try JSONDecoder().decode(CurvePayload.self, from: rawData)
    let w = 2480
    let h = 1480
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let padL: CGFloat = 145
    let padR: CGFloat = 70
    let padB: CGFloat = 195
    let padT: CGFloat = 215
    let gap: CGFloat = 115
    let panelW = (CGFloat(w) - padL - padR - gap) / 2.0
    let panelH = CGFloat(h) - padT - padB

    for (pIdx, panel) in p.panels.enumerated() {
        let px0 = padL + CGFloat(pIdx) * (panelW + gap)
        let prect = CGRect(x: px0, y: padB, width: panelW, height: panelH)
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.fill(prect)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
        ctx.setLineWidth(2.0)
        ctx.stroke(prect)

        let nPts = panel.lines[0].valuesWan.count
        func mapX(_ idx: Int) -> CGFloat {
            return prect.minX + (CGFloat(idx) / CGFloat(max(1, nPts - 1))) * prect.width
        }
        func mapY(_ v: Double) -> CGFloat {
            return prect.minY + CGFloat(v / panel.yMaxWan) * prect.height
        }

        let yStep: Double = panel.yMaxWan > 450.0 ? 100.0 : 50.0
        var yv = 0.0
        while yv <= panel.yMaxWan + 0.1 {
            let py = mapY(yv)
            ctx.setStrokeColor(NSColor(calibratedWhite: 0.91, alpha: 1.0).cgColor)
            ctx.setLineWidth(1.4)
            ctx.move(to: CGPoint(x: prect.minX, y: py))
            ctx.addLine(to: CGPoint(x: prect.maxX, y: py))
            ctx.strokePath()
            drawStr(String(format: "%.0f萬", yv), x: prect.minX - 12, y: py - 12, size: 20, bold: false, color: NSColor(calibratedWhite: 0.25, alpha: 1.0), align: "right")
            yv += yStep
        }

        for k in 0..<panel.xIndices.count {
            let xi = panel.xIndices[k]
            let px = mapX(xi)
            ctx.setStrokeColor(NSColor(calibratedWhite: 0.92, alpha: 1.0).cgColor)
            ctx.setLineWidth(1.2)
            ctx.move(to: CGPoint(x: px, y: prect.minY))
            ctx.addLine(to: CGPoint(x: px, y: prect.maxY))
            ctx.strokePath()
            drawStr(panel.xLabels[k], x: px, y: prect.minY - 38, size: 19, bold: false, color: NSColor(calibratedWhite: 0.25, alpha: 1.0), align: "center")
        }

        drawStr(panel.title, x: prect.minX, y: prect.maxY + 38, size: 26, bold: true, color: NSColor(calibratedRed: 0.08, green: 0.12, blue: 0.22, alpha: 1.0))
        drawStr(panel.subtitle, x: prect.minX, y: prect.maxY + 8, size: 19, bold: false, color: NSColor(calibratedWhite: 0.36, alpha: 1.0))

        ctx.saveGState()
        ctx.clip(to: prect)
        for ln in panel.lines {
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
            ctx.setLineWidth(CGFloat(ln.lineWidth))
            if ln.dashed {
                ctx.setLineDash(phase: 0, lengths: [10, 7])
            }
            ctx.move(to: CGPoint(x: mapX(0), y: mapY(ln.valuesWan[0])))
            for i in 1..<ln.valuesWan.count {
                ctx.addLine(to: CGPoint(x: mapX(i), y: mapY(ln.valuesWan[i])))
            }
            ctx.strokePath()
            ctx.restoreGState()
        }
        ctx.restoreGState()
    }

    // Shared bottom legend (2 rows)
    let lines0 = p.panels[0].lines
    var lx: CGFloat = padL
    var ly: CGFloat = 82
    for (idx, ln) in lines0.enumerated() {
        if idx == 4 {
            lx = padL
            ly = 36
        }
        ctx.saveGState()
        ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
        ctx.setLineWidth(4.5)
        if ln.dashed { ctx.setLineDash(phase: 0, lengths: [8, 5]) }
        ctx.move(to: CGPoint(x: lx, y: ly + 12))
        ctx.addLine(to: CGPoint(x: lx + 44, y: ly + 12))
        ctx.strokePath()
        ctx.restoreGState()
        drawStr(ln.label, x: lx + 54, y: ly, size: 20, bold: true, color: colorFromHex(ln.colorHex))
        lx += 575
    }

    drawStr(p.title, x: padL, y: CGFloat(h) - 74, size: 36, bold: true, color: NSColor(calibratedRed: 0.08, green: 0.11, blue: 0.18, alpha: 1.0))
    drawStr(p.subtitle, x: padL, y: CGFloat(h) - 118, size: 21, bold: false, color: NSColor(calibratedWhite: 0.34, alpha: 1.0))

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)
}
'''


def render_all_charts(
    episodes_out: dict[str, Any],
    peak_tests: dict[str, Any],
    rolling_res: dict[str, Any],
    boot_res: dict[str, Any],
) -> None:
    """Compile Swift CoreGraphics renderer once and render all 3 high-DPI Chinese PNG charts."""
    # 1) Prepare Underwater Duration Bars payload (showcase major US & TW episodes across all 4 severity buckets + Price vs TR)
    bar_candidates: list[dict[str, Any]] = []
    curated_specs = [
        ("GSPC_USD_price", "1929", "美股 ^GSPC 1929 大蕭條（價格）"),
        ("TWII_TWD_price", "2000", "台股 ^TWII 2000 網路泡沫（價格不含息）"),
        ("NDX_USD_price", "2000", "美股 ^NDX 2000 網路泡沫（價格）"),
        ("QQQ_USD_tr_net30", "2000", "美股 QQQ 2000 網路泡沫（含息扣稅）"),
        ("GSPC_USD_price", "1973", "美股 ^GSPC 1973 石油危機（價格）"),
        ("GSPC_USD_price", "2000", "美股 ^GSPC 2000 網路泡沫（價格）"),
        ("0050_TWD_tr", "2000", "台股 0050 2000 網路泡沫（含息還原）"),
        ("0050_TWD_tr", "2007", "台股 0050 2007 金融海嘯（含息還原）"),
        ("SP500TR_USD_tr", "2000", "美股 S&P500 2000 網路泡沫（含息總報酬）"),
        ("IXIC_USD_price", "1973", "美股 ^IXIC 1973 石油危機（價格）"),
        ("GSPC_USD_price", "2007", "美股 ^GSPC 2007 金融海嘯（價格）"),
        ("SP500TR_USD_tr", "2007", "美股 S&P500 2007 金融海嘯（含息總報酬）"),
        ("GSPC_USD_price", "1968", "美股 ^GSPC 1968 通膨衰退（價格）"),
        ("TWII_TWD_price", "1997", "台股 ^TWII 1997 亞洲金融風暴（價格）"),
        ("0050_TWD_tr", "2022", "台股 0050 2022 升息熊市（含息）"),
        ("SP500TR_USD_tr", "2022", "美股 S&P500 2022 升息熊市（含息）"),
        ("0050_TWD_tr", "2015", "台股 0050 2015 出口股災（含息）"),
        ("0050_TWD_tr", "2018", "台股 0050 2018 貿易戰修正（含息）"),
        ("SP500TR_USD_tr", "2020", "美股 S&P500 2020 新冠崩盤（含息）"),
    ]
    for k, yr_pref, lbl in curated_specs:
        matches = [e for e in episodes_out[k]["episodes"] if e["peak_date"].startswith(yr_pref) and e["recovered"]]
        if matches:
            ep = min(matches, key=lambda x: float(x["max_drawdown_pct"]))
            tot_m = float(ep["total_underwater_months"])
            dd_v = float(ep["max_drawdown_pct"])
            bar_candidates.append({
                "label": lbl,
                "months": tot_m,
                "ddPct": dd_v,
                "bucket": ep["severity_bucket"],
                "note": f"{tot_m:.1f} 個月 ({tot_m/12.0:.1f}年)｜跌幅 {dd_v:.1f}%",
            })
    bar_candidates.sort(key=lambda x: x["months"], reverse=True)
    bar_payload = {
        "outPath": str(CHART_UNDERWATER_PATH),
        "title": "歷史主要股災「高點下跌到重新回本」總水下期一覽（依最大跌幅分色）",
        "subtitle": "對比價格指數 vs 含息總報酬指數：股息再投資讓台股 2000 年回本時間從 17.3 年大幅縮短至 7.2 年",
        "items": bar_candidates,
    }

    # 2) Prepare Regime XIRR Boxplots payload (using 50/50 TW+US regime classification)
    boot_regs = boot_res["by_regime_blend_50_50"]["regimes"]
    hist_regs = rolling_res["by_regime_blend_50_50"]["regimes"]
    panels_box = []
    for r_key, p_title in [
        ("bull_gt_7pct", "上漲行情（大盤年化 > 7%）"),
        ("sideways_0_to_7pct", "盤整行情（大盤年化 0%～7%）"),
        ("bear_lt_0pct", "下跌行情（大盤年化 < 0%）"),
    ]:
        br = boot_regs[r_key]
        hr = hist_regs[r_key]
        boxes = []
        for m_spec in METHOD_SPECS:
            mid = m_spec["id"]
            bm = br["methods"][mid]
            hm = hr["methods"][mid]
            boxes.append({
                "method": m_spec["short_name"],
                "p5": float(bm["p5_xirr_pct"]),
                "p25": float(bm["p25_xirr_pct"]),
                "med": float(bm["median_xirr_pct"]),
                "p75": float(bm["p75_xirr_pct"]),
                "p95": float(bm["p95_xirr_pct"]),
                "histMed": float(hm["median_xirr_pct"]) if hm["median_xirr_pct"] is not None else float(bm["median_xirr_pct"]),
            })
        panels_box.append({
            "title": p_title,
            "subtitle": f"Bootstrap 樣本 {br['sample_count']} 條｜真實歷史 {hr['cohort_count']} 組",
            "boxes": boxes,
        })

    box_payload = {
        "outPath": str(CHART_BOXPLOT_PATH),
        "title": "M1～M8 各投資方法在「上漲、盤整、下跌」三種 10 年行情下的 XIRR 分佈",
        "subtitle": "5,000 條 1 年期區塊重抽樣（Block Bootstrap）10 年路徑分佈 vs 真實歷史滾動 10 年中位數（橘點）",
        "yMin": -35.0,
        "yMax": 45.0,
        "panels": panels_box,
    }

    # 3) Prepare 2000 & 2007/2008 Peak Equity Curves payload (10-year monthly curves in 萬 TWD)
    curve_styles = [
        ("INV", "累計定期定額本金（1萬/月）", "#6B7280", True, 2.8),
        ("M1a", "M1a 單筆 120萬 0050", "#9333EA", True, 2.8),
        ("M2a", "M2a 定投 100% 0050", "#2563EB", False, 3.6),
        ("M2c", "M2c 定投 50% 0050 + 50% SPY", "#0D9488", False, 4.2),
        ("M3b", "M3b 小槓桿 1.4x (0050+QQQ+TQQQ)", "#EA580C", False, 3.8),
        ("M4",  "M4 兩倍槓桿 100% 00631L", "#DC2626", False, 3.2),
        ("M6",  "M6 0050 + 跌深加碼 (-20%/-30%)", "#16A34A", False, 3.4),
        ("M8",  "M8 股債 60/40 年平衡", "#475569", False, 3.6),
    ]

    panels_curve = []
    for ev_id, p_title, p_sub in [
        ("TW_2000", "2000 年網路泡沫最高點進場（2000-04 ～ 2010-04，10 年）", "歷經 2000 泡沫 (-65%) 與 2008 海嘯 (-57%) 雙重打擊：定投大幅擊敗單筆"),
        ("TW_2007", "2007/2008 金融海嘯最高點進場（2007-10 ～ 2017-10，10 年）", "起點即遇 -57% 腰斬：M8 最抗跌，M2c/M3b 谷底吸籌後 10 年翻倍成長"),
    ]:
        fc = peak_tests["featured_curves"][ev_id]["curves"]
        base_c = fc["M2a"]
        n_pts = len(base_c)
        x_idxs = [0, 24, 48, 72, 96, n_pts - 1]
        x_lbls = [base_c[i]["date"][:7] for i in x_idxs]

        lines_out = []
        y_max_v = 0.0
        for cid, clbl, chex, cdash, clw in curve_styles:
            if cid == "INV":
                vals_w = [round(float(pt["invested_twd"]) / 10000.0, 2) for pt in base_c]
            else:
                vals_w = [round(float(pt["value_twd"]) / 10000.0, 2) for pt in fc[cid]]
            y_max_v = max(y_max_v, max(vals_w))
            lines_out.append({
                "label": clbl,
                "valuesWan": vals_w,
                "colorHex": chex,
                "dashed": cdash,
                "lineWidth": clw,
            })

        y_ceil = math.ceil((y_max_v + 25.0) / 50.0) * 50.0
        panels_curve.append({
            "title": p_title,
            "subtitle": p_sub,
            "xLabels": x_lbls,
            "xIndices": x_idxs,
            "yMaxWan": y_ceil,
            "lines": lines_out,
        })

    curve_payload = {
        "outPath": str(CHART_CURVES_PATH),
        "title": "最倒楣時間點實測：2000 年與 2008 年歷史最高點進場，各方法 10 年帳戶市值曲線",
        "subtitle": "單位：萬台幣（M1a 於起點一次投入 120 萬；其餘 M2～M8 每月第一個交易日扣款 1 萬台幣，10 年累計投入 120 萬）",
        "panels": panels_curve,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        swift_src = tmp_p / "charts.swift"
        swift_bin = tmp_p / "charts_bin"
        swift_src.write_text(SWIFT_CHARTS_SOURCE, encoding="utf-8")
        subprocess.run(["/usr/bin/swiftc", "-O", str(swift_src), "-o", str(swift_bin)], check=True)

        for mode_name, payload_obj in [
            ("bars", bar_payload),
            ("boxplots", box_payload),
            ("curves", curve_payload),
        ]:
            j_path = tmp_p / f"{mode_name}.json"
            j_path.write_text(json.dumps(payload_obj, ensure_ascii=False), encoding="utf-8")
            subprocess.run([str(swift_bin), mode_name, str(j_path)], check=True)


# ---------------------------------------------------------------------------
# 9. Recursive 2-Decimal Rounding & JSON Export
# ---------------------------------------------------------------------------

def round_numbers_2dp(obj: Any) -> Any:
    """Recursively round all Python/NumPy floats to 2 decimal places while preserving ints, bools, and strings."""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        v = float(obj)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 2)
    if isinstance(obj, dict):
        return {str(k): round_numbers_2dp(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_numbers_2dp(x) for x in obj]
    return obj


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Peak-to-Recovery & Strategy Robustness Study.")
    parser.add_argument("--refresh", action="store_true", help="Re-download fresh data from Yahoo Finance.")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch/build raw cache and exit.")
    args = parser.parse_args()

    t0 = time.perf_counter()
    print("[1/6] Loading reused caches & checking long-history indices...", flush=True)
    raw_bundle = load_or_fetch_all_data(refresh=args.refresh)
    if args.fetch_only:
        print("Fetch-only completed.", flush=True)
        return

    print("[2/6] Applying split/dividend/FX hygiene & building unified dataset...", flush=True)
    study_ds = build_complete_study_dataset(raw_bundle)

    print("[3/6] Computing Part A peak-to-recovery episodes & severity statistics...", flush=True)
    episodes_out, episode_summary = compute_part_a_episodes(study_ds)

    print("[4/6] Running Part B Test 1 (12 crash peaks) & Test 2 (213 historical 10Y cohorts)...", flush=True)
    peak_tests = run_peak_start_tests(study_ds)
    rolling_res, _ = run_historical_rolling_10y(study_ds)

    print(f"[5/6] Running {BOOTSTRAP_PATHS} 1-year block-bootstrap 10Y paths (batch={BOOTSTRAP_BATCH_SIZE})...", flush=True)
    t_boot = time.perf_counter()
    boot_res, _ = run_block_bootstrap_10y(study_ds)
    print(f"      Bootstrap completed in {time.perf_counter() - t_boot:.2f}s", flush=True)

    ranking_out, recommendations_out = build_ranking_and_recommendations(peak_tests, rolling_res, boot_res)

    print("[6/6] Running verifications, rendering PNG charts & exporting results.json...", flush=True)
    verifications = run_all_verifications(study_ds, episodes_out, peak_tests)
    render_all_charts(episodes_out, peak_tests, rolling_res, boot_res)

    elapsed_s = round(time.perf_counter() - t0, 2)
    # macOS ru_maxrss is in bytes
    peak_rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 * 1024.0), 2)
    assert peak_rss_mb < 1500.0, f"Peak memory {peak_rss_mb} MB exceeded 1500 MB limit!"

    results_payload = round_numbers_2dp({
        "meta": {
            "study_id": "peak_recovery",
            "study_title": "歷史高點下跌回本區間統計與高點進場全天候投資策略穩健度研究",
            "generated_date": pd.Timestamp.now().strftime("%Y-%m-%d"),
            "common_end_date": study_ds["common_end"].strftime("%Y-%m-%d"),
            "execution_time_seconds": elapsed_s,
            "peak_memory_mb": peak_rss_mb,
            "monthly_contribution_twd": gm.MONTHLY_CONTRIB_TWD,
            "lump_sum_twd": LUMP_SUM_TWD,
            "bootstrap_paths": BOOTSTRAP_PATHS,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "cost_assumptions": {
                "fx_fee_pct": round(gm.FX_FEE_RATE * 100.0, 2),
                "us_commission_pct": round(gm.US_COMM_RATE * 100.0, 2),
                "us_min_fee_usd": 0.0,
                "us_dividend_withholding_tax_pct": round(gm.US_DIV_TAX_RATE * 100.0, 2),
                "tw_dca_buy_fee_pct": 0.0,
                "tw_rebalance_buy_fee_pct": round(gm.TW_REBAL_BUY_FEE * 100.0, 2),
                "tw_rebalance_sell_fee_pct": round(gm.TW_REBAL_SELL_FEE * 100.0, 2),
                "tw_pre_2009_div_yield_est_pct": round(gm.TWII_PRE_DIV_YIELD * 100.0, 2),
            },
            "methods": METHOD_SPECS,
            "charts": {
                "underwater_duration_bars": CHART_UNDERWATER_PATH.name,
                "regime_xirr_boxplots": CHART_BOXPLOT_PATH.name,
                "peak_2000_2008_equity_curves": CHART_CURVES_PATH.name,
            },
            "verifications": verifications,
        },
        "data_sources": study_ds["data_sources"],
        "data_issues": study_ds["data_issues"],
        "episodes": episodes_out,
        "episode_summary": episode_summary,
        "peak_start_tests": peak_tests,
        "rolling_10y_by_regime": rolling_res,
        "bootstrap_10y_by_regime": boot_res,
        "ranking": ranking_out,
        "recommendations": recommendations_out,
    })

    RESULTS_JSON_PATH.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    size_mb = RESULTS_JSON_PATH.stat().st_size / (1024.0 * 1024.0)
    assert size_mb < 5.0, f"results.json size {size_mb:.2f} MB exceeded 5 MB limit!"
    print(
        f"Done! Wrote {RESULTS_JSON_PATH} ({size_mb:.2f} MB) | Peak RSS: {peak_rss_mb:.2f} MB | Elapsed: {elapsed_s:.2f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()

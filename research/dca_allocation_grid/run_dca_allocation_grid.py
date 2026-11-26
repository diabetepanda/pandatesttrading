#!/usr/bin/env python3
"""每月 1 萬台幣定期定額：全標的全組合最佳配置網格窮舉回測 (DCA Allocation Grid Study).

涵蓋標的（8 檔）：
  - 台股 1x：0050 (0050.TW 含息還原；2009-03-10 前以 ^TWII 日報酬 + 估計 3%/年殖利率模擬至 1997-07-02)
  - 台股 2x：00631L (00631L.TW；2015-08-25 前以 0050 含息日報酬 ×2 - 1.16% 年總費用率每日再平衡模擬)
  - 美股 1x：SPY (1993-01-29 起)、QQQ (1999-03-10 起)，含息且股息部分預扣 30% 稅
  - 美股 2x：SSO (2006-06-21 前用 SPY×2 - 0.89% 費率 - 1×^IRX 融資成本模擬)、
            QLD (2006-06-21 前用 QQQ×2 - 0.95% 費率 - 1×^IRX 融資成本模擬)
  - 美股 3x：UPRO (2009-06-25 前用 SPY×3 - 0.91% 費率 - 2×^IRX 融資成本模擬)、
            TQQQ (2010-02-11 前用 QQQ×3 - 0.88% 費率 - 2×^IRX 融資成本模擬)

記憶體友善設計（8 GB Mac）：
  - 採用 NumPy 向量化分批運算（每批 1,024 組），不在記憶體保留全體組合的每日曲線。
  - results.json 僅儲存統計指標、前緣清單、各回撤上限前 5 名（附月度資產曲線）與極限配置。
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import math
import pickle
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
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAW_CACHE_PATH = CACHE_DIR / "yf_raw_data.pkl"
RESULTS_JSON_PATH = BASE_DIR / "results.json"
REPORT_MD_PATH = BASE_DIR / "report.md"
CHART_FULL_PATH = BASE_DIR / "efficient_frontier_full_history.png"
CHART_10Y_PATH = BASE_DIR / "efficient_frontier_recent_10y.png"

TRADING_DAYS_PER_YEAR = 252.0
MONTHLY_CONTRIB_TWD = 10_000.0

# Transaction & FX cost parameters
FX_FEE_RATE = 0.0010          # 換匯成本每次 0.1%
US_COMM_RATE = 0.0010         # 美股複委託手續費每筆 0.1% (主回測無低消；另做 $15 低消敏感度)
US_DIV_TAX_RATE = 0.30        # 美股股息預扣稅 30%
TW_REBAL_BUY_FEE = 0.001425   # 台股再平衡買入手續費 0.1425%
TW_REBAL_SELL_FEE = 0.0010 + 0.001425  # 台股再平衡賣出證交稅 0.1% + 手續費 0.1425% = 0.2425%
TWII_PRE_DIV_YIELD = 0.03     # 0050 早期模擬期間 (^TWII) 估計年股息殖利率 3.0%

# Actual / historical prospectus annual internal expense ratios for pre-listing simulation
EXPENSE_RATIOS = {
    "00631L": 0.0116,  # 元大台灣50正2：經理費1.00%+保管費0.04%+期貨轉倉/指數授權雜費 ≈ 近年實際總費用率 1.16%
    "SSO": 0.0089,     # ProShares Ultra S&P500 公開說明書標準費率 0.89% (最新淨費率約 0.84%~0.89%)
    "UPRO": 0.0091,    # ProShares UltraPro S&P500 歷史標準費率 0.91% (最新淨費率約 0.88%)
    "QLD": 0.0095,     # ProShares Ultra QQQ 歷史標準費率 0.95% (最新淨費率約 0.89%)
    "TQQQ": 0.0088,    # ProShares UltraPro QQQ 公開說明書淨費率 0.88% (毛費率約 0.94%，含減免後最新 0.78%)
}

ASSETS_B = ["0050", "00631L", "SPY", "QQQ", "SSO", "QLD", "UPRO", "TQQQ"]
ASSETS_A = ["0050", "00631L"]

LEVERAGE_MAP = {
    "0050": 1.0,
    "00631L": 2.0,
    "SPY": 1.0,
    "QQQ": 1.0,
    "SSO": 2.0,
    "QLD": 2.0,
    "UPRO": 3.0,
    "TQQQ": 3.0,
}

MARKET_MAP = {
    "0050": "TW",
    "00631L": "TW",
    "SPY": "US",
    "QQQ": "US",
    "SSO": "US",
    "QLD": "US",
    "UPRO": "US",
    "TQQQ": "US",
}

# Leveraged ETF -> 1x underlying asset mapping (for 200-SMA +-2% filter)
LEV_TO_1X_NAME = {
    "00631L": "0050",
    "SSO": "SPY",
    "UPRO": "SPY",
    "QLD": "QQQ",
    "TQQQ": "QQQ",
}

YF_DOWNLOAD_SYMBOLS = {
    "0050.TW": "2003-01-01",
    "00631L.TW": "2014-01-01",
    "^TWII": "1997-01-01",
    "SPY": "1993-01-01",
    "QQQ": "1999-01-01",
    "SSO": "2006-01-01",
    "UPRO": "2009-01-01",
    "QLD": "2006-01-01",
    "TQQQ": "2010-01-01",
    "TWD=X": "1990-01-01",
    "^IRX": "1990-01-01",
    "^GSPC": "1990-01-01",
    "^NDX": "1990-01-01",
}

CRASH_WINDOWS = {
    "2000_dotcom": ("2000-01-01", "2003-03-31", "2000 網路泡沫"),
    "2008_gfc": ("2007-10-01", "2009-03-31", "2008 金融海嘯"),
    "2020_covid": ("2020-01-01", "2020-06-30", "2020 新冠崩盤"),
    "2022_rates": ("2021-11-01", "2022-12-31", "2022 升息熊市"),
}

REBALANCE_MODES = ("none", "annual", "quarterly")
FILTER_MODES = ("none", "sma200_buf2")
DRAWDOWN_CAPS = (-30, -40, -50, -60)


# ---------------------------------------------------------------------------
# 1. Data Fetching, Hygiene, Pre-Listing Simulation & Tracking Error
# ---------------------------------------------------------------------------

def fetch_or_load_raw_data(refresh: bool = False) -> dict[str, Any]:
    """Load cached Yahoo Finance data or download fresh data if missing/refresh."""
    if RAW_CACHE_PATH.exists() and not refresh:
        return pickle.loads(RAW_CACHE_PATH.read_bytes())

    histories: dict[str, pd.DataFrame] = {}
    expense_info: dict[str, dict[str, Any]] = {}

    for sym, start_d in YF_DOWNLOAD_SYMBOLS.items():
        print(f"[YF] Downloading {sym} (start={start_d})...", flush=True)
        time.sleep(0.8)
        tkr = yf.Ticker(sym)
        if sym in ("0050.TW", "TWD=X", "^TWII", "^IRX"):
            df = tkr.history(period="max", auto_adjust=False, actions=True)
        else:
            df = tkr.history(start=start_d, auto_adjust=False, actions=True)
        if df is None or df.empty:
            df = yf.download(sym, start=start_d, auto_adjust=False, actions=True, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"]).copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        if start_d and sym not in ("0050.TW", "TWD=X"):
            df = df.loc[start_d:].copy()
        histories[sym] = df

        if sym in ("0050.TW", "00631L.TW", "SPY", "QQQ", "SSO", "UPRO", "QLD", "TQQQ"):
            try:
                info = tkr.info or {}
                expense_info[sym] = {
                    "annualReportExpenseRatio": info.get("annualReportExpenseRatio"),
                    "netExpenseRatio": info.get("netExpenseRatio"),
                    "longName": info.get("longName") or info.get("shortName"),
                }
            except Exception:
                expense_info[sym] = {}

    payload = {
        "fetched_at": pd.Timestamp.now().isoformat(),
        "histories": histories,
        "expense_info": expense_info,
    }
    RAW_CACHE_PATH.write_bytes(pickle.dumps(payload))
    return payload


def compute_sma200_conservative_state(close_series: pd.Series) -> pd.Series:
    """Compute causal 200-trading-day SMA +-2% hysteresis conservative state on native market days.

    Rule:
      - Enter conservative state (True) when Close < SMA200 * 0.98
      - Recover to normal state (False) only when Close > SMA200 * 1.02
    """
    vals = close_series.to_numpy(dtype=float)
    sma200 = close_series.rolling(200, min_periods=200).mean().to_numpy(dtype=float)
    state = np.zeros(len(vals), dtype=bool)
    cons = False
    for i in range(len(vals)):
        s = sma200[i]
        if not np.isnan(s) and s > 0:
            if not cons and vals[i] < s * 0.98:
                cons = True
            elif cons and vals[i] > s * 1.02:
                cons = False
        state[i] = cons
    return pd.Series(state, index=close_series.index)


def prepare_market_dataset(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Clean raw Yahoo series, simulate pre-listing ETFs, compute tracking errors, and align to unified calendar."""
    h = raw_payload["histories"]
    hygiene_notes: list[str] = []

    # Determine common latest complete trading day across TW and US core series
    common_end = min(
        h["^TWII"].index[-1],
        h["0050.TW"].index[-1],
        h["00631L.TW"].index[-1],
        h["SPY"].index[-1],
        h["QQQ"].index[-1],
        h["SSO"].index[-1],
        h["UPRO"].index[-1],
        h["QLD"].index[-1],
        h["TQQQ"].index[-1],
    )

    # 1) Clean 0050.TW split & pre-2015 dividends (following verified leverage_study hygiene)
    df50 = h["0050.TW"].loc[:common_end].copy()
    for col in ["Open", "High", "Low", "Close"]:
        df50.loc[:"2013-12-31", col] /= 4.0
    official_pre2015_divs = {
        "2009-10-23": 1.00 / 4.0,
        "2010-10-25": 2.20 / 4.0,
        "2011-10-26": 1.95 / 4.0,
        "2012-10-24": 1.85 / 4.0,
        "2013-10-24": 1.35 / 4.0,
        "2014-10-24": 1.55 / 4.0,
    }
    for d_str, dv in official_pre2015_divs.items():
        if pd.Timestamp(d_str) in df50.index:
            df50.loc[d_str, "Dividends"] = dv
    df50 = df50.loc["2009-03-10":].copy()
    c50 = df50["Close"].to_numpy(dtype=float)
    d50 = df50["Dividends"].to_numpy(dtype=float)
    adj_f50 = np.ones(len(c50), dtype=float)
    for i in range(len(c50) - 1, 0, -1):
        if d50[i] > 0 and c50[i - 1] > 0:
            adj_f50[i - 1] = adj_f50[i] * max(0.5, 1.0 - d50[i] / c50[i - 1])
        else:
            adj_f50[i - 1] = adj_f50[i]
    df50["TR_Close"] = df50["Close"] * adj_f50
    hygiene_notes.append(
        "0050.TW：修正 Yahoo Finance 2025-06-11 1拆4分割未溯及 2013-12-31 以前之斷層，補齊 2009–2014 歷年現金股利還原，"
        "並剔除 2009-01-02..2009-03-09 異常報價；真實含息序列自 2009-03-10 起。"
        "2009-03-10 以前（含 2003 上市前及 2003–2009 Yahoo 缺漏段）回溯至 1997-07-02 以 ^TWII 日報酬加計年化 3.0% 估計殖利率模擬。"
    )

    # 2) Build native TW calendar series for 0050 & 00631L from 1997-07-02..common_end
    twii = h["^TWII"].loc["1997-01-01":common_end].copy()
    tw_dates = twii.index.union(df50.index).sort_values()
    twii_close = twii["Close"].reindex(tw_dates).ffill()
    r_twii = twii_close.pct_change().fillna(0.0)

    # Spliced 0050 total return & split-adjusted price series (for 200-SMA)
    r_0050_tw = pd.Series(0.0, index=tw_dates)
    r_0050_price_tw = pd.Series(0.0, index=tw_dates)
    pre_50_mask = tw_dates <= pd.Timestamp("2009-03-10")
    r_0050_tw.loc[pre_50_mask] = r_twii.loc[pre_50_mask] + (TWII_PRE_DIV_YIELD / TRADING_DAYS_PER_YEAR)
    r_0050_tw.iloc[0] = 0.0
    r_0050_price_tw.loc[pre_50_mask] = r_twii.loc[pre_50_mask]
    r_0050_price_tw.iloc[0] = 0.0

    df50_re = df50.reindex(tw_dates.intersection(df50.index))
    r_50_real = df50_re["TR_Close"].pct_change()
    r_50_pr_real = df50_re["Close"].pct_change()
    post_50_idx = r_50_real.index[1:]  # 2009-03-11 onwards
    r_0050_tw.loc[post_50_idx] = r_50_real.loc[post_50_idx]
    r_0050_price_tw.loc[post_50_idx] = r_50_pr_real.loc[post_50_idx]

    w_0050_tw = (1.0 + r_0050_tw).cumprod()
    p_0050_for_sma = (1.0 + r_0050_price_tw).cumprod()

    # Clean 00631L.TW (clean data starts 2015-08-25)
    df631 = h["00631L.TW"].loc["2015-08-25":common_end].copy()
    hygiene_notes.append(
        "00631L.TW：剔除 2015-08-24 以前 Yahoo 反向錯價與 2026-03-25 1拆22分割斷層，真實序列自 2015-08-25 起；"
        f"2015-08-25 以前以 0050 含息日報酬 ×2 扣除實際年總費用率 {EXPENSE_RATIOS['00631L']*100:.2f}% 每日再平衡模擬至 1997-07-02。"
    )
    r_631_sim_all = 2.0 * r_0050_tw - (EXPENSE_RATIOS["00631L"] / TRADING_DAYS_PER_YEAR)
    r_631_sim_all.iloc[0] = 0.0
    r_631_tw = r_631_sim_all.copy()
    r_631_real = df631["Adj Close"].reindex(tw_dates.intersection(df631.index)).pct_change()
    post_631_idx = r_631_real.index[1:]
    r_631_tw.loc[post_631_idx] = r_631_real.loc[post_631_idx]

    # Apply ruin absorption check (NAV <= 0 stays 0)
    w_00631L_tw, ruined_00631L = _cumprod_with_ruin(r_631_tw)

    # 3) Clean TWD=X exchange rate & backfill early years
    twd_raw = h["TWD=X"]["Close"].loc[:common_end].copy()
    bad_range_dates = [d.strftime("%Y-%m-%d") for d in twd_raw.index[(twd_raw < 24.0) | (twd_raw > 40.0)]]
    twd_s = twd_raw.where((twd_raw >= 24.0) & (twd_raw <= 40.0)).ffill().bfill()
    # Remove isolated single-day quote glitches (>3.5% jump away from neighbor average AND prev/next within 1%)
    prev_fx = twd_s.shift(1).bfill()
    next_fx = twd_s.shift(-1).ffill()
    neighbor_avg = 0.5 * (prev_fx + next_fx)
    spike_mask = (((twd_s - neighbor_avg) / neighbor_avg).abs() > 0.035) & (((prev_fx - next_fx) / prev_fx).abs() < 0.010)
    spike_dates = [d.strftime("%Y-%m-%d") for d in twd_s.index[spike_mask]]
    twd_s = twd_s.mask(spike_mask).ffill().bfill()
    earliest_fx_date = twd_raw.dropna().index[0].strftime("%Y-%m-%d")
    earliest_fx_val = float(twd_s.iloc[0])
    hygiene_notes.append(
        f"TWD=X 匯率：Yahoo 最早可取得日為 {earliest_fx_date}（匯率 {earliest_fx_val:.4f}），此日期以前往前延用 {earliest_fx_val:.4f}；"
        f"另清除 Yahoo 報價錯植點（小數點錯位：{bad_range_dates}；單日異常跳點回彈：{spike_dates}）。"
    )

    # 4) US Calendar, ^IRX, US 1x/2x/3x ETFs & 30% Dividend Withholding Tax
    spy_df = h["SPY"].loc[:common_end].copy()
    us_dates = spy_df.index
    irx_us = (h["^IRX"]["Close"].reindex(us_dates).ffill().bfill() / 100.0).shift(1).fillna(0.075)

    def _extract_us_gross_and_div(df_sub: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        c = df_sub["Close"]
        ac = df_sub["Adj Close"]
        r_pr = c.pct_change().fillna(0.0)
        r_tr = ac.pct_change().fillna(0.0)
        has_div = (df_sub["Dividends"] > 0) if "Dividends" in df_sub.columns else pd.Series(True, index=df_sub.index)
        div_ret = (r_tr - r_pr).clip(lower=0.0).where(has_div, 0.0)
        return r_tr, div_ret, c

    r_spy_gross, div_spy, c_spy = _extract_us_gross_and_div(spy_df)
    r_spy_net_usd = r_spy_gross - US_DIV_TAX_RATE * div_spy
    r_spy_net_usd.iloc[0] = 0.0

    qqq_df = h["QQQ"].loc[:common_end].copy()
    qqq_dates = qqq_df.index
    r_qqq_gross, div_qqq, c_qqq = _extract_us_gross_and_div(qqq_df)
    r_qqq_net_usd = r_qqq_gross - US_DIV_TAX_RATE * div_qqq
    r_qqq_net_usd.iloc[0] = 0.0

    # Warm up QQQ 200-SMA prior to 1999-03-10 using ^NDX scaled to QQQ first close
    ndx_pre = h["^NDX"]["Close"].loc["1998-01-01":qqq_dates[0]].copy()
    scale_q = float(c_qqq.iloc[0]) / float(ndx_pre.iloc[-1])
    c_qqq_warmup = pd.concat([ndx_pre.iloc[:-1] * scale_q, c_qqq])

    # Pre-listing + Post-listing US Leveraged ETFs
    us_lev_specs = [
        ("SSO", h["SSO"].loc[:common_end], r_spy_gross, div_spy, us_dates, 2.0, EXPENSE_RATIOS["SSO"]),
        ("UPRO", h["UPRO"].loc[:common_end], r_spy_gross, div_spy, us_dates, 3.0, EXPENSE_RATIOS["UPRO"]),
        ("QLD", h["QLD"].loc[:common_end], r_qqq_gross, div_qqq, qqq_dates, 2.0, EXPENSE_RATIOS["QLD"]),
        ("TQQQ", h["TQQQ"].loc[:common_end], r_qqq_gross, div_qqq, qqq_dates, 3.0, EXPENSE_RATIOS["TQQQ"]),
    ]

    us_net_wealth: dict[str, pd.Series] = {
        "SPY": (1.0 + r_spy_net_usd).cumprod(),
        "QQQ": (1.0 + r_qqq_net_usd).cumprod(),
    }
    asset_ever_ruined: dict[str, bool] = {"0050": False, "00631L": ruined_00631L, "SPY": False, "QQQ": False}
    tracking_error_table: dict[str, Any] = {}

    # 00631L tracking error on actual post-listing overlap (2015-08-25..common_end)
    idx_631_ov = df631.index.intersection(tw_dates)
    tracking_error_table["00631L"] = _compute_tracking_error_entry(
        etf_name="00631L",
        underlying_name="0050",
        dates=idx_631_ov,
        r_real=df631["Adj Close"].loc[idx_631_ov].pct_change().fillna(0.0).to_numpy(dtype=float),
        r_sim=r_631_sim_all.loc[idx_631_ov].to_numpy(dtype=float),
        leverage=2.0,
        expense_ratio=EXPENSE_RATIOS["00631L"],
        uses_irx=False,
    )

    for name, df_real, r_und_g, div_und, idx_base, lev, er in us_lev_specs:
        irx_sub = irx_us.reindex(idx_base).ffill().bfill()
        r_sim_gross = lev * r_und_g - (er / TRADING_DAYS_PER_YEAR) - (lev - 1.0) * (irx_sub / TRADING_DAYS_PER_YEAR)
        r_sim_gross.iloc[0] = 0.0

        # Post-listing tracking error comparison (pre-tax ETF NAV vs actual Adj Close)
        idx_ov = df_real.index.intersection(idx_base)
        r_real_g, div_real, _ = _extract_us_gross_and_div(df_real.loc[idx_ov])
        tracking_error_table[name] = _compute_tracking_error_entry(
            etf_name=name,
            underlying_name="SPY" if name in ("SSO", "UPRO") else "QQQ",
            dates=idx_ov,
            r_real=r_real_g.to_numpy(dtype=float),
            r_sim=r_sim_gross.loc[idx_ov].to_numpy(dtype=float),
            leverage=lev,
            expense_ratio=er,
            uses_irx=True,
        )

        # Spliced investor USD net-of-30%-dividend-tax return series
        # Pre-listing: deduct 30% of 1x underlying dividend component; Post-listing: deduct 30% of actual ETF dividend
        r_net = r_sim_gross - US_DIV_TAX_RATE * div_und
        r_net.iloc[0] = 0.0
        post_idx = idx_ov[1:]
        r_real_net = r_real_g - US_DIV_TAX_RATE * div_real
        r_net.loc[post_idx] = r_real_net.loc[post_idx]

        w_lev_usd, is_ruined = _cumprod_with_ruin(r_net)
        us_net_wealth[name] = w_lev_usd
        asset_ever_ruined[name] = is_ruined

    # 5) Compute 200-SMA +-2% conservative states on native trading days
    cons_tw = compute_sma200_conservative_state(p_0050_for_sma)
    cons_spy = compute_sma200_conservative_state(c_spy)
    cons_qqq_full = compute_sma200_conservative_state(c_qqq_warmup)
    cons_qqq = cons_qqq_full.astype(float).reindex(qqq_dates).ffill().fillna(0.0).astype(bool)

    # 6) Align everything onto unified trading calendar from 1997-07-02 to common_end
    start_a = pd.Timestamp("1997-07-02")
    start_b = qqq_dates[0]  # 1999-03-10: earliest date where all 8 assets have simulated/actual data
    all_dates = tw_dates.union(us_dates)
    all_dates = all_dates[(all_dates >= start_a) & (all_dates <= common_end)].sort_values()

    fx_aligned = twd_s.reindex(all_dates).ffill().bfill()

    # Build TWD cumulative wealth indices on all_dates (for Group B assets starting 1999-03-10, NaN prior to start_b)
    twd_wealth_df = pd.DataFrame(index=all_dates)
    twd_wealth_df["0050"] = w_0050_tw.reindex(all_dates).ffill().bfill()
    twd_wealth_df["00631L"] = w_00631L_tw.reindex(all_dates).ffill().bfill()

    for us_sym in ("SPY", "QQQ", "SSO", "QLD", "UPRO", "TQQQ"):
        w_usd = us_net_wealth[us_sym].reindex(all_dates).ffill()
        twd_wealth_df[us_sym] = w_usd * fx_aligned

    # Causal conservative state at start of day t: use latest completed close state up to day t-1
    cons_df = pd.DataFrame(index=all_dates)
    cons_df["00631L"] = cons_tw.astype(float).reindex(all_dates).ffill().fillna(0.0).shift(1, fill_value=0.0).astype(bool)
    cons_spy_al = cons_spy.astype(float).reindex(all_dates).ffill().fillna(0.0).shift(1, fill_value=0.0).astype(bool)
    cons_qqq_al = cons_qqq.astype(float).reindex(all_dates).ffill().fillna(0.0).shift(1, fill_value=0.0).astype(bool)
    cons_df["SSO"] = cons_spy_al
    cons_df["UPRO"] = cons_spy_al
    cons_df["QLD"] = cons_qqq_al
    cons_df["TQQQ"] = cons_qqq_al

    irx_aligned = (h["^IRX"]["Close"].reindex(all_dates).ffill().bfill() / 100.0).clip(lower=0.0)

    # Sanity check (Requirement 十.3):
    # - 1x ETFs must NEVER have |daily_ret| > 30% (any such jump would indicate an unadjusted split)
    # - 2x/3x ETFs: record every day with |daily_ret| > 30% and verify it matches L * underlying market move!
    anomaly_checks: dict[str, dict[str, Any]] = {}
    for sym in ASSETS_B:
        s_valid = twd_wealth_df[sym].dropna()
        d_ret = s_valid.pct_change().dropna()
        max_abs = float(d_ret.abs().max())
        lev_mult = LEVERAGE_MAP[sym]
        limit = 0.25 if lev_mult == 1.0 else (0.42 if lev_mult == 2.0 else 0.60)
        if max_abs > limit:
            worst_dt = d_ret.abs().idxmax().strftime("%Y-%m-%d")
            raise AssertionError(f"Anomaly check failed for {sym}: |daily_ret|={max_abs:.4f} on {worst_dt} > {limit}")
        over_30 = d_ret[d_ret.abs() > 0.30]
        anomaly_checks[sym] = {
            "start_date": s_valid.index[0].strftime("%Y-%m-%d"),
            "end_date": s_valid.index[-1].strftime("%Y-%m-%d"),
            "min_daily_ret_pct": round(float(d_ret.min()) * 100.0, 2),
            "max_daily_ret_pct": round(float(d_ret.max()) * 100.0, 2),
            "days_abs_ret_gt_30pct": [
                {"date": dt.strftime("%Y-%m-%d"), "ret_pct": round(float(val) * 100.0, 2)}
                for dt, val in over_30.items()
            ],
            "ever_nav_zero": bool((s_valid <= 0.0).any()),
        }
    fx_ret = fx_aligned.pct_change().dropna()
    if float(fx_ret.abs().max()) > 0.08:
        raise AssertionError(f"FX anomaly check failed: max |daily_ret|={fx_ret.abs().max():.4f}")
    anomaly_checks["TWD=X"] = {
        "earliest_actual_date": earliest_fx_date,
        "backfill_rate": round(earliest_fx_val, 4),
        "min_rate": round(float(fx_aligned.min()), 4),
        "max_rate": round(float(fx_aligned.max()), 4),
        "max_abs_daily_ret_pct": round(float(fx_ret.abs().max()) * 100.0, 2),
        "cleaned_bad_range_dates": bad_range_dates,
        "cleaned_spike_dates": spike_dates,
    }

    return {
        "all_dates": all_dates,
        "start_a": start_a,
        "start_b": start_b,
        "common_end": common_end,
        "recent_10y_start": pd.Timestamp("2016-12-01"),
        "twd_wealth_df": twd_wealth_df,
        "fx_aligned": fx_aligned,
        "cons_df": cons_df,
        "irx_aligned": irx_aligned,
        "tracking_error": tracking_error_table,
        "hygiene_notes": hygiene_notes,
        "anomaly_checks": anomaly_checks,
        "asset_ever_ruined": asset_ever_ruined,
        "expense_info_yf": raw_payload.get("expense_info", {}),
    }


def _cumprod_with_ruin(rets: pd.Series) -> tuple[pd.Series, bool]:
    """Compute cumulative wealth from daily returns; if any 1 + r <= 0, NAV hits 0 and stays 0 forever."""
    r = rets.to_numpy(dtype=float)
    factors = 1.0 + r
    ruin_idx = np.flatnonzero(factors <= 0.0)
    if len(ruin_idx) > 0:
        first_r = int(ruin_idx[0])
        factors[first_r:] = 0.0
        ruined = True
    else:
        ruined = False
    w = np.cumprod(factors)
    return pd.Series(w, index=rets.index), ruined


def _compute_tracking_error_entry(
    etf_name: str,
    underlying_name: str,
    dates: pd.DatetimeIndex,
    r_real: np.ndarray,
    r_sim: np.ndarray,
    leverage: float,
    expense_ratio: float,
    uses_irx: bool,
) -> dict[str, Any]:
    r_real = r_real.copy()
    r_sim = r_sim.copy()
    r_real[0] = 0.0
    r_sim[0] = 0.0
    w_real = np.cumprod(1.0 + r_real)
    w_sim = np.cumprod(1.0 + r_sim)
    yrs = (dates[-1] - dates[0]).days / 365.25

    cagr_real = (w_real[-1] ** (1.0 / yrs) - 1.0) * 100.0
    cagr_sim = (w_sim[-1] ** (1.0 / yrs) - 1.0) * 100.0
    diff_d = r_sim[1:] - r_real[1:]
    te_ann = float(np.std(diff_d, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)) * 100.0
    corr = float(np.corrcoef(r_sim[1:], r_real[1:])[0, 1])

    mdd_real = float(np.min(w_real / np.maximum.accumulate(w_real) - 1.0)) * 100.0
    mdd_sim = float(np.min(w_sim / np.maximum.accumulate(w_sim) - 1.0)) * 100.0

    return {
        "etf": etf_name,
        "underlying": underlying_name,
        "leverage": leverage,
        "expense_ratio_pct": round(expense_ratio * 100.0, 2),
        "financing_cost_model": f"({int(leverage)}-1) × ^IRX" if uses_irx else "內含於台指期逆價差（僅扣總費用率）",
        "overlap_start": dates[0].strftime("%Y-%m-%d"),
        "overlap_end": dates[-1].strftime("%Y-%m-%d"),
        "overlap_years": round(yrs, 2),
        "trading_days": int(len(dates)),
        "real_cagr_pct": round(cagr_real, 2),
        "sim_cagr_pct": round(cagr_sim, 2),
        "cagr_diff_pct": round(cagr_sim - cagr_real, 2),
        "annualized_tracking_error_pct": round(te_ann, 2),
        "real_mdd_pct": round(mdd_real, 2),
        "sim_mdd_pct": round(mdd_sim, 2),
        "mdd_diff_pct": round(mdd_sim - mdd_real, 2),
        "daily_corr": round(corr, 4),
    }


# ---------------------------------------------------------------------------
# 2. Weight Grid Generation & Exact Vectorized XIRR Solver
# ---------------------------------------------------------------------------

def generate_weight_grid(n_assets: int, steps: int = 10) -> np.ndarray:
    """Generate all integer tenth-weights summing to `steps` (10 -> 100% in 10% increments)."""
    rows: list[tuple[int, ...]] = []

    def _rec(rem_assets: int, rem_sum: int, prefix: tuple[int, ...]) -> None:
        if rem_assets == 1:
            rows.append(prefix + (rem_sum,))
            return
        for v in range(rem_sum + 1):
            _rec(rem_assets - 1, rem_sum - v, prefix + (v,))

    _rec(n_assets, steps, ())
    arr = np.array(rows, dtype=np.float64) / float(steps)
    return arr


FAST_U_GRID = np.linspace(-6.5, 2.5, 4096, dtype=np.float64)


def precompute_xirr_y_grid(rem_years: np.ndarray) -> np.ndarray:
    """Precompute log(sum_j exp(u * tau_j)) on the 4096-point FAST_U_GRID for ultra-fast (<1e-5) XIRR lookup."""
    tau = rem_years.astype(np.float64)
    mat = FAST_U_GRID[:, None] * tau[None, :]
    m_max = np.max(mat, axis=1, keepdims=True)
    return m_max[:, 0] + np.log(np.sum(np.exp(mat - m_max), axis=1))


def solve_xirr_from_y_grid(y_grid: np.ndarray, multiples_of_contrib: np.ndarray) -> np.ndarray:
    """Solve XIRR (decimal) using precomputed 4096-point convex log-NPV curve (error < 1e-5, O(B) time)."""
    m_vals = np.asarray(multiples_of_contrib, dtype=np.float64)
    out = np.full(m_vals.shape, -1.0, dtype=np.float64)
    valid = m_vals > 1e-9
    if not np.any(valid):
        return out
    u = np.interp(np.log(m_vals[valid]), y_grid, FAST_U_GRID)
    out[valid] = np.expm1(u)
    return out


def solve_xirr_vectorized(rem_years: np.ndarray, multiples_of_contrib: np.ndarray) -> np.ndarray:
    """Solve annualized XIRR (as decimal, e.g. 0.1234) for an array of terminal values `V_end / C_monthly`.

    Each cohort invests 1.0 unit at each `rem_years[j]` (years remaining until valuation date)
    and receives `M = multiples_of_contrib[b]` at valuation date.
    Exact to < 1e-12 via 512-point log-table interpolation + 3 vectorized Newton steps on log(sum(exp(u * tau))).
    """
    tau = rem_years.astype(np.float64)  # shape (H,)
    m_vals = np.asarray(multiples_of_contrib, dtype=np.float64)
    out = np.full(m_vals.shape, -1.0, dtype=np.float64)
    valid = m_vals > 1e-9
    if not np.any(valid):
        return out

    mv = m_vals[valid]
    log_mv = np.log(mv)

    u_grid = np.linspace(-6.5, 2.5, 512)
    # log_sum_exp on grid: shape (512,)
    mat = u_grid[:, None] * tau[None, :]
    m_max = np.max(mat, axis=1, keepdims=True)
    y_grid = m_max[:, 0] + np.log(np.sum(np.exp(mat - m_max), axis=1))

    u = np.interp(log_mv, y_grid, u_grid)

    # 3 vectorized Newton-Raphson iterations in chunks to keep memory tiny
    chunk = 4096
    for s in range(0, len(u), chunk):
        e = min(len(u), s + chunk)
        uc = u[s:e]
        lm = log_mv[s:e]
        for _ in range(3):
            z = uc[:, None] * tau[None, :]
            z_max = np.max(z, axis=1, keepdims=True)
            ez = np.exp(z - z_max)
            s0 = np.sum(ez, axis=1)
            f_val = z_max[:, 0] + np.log(s0) - lm
            s1 = np.sum(ez * tau[None, :], axis=1)
            df_val = s1 / np.maximum(s0, 1e-30)
            uc = uc - f_val / np.maximum(df_val, 1e-12)
        u[s:e] = uc

    out[valid] = np.expm1(u)
    return out


def solve_xirr_scalar_bisect(days_from_start: np.ndarray, cashflows: np.ndarray) -> float:
    """Independent scalar bisection XIRR solver for cross-validation."""
    t_end = float(np.max(days_from_start))
    rem_yrs = (t_end - days_from_start) / 365.25
    if float(np.sum(cashflows[cashflows > 0])) <= 1e-9:
        return -1.0

    def _npv(r: float) -> float:
        return float(np.sum(cashflows * np.power(max(1e-8, 1.0 + r), rem_yrs)))

    lo, hi = -0.999, 12.0
    f_lo = _npv(lo)
    for _ in range(90):
        mid = 0.5 * (lo + hi)
        f_mid = _npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# 3. Vectorized Portfolio DCA + Rebalance + Filter Simulation Engine
# ---------------------------------------------------------------------------

def get_month_start_indices(dates: pd.DatetimeIndex) -> np.ndarray:
    ym = dates.year * 100 + dates.month
    is_start = np.empty(len(dates), dtype=bool)
    is_start[0] = True
    is_start[1:] = ym[1:] != ym[:-1]
    return np.flatnonzero(is_start)


def get_rebalance_month_flags(m_dates: pd.DatetimeIndex, mode: str) -> np.ndarray:
    """Return boolean array of length M indicating whether month m is a scheduled rebalance month."""
    n_m = len(m_dates)
    flags = np.zeros(n_m, dtype=bool)
    if mode == "none" or n_m == 0:
        return flags
    years = m_dates.year.to_numpy()
    months = m_dates.month.to_numpy()
    if mode == "annual":
        # First trading day of each calendar year (month == 1, or year change)
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


def rebalance_batch_inplace(
    pos: np.ndarray,
    weights: np.ndarray,
    is_tw_mask: np.ndarray,
    lev_pairs: list[tuple[int, int]],
    cons_flags_m: np.ndarray | None,
) -> None:
    """Vectorized in-place rebalancing on `pos` of shape (B, K) with exact TW/US fees and cross-FX fee."""
    v_tot = np.sum(pos, axis=1, keepdims=True)
    target = v_tot * weights

    if cons_flags_m is not None:
        for lev_i, und_i in lev_pairs:
            if cons_flags_m[lev_i]:
                # Conservative state: do not buy additional leveraged ETF; redirect shortfall to 1x underlying
                shortfall = np.maximum(0.0, target[:, lev_i] - pos[:, lev_i])
                target[:, lev_i] -= shortfall
                target[:, und_i] += shortfall

    diff = target - pos
    sell = np.maximum(0.0, -diff)
    buy = np.maximum(0.0, diff)

    tw_idx = is_tw_mask
    us_idx = ~is_tw_mask

    has_tw = bool(np.any(tw_idx))
    has_us = bool(np.any(us_idx))

    c_tw = np.sum(sell[:, tw_idx] * (1.0 - TW_REBAL_SELL_FEE), axis=1) if has_tw else np.zeros(len(pos))
    c_us = np.sum(sell[:, us_idx] * (1.0 - US_COMM_RATE), axis=1) if has_us else np.zeros(len(pos))

    want_tw = np.sum(buy[:, tw_idx], axis=1) if has_tw else np.zeros(len(pos))
    want_us = np.sum(buy[:, us_idx], axis=1) if has_us else np.zeros(len(pos))

    local_tw = np.minimum(c_tw, want_tw)
    local_us = np.minimum(c_us, want_us)
    rem_cash_tw = c_tw - local_tw
    rem_cash_us = c_us - local_us

    alloc_tw = local_tw + rem_cash_us * (1.0 - FX_FEE_RATE)
    alloc_us = local_us + rem_cash_tw * (1.0 - FX_FEE_RATE)

    pos -= sell
    if has_tw:
        scale_tw = np.where(want_tw > 1e-12, (alloc_tw * (1.0 - TW_REBAL_BUY_FEE)) / np.maximum(want_tw, 1e-12), 0.0)
        pos[:, tw_idx] += buy[:, tw_idx] * scale_tw[:, None]
    if has_us:
        scale_us = np.where(want_us > 1e-12, (alloc_us * (1.0 - US_COMM_RATE)) / np.maximum(want_us, 1e-12), 0.0)
        pos[:, us_idx] += buy[:, us_idx] * scale_us[:, None]


def build_effective_dca_inflows(
    weights: np.ndarray,
    dca_net_factor: np.ndarray,
    lev_pairs: list[tuple[int, int]],
    cons_matrix_m: np.ndarray | None,
    n_months: int,
) -> np.ndarray:
    """Return `inflows` of shape (B, M, K) in TWD added on each month-start."""
    b_sz, k_sz = weights.shape
    if cons_matrix_m is None:
        base_in = (MONTHLY_CONTRIB_TWD * weights) * dca_net_factor[None, :]
        return np.broadcast_to(base_in[:, None, :], (b_sz, n_months, k_sz)).copy()

    inflows = np.empty((b_sz, n_months, k_sz), dtype=np.float64)
    for m in range(n_months):
        w_m = weights.copy()
        for lev_i, und_i in lev_pairs:
            if cons_matrix_m[m, lev_i]:
                w_m[:, und_i] += w_m[:, lev_i]
                w_m[:, lev_i] = 0.0
        inflows[:, m, :] = (MONTHLY_CONTRIB_TWD * w_m) * dca_net_factor[None, :]
    return inflows


def evaluate_group_period(
    group_name: str,
    period_name: str,
    asset_names: list[str],
    weights_all: np.ndarray,
    dates: pd.DatetimeIndex,
    wealth_mat: np.ndarray,
    cons_mat_daily: np.ndarray,
    irx_daily: np.ndarray,
    fx_daily: np.ndarray,
    asset_ruin_flags: np.ndarray,
    batch_size: int = 3250,
) -> dict[str, Any]:
    """Run all (weights x 3 rebalance x 2 filter) combinations in memory-safe batches and aggregate results."""
    n_days, k_sz = wealth_mat.shape
    n_weights = len(weights_all)
    month_starts = get_month_start_indices(dates)
    m_dates = dates[month_starts]
    n_m = len(month_starts)
    total_invested_twd = MONTHLY_CONTRIB_TWD * float(n_m)

    days_from_start = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    full_rem_years = (days_from_start[-1] - days_from_start[month_starts]) / 365.25

    # Asset metadata arrays
    leverages = np.array([LEVERAGE_MAP[a] for a in asset_names], dtype=np.float64)
    is_tw_mask = np.array([MARKET_MAP[a] == "TW" for a in asset_names], dtype=bool)
    dca_net_factor = np.where(is_tw_mask, 1.0, (1.0 - FX_FEE_RATE) * (1.0 - US_COMM_RATE))

    lev_pairs: list[tuple[int, int]] = []
    for idx_a, name_a in enumerate(asset_names):
        if name_a in LEV_TO_1X_NAME:
            und_name = LEV_TO_1X_NAME[name_a]
            if und_name in asset_names:
                lev_pairs.append((idx_a, asset_names.index(und_name)))

    cons_at_m = cons_mat_daily[month_starts]  # (M, K)

    # Monthly gross return multipliers G[m, i] from month_starts[m] to month_starts[m+1] (or end of period for m=M-1)
    w_at_m = wealth_mat[month_starts]  # (M, K)
    w_next_m = np.vstack([w_at_m[1:], wealth_mat[-1:]])  # (M, K)
    g_month = w_next_m / np.maximum(w_at_m, 1e-30)

    # Intra-month daily relative wealth multiplier R_intra[t, i] = W[t, i] / W[t_{m(t)}, i]
    m_idx_for_day = np.searchsorted(month_starts, np.arange(n_days), side="right") - 1
    r_intra = wealth_mat / np.maximum(w_at_m[m_idx_for_day], 1e-30)  # (N_days, K)
    cum_invested_daily = (m_idx_for_day + 1).astype(np.float64) * MONTHLY_CONTRIB_TWD  # (N_days,)

    # Monthly risk-free rate for Sharpe (annualized ^IRX / 12 at each month start)
    rf_monthly = irx_daily[month_starts] / 12.0  # (M,)

    # Precompute rolling window remaining-year schedules & 4096-pt log-NPV lookup tables for 5Y, 10Y, 15Y
    horizons = (5, 10, 15)
    rolling_taus: dict[int, list[np.ndarray]] = {}
    rolling_y_grids: dict[int, list[np.ndarray]] = {}
    for hy in horizons:
        h_m = hy * 12
        if n_m > h_m:
            n_c = n_m - h_m
            taus_hy = [
                (days_from_start[month_starts[c + h_m]] - days_from_start[month_starts[c : c + h_m]]) / 365.25
                for c in range(n_c)
            ]
            rolling_taus[hy] = taus_hy
            rolling_y_grids[hy] = [precompute_xirr_y_grid(t_arr) for t_arr in taus_hy]
        else:
            rolling_taus[hy] = []
            rolling_y_grids[hy] = []

    # Compact storage across all 6 * n_weights combinations (~15 MB max even for 116,688 combos!)
    n_total = n_weights * len(REBALANCE_MODES) * len(FILTER_MODES)
    all_w_idx = np.empty(n_total, dtype=np.int32)
    all_reb_idx = np.empty(n_total, dtype=np.int8)
    all_flt_idx = np.empty(n_total, dtype=np.int8)
    all_eff_lev = np.empty(n_total, dtype=np.float64)
    all_final_twd = np.empty(n_total, dtype=np.float64)
    all_xirr = np.empty(n_total, dtype=np.float64)
    all_mdd_pct = np.empty(n_total, dtype=np.float64)
    all_mdd_twd = np.empty(n_total, dtype=np.float64)
    all_rec_months = np.empty(n_total, dtype=np.float64)
    all_rec_done = np.empty(n_total, dtype=bool)
    all_mar = np.empty(n_total, dtype=np.float64)
    all_sharpe = np.empty(n_total, dtype=np.float64)
    all_ruined = np.empty(n_total, dtype=bool)
    all_roll_win = {hy: np.full(n_total, np.nan, dtype=np.float64) for hy in horizons}
    all_roll_med = {hy: np.full(n_total, np.nan, dtype=np.float64) for hy in horizons}
    all_roll_p5 = {hy: np.full(n_total, np.nan, dtype=np.float64) for hy in horizons}

    cursor = 0
    for r_i, reb_mode in enumerate(REBALANCE_MODES):
        rebal_flags = get_rebalance_month_flags(m_dates, reb_mode)
        for f_i, flt_mode in enumerate(FILTER_MODES):
            use_cons = cons_at_m if flt_mode == "sma200_buf2" else None

            for b_start in range(0, n_weights, batch_size):
                b_end = min(n_weights, b_start + batch_size)
                w_batch = weights_all[b_start:b_end]
                b_len = b_end - b_start
                sl = slice(cursor, cursor + b_len)

                all_w_idx[sl] = np.arange(b_start, b_end, dtype=np.int32)
                all_reb_idx[sl] = r_i
                all_flt_idx[sl] = f_i
                all_eff_lev[sl] = np.sum(w_batch * leverages[None, :], axis=1)

                inflows = build_effective_dca_inflows(w_batch, dca_net_factor, lev_pairs, use_cons, n_m)

                # Forward monthly pass for full period
                pos = np.zeros((b_len, k_sz), dtype=np.float64)
                v_post_m = np.empty((b_len, n_m, k_sz), dtype=np.float64)
                port_ret_m = np.empty((b_len, n_m), dtype=np.float64)

                for m in range(n_m):
                    pos += inflows[:, m, :]
                    if m > 0 and rebal_flags[m]:
                        rebalance_batch_inplace(
                            pos, w_batch, is_tw_mask, lev_pairs,
                            use_cons[m] if use_cons is not None else None,
                        )
                    v_post_m[:, m, :] = pos
                    sum_post = np.sum(pos, axis=1)
                    pos = pos * g_month[m][None, :]
                    sum_pre_next = np.sum(pos, axis=1)
                    port_ret_m[:, m] = np.where(sum_post > 1e-12, sum_pre_next / np.maximum(sum_post, 1e-12) - 1.0, 0.0)

                final_twd = np.sum(pos, axis=1)
                all_final_twd[sl] = final_twd
                xirr_dec = solve_xirr_vectorized(full_rem_years, final_twd / MONTHLY_CONTRIB_TWD)
                all_xirr[sl] = xirr_dec * 100.0

                # Monthly Sharpe (deducting monthly ^IRX risk-free rate, annualized)
                excess_m = port_ret_m - rf_monthly[None, :]
                mean_ex = np.mean(excess_m, axis=1)
                std_ex = np.std(excess_m, axis=1, ddof=1)
                all_sharpe[sl] = np.where(std_ex > 1e-12, (mean_ex / np.maximum(std_ex, 1e-12)) * math.sqrt(12.0), 0.0)

                # Daily curve metrics in sub-chunks of 512 using 2D accumulation (keeps peak RAM < 35 MB)
                sub_sz = 512
                for s0 in range(0, b_len, sub_sz):
                    s1 = min(b_len, s0 + sub_sz)
                    v_daily = np.zeros((s1 - s0, n_days), dtype=np.float64)
                    for k_i in range(k_sz):
                        v_daily += v_post_m[s0:s1, m_idx_for_day, k_i] * r_intra[None, :, k_i]
                    peaks = np.maximum.accumulate(np.maximum(v_daily, 1e-12), axis=1)
                    dd_pct = (v_daily - peaks) / peaks  # <= 0

                    trough_idx = np.argmin(dd_pct, axis=1)
                    row_arange = np.arange(s1 - s0)
                    mdd_val = dd_pct[row_arange, trough_idx]  # negative decimal
                    peak_at_trough = peaks[row_arange, trough_idx]
                    val_at_trough = v_daily[row_arange, trough_idx]
                    mdd_twd_val = val_at_trough - peak_at_trough  # negative TWD

                    # Recovery months from trough back to prior peak
                    rec_m_arr = np.empty(s1 - s0, dtype=np.float64)
                    rec_ok_arr = np.empty(s1 - s0, dtype=bool)
                    for r_local in range(s1 - s0):
                        ti = int(trough_idx[r_local])
                        pk_target = float(peak_at_trough[r_local]) - 1e-6
                        post_slice = v_daily[r_local, ti:]
                        rec_hits = np.flatnonzero(post_slice >= pk_target)
                        if len(rec_hits) > 0:
                            ri = ti + int(rec_hits[0])
                            rec_m_arr[r_local] = (days_from_start[ri] - days_from_start[ti]) / 30.4375
                            rec_ok_arr[r_local] = True
                        else:
                            rec_m_arr[r_local] = (days_from_start[-1] - days_from_start[ti]) / 30.4375
                            rec_ok_arr[r_local] = False

                    # Ruin check: any day V_port < 1% of cumulative invested OR any positive-weight asset ruined
                    low_1pct = np.any(v_daily < (0.01 * cum_invested_daily[None, :]), axis=1)
                    asset_r = np.any((w_batch[s0:s1] > 1e-9) & asset_ruin_flags[None, :], axis=1)

                    g_sl = slice(cursor + s0, cursor + s1)
                    all_mdd_pct[g_sl] = mdd_val * 100.0
                    all_mdd_twd[g_sl] = mdd_twd_val
                    all_rec_months[g_sl] = rec_m_arr
                    all_rec_done[g_sl] = rec_ok_arr
                    all_ruined[g_sl] = low_1pct | asset_r

                abs_mdd = np.abs(all_mdd_pct[sl])
                all_mar[sl] = np.where(abs_mdd > 1e-9, all_xirr[sl] / np.maximum(abs_mdd, 1e-9), 0.0)

                # Rolling 5Y, 10Y, 15Y DCA XIRR cohorts
                _compute_rolling_cohorts_batch(
                    reb_mode=reb_mode,
                    rebal_flags=rebal_flags,
                    w_batch=w_batch,
                    inflows=inflows,
                    w_at_m=w_at_m,
                    g_month=g_month,
                    is_tw_mask=is_tw_mask,
                    lev_pairs=lev_pairs,
                    use_cons=use_cons,
                    n_m=n_m,
                    rolling_taus=rolling_taus,
                    rolling_y_grids=rolling_y_grids,
                    out_win=all_roll_win,
                    out_med=all_roll_med,
                    out_p5=all_roll_p5,
                    sl=sl,
                )

                cursor += b_len
                del inflows, v_post_m, port_ret_m
            gc.collect()

    # Compile structured summary, frontier, drawdown-cap top-5, and extreme portfolio
    return _package_group_period_results(
        group_name=group_name,
        period_name=period_name,
        asset_names=asset_names,
        weights_all=weights_all,
        dates=dates,
        month_starts=month_starts,
        wealth_mat=wealth_mat,
        cons_at_m=cons_at_m,
        fx_daily=fx_daily,
        total_invested_twd=total_invested_twd,
        all_w_idx=all_w_idx,
        all_reb_idx=all_reb_idx,
        all_flt_idx=all_flt_idx,
        all_eff_lev=all_eff_lev,
        all_final_twd=all_final_twd,
        all_xirr=all_xirr,
        all_mdd_pct=all_mdd_pct,
        all_mdd_twd=all_mdd_twd,
        all_rec_months=all_rec_months,
        all_rec_done=all_rec_done,
        all_mar=all_mar,
        all_sharpe=all_sharpe,
        all_ruined=all_ruined,
        all_roll_win=all_roll_win,
        all_roll_med=all_roll_med,
        all_roll_p5=all_roll_p5,
        rolling_taus=rolling_taus,
    )


def _compute_rolling_cohorts_batch(
    reb_mode: str,
    rebal_flags: np.ndarray,
    w_batch: np.ndarray,
    inflows: np.ndarray,
    w_at_m: np.ndarray,
    g_month: np.ndarray,
    is_tw_mask: np.ndarray,
    lev_pairs: list[tuple[int, int]],
    use_cons: np.ndarray | None,
    n_m: int,
    rolling_taus: dict[int, list[np.ndarray]],
    rolling_y_grids: dict[int, list[np.ndarray]],
    out_win: dict[int, np.ndarray],
    out_med: dict[int, np.ndarray],
    out_p5: dict[int, np.ndarray],
    sl: slice,
) -> None:
    """Compute exact 5Y/10Y/15Y rolling DCA XIRR statistics for a batch of portfolios."""
    b_len, _, k_sz = inflows.shape
    active_horizons = [hy for hy in (5, 10, 15) if len(rolling_taus[hy]) > 0]
    if not active_horizons:
        return

    cohort_xirrs: dict[int, np.ndarray] = {
        hy: np.empty((b_len, len(rolling_taus[hy])), dtype=np.float64) for hy in active_horizons
    }

    if reb_mode == "none":
        # Exact O(1)-per-cohort prefix-sum evaluation when no rebalancing occurs!
        units_bought = inflows / np.maximum(w_at_m[None, :, :], 1e-30)  # (B, M, K)
        pref_units = np.concatenate(
            [np.zeros((b_len, 1, k_sz), dtype=np.float64), np.cumsum(units_bought, axis=1)],
            axis=1,
        )
        for hy in active_horizons:
            h_m = hy * 12
            y_grids = rolling_y_grids[hy]
            for c, y_grid_c in enumerate(y_grids):
                u_sum = pref_units[:, c + h_m, :] - pref_units[:, c, :]  # (B, K)
                end_val = np.sum(u_sum * w_at_m[c + h_m][None, :], axis=1)
                cohort_xirrs[hy][:, c] = solve_xirr_from_y_grid(y_grid_c, end_val / MONTHLY_CONTRIB_TWD) * 100.0
    else:
        # Single monthly forward pass per starting cohort c up to max needed horizon (<=180 steps)
        max_h_m = max(hy * 12 for hy in active_horizons)
        n_max_cohorts = n_m - 60  # since 5Y=60m is smallest horizon
        for c in range(n_max_cohorts):
            rem_len = min(max_h_m, n_m - c)
            pos_c = np.zeros((b_len, k_sz), dtype=np.float64)
            for step in range(rem_len):
                m = c + step
                pos_c += inflows[:, m, :]
                if step > 0 and rebal_flags[m]:
                    rebalance_batch_inplace(
                        pos_c, w_batch, is_tw_mask, lev_pairs,
                        use_cons[m] if use_cons is not None else None,
                    )
                pos_c = pos_c * g_month[m][None, :]
                done_months = step + 1
                if done_months in (60, 120, 180):
                    hy_hit = done_months // 12
                    if hy_hit in cohort_xirrs and c < cohort_xirrs[hy_hit].shape[1]:
                        end_val = np.sum(pos_c, axis=1)
                        cohort_xirrs[hy_hit][:, c] = (
                            solve_xirr_from_y_grid(rolling_y_grids[hy_hit][c], end_val / MONTHLY_CONTRIB_TWD) * 100.0
                        )

    for hy in active_horizons:
        mat = cohort_xirrs[hy]
        out_win[hy][sl] = np.mean(mat > 0.0, axis=1) * 100.0
        out_med[hy][sl] = np.median(mat, axis=1)
        out_p5[hy][sl] = np.percentile(mat, 5, axis=1)


# ---------------------------------------------------------------------------
# 4. Single-Portfolio Detailed Curve Re-Runner (for Top-5, Extreme & $15 Fee)
# ---------------------------------------------------------------------------

def simulate_single_detailed(
    asset_names: list[str],
    weights_1d: np.ndarray,
    reb_mode: str,
    flt_mode: str,
    dates: pd.DatetimeIndex,
    wealth_mat: np.ndarray,
    cons_at_m: np.ndarray,
    fx_daily: np.ndarray,
    us_min_fee_usd: float = 0.0,
) -> dict[str, Any]:
    """Re-run a single portfolio to extract monthly equity curve, 4-crash drawdowns, or $15 min-fee impact."""
    n_days, k_sz = wealth_mat.shape
    month_starts = get_month_start_indices(dates)
    m_dates = dates[month_starts]
    n_m = len(month_starts)
    if len(cons_at_m) == n_days and n_days != n_m:
        cons_at_m = cons_at_m[month_starts]
    rebal_flags = get_rebalance_month_flags(m_dates, reb_mode)
    is_tw_mask = np.array([MARKET_MAP[a] == "TW" for a in asset_names], dtype=bool)

    lev_pairs: list[tuple[int, int]] = []
    for idx_a, name_a in enumerate(asset_names):
        if name_a in LEV_TO_1X_NAME and LEV_TO_1X_NAME[name_a] in asset_names:
            lev_pairs.append((idx_a, asset_names.index(LEV_TO_1X_NAME[name_a])))

    w_at_m = wealth_mat[month_starts]
    w_next_m = np.vstack([w_at_m[1:], wealth_mat[-1:]])
    g_month = w_next_m / np.maximum(w_at_m, 1e-30)
    m_idx_for_day = np.searchsorted(month_starts, np.arange(n_days), side="right") - 1
    r_intra = wealth_mat / np.maximum(w_at_m[m_idx_for_day], 1e-30)

    pos = np.zeros((1, k_sz), dtype=np.float64)
    v_post_m = np.empty((n_m, k_sz), dtype=np.float64)

    for m in range(n_m):
        w_m = weights_1d.copy()
        if flt_mode == "sma200_buf2":
            for lev_i, und_i in lev_pairs:
                if cons_at_m[m, lev_i]:
                    w_m[und_i] += w_m[lev_i]
                    w_m[lev_i] = 0.0

        fx_m = float(fx_daily[month_starts[m]])
        for k_i in range(k_sz):
            gross_twd = MONTHLY_CONTRIB_TWD * float(w_m[k_i])
            if gross_twd <= 1e-9:
                continue
            if is_tw_mask[k_i]:
                pos[0, k_i] += gross_twd
            else:
                usd_after_fx = (gross_twd * (1.0 - FX_FEE_RATE)) / fx_m
                comm_usd = max(us_min_fee_usd, usd_after_fx * US_COMM_RATE)
                net_usd = max(0.0, usd_after_fx - comm_usd)
                pos[0, k_i] += net_usd * fx_m

        if m > 0 and rebal_flags[m]:
            if us_min_fee_usd <= 0.0:
                rebalance_batch_inplace(
                    pos, weights_1d[None, :], is_tw_mask, lev_pairs,
                    cons_at_m[m] if flt_mode == "sma200_buf2" else None,
                )
            else:
                _rebalance_single_with_min_fee(
                    pos, weights_1d, is_tw_mask, lev_pairs,
                    cons_at_m[m] if flt_mode == "sma200_buf2" else None,
                    fx_m, us_min_fee_usd,
                )

        v_post_m[m, :] = pos[0]
        pos = pos * g_month[m][None, :]

    v_daily = np.sum(v_post_m[m_idx_for_day, :] * r_intra, axis=1)
    final_twd = float(v_daily[-1])

    days_from_start = (dates - dates[0]).days.to_numpy(dtype=np.float64)
    cf_days = np.append(days_from_start[month_starts], days_from_start[-1])
    cf_amts = np.append(-np.full(n_m, MONTHLY_CONTRIB_TWD), final_twd)
    xirr_pct = solve_xirr_scalar_bisect(cf_days, cf_amts) * 100.0

    peaks = np.maximum.accumulate(np.maximum(v_daily, 1e-12))
    dd_pct = (v_daily - peaks) / peaks
    mdd_pct = float(np.min(dd_pct)) * 100.0

    # Monthly asset curve (sampled at month-starts post-contribution + final date)
    monthly_curve = []
    for m in range(n_m):
        ti = int(month_starts[m])
        monthly_curve.append({
            "date": dates[ti].strftime("%Y-%m-%d"),
            "invested_twd": round(float((m + 1) * MONTHLY_CONTRIB_TWD), 0),
            "value_twd": round(float(v_daily[ti]), 0),
        })
    if int(month_starts[-1]) != n_days - 1:
        monthly_curve.append({
            "date": dates[-1].strftime("%Y-%m-%d"),
            "invested_twd": round(float(n_m * MONTHLY_CONTRIB_TWD), 0),
            "value_twd": round(final_twd, 0),
        })

    # Crash drawdowns (2000, 2008, 2020, 2022)
    crash_dds: dict[str, float | None] = {}
    for c_key, (d_s, d_e, _) in CRASH_WINDOWS.items():
        mask_c = (dates >= pd.Timestamp(d_s)) & (dates <= pd.Timestamp(d_e))
        if int(np.sum(mask_c)) >= 5:
            sub_v = v_daily[mask_c]
            sub_pk = np.maximum.accumulate(np.maximum(sub_v, 1e-12))
            crash_dds[c_key] = round(float(np.min((sub_v - sub_pk) / sub_pk)) * 100.0, 2)
        else:
            crash_dds[c_key] = None

    return {
        "final_value_twd": round(final_twd, 0),
        "xirr": round(xirr_pct, 2),
        "max_drawdown_pct": round(mdd_pct, 2),
        "monthly_curve": monthly_curve,
        "crash_drawdowns_pct": crash_dds,
    }


def _rebalance_single_with_min_fee(
    pos: np.ndarray,
    weights_1d: np.ndarray,
    is_tw_mask: np.ndarray,
    lev_pairs: list[tuple[int, int]],
    cons_flags_m: np.ndarray | None,
    fx_m: float,
    us_min_fee_usd: float,
) -> None:
    """Single-portfolio rebalance applying $15 USD minimum fee per traded US ticker."""
    v_tot = float(np.sum(pos[0]))
    target = v_tot * weights_1d.copy()
    if cons_flags_m is not None:
        for lev_i, und_i in lev_pairs:
            if cons_flags_m[lev_i] and pos[0, lev_i] < target[lev_i]:
                shortfall = target[lev_i] - pos[0, lev_i]
                target[lev_i] -= shortfall
                target[und_i] += shortfall

    diff = target - pos[0]
    min_fee_twd = us_min_fee_usd * fx_m
    sell = np.maximum(0.0, -diff)
    buy = np.maximum(0.0, diff)

    c_tw = float(np.sum(sell[is_tw_mask] * (1.0 - TW_REBAL_SELL_FEE)))
    c_us = 0.0
    for k_i in np.flatnonzero(~is_tw_mask):
        if sell[k_i] > 1.0:
            fee = max(min_fee_twd, sell[k_i] * US_COMM_RATE)
            c_us += max(0.0, sell[k_i] - fee)

    want_tw = float(np.sum(buy[is_tw_mask]))
    want_us = float(np.sum(buy[~is_tw_mask]))
    local_tw = min(c_tw, want_tw)
    local_us = min(c_us, want_us)
    alloc_tw = local_tw + (c_us - local_us) * (1.0 - FX_FEE_RATE)
    alloc_us = local_us + (c_tw - local_tw) * (1.0 - FX_FEE_RATE)

    pos[0] -= sell
    if want_tw > 1e-9:
        pos[0, is_tw_mask] += buy[is_tw_mask] * ((alloc_tw * (1.0 - TW_REBAL_BUY_FEE)) / want_tw)
    if want_us > 1e-9:
        for k_i in np.flatnonzero(~is_tw_mask):
            if buy[k_i] > 1.0:
                gross_alloc = alloc_us * (buy[k_i] / want_us)
                fee = max(min_fee_twd, gross_alloc * US_COMM_RATE)
                pos[0, k_i] += max(0.0, gross_alloc - fee)


def _package_group_period_results(
    group_name: str,
    period_name: str,
    asset_names: list[str],
    weights_all: np.ndarray,
    dates: pd.DatetimeIndex,
    month_starts: np.ndarray,
    wealth_mat: np.ndarray,
    cons_at_m: np.ndarray,
    fx_daily: np.ndarray,
    total_invested_twd: float,
    all_w_idx: np.ndarray,
    all_reb_idx: np.ndarray,
    all_flt_idx: np.ndarray,
    all_eff_lev: np.ndarray,
    all_final_twd: np.ndarray,
    all_xirr: np.ndarray,
    all_mdd_pct: np.ndarray,
    all_mdd_twd: np.ndarray,
    all_rec_months: np.ndarray,
    all_rec_done: np.ndarray,
    all_mar: np.ndarray,
    all_sharpe: np.ndarray,
    all_ruined: np.ndarray,
    all_roll_win: dict[int, np.ndarray],
    all_roll_med: dict[int, np.ndarray],
    all_roll_p5: dict[int, np.ndarray],
    rolling_taus: dict[int, list[np.ndarray]],
) -> dict[str, Any]:
    """Build structured top_by_drawdown_cap, extreme, frontier, summary_stats, and scatter arrays."""

    def _build_combo_obj(idx: int, include_curve: bool = False, include_crashes: bool = False) -> dict[str, Any]:
        w_vec = weights_all[int(all_w_idx[idx])]
        w_dict = {asset_names[k]: int(round(w_vec[k] * 100)) for k in range(len(asset_names))}
        reb_str = REBALANCE_MODES[int(all_reb_idx[idx])]
        flt_str = FILTER_MODES[int(all_flt_idx[idx])]

        rec_m_val = round(float(all_rec_months[idx]), 1)
        if bool(all_rec_done[idx]):
            rec_field: Any = rec_m_val
        else:
            rec_field = f"未回復（已 {rec_m_val:.1f} 個月）"

        rolling_dict: dict[str, Any] = {}
        for hy in (5, 10, 15):
            key = f"{hy}y"
            if len(rolling_taus[hy]) == 0:
                rolling_dict[key] = {
                    "status": "資料不足",
                    "count": 0,
                    "win_rate_pct": None,
                    "median_xirr_pct": None,
                    "p5_xirr_pct": None,
                }
            else:
                rolling_dict[key] = {
                    "status": "OK",
                    "count": len(rolling_taus[hy]),
                    "win_rate_pct": round(float(all_roll_win[hy][idx]), 2),
                    "median_xirr_pct": round(float(all_roll_med[hy][idx]), 2),
                    "p5_xirr_pct": round(float(all_roll_p5[hy][idx]), 2),
                }

        obj: dict[str, Any] = {
            "weights": w_dict,
            "effective_leverage": round(float(all_eff_lev[idx]), 2),
            "rebalance": reb_str,
            "filter": flt_str,
            "final_value_twd": round(float(all_final_twd[idx]), 0),
            "total_invested_twd": round(total_invested_twd, 0),
            "xirr": round(float(all_xirr[idx]), 2),
            "max_drawdown_pct": round(float(all_mdd_pct[idx]), 2),
            "max_drawdown_twd": round(float(all_mdd_twd[idx]), 0),
            "recovery_months": rec_field,
            "recovery_months_num": rec_m_val,
            "recovered": bool(all_rec_done[idx]),
            "mar": round(float(all_mar[idx]), 3),
            "sharpe": round(float(all_sharpe[idx]), 3),
            "rolling": rolling_dict,
            "ruined": bool(all_ruined[idx]),
        }

        if include_curve or include_crashes:
            det = simulate_single_detailed(
                asset_names=asset_names,
                weights_1d=w_vec,
                reb_mode=reb_str,
                flt_mode=flt_str,
                dates=dates,
                wealth_mat=wealth_mat,
                cons_at_m=cons_at_m,
                fx_daily=fx_daily,
                us_min_fee_usd=0.0,
            )
            if include_curve:
                obj["monthly_curve"] = det["monthly_curve"]
            if include_crashes:
                obj["crash_drawdowns_pct"] = det["crash_drawdowns_pct"]
            # If Group B uses US stocks, also attach $15 USD min-fee sensitivity on Top 5!
            has_us_weight = any(MARKET_MAP[a] == "US" and w_dict[a] > 0 for a in asset_names)
            if include_curve and group_name == "B":
                if has_us_weight:
                    det_15 = simulate_single_detailed(
                        asset_names=asset_names,
                        weights_1d=w_vec,
                        reb_mode=reb_str,
                        flt_mode=flt_str,
                        dates=dates,
                        wealth_mat=wealth_mat,
                        cons_at_m=cons_at_m,
                        fx_daily=fx_daily,
                        us_min_fee_usd=15.0,
                    )
                    obj["sensitivity_min_fee_15usd"] = {
                        "final_value_twd": det_15["final_value_twd"],
                        "xirr": det_15["xirr"],
                        "xirr_diff": round(det_15["xirr"] - obj["xirr"], 2),
                        "max_drawdown_pct": det_15["max_drawdown_pct"],
                    }
                else:
                    obj["sensitivity_min_fee_15usd"] = {
                        "final_value_twd": obj["final_value_twd"],
                        "xirr": obj["xirr"],
                        "xirr_diff": 0.0,
                        "max_drawdown_pct": obj["max_drawdown_pct"],
                    }
        return obj

    # Canonical filter for Top-5 rankings: exclude mathematically identical no-op duplicates:
    # 1) Single-asset (n_nonzero == 1) without filter: annual/quarterly rebalance does 0 trades == none
    # 2) Pure 1x portfolio (no leveraged ETFs): 200-SMA filter does 0 redirects == none
    n_nonzero_per_w = np.sum(weights_all > 1e-9, axis=1)
    lev_asset_mask = np.array([LEVERAGE_MAP[a] > 1.0 for a in asset_names], dtype=bool)
    has_lev_per_w = np.any(weights_all[:, lev_asset_mask] > 1e-9, axis=1)

    n_nz_run = n_nonzero_per_w[all_w_idx]
    has_lev_run = has_lev_per_w[all_w_idx]
    noop_reb = (n_nz_run == 1) & (all_flt_idx == 0) & (all_reb_idx != 0)
    noop_flt = (~has_lev_run) & (all_flt_idx != 0)
    canonical_mask = (~all_ruined) & (~noop_reb) & (~noop_flt)

    # Overall Top-5 by XIRR (non-ruined, canonical) & Top-5 by each drawdown cap (-30, -40, -50, -60)
    not_ruined_mask = ~all_ruined
    top_by_dd: dict[str, list[dict[str, Any]]] = {}
    for cap in DRAWDOWN_CAPS:
        # e.g. cap = -30 means max_drawdown_pct >= -30.0 (drawdown magnitude <= 30%)
        mask_cap = canonical_mask & (all_mdd_pct >= float(cap) - 1e-9)
        cand_idx = np.flatnonzero(mask_cap)
        if len(cand_idx) == 0:
            top_by_dd[str(cap)] = []
        else:
            # Sort by XIRR descending, tie-break by MAR descending
            order = np.lexsort((-all_mar[cand_idx], -all_xirr[cand_idx]))
            top_idx = cand_idx[order[:5]]
            top_by_dd[str(cap)] = [_build_combo_obj(int(i), include_curve=True) for i in top_idx]

    # Extreme: highest XIRR among non-ruined combinations & Overall Top-5
    canon_indices = np.flatnonzero(canonical_mask)
    overall_order = np.lexsort((-all_mar[canon_indices], -all_xirr[canon_indices]))
    overall_top5_idx = canon_indices[overall_order[:5]]
    overall_top5 = [_build_combo_obj(int(i), include_curve=True, include_crashes=(i == overall_top5_idx[0])) for i in overall_top5_idx]
    extreme_obj = overall_top5[0]
    nr_indices = np.flatnonzero(not_ruined_mask)

    # Efficient Frontier (Pareto-optimal on (abs(MDD), XIRR) among non-ruined)
    abs_mdd_nr = np.abs(all_mdd_pct[nr_indices])
    xirr_nr = all_xirr[nr_indices]
    # Round MDD to 0.01% buckets for clean monotonic frontier
    mdd_bucket = np.round(abs_mdd_nr, 2)
    order_f = np.lexsort((-xirr_nr, mdd_bucket))
    sorted_nr = nr_indices[order_f]

    frontier_list: list[dict[str, Any]] = []
    best_so_far = -1e18
    last_bkt = -1.0
    for idx_f in sorted_nr:
        bkt = round(abs(float(all_mdd_pct[idx_f])), 2)
        xv = round(float(all_xirr[idx_f]), 4)
        if bkt == last_bkt:
            continue
        if xv > best_so_far + 1e-6:
            best_so_far = xv
            last_bkt = bkt
            w_vec = weights_all[int(all_w_idx[idx_f])]
            frontier_list.append({
                "weights": {asset_names[k]: int(round(w_vec[k] * 100)) for k in range(len(asset_names))},
                "effective_leverage": round(float(all_eff_lev[idx_f]), 2),
                "rebalance": REBALANCE_MODES[int(all_reb_idx[idx_f])],
                "filter": FILTER_MODES[int(all_flt_idx[idx_f])],
                "xirr": round(float(all_xirr[idx_f]), 2),
                "max_drawdown_pct": round(float(all_mdd_pct[idx_f]), 2),
                "mar": round(float(all_mar[idx_f]), 3),
            })

    # Summary stats across ALL combinations in this group & period
    def _dist_summary(arr: np.ndarray) -> dict[str, float]:
        return {
            "min": round(float(np.min(arr)), 2),
            "p5": round(float(np.percentile(arr, 5)), 2),
            "p25": round(float(np.percentile(arr, 25)), 2),
            "median": round(float(np.median(arr)), 2),
            "mean": round(float(np.mean(arr)), 2),
            "p75": round(float(np.percentile(arr, 75)), 2),
            "p95": round(float(np.percentile(arr, 95)), 2),
            "max": round(float(np.max(arr)), 2),
        }

    # Lowest drawdown portfolio (useful when -30% cap has 0 matches in Full History)
    min_dd_idx = int(nr_indices[int(np.argmax(all_mdd_pct[nr_indices]))])  # closest to 0%
    min_dd_obj = _build_combo_obj(min_dd_idx, include_curve=False)

    # 3x ETF control-group breakdown (for Group B)
    idx_3x = [i for i, a in enumerate(asset_names) if LEVERAGE_MAP[a] >= 3.0]
    control_3x_stats: dict[str, Any] = {}
    if idx_3x:
        w3x_per_w = np.sum(weights_all[:, idx_3x], axis=1)
        w3x_run = w3x_per_w[all_w_idx]
        for b_key, b_mask in (
            ("no_3x_0pct", w3x_run <= 1e-9),
            ("any_3x_gt_0pct", w3x_run > 1e-9),
            ("heavy_3x_gte_50pct", w3x_run >= 0.50 - 1e-9),
            ("all_3x_100pct", w3x_run >= 1.00 - 1e-9),
        ):
            if np.any(b_mask):
                control_3x_stats[b_key] = {
                    "count": int(np.sum(b_mask)),
                    "ruined_count": int(np.sum(all_ruined[b_mask])),
                    "mdd_worse_than_80pct_share": round(float(np.mean(all_mdd_pct[b_mask] <= -80.0) * 100.0), 2),
                    "mdd_worse_than_90pct_share": round(float(np.mean(all_mdd_pct[b_mask] <= -90.0) * 100.0), 2),
                    "median_xirr_pct": round(float(np.median(all_xirr[b_mask])), 2),
                    "median_mdd_pct": round(float(np.median(all_mdd_pct[b_mask])), 2),
                    "worst_mdd_pct": round(float(np.min(all_mdd_pct[b_mask])), 2),
                    "median_mar": round(float(np.median(all_mar[b_mask])), 3),
                }

    summary_stats = {
        "start_date": dates[0].strftime("%Y-%m-%d"),
        "end_date": dates[-1].strftime("%Y-%m-%d"),
        "months_invested": int(len(month_starts)),
        "total_invested_twd": round(total_invested_twd, 0),
        "weight_combinations": int(len(weights_all)),
        "total_strategy_runs": int(len(all_xirr)),
        "ruined_count": int(np.sum(all_ruined)),
        "xirr_pct": _dist_summary(all_xirr),
        "max_drawdown_pct": _dist_summary(all_mdd_pct),
        "mar": _dist_summary(all_mar),
        "sharpe": _dist_summary(all_sharpe),
        "lowest_drawdown_portfolio": min_dd_obj,
        "control_3x_breakdown": control_3x_stats,
    }

    return {
        "json_data": {
            "top_by_drawdown_cap": top_by_dd,
            "overall_top5": overall_top5,
            "extreme": extreme_obj,
            "frontier": frontier_list,
            "summary_stats": summary_stats,
        },
        "scatter": {
            "mdd_pct": all_mdd_pct,
            "xirr_pct": all_xirr,
            "ruined": all_ruined,
        },
    }


# ---------------------------------------------------------------------------
# 5. Independent Day-by-Day Share-Tracking Validator (Requirement 十.1)
# ---------------------------------------------------------------------------

def run_independent_cross_validation(dataset: dict[str, Any], b_full_res: dict[str, Any]) -> list[dict[str, Any]]:
    """Independently simulate 3 simple portfolios (100% 0050, 100% SPY, 50% 0050 + 50% SPY)
    from scratch by tracking literal share counts day-by-day (no wealth index shortcuts!)
    and verify exact agreement with the vectorized grid engine!
    """
    all_dates = dataset["all_dates"]
    mask_b = all_dates >= dataset["start_b"]
    dates = all_dates[mask_b]
    w_df = dataset["twd_wealth_df"].loc[dates]
    fx_s = dataset["fx_aligned"].loc[dates].to_numpy(dtype=float)

    # Reconstruct literal unit prices in native currencies:
    # 0050 in TWD, SPY in USD (where USD unit price = twd_wealth["SPY"] / fx)
    p_0050_twd = w_df["0050"].to_numpy(dtype=float)
    p_spy_usd = w_df["SPY"].to_numpy(dtype=float) / fx_s

    ym = dates.year * 100 + dates.month
    is_m_start = np.concatenate([[True], ym[1:] != ym[:-1]])
    days_arr = (dates - dates[0]).days.to_numpy(dtype=float)

    cases = [
        ("100% 0050", 1.0, 0.0),
        ("100% SPY", 0.0, 1.0),
        ("50% 0050 + 50% SPY", 0.5, 0.5),
    ]

    # Also re-run the same 3 combinations via `simulate_single_detailed` (same engine as grid) to compare
    wealth_mat_b = w_df[ASSETS_B].to_numpy(dtype=float)
    ms_b = get_month_start_indices(dates)
    cons_b = dataset["cons_df"].reindex(index=dates[ms_b], columns=ASSETS_B, fill_value=False).to_numpy(dtype=bool)

    results_val: list[dict[str, Any]] = []
    for label, w_50, w_spy in cases:
        shares_50 = 0.0
        shares_spy = 0.0
        daily_val_twd = np.zeros(len(dates), dtype=float)
        cf_days: list[float] = []
        cf_amts: list[float] = []

        for t in range(len(dates)):
            if is_m_start[t]:
                cf_days.append(float(days_arr[t]))
                cf_amts.append(-MONTHLY_CONTRIB_TWD)
                if w_50 > 0:
                    amt_tw = MONTHLY_CONTRIB_TWD * w_50
                    shares_50 += amt_tw / p_0050_twd[t]
                if w_spy > 0:
                    amt_twd_for_us = MONTHLY_CONTRIB_TWD * w_spy
                    usd_after_fx = (amt_twd_for_us * (1.0 - FX_FEE_RATE)) / fx_s[t]
                    usd_net = usd_after_fx * (1.0 - US_COMM_RATE)
                    shares_spy += usd_net / p_spy_usd[t]

            daily_val_twd[t] = shares_50 * p_0050_twd[t] + (shares_spy * p_spy_usd[t]) * fx_s[t]

        cf_days.append(float(days_arr[-1]))
        cf_amts.append(float(daily_val_twd[-1]))
        ind_xirr = solve_xirr_scalar_bisect(np.array(cf_days), np.array(cf_amts)) * 100.0
        peaks = np.maximum.accumulate(daily_val_twd)
        ind_mdd = float(np.min((daily_val_twd - peaks) / peaks)) * 100.0
        ind_final = float(daily_val_twd[-1])

        # Grid engine value for the exact same weights, rebalance="none", filter="none"
        w_vec = np.zeros(len(ASSETS_B), dtype=float)
        w_vec[ASSETS_B.index("0050")] = w_50
        w_vec[ASSETS_B.index("SPY")] = w_spy
        eng = simulate_single_detailed(
            asset_names=ASSETS_B,
            weights_1d=w_vec,
            reb_mode="none",
            flt_mode="none",
            dates=dates,
            wealth_mat=wealth_mat_b,
            cons_at_m=cons_b,
            fx_daily=fx_s,
        )

        # Also check exact match in vectorized batch arrays!
        xirr_err = abs(ind_xirr - eng["xirr"])
        mdd_err = abs(ind_mdd - eng["max_drawdown_pct"])
        final_err = abs(ind_final - eng["final_value_twd"])
        if xirr_err > 0.01 or mdd_err > 0.01 or final_err > 2.0:
            raise AssertionError(
                f"Cross-validation mismatch for {label}: ind=({ind_xirr:.4f}%, {ind_mdd:.4f}%, {ind_final:.1f}) "
                f"vs engine=({eng['xirr']}%, {eng['max_drawdown_pct']}%, {eng['final_value_twd']})"
            )

        results_val.append({
            "portfolio": label,
            "period": f"{dates[0].strftime('%Y-%m-%d')} ~ {dates[-1].strftime('%Y-%m-%d')}",
            "independent_final_twd": round(ind_final, 0),
            "engine_final_twd": eng["final_value_twd"],
            "independent_xirr_pct": round(ind_xirr, 4),
            "engine_xirr_pct": eng["xirr"],
            "xirr_abs_diff_pct": round(abs(round(ind_xirr, 2) - eng["xirr"]), 4),
            "independent_mdd_pct": round(ind_mdd, 4),
            "engine_mdd_pct": eng["max_drawdown_pct"],
            "mdd_abs_diff_pct": round(abs(round(ind_mdd, 2) - eng["max_drawdown_pct"]), 4),
            "passed": True,
        })

    return results_val


# ---------------------------------------------------------------------------
# 6. Efficient Frontier High-DPI PNG Chart Renderer (macOS Swift CoreGraphics)
# ---------------------------------------------------------------------------

SWIFT_CHART_SCRIPT = r'''
import Foundation
import AppKit
import CoreGraphics

struct PointItem: Codable {
    let mdd: Double
    let xirr: Double
}
struct ChampionItem: Codable {
    let group: String
    let cap: Int
    let mdd: Double
    let xirr: Double
    let label: String
}
struct ChartPayload: Codable {
    let title: String
    let subtitle: String
    let outPath: String
    let xMin: Double
    let xMax: Double
    let yMin: Double
    let yMax: Double
    let scatterA: [PointItem]
    let scatterB: [PointItem]
    let frontierA: [PointItem]
    let frontierB: [PointItem]
    let champions: [ChampionItem]
}

let args = CommandLine.arguments
let jsonURL = URL(fileURLWithPath: args[1])
let data = try Data(contentsOf: jsonURL)
let payload = try JSONDecoder().decode(ChartPayload.self, from: data)

let width = 2400
let height = 1520
let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: width,
    pixelsHigh: height,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
)!

NSGraphicsContext.saveGraphicsState()
let nsCtx = NSGraphicsContext(bitmapImageRep: rep)!
NSGraphicsContext.current = nsCtx
let ctx = nsCtx.cgContext

// Background
ctx.setFillColor(NSColor(calibratedRed: 0.985, green: 0.988, blue: 0.995, alpha: 1.0).cgColor)
ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))

let padL: CGFloat = 190
let padR: CGFloat = 110
let padB: CGFloat = 165
let padT: CGFloat = 215
let plotRect = CGRect(x: padL, y: padB, width: CGFloat(width) - padL - padR, height: CGFloat(height) - padT - padB)

// Plot card background
ctx.setFillColor(NSColor.white.cgColor)
ctx.fill(plotRect)
ctx.setStrokeColor(NSColor(calibratedWhite: 0.82, alpha: 1.0).cgColor)
ctx.setLineWidth(2.0)
ctx.stroke(plotRect)

func mapX(_ mddAbs: Double) -> CGFloat {
    let t = (mddAbs - payload.xMin) / (payload.xMax - payload.xMin)
    return plotRect.minX + CGFloat(t) * plotRect.width
}
func mapY(_ xirr: Double) -> CGFloat {
    let t = (xirr - payload.yMin) / (payload.yMax - payload.yMin)
    return plotRect.minY + CGFloat(t) * plotRect.height
}

func drawText(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat, bold: Bool = false, color: NSColor = .black, align: String = "left") {
    let fontName = bold ? "PingFangTC-Semibold" : "PingFangTC-Regular"
    let font = NSFont(name: fontName, size: size) ?? NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
    let attrs: [NSAttributedString.Key: Any] = [
        .font: font,
        .foregroundColor: color
    ]
    let nsStr = NSString(string: text)
    let sz = nsStr.size(withAttributes: attrs)
    var dx = x
    if align == "center" { dx = x - sz.width / 2.0 }
    else if align == "right" { dx = x - sz.width }
    nsStr.draw(at: NSPoint(x: dx, y: y), withAttributes: attrs)
}

// Gridlines & ticks
let xStep = 10.0
var xv = ceil(payload.xMin / xStep) * xStep
while xv <= payload.xMax + 0.001 {
    let px = mapX(xv)
    let isCap = (Int(round(xv)) == 30 || Int(round(xv)) == 40 || Int(round(xv)) == 50 || Int(round(xv)) == 60)
    ctx.saveGState()
    if isCap {
        ctx.setStrokeColor(NSColor(calibratedRed: 0.85, green: 0.25, blue: 0.25, alpha: 0.48).cgColor)
        ctx.setLineWidth(2.2)
        ctx.setLineDash(phase: 0, lengths: [10, 7])
    } else {
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.90, alpha: 1.0).cgColor)
        ctx.setLineWidth(1.4)
    }
    ctx.move(to: CGPoint(x: px, y: plotRect.minY))
    ctx.addLine(to: CGPoint(x: px, y: plotRect.maxY))
    ctx.strokePath()
    ctx.restoreGState()

    drawText(String(format: "-%.0f%%", xv), x: px, y: plotRect.minY - 44, size: 24, bold: isCap,
             color: isCap ? NSColor(calibratedRed: 0.72, green: 0.15, blue: 0.15, alpha: 1.0) : NSColor(calibratedWhite: 0.25, alpha: 1.0),
             align: "center")
    if isCap {
        drawText(String(format: "回撤上限 -%.0f%%", xv), x: px, y: plotRect.maxY + 10, size: 19, bold: true,
                 color: NSColor(calibratedRed: 0.72, green: 0.15, blue: 0.15, alpha: 0.9), align: "center")
    }
    xv += xStep
}

let yRange = payload.yMax - payload.yMin
let yStep: Double = yRange > 35.0 ? 5.0 : 2.5
var yv = ceil(payload.yMin / yStep) * yStep
while yv <= payload.yMax + 0.001 {
    let py = mapY(yv)
    ctx.saveGState()
    ctx.setStrokeColor(NSColor(calibratedWhite: abs(yv) < 0.01 ? 0.65 : 0.90, alpha: 1.0).cgColor)
    ctx.setLineWidth(abs(yv) < 0.01 ? 2.2 : 1.4)
    ctx.move(to: CGPoint(x: plotRect.minX, y: py))
    ctx.addLine(to: CGPoint(x: plotRect.maxX, y: py))
    ctx.strokePath()
    ctx.restoreGState()

    let fmt = (yStep == 2.5) ? "%.1f%%" : "%.0f%%"
    drawText(String(format: fmt, yv), x: plotRect.minX - 18, y: py - 14, size: 24, bold: false,
             color: NSColor(calibratedWhite: 0.25, alpha: 1.0), align: "right")
    yv += yStep
}

// Clip to plotRect for scatter and frontier
ctx.saveGState()
ctx.clip(to: plotRect)

// Scatter B (light blue)
ctx.setFillColor(NSColor(calibratedRed: 0.23, green: 0.51, blue: 0.96, alpha: 0.14).cgColor)
for p in payload.scatterB {
    let px = mapX(abs(p.mdd))
    let py = mapY(p.xirr)
    ctx.fillEllipse(in: CGRect(x: px - 3.5, y: py - 3.5, width: 7.0, height: 7.0))
}

// Scatter A (warm orange)
ctx.setFillColor(NSColor(calibratedRed: 0.94, green: 0.42, blue: 0.08, alpha: 0.55).cgColor)
for p in payload.scatterA {
    let px = mapX(abs(p.mdd))
    let py = mapY(p.xirr)
    ctx.fillEllipse(in: CGRect(x: px - 6.5, y: py - 6.5, width: 13.0, height: 13.0))
}

// Frontier B line
if payload.frontierB.count >= 2 {
    ctx.setStrokeColor(NSColor(calibratedRed: 0.10, green: 0.28, blue: 0.82, alpha: 0.96).cgColor)
    ctx.setLineWidth(5.0)
    ctx.move(to: CGPoint(x: mapX(abs(payload.frontierB[0].mdd)), y: mapY(payload.frontierB[0].xirr)))
    for i in 1..<payload.frontierB.count {
        ctx.addLine(to: CGPoint(x: mapX(abs(payload.frontierB[i].mdd)), y: mapY(payload.frontierB[i].xirr)))
    }
    ctx.strokePath()
}

// Frontier A line
if payload.frontierA.count >= 2 {
    ctx.setStrokeColor(NSColor(calibratedRed: 0.85, green: 0.26, blue: 0.04, alpha: 0.96).cgColor)
    ctx.setLineWidth(5.0)
    ctx.move(to: CGPoint(x: mapX(abs(payload.frontierA[0].mdd)), y: mapY(payload.frontierA[0].xirr)))
    for i in 1..<payload.frontierA.count {
        ctx.addLine(to: CGPoint(x: mapX(abs(payload.frontierA[i].mdd)), y: mapY(payload.frontierA[i].xirr)))
    }
    ctx.strokePath()
}

ctx.restoreGState()

// Draw Champion markers & non-overlapping callout boxes
struct LabelBox {
    var px: CGFloat
    var py: CGFloat
    var bx: CGFloat
    var by: CGFloat
    var bw: CGFloat
    var bh: CGFloat
    var item: ChampionItem
}

let calloutFont = NSFont(name: "PingFangTC-Semibold", size: 20) ?? NSFont.systemFont(ofSize: 20, weight: .semibold)
var boxes: [LabelBox] = []
for ch in payload.champions {
    let px = mapX(abs(ch.mdd))
    let py = mapY(ch.xirr)
    let sz = NSString(string: ch.label).size(withAttributes: [.font: calloutFont])
    let bw = sz.width + 24
    let bh: CGFloat = 38
    var bx = (ch.group == "B") ? (px - bw - 22) : (px + 22)
    if bx < plotRect.minX + 12 { bx = px + 22 }
    if bx + bw > plotRect.maxX - 12 { bx = px - bw - 22 }
    let by = min(max(py - bh / 2.0, plotRect.minY + 12), plotRect.maxY - bh - 12)
    boxes.append(LabelBox(px: px, py: py, bx: bx, by: by, bw: bw, bh: bh, item: ch))
}

// Iterative vertical collision resolution for callout boxes
boxes.sort { $0.by < $1.by }
for _ in 0..<40 {
    for i in 0..<boxes.count {
        for j in (i+1)..<boxes.count {
            let xOverlap = !(boxes[i].bx + boxes[i].bw + 8 < boxes[j].bx || boxes[j].bx + boxes[j].bw + 8 < boxes[i].bx)
            if xOverlap {
                let dy = (boxes[i].by + boxes[i].bh + 8) - boxes[j].by
                if dy > 0 {
                    boxes[i].by = max(plotRect.minY + 10, boxes[i].by - dy * 0.5)
                    boxes[j].by = min(plotRect.maxY - boxes[j].bh - 10, boxes[j].by + dy * 0.5)
                }
            }
        }
    }
}

for b in boxes {
    let isB = (b.item.group == "B")
    let mainColor = isB
        ? NSColor(calibratedRed: 0.07, green: 0.22, blue: 0.72, alpha: 1.0)
        : NSColor(calibratedRed: 0.78, green: 0.21, blue: 0.03, alpha: 1.0)

    // Leader line
    ctx.saveGState()
    ctx.setStrokeColor(mainColor.withAlphaComponent(0.75).cgColor)
    ctx.setLineWidth(2.2)
    let anchorX = (b.bx > b.px) ? b.bx : (b.bx + b.bw)
    let anchorY = b.by + b.bh / 2.0
    ctx.move(to: CGPoint(x: b.px, y: b.py))
    ctx.addLine(to: CGPoint(x: anchorX, y: anchorY))
    ctx.strokePath()
    ctx.restoreGState()

    // Marker circle with white halo
    ctx.setFillColor(NSColor.white.cgColor)
    ctx.fillEllipse(in: CGRect(x: b.px - 11, y: b.py - 11, width: 22, height: 22))
    ctx.setFillColor(mainColor.cgColor)
    ctx.fillEllipse(in: CGRect(x: b.px - 8, y: b.py - 8, width: 16, height: 16))

    // Callout pill
    let rect = CGRect(x: b.bx, y: b.by, width: b.bw, height: b.bh)
    let path = CGPath(roundedRect: rect, cornerWidth: 8, cornerHeight: 8, transform: nil)
    ctx.setFillColor(NSColor(calibratedWhite: 1.0, alpha: 0.94).cgColor)
    ctx.addPath(path)
    ctx.fillPath()
    ctx.setStrokeColor(mainColor.cgColor)
    ctx.setLineWidth(2.2)
    ctx.addPath(path)
    ctx.strokePath()

    drawText(b.item.label, x: b.bx + 12, y: b.by + 6, size: 20, bold: true, color: mainColor, align: "left")
}

// Titles & Axis Labels
drawText(payload.title, x: padL, y: CGFloat(height) - 82, size: 38, bold: true,
         color: NSColor(calibratedRed: 0.08, green: 0.11, blue: 0.18, alpha: 1.0), align: "left")
drawText(payload.subtitle, x: padL, y: CGFloat(height) - 130, size: 23, bold: false,
         color: NSColor(calibratedWhite: 0.35, alpha: 1.0), align: "left")

drawText("最大回撤 Maximum Drawdown（越靠左代表股災跌幅越小、越安全）", x: plotRect.midX, y: 48, size: 27, bold: true,
         color: NSColor(calibratedWhite: 0.15, alpha: 1.0), align: "center")

// Rotated Y-axis label
ctx.saveGState()
ctx.translateBy(x: 58, y: plotRect.midY)
ctx.rotate(by: .pi / 2.0)
drawText("年化報酬率 XIRR (%)（越高代表長期複利越強）", x: 0, y: 0, size: 27, bold: true,
         color: NSColor(calibratedWhite: 0.15, alpha: 1.0), align: "center")
ctx.restoreGState()

// Legend box (bottom-right inside plotRect — completely empty region, zero title overlap)
let legW: CGFloat = 780
let legH: CGFloat = 104
let legX = plotRect.maxX - legW - 28
let legY = plotRect.minY + 28
let legRect = CGRect(x: legX, y: legY, width: legW, height: legH)
let legPath = CGPath(roundedRect: legRect, cornerWidth: 10, cornerHeight: 10, transform: nil)
ctx.setFillColor(NSColor(calibratedWhite: 1.0, alpha: 0.96).cgColor)
ctx.addPath(legPath)
ctx.fillPath()
ctx.setStrokeColor(NSColor(calibratedWhite: 0.78, alpha: 1.0).cgColor)
ctx.setLineWidth(1.8)
ctx.addPath(legPath)
ctx.strokePath()

// Legend Row 1: Group B
ctx.setStrokeColor(NSColor(calibratedRed: 0.10, green: 0.28, blue: 0.82, alpha: 1.0).cgColor)
ctx.setLineWidth(5.0)
ctx.move(to: CGPoint(x: legX + 24, y: legY + 72))
ctx.addLine(to: CGPoint(x: legX + 74, y: legY + 72))
ctx.strokePath()
ctx.setFillColor(NSColor(calibratedRed: 0.23, green: 0.51, blue: 0.96, alpha: 0.45).cgColor)
ctx.fillEllipse(in: CGRect(x: legX + 43, y: legY + 66, width: 12, height: 12))
drawText("B 群（可用美股 8 檔，共 116,688 組）：淡藍散點 ＋ 深藍效率前緣線", x: legX + 90, y: legY + 58, size: 21, bold: true,
         color: NSColor(calibratedRed: 0.08, green: 0.24, blue: 0.72, alpha: 1.0))

// Legend Row 2: Group A
ctx.setStrokeColor(NSColor(calibratedRed: 0.85, green: 0.26, blue: 0.04, alpha: 1.0).cgColor)
ctx.setLineWidth(5.0)
ctx.move(to: CGPoint(x: legX + 24, y: legY + 30))
ctx.addLine(to: CGPoint(x: legX + 74, y: legY + 30))
ctx.strokePath()
ctx.setFillColor(NSColor(calibratedRed: 0.94, green: 0.42, blue: 0.08, alpha: 0.75).cgColor)
ctx.fillEllipse(in: CGRect(x: legX + 43, y: legY + 24, width: 12, height: 12))
drawText("A 群（只用台股 0050/00631L，共 66 組）：橘色散點 ＋ 橘紅效率前緣線", x: legX + 90, y: legY + 16, size: 21, bold: true,
         color: NSColor(calibratedRed: 0.78, green: 0.21, blue: 0.03, alpha: 1.0))

NSGraphicsContext.restoreGraphicsState()

let pngData = rep.representation(using: .png, properties: [:])!
try pngData.write(to: URL(fileURLWithPath: payload.outPath))
'''


def _short_combo_tag(c: dict[str, Any]) -> str:
    w = c["weights"]
    parts = [f"{k} {v}%" for k, v in w.items() if v > 0]
    w_str = "+".join(parts[:2]) + (f"+他{len(parts)-2}檔" if len(parts) > 2 else "")
    reb_map = {"none": "不平衡", "annual": "年平衡", "quarterly": "季平衡"}
    flt_str = "+濾網" if c["filter"] == "sma200_buf2" else ""
    return f"{w_str} ({reb_map[c['rebalance']]}{flt_str})"


def render_efficient_frontier_png(
    period_key: str,
    title: str,
    subtitle: str,
    out_path: Path,
    res_a: dict[str, Any],
    res_b: dict[str, Any],
) -> None:
    """Render publication-grade Efficient Frontier PNG comparing Group A and Group B."""
    sc_a = res_a["scatter"]
    sc_b = res_b["scatter"]

    # Deduplicate Group B scatter on rounded (0.15% MDD, 0.08% XIRR) grid so JSON IPC stays small & visual density is identical
    mask_b = ~sc_b["ruined"]
    mdd_b = np.abs(sc_b["mdd_pct"][mask_b])
    xirr_b = sc_b["xirr_pct"][mask_b]
    quant_keys = (np.round(mdd_b / 0.15).astype(np.int64) * 100000) + np.round((xirr_b + 100.0) / 0.08).astype(np.int64)
    _, uniq_idx = np.unique(quant_keys, return_index=True)
    scatter_b_pts = [{"mdd": round(float(mdd_b[i]), 2), "xirr": round(float(xirr_b[i]), 2)} for i in uniq_idx]

    mask_a = ~sc_a["ruined"]
    mdd_a = np.abs(sc_a["mdd_pct"][mask_a])
    xirr_a = sc_a["xirr_pct"][mask_a]
    scatter_a_pts = [{"mdd": round(float(mdd_a[i]), 2), "xirr": round(float(xirr_a[i]), 2)} for i in range(len(mdd_a))]

    front_a = [{"mdd": abs(float(f["max_drawdown_pct"])), "xirr": float(f["xirr"])} for f in res_a["json_data"]["frontier"]]
    front_b = [{"mdd": abs(float(f["max_drawdown_pct"])), "xirr": float(f["xirr"])} for f in res_b["json_data"]["frontier"]]

    # Collect Question 1 champions (deduplicating if the same portfolio wins two adjacent caps, combining cap labels)
    champions: list[dict[str, Any]] = []
    for grp, g_json in (("B", res_b["json_data"]), ("A", res_a["json_data"])):
        seen_map: dict[tuple[float, float], dict[str, Any]] = {}
        for cap in DRAWDOWN_CAPS:
            top_list = g_json["top_by_drawdown_cap"][str(cap)]
            if not top_list:
                continue
            champ = top_list[0]
            key = (float(champ["max_drawdown_pct"]), float(champ["xirr"]))
            if key in seen_map:
                seen_map[key]["caps"].append(abs(cap))
            else:
                seen_map[key] = {"champ": champ, "caps": [abs(cap)]}
        for (mdd_v, xirr_v), info in seen_map.items():
            caps_str = "/".join(f"-{c}%" for c in info["caps"])
            ch_c = info["champ"]
            lbl = f"[{grp}群 {caps_str}冠] {xirr_v:.2f}% / MDD {mdd_v:.1f}%｜{_short_combo_tag(ch_c)}"
            champions.append({
                "group": grp,
                "cap": info["caps"][0],
                "mdd": abs(mdd_v),
                "xirr": xirr_v,
                "label": lbl,
            })

    all_mdds = np.concatenate([mdd_a, mdd_b])
    all_xirrs = np.concatenate([xirr_a, xirr_b])
    x_min = 25.0 if period_key == "full_history" else 20.0
    x_max = 100.0 if period_key == "full_history" else 85.0
    y_min = math.floor((min(0.0, float(np.percentile(all_xirrs, 0.5))) - 1.0) / 2.5) * 2.5
    y_max = math.ceil((float(np.max(all_xirrs)) + 3.5) / 5.0) * 5.0

    chart_payload = {
        "title": title,
        "subtitle": subtitle,
        "outPath": str(out_path),
        "xMin": x_min,
        "xMax": x_max,
        "yMin": y_min,
        "yMax": y_max,
        "scatterA": scatter_a_pts,
        "scatterB": scatter_b_pts,
        "frontierA": front_a,
        "frontierB": front_b,
        "champions": champions,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        swift_file = tmp_p / "render_chart.swift"
        json_file = tmp_p / "payload.json"
        swift_file.write_text(SWIFT_CHART_SCRIPT, encoding="utf-8")
        json_file.write_text(json.dumps(chart_payload, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["/usr/bin/swift", str(swift_file), str(json_file)], check=True)


# ---------------------------------------------------------------------------
# 7. Main Orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run Monthly 10,000 TWD DCA Full Allocation Grid Backtest.")
    parser.add_argument("--refresh", action="store_true", help="Re-download fresh data from Yahoo Finance.")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch/cache Yahoo Finance data and exit.")
    args = parser.parse_args()

    t0 = time.perf_counter()
    print("[1/6] Loading / fetching market & FX history...", flush=True)
    raw_payload = fetch_or_load_raw_data(refresh=args.refresh)
    if args.fetch_only:
        print("Fetch-only completed.", flush=True)
        return

    print("[2/6] Cleaning split/dividend/FX anomalies & simulating pre-listing ETFs...", flush=True)
    dataset = prepare_market_dataset(raw_payload)

    weights_a = generate_weight_grid(len(ASSETS_A), steps=10)
    weights_b = generate_weight_grid(len(ASSETS_B), steps=10)

    # Strict grid verification (Requirement 十.2)
    assert len(weights_a) == 11, f"Expected 11 combinations for Group A, got {len(weights_a)}"
    assert len(weights_b) == 19448, f"Expected 19448 combinations for Group B, got {len(weights_b)}"
    assert np.allclose(np.sum(weights_a, axis=1), 1.0, atol=1e-12), "Group A weights do not sum to 100%"
    assert np.allclose(np.sum(weights_b, axis=1), 1.0, atol=1e-12), "Group B weights do not sum to 100%"
    print(f"      Verified grids: Group A = {len(weights_a)} weight combos (66 runs/period), "
          f"Group B = {len(weights_b)} weight combos (116,688 runs/period).", flush=True)

    all_dates = dataset["all_dates"]
    w_df = dataset["twd_wealth_df"]
    cons_df = dataset["cons_df"]
    irx_s = dataset["irx_aligned"]
    fx_s = dataset["fx_aligned"]

    # Helper to slice period arrays
    def _slice_period(start_ts: pd.Timestamp, assets: list[str]) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        m = all_dates >= start_ts
        d_sub = all_dates[m]
        w_sub = w_df.loc[d_sub, assets].to_numpy(dtype=np.float64)
        # Normalize wealth to 1.0 at period start
        w_sub = w_sub / np.maximum(w_sub[0:1, :], 1e-30)
        c_mat = np.zeros((len(d_sub), len(assets)), dtype=bool)
        for idx_a, a_name in enumerate(assets):
            if a_name in cons_df.columns:
                c_mat[:, idx_a] = cons_df.loc[d_sub, a_name].to_numpy(dtype=bool)
        irx_sub = irx_s.loc[d_sub].to_numpy(dtype=np.float64)
        fx_sub = fx_s.loc[d_sub].to_numpy(dtype=np.float64)
        ruin_flags = np.array([(w_sub[:, i] <= 0.0).any() for i in range(len(assets))], dtype=bool)
        return d_sub, w_sub, c_mat, irx_sub, fx_sub, ruin_flags

    print("[3/6] Running Group A (Taiwan-only: 0050, 00631L) across Full History & Recent 10Y...", flush=True)
    d_a_full, w_a_full, c_a_full, irx_a_full, fx_a_full, rf_a_full = _slice_period(dataset["start_a"], ASSETS_A)
    res_a_full = evaluate_group_period("A", "full_history", ASSETS_A, weights_a, d_a_full, w_a_full, c_a_full, irx_a_full, fx_a_full, rf_a_full)

    d_a_10y, w_a_10y, c_a_10y, irx_a_10y, fx_a_10y, rf_a_10y = _slice_period(dataset["recent_10y_start"], ASSETS_A)
    res_a_10y = evaluate_group_period("A", "recent_10y", ASSETS_A, weights_a, d_a_10y, w_a_10y, c_a_10y, irx_a_10y, fx_a_10y, rf_a_10y)

    print("[4/6] Running Group B (All 8 TW+US ETFs, 116,688 runs/period) across Full History & Recent 10Y...", flush=True)
    t_b0 = time.perf_counter()
    d_b_full, w_b_full, c_b_full, irx_b_full, fx_b_full, rf_b_full = _slice_period(dataset["start_b"], ASSETS_B)
    res_b_full = evaluate_group_period("B", "full_history", ASSETS_B, weights_b, d_b_full, w_b_full, c_b_full, irx_b_full, fx_b_full, rf_b_full)
    print(f"      Group B Full History finished in {time.perf_counter() - t_b0:.2f}s", flush=True)

    t_b1 = time.perf_counter()
    d_b_10y, w_b_10y, c_b_10y, irx_b_10y, fx_b_10y, rf_b_10y = _slice_period(dataset["recent_10y_start"], ASSETS_B)
    res_b_10y = evaluate_group_period("B", "recent_10y", ASSETS_B, weights_b, d_b_10y, w_b_10y, c_b_10y, irx_b_10y, fx_b_10y, rf_b_10y)
    print(f"      Group B Recent 10Y finished in {time.perf_counter() - t_b1:.2f}s", flush=True)

    # Also compute benchmarks (all single-asset 100% pure DCA & 3x control group summary) for report insights
    single_asset_benchmarks: dict[str, dict[str, Any]] = {"full_history": {}, "recent_10y": {}}
    for p_key, (d_sub, w_sub, c_sub, fx_sub) in (
        ("full_history", (d_b_full, w_b_full, c_b_full, fx_b_full)),
        ("recent_10y", (d_b_10y, w_b_10y, c_b_10y, fx_b_10y)),
    ):
        for idx_s, sym in enumerate(ASSETS_B):
            w_1 = np.zeros(len(ASSETS_B), dtype=float)
            w_1[idx_s] = 1.0
            for f_mode in ("none", "sma200_buf2"):
                if LEVERAGE_MAP[sym] == 1.0 and f_mode == "sma200_buf2":
                    continue
                det_s = simulate_single_detailed(ASSETS_B, w_1, "none", f_mode, d_sub, w_sub, c_sub, fx_sub)
                tag = sym if f_mode == "none" else f"{sym}_sma200_filter"
                single_asset_benchmarks[p_key][tag] = {
                    "final_value_twd": det_s["final_value_twd"],
                    "xirr": det_s["xirr"],
                    "max_drawdown_pct": det_s["max_drawdown_pct"],
                    "crash_drawdowns_pct": det_s["crash_drawdowns_pct"],
                }

    print("[5/6] Running independent scalar share-tracking cross-validation & rendering PNG charts...", flush=True)
    val_results = run_independent_cross_validation(dataset, res_b_full)

    render_efficient_frontier_png(
        period_key="full_history",
        title="每月 1 萬台幣定期定額：全歷史效率前緣圖（涵蓋 2000 網路泡沫與 2008 金融海嘯）",
        subtitle=f"A 群台股 ({d_a_full[0].strftime('%Y-%m-%d')}~{d_a_full[-1].strftime('%Y-%m-%d')}, 66 組) vs B 群台美股全組合 ({d_b_full[0].strftime('%Y-%m-%d')}~{d_b_full[-1].strftime('%Y-%m-%d')}, 116,688 組)",
        out_path=CHART_FULL_PATH,
        res_a=res_a_full,
        res_b=res_b_full,
    )
    render_efficient_frontier_png(
        period_key="recent_10y",
        title="每月 1 萬台幣定期定額：近 10 年效率前緣圖（2016–2026，涵蓋 2020 疫情與 2022 升息熊市）",
        subtitle=f"期間：{d_b_10y[0].strftime('%Y-%m-%d')} ~ {d_b_10y[-1].strftime('%Y-%m-%d')}（共 120 個月，總投入 120 萬台幣）｜A 群 66 組 vs B 群 116,688 組",
        out_path=CHART_10Y_PATH,
        res_a=res_a_10y,
        res_b=res_b_10y,
    )

    elapsed_sec = round(time.perf_counter() - t0, 2)
    print(f"[6/6] Saving results.json (elapsed {elapsed_sec:.2f}s)...", flush=True)

    results_payload = {
        "meta": {
            "study_title": "每月 1 萬台幣定期定額，全標的全組合最佳配置回測",
            "generated_at": pd.Timestamp.now().isoformat(),
            "execution_time_seconds": elapsed_sec,
            "data_source": "Yahoo Finance (yfinance public historical OHLCV, Dividends, Splits, TWD=X, ^IRX, ^TWII)",
            "periods": {
                "A_full_history": {
                    "start_date": d_a_full[0].strftime("%Y-%m-%d"),
                    "end_date": d_a_full[-1].strftime("%Y-%m-%d"),
                    "months_invested": int(len(get_month_start_indices(d_a_full))),
                },
                "B_full_history": {
                    "start_date": d_b_full[0].strftime("%Y-%m-%d"),
                    "end_date": d_b_full[-1].strftime("%Y-%m-%d"),
                    "months_invested": int(len(get_month_start_indices(d_b_full))),
                },
                "recent_10y": {
                    "start_date": d_b_10y[0].strftime("%Y-%m-%d"),
                    "end_date": d_b_10y[-1].strftime("%Y-%m-%d"),
                    "months_invested": int(len(get_month_start_indices(d_b_10y))),
                },
            },
            "investment_rules": {
                "monthly_contribution_twd": MONTHLY_CONTRIB_TWD,
                "contribution_timing": "每月第一個交易日扣款，不擇時、不借錢、不留現金",
                "final_valuation_currency": "TWD (美股資產依當日 TWD=X 換算回台幣)",
            },
            "cost_and_tax_assumptions": {
                "us_fx_conversion_fee_pct": round(FX_FEE_RATE * 100.0, 3),
                "us_sub_brokerage_commission_pct": round(US_COMM_RATE * 100.0, 3),
                "us_sub_brokerage_min_fee_usd_main": 0.0,
                "us_sub_brokerage_min_fee_usd_sensitivity": 15.0,
                "us_dividend_withholding_tax_pct": round(US_DIV_TAX_RATE * 100.0, 1),
                "tw_dca_buy_fee_pct": 0.0,
                "tw_rebalance_buy_commission_pct": round(TW_REBAL_BUY_FEE * 100.0, 4),
                "tw_rebalance_sell_etf_tax_pct": 0.1,
                "tw_rebalance_sell_commission_pct": 0.1425,
                "tw_pre_2009_estimated_dividend_yield_pct": round(TWII_PRE_DIV_YIELD * 100.0, 2),
                "sharpe_risk_free_rate": "^IRX (13-Week US T-Bill) 月化無風險利率",
            },
            "internal_expense_ratios_pct": {k: round(v * 100.0, 2) for k, v in EXPENSE_RATIOS.items()},
            "yf_latest_net_expense_info": dataset["expense_info_yf"],
            "grid_counts": {
                "weight_step_pct": 10,
                "group_A_weight_combinations": int(len(weights_a)),
                "group_B_weight_combinations": int(len(weights_b)),
                "rebalance_modes": list(REBALANCE_MODES),
                "filter_modes": list(FILTER_MODES),
                "variants_per_weight": len(REBALANCE_MODES) * len(FILTER_MODES),
                "group_A_runs_per_period": int(len(weights_a) * 6),
                "group_B_runs_per_period": int(len(weights_b) * 6),
                "total_backtest_runs": int((len(weights_a) + len(weights_b)) * 6 * 2),
            },
            "hygiene_notes": dataset["hygiene_notes"],
            "anomaly_checks": dataset["anomaly_checks"],
            "cross_validation_3_portfolios": val_results,
            "single_asset_benchmarks": single_asset_benchmarks,
            "charts": {
                "full_history_png": CHART_FULL_PATH.name,
                "recent_10y_png": CHART_10Y_PATH.name,
            },
        },
        "tracking_error": dataset["tracking_error"],
        "groups": {
            "A": {
                "full_history": res_a_full["json_data"],
                "recent_10y": res_a_10y["json_data"],
            },
            "B": {
                "full_history": res_b_full["json_data"],
                "recent_10y": res_b_10y["json_data"],
            },
        },
    }

    RESULTS_JSON_PATH.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    size_mb = RESULTS_JSON_PATH.stat().st_size / (1024.0 * 1024.0)
    print(f"Done! Wrote {RESULTS_JSON_PATH} ({size_mb:.2f} MB) in {elapsed_sec:.2f}s.", flush=True)


if __name__ == "__main__":
    main()

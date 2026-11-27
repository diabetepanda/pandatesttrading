#!/usr/bin/env python3
"""Fetch missing Yahoo Finance tickers and Robert Shiller public dataset into research/final_check/cache/."""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

YF_EXTRA_CACHE = CACHE_DIR / "final_check_yf.pkl"
SHILLER_XLS_PATH = CACHE_DIR / "ie_data.xls"
SHILLER_CSV_PATH = CACHE_DIR / "shiller_monthly.csv"
SHILLER_META_PATH = CACHE_DIR / "shiller_meta.json"

EXTRA_SYMBOLS = [
    "CSPX.L",
    "VOO",
    "SHY",
    "VGIT",
    "IEF",
    "^TNX",
    "^FVX",
    "006208.TW",
]

SHILLER_URLS = [
    "https://shillerdata.com/",
    "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
    "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv",
]


def fetch_yf_extra(refresh: bool = False) -> dict[str, Any]:
    if YF_EXTRA_CACHE.exists() and not refresh:
        return pickle.loads(YF_EXTRA_CACHE.read_bytes())

    histories: dict[str, pd.DataFrame] = {}
    meta: dict[str, Any] = {}
    for sym in EXTRA_SYMBOLS:
        print(f"[YF] Downloading {sym}...", flush=True)
        time.sleep(0.5)
        tkr = yf.Ticker(sym)
        df = tkr.history(period="max", auto_adjust=False, actions=True)
        if df is not None and not df.empty:
            df = df.dropna(subset=["Close"]).copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
        histories[sym] = df
        try:
            info = tkr.info or {}
            meta[sym] = {
                "currency": info.get("currency"),
                "longName": info.get("longName") or info.get("shortName"),
                "annualReportExpenseRatio": info.get("annualReportExpenseRatio"),
                "netExpenseRatio": info.get("netExpenseRatio"),
                "fundInceptionDate": info.get("fundInceptionDate"),
                "exchange": info.get("exchange"),
            }
        except Exception as e:
            meta[sym] = {"error": str(e)}
        print(
            f"  -> {sym}: rows={0 if df is None else len(df)}, "
            f"range={df.index[0].strftime('%Y-%m-%d') if df is not None and len(df) else 'EMPTY'}.."
            f"{df.index[-1].strftime('%Y-%m-%d') if df is not None and len(df) else 'EMPTY'}, "
            f"currency={meta[sym].get('currency')}",
            flush=True,
        )

    payload = {
        "fetched_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "histories": histories,
        "meta": meta,
    }
    YF_EXTRA_CACHE.write_bytes(pickle.dumps(payload))
    return payload


def fetch_shiller(refresh: bool = False) -> dict[str, Any]:
    if SHILLER_XLS_PATH.exists() and SHILLER_META_PATH.exists() and not refresh:
        return json.loads(SHILLER_META_PATH.read_text(encoding="utf-8"))

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    chosen_url = None

    # 1) Try official Yale URL directly
    yale_url = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"
    try:
        r = requests.get(yale_url, headers=headers, timeout=30)
        if r.status_code == 200 and len(r.content) > 500_000 and r.content[:4] == b"\xd0\xcf\x11\xe0":
            SHILLER_XLS_PATH.write_bytes(r.content)
            chosen_url = yale_url
            print(f"[Shiller] Downloaded ie_data.xls from {yale_url} ({len(r.content)} bytes)", flush=True)
    except Exception as e:
        print(f"[Shiller] Yale URL failed: {e}", flush=True)

    # 2) Try shillerdata.com page to find current ie_data.xls link
    if chosen_url is None:
        try:
            r_page = requests.get("https://shillerdata.com/", headers=headers, timeout=20)
            if r_page.status_code == 200:
                import re
                m = re.search(r'(?:https?:)?//[^\s"\'<>]+ie_data\.xls[^\s"\'<>]*', r_page.text)
                if m:
                    u = m.group(0)
                    if u.startswith("//"):
                        u = "https:" + u
                    r2 = requests.get(u, headers=headers, timeout=30)
                    if r2.status_code == 200 and len(r2.content) > 500_000:
                        SHILLER_XLS_PATH.write_bytes(r2.content)
                        chosen_url = u
                        print(f"[Shiller] Downloaded ie_data.xls from {u} ({len(r2.content)} bytes)", flush=True)
        except Exception as e:
            print(f"[Shiller] shillerdata.com failed: {e}", flush=True)

    # 3) Fallback CSV mirror (datasets/s-and-p-500 on GitHub derived directly from Shiller ie_data.xls)
    csv_url = "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"
    csv_ok = False
    try:
        r_csv = requests.get(csv_url, headers=headers, timeout=20)
        if r_csv.status_code == 200 and len(r_csv.content) > 50_000 and "SP500" in r_csv.text[:200]:
            SHILLER_CSV_PATH.write_bytes(r_csv.content)
            csv_ok = True
            print(f"[Shiller] Downloaded CSV mirror from {csv_url} ({len(r_csv.content)} bytes)", flush=True)
    except Exception as e:
        print(f"[Shiller] CSV mirror failed: {e}", flush=True)

    meta = {
        "fetched_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "xls_url": chosen_url,
        "xls_exists": SHILLER_XLS_PATH.exists(),
        "csv_url": csv_url if csv_ok else None,
        "csv_exists": SHILLER_CSV_PATH.exists(),
    }
    SHILLER_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    fetch_yf_extra(refresh=False)
    fetch_shiller(refresh=False)

#!/usr/bin/env python3
"""Polite public data fetcher for Institutional / Large-Trader Flow Signals Research.

Rules enforced:
1. Keyless public data sources only (no API keys, tokens, or secrets).
2. Polite rate limiting: >= 1.05s sleep between every HTTP request, max 3 retries on error.
3. All raw data saved under `research/flow_signals/cache/` (excluded from git via .git/info/exclude).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import curl_cffi.requests as cf_requests
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_LAST_REQ_TS = 0.0
MIN_INTERVAL_SEC = 1.05
MAX_RETRIES = 3


def polite_request(
    method: str,
    url: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    use_cf: bool = False,
    timeout: int = 25,
) -> Any:
    """Perform HTTP request with >=1s interval and at most 3 retries."""
    global _LAST_REQ_TS
    elapsed = time.time() - _LAST_REQ_TS
    if elapsed < MIN_INTERVAL_SEC:
        time.sleep(MIN_INTERVAL_SEC - elapsed)

    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _LAST_REQ_TS = time.time()
        try:
            if use_cf:
                resp = cf_requests.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=hdrs,
                    impersonate="chrome110",
                    timeout=timeout,
                )
            else:
                resp = requests.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=hdrs,
                    timeout=timeout,
                )
            if resp.status_code == 200:
                return resp
            last_err = RuntimeError(f"HTTP {resp.status_code} from {url}")
        except Exception as exc:
            last_err = exc

        if attempt < MAX_RETRIES:
            time.sleep(1.5 * attempt)

    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts for {url}: {last_err}")


def fetch_yfinance_prices(refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Fetch daily OHLCV via yfinance for Taiwan, US Futures/Indices, and Crypto."""
    out_path = CACHE_DIR / "prices_daily.json"
    if out_path.exists() and not refresh:
        print("[CACHE] Loading prices_daily.json")
        return json.loads(out_path.read_text(encoding="utf-8"))

    tickers = {
        "TWII": ("^TWII", "2014-06-01"),
        "2330.TW": ("2330.TW", "2014-06-01"),
        "SP500": ("^GSPC", "2009-01-01"),
        "NDX": ("^NDX", "2009-01-01"),
        "GOLD": ("GC=F", "2009-01-01"),
        "WTI": ("CL=F", "2009-01-01"),
        "BTC": ("BTC-USD", "2014-01-01"),
        "ETH": ("ETH-USD", "2017-01-01"),
    }
    result: dict[str, dict[str, Any]] = {}
    for key, (yf_sym, start_d) in tickers.items():
        time.sleep(MIN_INTERVAL_SEC)
        print(f"[YF] Downloading {key} ({yf_sym}) from {start_d}...")
        df = yf.download(yf_sym, start=start_d, auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"]).sort_index()
        rows = []
        for idx, r in df.iterrows():
            d_str = pd.Timestamp(idx).strftime("%Y-%m-%d")
            rows.append(
                {
                    "date": d_str,
                    "open": float(r["Open"]),
                    "high": float(r["High"]),
                    "low": float(r["Low"]),
                    "close": float(r["Close"]),
                    "volume": float(r["Volume"]) if pd.notna(r["Volume"]) else 0.0,
                }
            )
        result[key] = {
            "symbol": yf_sym,
            "count": len(rows),
            "start": rows[0]["date"] if rows else None,
            "end": rows[-1]["date"] if rows else None,
            "rows": rows,
        }
        print(f"  -> {key}: {len(rows)} bars ({result[key]['start']} ~ {result[key]['end']})")

    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _fetch_finmind_chunks(
    dataset: str,
    chunks: list[tuple[str, str]],
    data_id: str | None = None,
) -> list[dict[str, Any]]:
    url = "https://api.finmindtrade.com/api/v4/data"
    all_rows: list[dict[str, Any]] = []
    for s_d, e_d in chunks:
        params: dict[str, Any] = {
            "dataset": dataset,
            "start_date": s_d,
            "end_date": e_d,
        }
        if data_id:
            params["data_id"] = data_id
        resp = polite_request("GET", url, params=params)
        j = resp.json()
        rows = j.get("data") or []
        print(f"  [FinMind] {dataset} ({data_id or 'ALL'}) {s_d}~{e_d}: {len(rows)} rows")
        all_rows.extend(rows)
    return all_rows


def fetch_taiwan_data(refresh: bool = False) -> dict[str, Any]:
    """Fetch A1 (BFI82U total inst), A2 (TAIFEX TXF foreign net OI), A3 (MI_MARGN margin), A4 (2330 T86 foreign)."""
    out_path = CACHE_DIR / "taiwan_flow.json"
    if out_path.exists() and not refresh:
        print("[CACHE] Loading taiwan_flow.json")
        return json.loads(out_path.read_text(encoding="utf-8"))

    chunks_2014_2026 = [
        ("2014-09-01", "2017-12-31"),
        ("2018-01-01", "2020-12-31"),
        ("2021-01-01", "2023-12-31"),
        ("2024-01-01", "2026-12-31"),
    ]

    print("[TW-A1] Fetching Total Institutional Investors (BFI82U)...")
    a1_raw = _fetch_finmind_chunks("TaiwanStockTotalInstitutionalInvestors", chunks_2014_2026)
    a1_by_date: dict[str, dict[str, float]] = {}
    for r in a1_raw:
        d = r.get("date")
        name = r.get("name")
        if not d or not name:
            continue
        net = float(r.get("buy", 0) or 0) - float(r.get("sell", 0) or 0)
        rec = a1_by_date.setdefault(
            d,
            {"foreign": 0.0, "trust": 0.0, "dealer": 0.0, "total": 0.0},
        )
        if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
            rec["foreign"] += net
        elif name == "Investment_Trust":
            rec["trust"] += net
        elif name in ("Dealer_self", "Dealer_Hedging"):
            rec["dealer"] += net
        elif name == "total":
            rec["total"] = net
    a1_rows = [{"date": d, **vals} for d, vals in sorted(a1_by_date.items())]

    # Official TWSE BFI82U sample check
    twse_verify = {}
    try:
        r_bfi = polite_request(
            "GET",
            "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
            params={"type": "day", "dayDate": "20241115", "response": "json"},
        )
        twse_verify["BFI82U_20241115"] = r_bfi.json().get("data", [])
    except Exception as exc:
        twse_verify["BFI82U_err"] = str(exc)

    print("[TW-A2] Fetching Foreign TXF Net Open Interest (TAIFEX + FinMind)...")
    a2_by_date: dict[str, dict[str, Any]] = {}
    # 1) FinMind from 2018-01-01 to 2026-12-31
    chunks_fut = [
        ("2018-01-01", "2020-12-31"),
        ("2021-01-01", "2023-12-31"),
        ("2024-01-01", "2026-12-31"),
    ]
    a2_fm = _fetch_finmind_chunks("TaiwanFuturesInstitutionalInvestors", chunks_fut, data_id="TX")
    for r in a2_fm:
        inv = str(r.get("institutional_investors") or "")
        if "外資" not in inv:
            continue
        d = r.get("date")
        if not d:
            continue
        long_oi = int(r.get("long_open_interest_balance_volume", 0) or 0)
        short_oi = int(r.get("short_open_interest_balance_volume", 0) or 0)
        net_oi = long_oi - short_oi
        a2_by_date[d] = {
            "date": d,
            "long_oi": long_oi,
            "short_oi": short_oi,
            "net_oi": net_oi,
            "source": "FinMind_TAIFEX",
        }

    # 2) Direct TAIFEX official CSV download for recent years (2023-2026)
    for yr in (2023, 2024, 2025, 2026):
        try:
            resp = polite_request(
                "POST",
                "https://www.taifex.com.tw/cht/3/futContractsDateDown",
                data={
                    "queryStartDate": f"{yr}/01/01",
                    "queryEndDate": f"{yr}/12/31",
                    "commodityId": "TXF",
                },
            )
            txt = resp.content.decode("cp950", errors="replace")
            reader = csv.reader(io.StringIO(txt))
            cnt_yr = 0
            for row in reader:
                if len(row) < 15:
                    continue
                if row[1].strip() == "臺股期貨" and "外資" in row[2].strip():
                    raw_d = row[0].strip().replace("/", "-")
                    parts = raw_d.split("-")
                    if len(parts) == 3 and len(parts[0]) == 4:
                        d_iso = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                        long_oi = int(row[9].strip().replace(",", ""))
                        short_oi = int(row[11].strip().replace(",", ""))
                        net_oi = int(row[13].strip().replace(",", ""))
                        a2_by_date[d_iso] = {
                            "date": d_iso,
                            "long_oi": long_oi,
                            "short_oi": short_oi,
                            "net_oi": net_oi,
                            "source": "TAIFEX_Direct",
                        }
                        cnt_yr += 1
            print(f"  [TAIFEX Direct] {yr}: {cnt_yr} foreign TXF rows")
        except Exception as exc:
            print(f"  [TAIFEX Direct] {yr} warning: {exc}")

    a2_rows = [vals for _, vals in sorted(a2_by_date.items())]

    print("[TW-A3] Fetching Margin Purchase Balance (MI_MARGN)...")
    a3_raw = _fetch_finmind_chunks("TaiwanStockTotalMarginPurchaseShortSale", chunks_2014_2026)
    a3_by_date: dict[str, dict[str, float]] = {}
    for r in a3_raw:
        d = r.get("date")
        name = r.get("name")
        if not d or not name:
            continue
        rec = a3_by_date.setdefault(d, {})
        if name == "MarginPurchaseMoney":
            # TodayBalance is in NTD (元); convert to 億元 (1e8 NTD)
            rec["margin_money_100m"] = float(r.get("TodayBalance", 0) or 0) / 1e8
            rec["margin_money_prev_100m"] = float(r.get("YesBalance", 0) or 0) / 1e8
        elif name == "MarginPurchase":
            rec["margin_unit"] = float(r.get("TodayBalance", 0) or 0)
        elif name == "ShortSale":
            rec["shortsale_unit"] = float(r.get("TodayBalance", 0) or 0)
    a3_rows = [
        {"date": d, **vals}
        for d, vals in sorted(a3_by_date.items())
        if "margin_money_100m" in vals
    ]

    print("[TW-A4] Fetching 2330.TW Foreign Buy/Sell (T86)...")
    a4_raw = _fetch_finmind_chunks("TaiwanStockInstitutionalInvestorsBuySell", chunks_2014_2026, data_id="2330")
    a4_by_date: dict[str, dict[str, float]] = {}
    for r in a4_raw:
        d = r.get("date")
        name = r.get("name")
        if not d or not name:
            continue
        net_shares = float(r.get("buy", 0) or 0) - float(r.get("sell", 0) or 0)
        rec = a4_by_date.setdefault(d, {"foreign_net_shares": 0.0, "trust_net_shares": 0.0, "dealer_net_shares": 0.0})
        if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
            rec["foreign_net_shares"] += net_shares
        elif name == "Investment_Trust":
            rec["trust_net_shares"] += net_shares
        elif "Dealer" in name:
            rec["dealer_net_shares"] += net_shares
    a4_rows = [
        {
            "date": d,
            "foreign_net_lots": vals["foreign_net_shares"] / 1000.0,
            "trust_net_lots": vals["trust_net_shares"] / 1000.0,
            "dealer_net_lots": vals["dealer_net_shares"] / 1000.0,
        }
        for d, vals in sorted(a4_by_date.items())
    ]

    out = {
        "A1_total_institutional": {
            "count": len(a1_rows),
            "start": a1_rows[0]["date"] if a1_rows else None,
            "end": a1_rows[-1]["date"] if a1_rows else None,
            "rows": a1_rows,
        },
        "A2_txf_foreign_oi": {
            "count": len(a2_rows),
            "start": a2_rows[0]["date"] if a2_rows else None,
            "end": a2_rows[-1]["date"] if a2_rows else None,
            "rows": a2_rows,
        },
        "A3_margin_balance": {
            "count": len(a3_rows),
            "start": a3_rows[0]["date"] if a3_rows else None,
            "end": a3_rows[-1]["date"] if a3_rows else None,
            "rows": a3_rows,
        },
        "A4_2330_foreign": {
            "count": len(a4_rows),
            "start": a4_rows[0]["date"] if a4_rows else None,
            "end": a4_rows[-1]["date"] if a4_rows else None,
            "rows": a4_rows,
        },
        "twse_verify": twse_verify,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def fetch_cftc_cot_data(refresh: bool = False) -> dict[str, Any]:
    """Fetch CFTC COT history from official CFTC Public Reporting Socrata API (keyless)."""
    out_path = CACHE_DIR / "cftc_cot.json"
    if out_path.exists() and not refresh:
        print("[CACHE] Loading cftc_cot.json")
        return json.loads(out_path.read_text(encoding="utf-8"))

    # TFF: gpe5-46if (Leveraged Funds: lev_money_positions_long, lev_money_positions_short)
    # Disaggregated: 72hh-3qpy (Managed Money: m_money_positions_long_all, m_money_positions_short_all)
    specs = {
        "SP500": {
            "endpoint": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
            "report_type": "TFF (Leveraged Funds)",
            "code": "13874A",
            "long_col": "lev_money_positions_long",
            "short_col": "lev_money_positions_short",
            "price_key": "SP500",
            "start_query": "2006-01-01",
        },
        "NDX": {
            "endpoint": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
            "report_type": "TFF (Leveraged Funds)",
            "code": "209742",
            "long_col": "lev_money_positions_long",
            "short_col": "lev_money_positions_short",
            "price_key": "NDX",
            "start_query": "2006-01-01",
        },
        "BTC_CME": {
            "endpoint": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
            "report_type": "TFF (Leveraged Funds)",
            "code": "133741",
            "long_col": "lev_money_positions_long",
            "short_col": "lev_money_positions_short",
            "price_key": "BTC",
            "start_query": "2017-01-01",
        },
        "GOLD": {
            "endpoint": "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
            "report_type": "Disaggregated (Managed Money)",
            "code": "088691",
            "long_col": "m_money_positions_long_all",
            "short_col": "m_money_positions_short_all",
            "price_key": "GOLD",
            "start_query": "2006-01-01",
        },
        "WTI": {
            "endpoint": "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
            "report_type": "Disaggregated (Managed Money)",
            "code": "067651",
            "long_col": "m_money_positions_long_all",
            "short_col": "m_money_positions_short_all",
            "price_key": "WTI",
            "start_query": "2006-01-01",
        },
    }

    out: dict[str, Any] = {}
    for key, cfg in specs.items():
        print(f"[CFTC] Fetching {key} (code={cfg['code']}, {cfg['report_type']})...")
        params = {
            "$where": f"cftc_contract_market_code='{cfg['code']}' AND report_date_as_yyyy_mm_dd >= '{cfg['start_query']}'",
            "$order": "report_date_as_yyyy_mm_dd ASC",
            "$limit": 5000,
        }
        resp = polite_request("GET", cfg["endpoint"], params=params)
        raw_rows = resp.json()
        by_date: dict[str, dict[str, Any]] = {}
        for r in raw_rows:
            d_raw = str(r.get("report_date_as_yyyy_mm_dd") or "")[:10]
            if len(d_raw) != 10:
                continue
            try:
                long_pos = float(r.get(cfg["long_col"], 0) or 0)
                short_pos = float(r.get(cfg["short_col"], 0) or 0)
                oi = float(r.get("open_interest_all", 0) or 0)
                nonrept_long = float(r.get("nonrept_positions_long_all", 0) or 0)
                nonrept_short = float(r.get("nonrept_positions_short_all", 0) or 0)
            except (TypeError, ValueError):
                continue
            by_date[d_raw] = {
                "report_date": d_raw,
                "long": long_pos,
                "short": short_pos,
                "net": long_pos - short_pos,
                "oi": oi,
                "net_pct_oi": ((long_pos - short_pos) / oi * 100.0) if oi > 0 else 0.0,
                "nonrept_net": nonrept_long - nonrept_short,
            }
        rows = [v for _, v in sorted(by_date.items())]
        out[key] = {
            "report_type": cfg["report_type"],
            "cftc_code": cfg["code"],
            "price_key": cfg["price_key"],
            "count": len(rows),
            "start": rows[0]["report_date"] if rows else None,
            "end": rows[-1]["report_date"] if rows else None,
            "rows": rows,
        }
        print(f"  -> {key}: {len(rows)} weekly reports ({out[key]['start']} ~ {out[key]['end']})")

    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _paginate_okx_rubik(
    endpoint: str,
    base_params: dict[str, str],
    max_pages: int = 8,
    supports_pagination: bool = True,
) -> list[list[Any]]:
    url = "https://www.okx.com" + endpoint
    collected: dict[int, list[Any]] = {}
    cur_end: str | None = None
    pages = max_pages if supports_pagination else 1
    for page_idx in range(pages):
        params = dict(base_params)
        if supports_pagination:
            params["limit"] = "100"
            if cur_end is not None:
                params["end"] = cur_end
        try:
            resp = polite_request("GET", url, params=params)
        except Exception as exc:
            if page_idx == 0:
                print(f"    [WARN] {endpoint} {params} failed on page 1: {exc}")
            break
        j = resp.json()
        data = j.get("data") or []
        if not data:
            break
        added = 0
        oldest_ts: int | None = None
        for item in data:
            ts = int(item[0])
            if ts not in collected:
                collected[ts] = item
                added += 1
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
        if added == 0 or oldest_ts is None:
            break
        cur_end = str(oldest_ts - 1)
    return [collected[k] for k in sorted(collected.keys())]


def _paginate_okx_candles(
    inst_id: str,
    bar: str,
    max_pages: int = 8,
) -> list[dict[str, Any]]:
    url = "https://www.okx.com/api/v5/market/history-candles"
    collected: dict[int, dict[str, Any]] = {}
    cur_after: str | None = None
    for _ in range(max_pages):
        params = {"instId": inst_id, "bar": bar, "limit": "100"}
        if cur_after is not None:
            params["after"] = cur_after
        resp = polite_request("GET", url, params=params)
        data = resp.json().get("data") or []
        if not data:
            break
        added = 0
        oldest_ts: int | None = None
        for item in data:
            ts = int(item[0])
            if ts not in collected:
                collected[ts] = {
                    "ts": ts,
                    "dt_utc": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "vol": float(item[5]),
                }
                added += 1
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
        if added == 0 or oldest_ts is None:
            break
        cur_after = str(oldest_ts)
    return [collected[k] for k in sorted(collected.keys())]


def _paginate_okx_funding(
    inst_id: str,
    max_pages: int = 12,
) -> list[dict[str, Any]]:
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    collected: dict[int, dict[str, Any]] = {}
    cur_after: str | None = None
    for _ in range(max_pages):
        params = {"instId": inst_id, "limit": "100"}
        if cur_after is not None:
            params["after"] = cur_after
        resp = polite_request("GET", url, params=params)
        data = resp.json().get("data") or []
        if not data:
            break
        added = 0
        oldest_ts: int | None = None
        for item in data:
            ts = int(item["fundingTime"])
            if ts not in collected:
                collected[ts] = {
                    "ts": ts,
                    "dt_utc": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "funding_rate": float(item["fundingRate"]),
                }
                added += 1
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
        if added == 0 or oldest_ts is None:
            break
        cur_after = str(oldest_ts)
    return [collected[k] for k in sorted(collected.keys())]


def fetch_okx_crypto_data(refresh: bool = False) -> dict[str, Any]:
    """Fetch OKX Rubik flow indicators, funding rate history, open interest, and 1D/4H candles for BTC & ETH."""
    out_path = CACHE_DIR / "okx_crypto.json"
    if out_path.exists() and not refresh:
        print("[CACHE] Loading okx_crypto.json")
        return json.loads(out_path.read_text(encoding="utf-8"))

    out: dict[str, Any] = {}
    for ccy in ("BTC", "ETH"):
        inst_id = f"{ccy}-USDT-SWAP"
        print(f"[OKX] Fetching {ccy} ({inst_id})...")
        ccy_data: dict[str, Any] = {}

        # Candles 1D and 4H
        for bar, pages in (("1D", 10), ("4H", 10)):
            candles = _paginate_okx_candles(inst_id, bar=bar, max_pages=pages)
            ccy_data[f"candles_{bar}"] = candles
            print(
                f"  -> {ccy} candles_{bar}: {len(candles)} bars "
                f"({candles[0]['dt_utc'] if candles else None} ~ {candles[-1]['dt_utc'] if candles else None})"
            )

        # Funding rate history (8h settlements)
        funding = _paginate_okx_funding(inst_id, max_pages=12)
        ccy_data["funding_8h"] = funding
        print(
            f"  -> {ccy} funding_8h: {len(funding)} records "
            f"({funding[0]['dt_utc'] if funding else None} ~ {funding[-1]['dt_utc'] if funding else None})"
        )

        # Rubik endpoints for 1D and 4H
        rubik_specs = {
            "top_trader_acct_ls": (
                "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
                {"instId": inst_id},
                True,
                ("1D", "4H"),
            ),
            "top_trader_pos_ls": (
                "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
                {"instId": inst_id},
                True,
                ("1D", "4H"),
            ),
            "all_acct_ls": (
                "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                {"ccy": ccy},
                False,
                ("1D", "1H"),
            ),
            "taker_vol_contract": (
                "/api/v5/rubik/stat/taker-volume-contract",
                {"instId": inst_id},
                True,
                ("1D", "4H"),
            ),
            "oi_history": (
                "/api/v5/rubik/stat/contracts/open-interest-history",
                {"instId": inst_id},
                True,
                ("1D", "4H"),
            ),
            "oi_volume_ccy": (
                "/api/v5/rubik/stat/contracts/open-interest-volume",
                {"ccy": ccy},
                False,
                ("1D",),
            ),
        }

        for r_key, (ep, base_p, can_page, periods) in rubik_specs.items():
            for period in periods:
                p = dict(base_p)
                p["period"] = period
                rows = _paginate_okx_rubik(ep, p, max_pages=10, supports_pagination=can_page)
                k = f"{r_key}_{period}"
                ccy_data[k] = rows
                start_dt = (
                    datetime.fromtimestamp(int(rows[0][0]) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if rows
                    else None
                )
                end_dt = (
                    datetime.fromtimestamp(int(rows[-1][0]) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if rows
                    else None
                )
                print(f"  -> {ccy} {k}: {len(rows)} rows ({start_dt} ~ {end_dt})")

        out[ccy] = ccy_data

    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _parse_farside_num(val: str) -> float | None:
    s = str(val or "").strip().replace(",", "")
    if not s or s in ("-", "—", "N/A"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def fetch_farside_btc_etf(refresh: bool = False) -> dict[str, Any]:
    """Fetch US Spot Bitcoin ETF daily net inflows (US$ Millions) from Farside Investors public table."""
    out_path = CACHE_DIR / "btc_etf_farside.json"
    if out_path.exists() and not refresh:
        print("[CACHE] Loading btc_etf_farside.json")
        return json.loads(out_path.read_text(encoding="utf-8"))

    url = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
    print(f"[ETF] Fetching Farside BTC Spot ETF flow table from {url}...")
    resp = polite_request("GET", url, use_cf=True)
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    target_table = None
    for t in tables:
        if len(t.find_all("tr")) > 50:
            target_table = t
            break

    if target_table is None:
        raise RuntimeError("Could not find main ETF flow table on Farside page")

    rows_out: list[dict[str, Any]] = []
    headers: list[str] = []
    for tr in target_table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        if not headers and ("Date" in cells[0] or "IBIT" in cells):
            headers = cells
            continue
        if len(cells) < 5:
            continue
        raw_d = cells[0]
        if not re.match(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$", raw_d):
            continue
        dt = datetime.strptime(raw_d, "%d %b %Y")
        d_iso = dt.strftime("%Y-%m-%d")
        total_val = _parse_farside_num(cells[-1])
        ibit_val = _parse_farside_num(cells[1]) if len(cells) > 1 else None
        fbtc_val = _parse_farside_num(cells[2]) if len(cells) > 2 else None
        gbtc_val = _parse_farside_num(cells[-3]) if len(cells) >= 3 else None

        # Check if all individual ETF cells are '-' (US market holiday)
        non_date_cells = cells[1:-1]
        if all(c in ("-", "", "—") for c in non_date_cells):
            continue
        if total_val is None:
            continue

        rows_out.append(
            {
                "date": d_iso,
                "total_musd": total_val,
                "ibit_musd": ibit_val or 0.0,
                "fbtc_musd": fbtc_val or 0.0,
                "gbtc_musd": gbtc_val or 0.0,
            }
        )

    rows_out.sort(key=lambda x: x["date"])
    out = {
        "source": url,
        "unit": "USD Millions",
        "count": len(rows_out),
        "start": rows_out[0]["date"] if rows_out else None,
        "end": rows_out[-1]["date"] if rows_out else None,
        "rows": rows_out,
    }
    print(f"  -> Farside BTC Spot ETF: {len(rows_out)} trading days ({out['start']} ~ {out['end']})")
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and cache public flow signal datasets.")
    parser.add_argument("--refresh", action="store_true", help="Re-download even if cached files exist.")
    args = parser.parse_args()

    fetch_yfinance_prices(refresh=args.refresh)
    fetch_taiwan_data(refresh=args.refresh)
    fetch_cftc_cot_data(refresh=args.refresh)
    fetch_okx_crypto_data(refresh=args.refresh)
    fetch_farside_btc_etf(refresh=args.refresh)
    print("\n[DONE] All raw datasets cached under:", CACHE_DIR)


if __name__ == "__main__":
    main()

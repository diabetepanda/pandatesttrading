"""Daily macro economic data generator producing `data/macro.json`."""

from __future__ import annotations

import argparse
import calendar
import csv
import html
import io
import json
import math
import re
import time as time_mod
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
import yaml

from src.data.models import OHLCVBar
from src.jobs.ta import (
    HISTORY_1D_FILENAME,
    _load_history_1d_store,
    _load_research_1d_cache,
    _load_scan_bars_1d,
    _merge_daily_bars,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CALENDAR_PATH = ROOT_DIR / "config" / "macro_calendar.yaml"

NY_TZ = ZoneInfo("America/New_York")
TPE_TZ = ZoneInfo("Asia/Taipei")
UTC_TZ = timezone.utc

REACTION_SYMBOLS = ("NAS100", "SP500", "TWII", "BTCUSDT")
REACTION_LABELS = {
    "NAS100": "那指 100",
    "SP500": "標普 500",
    "TWII": "台股加權",
    "BTCUSDT": "BTC",
}

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
BLS_API_V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
TREASURY_YIELD_MONTHLY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/{yyyymm}"
    "?type=daily_treasury_yield_curve&field_tdr_date_value_month={yyyymm}&page&_format=csv"
)
DBNOMICS_SERIES_URL = "https://api.db.nomics.world/v22/series/{dataset_path}?observations=1"
CENSUS_RETAIL_ADV_URL = "https://www.census.gov/retail/marts/www/adv44X72.txt"
DOL_OUI_WKCLAIMS_URL = "https://oui.doleta.gov/unemploy/wkclaims/report.asp"
TE_ICSA_URL = "https://tradingeconomics.com/united-states/jobless-claims"

REQUEST_TIMEOUT_SEC = 8
MAX_HTTP_ATTEMPTS = 2

BLS_FALLBACK_SERIES: dict[str, str] = {
    "CPI": "CUSR0000SA0",
    "CORE_CPI": "CUSR0000SA0L1E",
    "PPI": "WPSFD4",
    "NFP": "CES0000000001",
    "UNRATE": "LNS14000000",
    "AVG_HOURLY_EARNINGS": "CES0500000003",
    "JOLTS": "JTS000000000000000JOL",
}

# CBOT 30 天期聯邦基金期貨（ZQ）月份代碼（yfinance 實測結果：
# ZQZ26.CBT / ZQF27.CBT / ZQG27.CBT / ZQH27.CBT 可正常抓取日線；
# .CME、FF*.CBT 與單碼年份如 ZQZ6.CBT 均回傳 404/delisted）
ZQ_MONTH_CODES: dict[int, str] = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}

FEDWATCH_BUCKETS: tuple[tuple[str, str], ...] = (
    ("cut_2_plus", "降 2 碼以上"),
    ("cut_1", "降 1 碼"),
    ("hold", "維持"),
    ("hike_1", "升 1 碼"),
    ("hike_2_plus", "升 2 碼以上"),
)

DEFAULT_MARKET_SNAPSHOT_RULES: dict[str, dict[str, Any]] = {
    "DXY": {
        "symbol": "DX-Y.NYB",
        "name_zh": "美元指數",
        "unit": "",
        "decimals": 2,
        "chg_20d_bear_pct": 1.5,
        "chg_20d_bull_pct": -1.5,
        "what_is": "美元指數衡量美元對六大貨幣強弱；美元走強通常不利新興市場股市與台幣、對黃金與比特幣偏壓力；走弱相反。",
        "rationale": "近 20 日上漲超過 1.5% 標偏空（美元轉強），下跌超過 1.5% 標偏多（美元轉弱），其餘中性。",
    },
    "USDTWD": {
        "symbol": "TWD=X",
        "name_zh": "美元／台幣",
        "unit": "",
        "decimals": 2,
        "chg_20d_bear_pct": 1.0,
        "chg_20d_bull_pct": -1.0,
        "what_is": "台幣升值（數字變小）通常代表外資匯入，對台股偏多；台幣貶值代表外資匯出，偏空。",
        "rationale": "近 20 日變動超過 ±1.0%：台幣貶值逾 1% 標偏空（外資匯出壓力），升值逾 1% 標偏多（外資匯入）。",
    },
    "HSI": {
        "symbol": "^HSI",
        "name_zh": "恒生指數",
        "unit": "點",
        "decimals": 1,
        "chg_20d_bull_pct": 3.0,
        "chg_20d_bear_pct": -3.0,
        "what_is": "恒生指數是香港股市主要指數，成分股以大型陸資與香港企業為主，可以看出中國經濟與亞洲資金的情緒；恒生走強通常代表亞洲風險偏好回升，對台股偏正面。",
        "rationale": "以對台股、亞洲風險情緒為視角：近 20 日恒生指數上漲超過 3.0% 標「偏多（亞股轉強）」，下跌超過 3.0% 標「偏空（亞股轉弱）」，其餘介於 ±3.0% 間標「中性」。",
    },
    "GOLD": {
        "symbol": "GC=F",
        "name_zh": "黃金",
        "unit": "美元",
        "decimals": 1,
        "chg_20d_bull_pct": 3.0,
        "chg_20d_bear_pct": -3.0,
        "what_is": "黃金為無息避險與抗通膨資產；美元走弱或實質利率下降時通常走強，急漲時亦反映市場避險需求升溫。",
        "rationale": "近 20 日上漲逾 3.0% 反映寬鬆或避險買盤共振（偏多，但留意避險情緒）；下跌逾 3.0% 代表強美元或高利率壓抑（偏空）。",
    },
    "WTI": {
        "symbol": "CL=F",
        "name_zh": "原油 (WTI)",
        "unit": "美元",
        "decimals": 2,
        "chg_20d_bear_pct": 5.0,
        "chg_20d_bull_pct": -5.0,
        "what_is": "西德州原油期貨反映全球景氣需求與能源通膨壓力；油價急漲易推升通膨預期並壓抑聯準會降息空間。",
        "rationale": "近 20 日上漲逾 5.0% 推升通膨預期（對股幣偏空）；下跌逾 5.0% 舒緩通膨壓力（偏多）。",
    },
    "US10Y": {
        "symbol": "^TNX",
        "name_zh": "美國 10 年期殖利率",
        "unit": "%",
        "decimals": 2,
        "chg_20d_bear_pp": 0.25,
        "chg_20d_bull_pp": -0.25,
        "what_is": "10 年期美債殖利率是全球風險資產的定價錨；殖利率走高壓縮科技股估值並推升資金成本，回落則相反。",
        "rationale": "近 20 日上升超過 0.25 個百分點對股市與加密貨幣偏空；下降超過 0.25 個百分點則偏多。",
    },
}

DEFAULT_JOLTS_RULES: dict[str, Any] = {
    "diff_threshold_m": 0.2,
    "sharp_drop_m": 0.6,
    "rationale": "職位空缺較前值或預期增加 0.2 百萬以上代表勞動市場偏緊、不利降息（偏空）；下降 0.2 百萬以上代表降溫（偏多），單月降幅逾 0.6 百萬附註「留意景氣放緩」。",
}

# 總覽「對各資產的影響」7 塊判斷規則表（實際判讀結合 17 項指標、FedWatch 與市場快照）
ASSET_IMPACT_ORDER: tuple[str, ...] = ("美股", "台股", "加密貨幣", "美債", "黃金", "原油", "美元／台幣")
ASSET_IMPACT_SUBS: dict[str, str] = {
    "美股": "標普 500 · 那斯達克 100",
    "台股": "加權指數 · 電子權值股",
    "加密貨幣": "BTC · 主流幣",
    "美債": "2Y / 10Y 殖利率（債價視角）",
    "黃金": "COMEX 黃金期貨",
    "原油": "WTI 西德州原油",
    "美元／台幣": "USD/TWD 匯率",
}
DBNOMICS_BEA_SERIES: dict[str, str] = {
    "PCE": "BEA/NIPA-T20804/DPCERG-M",
    "CORE_PCE": "BEA/NIPA-T20804/DPCCRG-M",
    "GDP": "BEA/NIPA-T10101/A191RL-Q",
}
DBNOMICS_H15_YIELDS: dict[str, str] = {
    "DGS2": "FED/H15/RIFLGFCY02_N.B",
    "DGS10": "FED/H15/RIFLGFCY10_N.B",
}

TE_ISM_URLS = {
    "ISM_MFG": "https://tradingeconomics.com/united-states/business-confidence",
    "ISM_SVC": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
}

WEEKDAY_ZH = ("一", "二", "三", "四", "五", "六", "日")


def load_macro_calendar(path: Path | None = None) -> dict[str, Any]:
    """Load macro release calendar and verified historical metadata."""
    target = path or DEFAULT_CALENDAR_PATH
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid macro calendar format in {target}")
    return data


def _parse_et_iso(et_str: str) -> datetime:
    """Parse naive/aware ET timestamp string into timezone-aware America/New_York datetime."""
    dt = datetime.fromisoformat(str(et_str))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NY_TZ)
    return dt.astimezone(NY_TZ)


def _fmt_tpe(dt: datetime) -> dict[str, str]:
    tpe = dt.astimezone(TPE_TZ)
    utc = dt.astimezone(UTC_TZ)
    wd = WEEKDAY_ZH[tpe.weekday()]
    return {
        "time_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_taipei_iso": tpe.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "time_taipei": tpe.strftime("%Y-%m-%d %H:%M"),
        "date_taipei": tpe.strftime("%Y-%m-%d"),
        "short_taipei": f"{tpe.month:02d}/{tpe.day:02d}（{wd}）{tpe.hour:02d}:{tpe.minute:02d}",
        "weekday_taipei": wd,
    }


def _period_zh(period: str) -> str:
    """Format period like `2026-10` -> `2026年10月`, `2026-Q3` -> `2026 Q3`."""
    p = str(period or "")
    m_mo = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m_mo:
        return f"{m_mo.group(1)}年{int(m_mo.group(2))}月"
    m_q = re.fullmatch(r"(\d{4})-(Q[1-4])(.*)", p)
    if m_q:
        return f"{m_q.group(1)} {m_q.group(2)}{m_q.group(3)}"
    m_d = re.fullmatch(r"\d{4}-(\d{2})-(\d{2})", p)
    if m_d:
        return f"{int(m_d.group(1))}/{int(m_d.group(2))}當週"
    return p


def _load_market_bars(out_dir: Path, use_local_cache: bool = True) -> dict[str, list[OHLCVBar]]:
    """Load daily bars for NAS100, SP500, TWII, BTCUSDT strictly from history_1d.json.gz (or local cache fallback)."""
    store = _load_history_1d_store(out_dir)
    if not store:
        alt_hist = ROOT_DIR / "data" / HISTORY_1D_FILENAME
        if alt_hist.exists() and alt_hist.parent.resolve() != out_dir.resolve():
            store = _load_history_1d_store(alt_hist.parent)

    research_cache = _load_research_1d_cache() if use_local_cache else {}
    bars_by_sym: dict[str, list[OHLCVBar]] = {}
    for sym in REACTION_SYMBOLS:
        rec = store.get(sym) or {}
        hist_bars = list(rec.get("bars") or [])
        if not hist_bars and use_local_cache:
            hist_bars = _merge_daily_bars(
                list(research_cache.get(sym, [])),
                _load_scan_bars_1d(sym),
            )
        bars_by_sym[sym] = sorted(hist_bars, key=lambda b: b.ts_utc)
    return bars_by_sym


def _index_bars_with_dates(bars: list[OHLCVBar]) -> list[tuple[date, OHLCVBar]]:
    out: list[tuple[date, OHLCVBar]] = []
    for b in bars:
        try:
            d = date.fromisoformat(b.ts_utc[:10])
        except Exception:
            continue
        out.append((d, b))
    out.sort(key=lambda x: x[0])
    return out


def compute_single_asset_reaction(
    sym: str,
    release_dt: datetime,
    indexed_bars: list[tuple[date, OHLCVBar]],
) -> float | None:
    """Compute post-release % return using history_1d.json.gz daily bars.

    Conventions:
    - US indices (NAS100, SP500): release day close (or next trading close if >=16:00 ET)
      vs previous US trading day close.
    - TWII: first TWSE trading close strictly after release time (13:30 TPE close cutoff)
      vs previous TWSE trading day close.
    - BTCUSDT: first daily UTC candle closing strictly after release_utc
      vs last daily UTC candle closed at or before release_utc.
    """
    if len(indexed_bars) < 2:
        return None

    if sym in ("NAS100", "SP500"):
        rel_ny = release_dt.astimezone(NY_TZ)
        cutoff_date = rel_ny.date()
        need_strictly_after = rel_ny.time() >= time(16, 0)
        after_idx: int | None = None
        for idx, (d, _) in enumerate(indexed_bars):
            if (d > cutoff_date) if need_strictly_after else (d >= cutoff_date):
                after_idx = idx
                break
        if after_idx is None or after_idx == 0:
            return None
        after_d, after_bar = indexed_bars[after_idx]
        before_d, before_bar = indexed_bars[after_idx - 1]
        if (after_d - cutoff_date).days > 7 or (after_d - before_d).days > 7:
            return None
        if before_bar.close <= 0:
            return None
        return round((after_bar.close / before_bar.close - 1.0) * 100.0, 2)

    if sym == "TWII":
        rel_tpe = release_dt.astimezone(TPE_TZ)
        cutoff_date = rel_tpe.date()
        need_strictly_after = rel_tpe.time() >= time(13, 30)
        after_idx = None
        for idx, (d, _) in enumerate(indexed_bars):
            if (d > cutoff_date) if need_strictly_after else (d >= cutoff_date):
                after_idx = idx
                break
        if after_idx is None or after_idx == 0:
            return None
        after_d, after_bar = indexed_bars[after_idx]
        before_d, before_bar = indexed_bars[after_idx - 1]
        if (after_d - cutoff_date).days > 10 or (after_d - before_d).days > 12:
            return None
        if before_bar.close <= 0:
            return None
        return round((after_bar.close / before_bar.close - 1.0) * 100.0, 2)

    if sym == "BTCUSDT":
        rel_utc = release_dt.astimezone(UTC_TZ)
        before_bar: OHLCVBar | None = None
        before_close_dt: datetime | None = None
        after_bar: OHLCVBar | None = None
        after_close_dt: datetime | None = None
        for d, bar in indexed_bars:
            open_dt = datetime(d.year, d.month, d.day, tzinfo=UTC_TZ)
            close_dt = open_dt + timedelta(days=1)
            if close_dt <= rel_utc:
                before_bar = bar
                before_close_dt = close_dt
            elif after_bar is None:
                after_bar = bar
                after_close_dt = close_dt
                break
        if before_bar is None or after_bar is None or before_close_dt is None or after_close_dt is None:
            return None
        if (after_close_dt - before_close_dt).days > 3 or before_bar.close <= 0:
            return None
        return round((after_bar.close / before_bar.close - 1.0) * 100.0, 2)

    return None


def compute_market_reactions(
    release_dt: datetime,
    indexed_bars_map: dict[str, list[tuple[date, OHLCVBar]]],
) -> dict[str, float | None]:
    """Compute post-release returns for NAS100, SP500, TWII, BTCUSDT."""
    return {
        sym: compute_single_asset_reaction(sym, release_dt, indexed_bars_map.get(sym, []))
        for sym in REACTION_SYMBOLS
    }


class FredTransportError(RuntimeError):
    """Raised when FRED times out, fails network connect, or returns block/rate-limit codes."""


def _is_transport_or_block_exc(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    req_exc = getattr(requests, "exceptions", None)
    if req_exc is not None and isinstance(exc, (req_exc.Timeout, req_exc.ConnectionError)):
        return True
    msg = str(exc).lower()
    return any(tok in msg for tok in ("timed out", "timeout", "connection", "max retries", "name resolution"))


def _http_call(
    method: str,
    url: str,
    http_get: Any = None,
    http_post: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
    **kwargs: Any,
) -> Any:
    """Execute HTTP GET/POST with strict <=10s timeout and at most 1 retry."""
    eff_timeout = max(1, min(int(timeout_sec), 10))
    eff_attempts = max(1, min(int(max_attempts), 2))
    if method.upper() == "POST":
        caller = http_post or http_get or requests.post
    else:
        caller = http_get or requests.get

    last_exc: Exception | None = None
    for attempt in range(eff_attempts):
        try:
            resp = caller(url, timeout=eff_timeout, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < eff_attempts:
                last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= eff_attempts:
                raise
    raise RuntimeError(str(last_exc or f"HTTP failed for {url}"))


def fetch_fred_series(
    series_id: str,
    http_get: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
    fred_url_template: str | None = None,
) -> list[tuple[str, float | None]]:
    """Fetch public FRED CSV (no API key) and parse `(YYYY-MM-DD, float|None)` rows."""
    tpl = fred_url_template or FRED_CSV_URL
    url = tpl.format(series_id=series_id) if "{series_id}" in tpl else f"{tpl}{series_id}"
    eff_timeout = max(1, min(int(timeout_sec), 10))
    last_err: Exception | None = None
    saw_transport = False
    for attempt in range(MAX_HTTP_ATTEMPTS):
        try:
            caller = http_get or requests.get
            resp = caller(url, timeout=eff_timeout, headers={"User-Agent": "SignalDesk-Macro/1.0"})
            if resp.status_code in (403, 429):
                saw_transport = True
                raise FredTransportError(f"FRED HTTP {resp.status_code} (blocked/limited) for {series_id}")
            if resp.status_code != 200:
                raise RuntimeError(f"FRED HTTP {resp.status_code} for {series_id}")
            text = resp.text.strip()
            if not text or text.lstrip().startswith("<"):
                raise RuntimeError(f"FRED non-CSV response for {series_id}")
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header or len(header) < 2:
                raise RuntimeError(f"FRED invalid CSV header for {series_id}")
            rows: list[tuple[str, float | None]] = []
            for row in reader:
                if len(row) < 2:
                    continue
                d_str = row[0].strip()
                v_str = row[1].strip()
                if len(d_str) != 10:
                    continue
                val: float | None
                if not v_str or v_str == ".":
                    val = None
                else:
                    val = float(v_str)
                rows.append((d_str, val))
            if not rows:
                raise RuntimeError(f"FRED empty series for {series_id}")
            return rows
        except Exception as exc:
            last_err = exc
            if isinstance(exc, FredTransportError) or _is_transport_or_block_exc(exc):
                saw_transport = True
            if attempt + 1 >= MAX_HTTP_ATTEMPTS:
                break
    msg = str(last_err or f"Failed fetching FRED {series_id}")
    if saw_transport:
        raise FredTransportError(msg)
    raise RuntimeError(msg)


_MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def fetch_ism_te(
    indicator_id: str,
    http_get: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Fetch latest ISM Manufacturing / Services PMI reading from TradingEconomics public page."""
    url = TE_ISM_URLS[indicator_id]
    resp = _http_call(
        "GET",
        url,
        http_get=http_get,
        timeout_sec=timeout_sec,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TradingEconomics HTTP {resp.status_code} for {indicator_id}")
    html = resp.text

    # Extract summary sentence from meta description or historical-desc
    # e.g. "Non Manufacturing PMI in the United States decreased to 54.50 points in October from 54.90 points in September of 2026."
    # or "ISM Services PMI decreased to 54.5 in October 2026... compared to 54.9 in September and forecasts of 54.8."
    desc_m = re.search(r'id="historical-desc"[^>]*>(.*?)</', html, re.S | re.I)
    meta_m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html, re.I)
    text_candidates = []
    if desc_m:
        text_candidates.append(re.sub(r"<[^>]+>", " ", desc_m.group(1)))
    if meta_m:
        text_candidates.append(meta_m.group(1))
    combined_desc = re.sub(r"\s+", " ", " ".join(text_candidates)).strip()

    # Try calendar table row first (works on business-confidence page)
    cal_matches = re.findall(
        r"(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}\s+[AP]M\s+ISM\s+(?:Manufacturing|Services|Non[- ]Manufacturing)\s+PMI\s+([A-Za-z]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)),
        re.I,
    )
    parsed: dict[str, Any] = {}
    if cal_matches:
        d_str, _mo_str, act_s, prev_s, cons_s = cal_matches[-1]
        dt_pub = datetime.strptime(d_str, "%Y-%m-%d")
        # Reference month is typically previous month for ISM
        ref_mo = _MONTH_MAP.get(_mo_str.lower())
        if ref_mo:
            ref_yr = dt_pub.year if ref_mo <= dt_pub.month else dt_pub.year - 1
            parsed["period"] = f"{ref_yr:04d}-{ref_mo:02d}"
        parsed["actual"] = float(act_s)
        parsed["prev"] = float(prev_s)
        parsed["consensus"] = float(cons_s)
        parsed["consensus_source"] = "TradingEconomics"
        return parsed

    # Fallback parse from historical-desc / meta description
    m_hist = re.search(
        r"(?:to|at)\s+([\d.]+)\s*(?:points)?\s+in\s+([A-Za-z]+)\s*(?:of\s+)?(\d{4}).*?from\s+([\d.]+)|"
        r"(?:to|at)\s+([\d.]+)\s+in\s+([A-Za-z]+)\s+(\d{4}).*?compared\s+to\s+([\d.]+).*?(?:forecasts?\s+of\s+([\d.]+))?",
        combined_desc,
        re.I,
    )
    if m_hist:
        g = m_hist.groups()
        if g[0] is not None:
            act_v, mo_s, yr_s, prev_v = float(g[0]), g[1], int(g[2]), float(g[3])
            cons_v = None
        else:
            act_v, mo_s, yr_s, prev_v = float(g[4]), g[5], int(g[6]), float(g[7])
            cons_v = float(g[8]) if g[8] else None
        mo_num = _MONTH_MAP.get(mo_s.lower())
        if mo_num:
            parsed["period"] = f"{yr_s:04d}-{mo_num:02d}"
            parsed["actual"] = act_v
            parsed["prev"] = prev_v
            if cons_v is not None:
                parsed["consensus"] = cons_v
                parsed["consensus_source"] = "TradingEconomics"
            return parsed

    raise RuntimeError(f"Could not parse ISM data from TradingEconomics for {indicator_id}")


def fetch_bls_batch_v1(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    http_get: Any = None,
    http_post: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, list[tuple[str, float | None]]]:
    """Fetch multiple BLS series via Public Data API v1 (no API key required)."""
    if not series_ids:
        return {}
    payload = {
        "seriesid": list(series_ids),
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    resp = _http_call(
        "POST",
        BLS_API_V1_URL,
        http_get=http_get,
        http_post=http_post,
        timeout_sec=timeout_sec,
        json=payload,
        headers={
            "User-Agent": "SignalDesk-Macro/1.0",
            "Content-Type": "application/json",
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"BLS API v1 HTTP {resp.status_code}")
    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        msg = "; ".join(str(m) for m in (data.get("message") or []))
        raise RuntimeError(f"BLS API v1 status={data.get('status')} {msg}".strip())

    out: dict[str, list[tuple[str, float | None]]] = {}
    for s_obj in (data.get("Results") or {}).get("series") or []:
        sid = str(s_obj.get("seriesID") or "").strip()
        if not sid:
            continue
        by_date: dict[str, float | None] = {}
        for item in s_obj.get("data") or []:
            yr_s = str(item.get("year") or "").strip()
            p_s = str(item.get("period") or "").strip()
            if not re.fullmatch(r"\d{4}", yr_s) or not re.fullmatch(r"M(0[1-9]|1[0-2])", p_s):
                continue
            d_s = f"{yr_s}-{p_s[1:]}-01"
            v_raw = str(item.get("value") or "").strip().replace(",", "")
            if not v_raw or v_raw in ("-", "."):
                continue
            try:
                by_date[d_s] = float(v_raw)
            except ValueError:
                continue
        if by_date:
            out[sid] = sorted(by_date.items(), key=lambda x: x[0])
    return out


def fetch_dbnomics_series(
    dataset_path: str,
    http_get: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> list[tuple[str, float | None]]:
    """Fetch public economic series from DBnomics v22 API (no key required)."""
    url = DBNOMICS_SERIES_URL.format(dataset_path=dataset_path)
    resp = _http_call(
        "GET",
        url,
        http_get=http_get,
        timeout_sec=timeout_sec,
        headers={"User-Agent": "SignalDesk-Macro/1.0"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"DBnomics HTTP {resp.status_code} for {dataset_path}")
    j = resp.json()
    docs = (j.get("series") or {}).get("docs") or []
    if not docs:
        raise RuntimeError(f"DBnomics empty docs for {dataset_path}")
    doc = docs[0]
    periods = doc.get("period") or []
    values = doc.get("value") or []
    by_date: dict[str, float | None] = {}
    q_to_mo = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}
    for p_raw, v_raw in zip(periods, values):
        p_s = str(p_raw or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p_s):
            d_s = p_s
        elif re.fullmatch(r"\d{4}-\d{2}", p_s):
            d_s = f"{p_s}-01"
        else:
            m_q = re.fullmatch(r"(\d{4})-(Q[1-4])", p_s)
            if not m_q:
                continue
            d_s = f"{m_q.group(1)}-{q_to_mo[m_q.group(2)]}-01"
        if v_raw is None or str(v_raw).strip().upper() in ("", "NA", "NONE", "."):
            continue
        try:
            by_date[d_s] = float(v_raw)
        except (TypeError, ValueError):
            continue
    if not by_date:
        raise RuntimeError(f"DBnomics no valid observations for {dataset_path}")
    return sorted(by_date.items(), key=lambda x: x[0])


def fetch_treasury_yield_curve(
    now_utc: datetime,
    http_get: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, list[tuple[str, float | None]]]:
    """Fetch Daily Treasury Par Yield Curve CSV (2 Yr & 10 Yr) plus optional FED/H15 sparkline backfill."""
    ny_d = now_utc.astimezone(NY_TZ).date()
    ym_targets: list[tuple[int, int]] = []
    cur_y, cur_m = ny_d.year, ny_d.month
    for offset in range(3):
        m = cur_m - offset
        y = cur_y
        while m <= 0:
            m += 12
            y -= 1
        ym_targets.append((y, m))
    ym_targets.reverse()

    merged: dict[str, dict[str, float | None]] = {"DGS2": {}, "DGS10": {}}

    # Best-effort historical sparkline backfill via DBnomics FED/H15 (optional, non-fatal if slow/unavailable)
    for yid, db_path in DBNOMICS_H15_YIELDS.items():
        try:
            hist_rows = fetch_dbnomics_series(db_path, http_get=http_get, timeout_sec=min(timeout_sec, 6))
            for d_s, val in hist_rows:
                if val is not None:
                    merged[yid][d_s] = val
        except Exception:
            pass

    headers = {"User-Agent": "Mozilla/5.0 (compatible; SignalDesk-Macro/1.0)"}
    treasury_rows_parsed = 0
    last_err: Exception | None = None
    for yr, mo in ym_targets:
        yyyymm = f"{yr:04d}{mo:02d}"
        url = TREASURY_YIELD_MONTHLY_CSV_URL.format(year=yr, yyyymm=yyyymm)
        try:
            resp = _http_call("GET", url, http_get=http_get, timeout_sec=timeout_sec, headers=headers)
            if resp.status_code != 200:
                last_err = RuntimeError(f"Treasury HTTP {resp.status_code} for {yyyymm}")
                continue
            text = resp.text.strip()
            if not text or text.lstrip().startswith("<"):
                continue
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                raw_d = str(row.get("Date") or "").strip()
                m_d = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw_d)
                if not m_d:
                    continue
                d_iso = f"{int(m_d.group(3)):04d}-{int(m_d.group(1)):02d}-{int(m_d.group(2)):02d}"
                for col, yid in (("2 Yr", "DGS2"), ("10 Yr", "DGS10")):
                    v_s = str(row.get(col) or "").strip()
                    if v_s and v_s.upper() not in ("N/A", "NA", "."):
                        try:
                            merged[yid][d_iso] = float(v_s)
                            treasury_rows_parsed += 1
                        except ValueError:
                            pass
        except Exception as exc:
            last_err = exc

    if treasury_rows_parsed == 0 and (not merged["DGS2"] or not merged["DGS10"]):
        raise RuntimeError(str(last_err or "Treasury yield curve CSV returned no valid rows"))

    out: dict[str, list[tuple[str, float | None]]] = {}
    for yid in ("DGS2", "DGS10"):
        if not merged[yid]:
            raise RuntimeError(f"Treasury yield fallback empty for {yid}")
        out[yid] = sorted(merged[yid].items(), key=lambda x: x[0])
    return out


def fetch_census_retail_sales(
    http_get: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> list[tuple[str, float | None]]:
    """Fetch Census MRTS seasonally adjusted Retail & Food Services (`adv44X72.txt`, millions USD)."""
    resp = _http_call(
        "GET",
        CENSUS_RETAIL_ADV_URL,
        http_get=http_get,
        timeout_sec=timeout_sec,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SignalDesk-Macro/1.0)"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Census MRTS HTTP {resp.status_code}")

    by_date: dict[str, float | None] = {}
    for raw_ln in resp.text.splitlines():
        up = raw_ln.strip().upper()
        if "SEASONAL FACTORS" in up:
            break
        m_yr = re.match(r"^\s*((?:19|20)\d{2})\s+(.*)$", raw_ln)
        if not m_yr:
            continue
        yr = int(m_yr.group(1))
        tokens = m_yr.group(2).split()
        for idx, tok in enumerate(tokens[:12]):
            clean_tok = tok.replace(",", "").strip()
            if not clean_tok or clean_tok.upper() in ("NA", "N/A", "."):
                continue
            try:
                val = float(clean_tok)
            except ValueError:
                continue
            by_date[f"{yr:04d}-{idx + 1:02d}-01"] = val

    if not by_date:
        raise RuntimeError("Census adv44X72.txt parsed no monthly observations")
    return sorted(by_date.items(), key=lambda x: x[0])


def _parse_te_k_number(raw: str) -> float | None:
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return None
    mult = 1.0
    if s.upper().endswith("K"):
        mult = 1000.0
        s = s[:-1].strip()
    elif s.upper().endswith("M"):
        mult = 1_000_000.0
        s = s[:-1].strip()
    try:
        return float(s) * mult
    except ValueError:
        return None


def fetch_icsa_fallback(
    now_utc: datetime,
    http_get: Any = None,
    http_post: Any = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> tuple[list[tuple[str, float | None]], str, str]:
    """Fetch Initial Jobless Claims (SA weekly headcount) from DOL OUI XML + TradingEconomics."""
    merged: dict[str, float] = {}
    used_sources: list[str] = []
    errors: list[str] = []

    ny_y = now_utc.astimezone(NY_TZ).year
    try:
        resp_dol = _http_call(
            "POST",
            DOL_OUI_WKCLAIMS_URL,
            http_get=http_get,
            http_post=http_post,
            timeout_sec=timeout_sec,
            data={
                "level": "us",
                "strtdate": str(ny_y - 2),
                "enddate": str(ny_y + 1),
                "filetype": "xml",
                "submit": "Submit",
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; SignalDesk-Macro/1.0)"},
        )
        if resp_dol.status_code != 200:
            raise RuntimeError(f"DOL OUI HTTP {resp_dol.status_code}")
        clean_xml = html.unescape(resp_dol.text).replace("\xa0", "").strip()
        root = ET.fromstring(clean_xml)
        dol_count = 0
        for wk in root.findall("week"):
            d_raw = (wk.findtext("weekEnded") or "").strip()
            sa_raw = (wk.findtext("InitialClaims/SA") or "").strip().replace(",", "")
            m_d = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", d_raw)
            if not m_d or not re.fullmatch(r"\d+(?:\.\d+)?", sa_raw):
                continue
            d_iso = f"{m_d.group(3)}-{m_d.group(1)}-{m_d.group(2)}"
            merged[d_iso] = float(sa_raw)
            dol_count += 1
        if dol_count > 0:
            used_sources.append("DOL OUI")
        else:
            errors.append("DOL OUI empty XML")
    except Exception as exc:
        errors.append(f"DOL OUI: {exc}")

    try:
        resp_te = _http_call(
            "GET",
            TE_ICSA_URL,
            http_get=http_get,
            timeout_sec=timeout_sec,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        if resp_te.status_code != 200:
            raise RuntimeError(f"TE HTTP {resp_te.status_code}")
        te_html = resp_te.text
        te_count = 0

        # Parse calendar table rows on TradingEconomics jobless-claims page
        for tb in re.findall(r"<table[^>]*>.*?</table>", te_html, re.S):
            if "Initial Jobless Claims" not in tb:
                continue
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tb, re.S):
                cells = [
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                    for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
                ]
                if len(cells) >= 6 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]) and "Initial Jobless Claims" in cells[2]:
                    rel_d = date.fromisoformat(cells[0])
                    m_ref = re.fullmatch(r"([A-Za-z]+)/(\d{1,2})", cells[3])
                    act_raw = _parse_te_k_number(cells[4])
                    prev_raw = _parse_te_k_number(cells[5])
                    if not m_ref or act_raw is None:
                        continue
                    mo_n = _MONTH_MAP.get(m_ref.group(1).lower())
                    if not mo_n:
                        continue
                    yr_n = rel_d.year
                    if mo_n == 12 and rel_d.month == 1:
                        yr_n -= 1
                    wk_end = date(yr_n, mo_n, int(m_ref.group(2)))
                    if prev_raw is not None:
                        merged[(wk_end - timedelta(days=7)).isoformat()] = prev_raw
                    merged[wk_end.isoformat()] = act_raw
                    te_count += 1

        # Parse summary description sentence as extra check / fallback
        desc_m = re.search(r'id="historical-desc"[^>]*>(.*?)</', te_html, re.S | re.I)
        meta_m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', te_html, re.I)
        combo = " ".join([
            re.sub(r"<[^>]+>", " ", desc_m.group(1)) if desc_m else "",
            meta_m.group(1) if meta_m else "",
        ])
        m_txt = re.search(
            r"to\s+([\d.]+)\s+thousand\s+in\s+the\s+week\s+ending\s+([A-Za-z]+)\s+(\d{1,2})\s+of\s+(\d{4})\s+from\s+([\d.]+)\s+thousand",
            re.sub(r"\s+", " ", combo),
            re.I,
        )
        if m_txt:
            mo_n = _MONTH_MAP.get(m_txt.group(2).lower())
            if mo_n:
                wk_end = date(int(m_txt.group(4)), mo_n, int(m_txt.group(3)))
                act_v = float(m_txt.group(1)) * 1000.0
                prev_v = float(m_txt.group(5)) * 1000.0
                merged[(wk_end - timedelta(days=7)).isoformat()] = prev_v
                merged[wk_end.isoformat()] = act_v
                te_count += 1

        if te_count > 0:
            used_sources.append("TradingEconomics")
        else:
            errors.append("TradingEconomics ICSA no parsed rows")
    except Exception as exc:
        errors.append(f"TradingEconomics ICSA: {exc}")

    if len(merged) < 2:
        raise RuntimeError("ICSA fallback failed: " + " | ".join(errors or ["no rows"]))

    if used_sources == ["DOL OUI", "TradingEconomics"]:
        src_used = "DOL / TradingEconomics（備用）"
        src_detail = "DOL OUI r539cy XML ＋ TradingEconomics（備用）"
    elif "DOL OUI" in used_sources:
        src_used = "DOL OUI（備用）"
        src_detail = "DOL OUI r539cy XML（備用）"
    else:
        src_used = "TradingEconomics（備用）"
        src_detail = "TradingEconomics 公開頁面（備用）"
    return sorted(merged.items(), key=lambda x: x[0]), src_used, src_detail


def build_fomc_fallback_series(
    cal_cfg: dict[str, Any],
    now_utc: datetime,
) -> tuple[list[tuple[str, float | None]], list[tuple[str, float | None]]]:
    """Derive daily/monthly Fed Funds target range series from config/macro_calendar.yaml FOMC decisions."""
    past_decs = list((cal_cfg.get("fomc") or {}).get("past_decisions") or [])
    if not past_decs:
        raise RuntimeError("No fomc.past_decisions in macro_calendar.yaml")

    parsed_decs: list[tuple[date, float, float]] = []
    for dec in past_decs:
        rel_dt = _parse_et_iso(dec["release_et"])
        parsed_decs.append((rel_dt.date(), float(dec["rate_high"]), float(dec["rate_low"])))
    parsed_decs.sort(key=lambda x: x[0])

    end_d = now_utc.astimezone(NY_TZ).date()
    start_d = parsed_decs[0][0].replace(day=1)
    upper_rows: list[tuple[str, float | None]] = []
    lower_rows: list[tuple[str, float | None]] = []

    cursor = start_d
    idx = 0
    while cursor <= end_d:
        # Active target at end of month `cursor`
        if cursor.month == 12:
            month_end = date(cursor.year, 12, 31)
        else:
            month_end = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
        eff_end = min(month_end, end_d)
        while idx + 1 < len(parsed_decs) and parsed_decs[idx + 1][0] <= eff_end:
            idx += 1
        _, hi, lo = parsed_decs[idx]
        d_s = eff_end.isoformat()
        upper_rows.append((d_s, hi))
        lower_rows.append((d_s, lo))
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)

    return upper_rows, lower_rows


def _classify_surprise(
    indicator_id: str,
    actual: float | None,
    consensus: float | None,
    prev: float | None,
) -> tuple[str, str]:
    """Return (surprise_label, basis) where basis is 'consensus', 'prev', or 'none'."""
    if actual is None:
        return "資料不足", "none"
    if consensus is not None:
        if indicator_id == "NFP":
            if abs(actual - consensus) <= 2.0:
                return "符合預期", "consensus"
            return ("高於預期" if actual > consensus else "低於預期"), "consensus"
        if indicator_id == "ICSA":
            if abs(actual - consensus) <= 1.0:
                return "符合預期", "consensus"
            return ("高於預期" if actual > consensus else "低於預期"), "consensus"
        if indicator_id in ("ISM_MFG", "ISM_SVC"):
            if abs(actual - consensus) <= 0.5:
                return "符合預期", "consensus"
            return ("高於預期" if actual > consensus else "低於預期"), "consensus"
        if indicator_id == "JOLTS":
            a_r = round(actual, 1)
            c_r = round(consensus, 1)
            if abs(a_r - c_r) <= 0.1 + 1e-9:
                return "符合預期", "consensus"
            return ("高於預期" if a_r > c_r else "低於預期"), "consensus"
        a_r = round(actual, 1)
        c_r = round(consensus, 1)
        if a_r == c_r:
            return "符合預期", "consensus"
        return ("高於預期" if a_r > c_r else "低於預期"), "consensus"

    if prev is not None:
        if math.isclose(round(actual, 2), round(prev, 2), abs_tol=1e-9):
            return "持平前值", "prev"
        return ("高於前值" if actual > prev else "低於前值"), "prev"

    return "無比較基準", "none"


def _compute_group_stats(
    rows: list[dict[str, Any]],
    group_key: str,
    group_values: list[str],
) -> list[dict[str, Any]]:
    """Aggregate average return, win rate (up %), and sample count per surprise bucket."""
    groups_out: list[dict[str, Any]] = []
    for gval in group_values:
        matched = [r for r in rows if r.get(group_key) == gval]
        assets_stats: dict[str, dict[str, Any]] = {}
        for sym in REACTION_SYMBOLS:
            vals = [
                float(r["reactions"][sym])
                for r in matched
                if isinstance(r.get("reactions"), dict) and r["reactions"].get(sym) is not None
            ]
            n = len(vals)
            if n == 0:
                assets_stats[sym] = {"label": REACTION_LABELS[sym], "n": 0, "avg_pct": None, "up_prob_pct": None}
            else:
                up_cnt = sum(1 for v in vals if v > 0)
                assets_stats[sym] = {
                    "label": REACTION_LABELS[sym],
                    "n": n,
                    "avg_pct": round(sum(vals) / n, 2),
                    "up_prob_pct": round(up_cnt / n * 100.0, 0),
                }
        groups_out.append({
            "group": gval,
            "n_rows": len(matched),
            "assets": assets_stats,
        })
    return groups_out


def _build_modal_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute modal summary statistics by consensus (if >=3 rows have consensus) and by prev."""
    cons_rows = [r for r in rows if r.get("surprise_basis") == "consensus"]
    prev_rows = [r for r in rows if r.get("prev_cmp") in ("高於前值", "低於前值", "持平前值")]

    by_consensus = _compute_group_stats(cons_rows, "surprise", ["高於預期", "符合預期", "低於預期"]) if cons_rows else []
    by_prev = _compute_group_stats(prev_rows, "prev_cmp", ["高於前值", "低於前值"])

    primary = "consensus" if len(cons_rows) >= 3 else "prev"
    return {
        "primary_grouping": primary,
        "consensus_rows_count": len(cons_rows),
        "total_rows_count": len(rows),
        "by_consensus": by_consensus,
        "by_prev": by_prev,
    }


def _fmt_signed_pct(val: float | None, decimals: int = 2) -> str:
    if val is None:
        return "資料不足"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.{decimals}f}%"


def _fmt_val_unit(indicator_id: str, val: float | None) -> str:
    if val is None:
        return "資料不足"
    if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI", "AVG_HOURLY_EARNINGS", "RETAIL_SALES"):
        return _fmt_signed_pct(val, 2)
    if indicator_id == "GDP":
        return f"{'+' if val > 0 else ''}{val:.1f}%"
    if indicator_id == "UNRATE":
        return f"{val:.1f}%"
    if indicator_id == "NFP":
        return f"{'+' if val > 0 else ''}{val:.1f} 萬人"
    if indicator_id == "ICSA":
        return f"{val:.1f} 萬人"
    if indicator_id == "JOLTS":
        return f"{val:.1f} 百萬個"
    if indicator_id in ("ISM_MFG", "ISM_SVC"):
        return f"{val:.1f}"
    if indicator_id in ("DGS2", "DGS10"):
        return f"{val:.2f}%"
    return f"{val:.2f}"


def _fmt_consensus(indicator_id: str, val: float | None) -> str | None:
    if val is None:
        return None
    if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI", "AVG_HOURLY_EARNINGS", "RETAIL_SALES", "GDP", "UNRATE"):
        sign = "+" if (indicator_id != "UNRATE" and val > 0) else ""
        return f"{sign}{val:.1f}%"
    if indicator_id in ("NFP", "ICSA"):
        sign = "+" if (indicator_id == "NFP" and val > 0) else ""
        return f"{sign}{val:.1f} 萬人"
    if indicator_id == "JOLTS":
        return f"{val:.1f} 百萬個"
    if indicator_id in ("ISM_MFG", "ISM_SVC"):
        return f"{val:.1f}"
    return str(val)


def _fmt_change(indicator_id: str, cur: float | None, prev: float | None) -> tuple[str, str]:
    """Return (change_str, direction 'up'|'down'|'flat') aligned to displayed decimal precision."""
    if cur is None or prev is None:
        return "—", "flat"
    if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI", "AVG_HOURLY_EARNINGS", "RETAIL_SALES", "DGS2", "DGS10"):
        dec = 2
        unit = "個百分點"
    elif indicator_id in ("UNRATE", "GDP"):
        dec = 1
        unit = "個百分點"
    elif indicator_id in ("NFP", "ICSA"):
        dec = 1
        unit = "萬人"
    elif indicator_id == "JOLTS":
        dec = 1
        unit = "百萬個"
    elif indicator_id in ("ISM_MFG", "ISM_SVC"):
        dec = 1
        unit = "點"
    else:
        dec = 2
        unit = ""
    diff = round(cur, dec) - round(prev, dec)
    if abs(diff) < 1e-9:
        return "0（持平）", "flat"
    direction = "up" if diff > 0 else "down"
    sign = "+" if diff > 0 else ""
    suffix = f" {unit}" if unit else ""
    return f"{sign}{diff:.{dec}f}{suffix}", direction


def _find_next_upcoming(
    upcoming_list: list[dict[str, Any]],
    now_utc: datetime,
) -> dict[str, Any] | None:
    for item in upcoming_list:
        try:
            dt = _parse_et_iso(item["release_et"])
        except Exception:
            continue
        if dt.astimezone(UTC_TZ) > now_utc:
            t_info = _fmt_tpe(dt)
            return {
                "period": item.get("period", ""),
                "period_zh": _period_zh(item.get("period", "")),
                "release_taipei": t_info["short_taipei"],
                "release_taipei_iso": t_info["time_taipei_iso"],
                "note": item.get("note", ""),
                "consensus": item.get("consensus"),
                "consensus_source": item.get("consensus_source"),
            }
    return None


def _build_icsa_schedule_map(
    fred_rows: list[tuple[str, float | None]],
    cal_cfg: dict[str, Any],
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build past 24 weekly ICSA release metadata and upcoming 60-day ICSA schedule."""
    icfg = cal_cfg.get("indicators", {}).get("ICSA", {})
    holiday_overrides = icfg.get("holiday_release_overrides") or {}
    past_cons_map = icfg.get("past_consensus") or {}

    clean = [(d, v / 10000.0) for d, v in fred_rows if v is not None]
    past_releases: list[dict[str, Any]] = []
    for idx in range(max(1, len(clean) - 24), len(clean)):
        wk_end_s, val = clean[idx]
        prev_val = clean[idx - 1][1]
        if wk_end_s in holiday_overrides:
            rel_et_str = holiday_overrides[wk_end_s]
        else:
            wk_dt = date.fromisoformat(wk_end_s)
            rel_d = wk_dt + timedelta(days=5)  # Saturday + 5 = Thursday
            rel_et_str = f"{rel_d.isoformat()}T08:30:00"
        cons_info = past_cons_map.get(wk_end_s) or {}
        past_releases.append({
            "period": wk_end_s,
            "release_et": rel_et_str,
            "actual": round(val, 1),
            "prev": round(prev_val, 1),
            "actual_as_released": cons_info.get("actual_as_released"),
            "consensus": cons_info.get("consensus"),
            "consensus_source": cons_info.get("consensus_source"),
        })

    # Generate upcoming Thursdays for next 90 days from now_utc
    upcoming: list[dict[str, Any]] = []
    now_ny = now_utc.astimezone(NY_TZ)
    d_cursor = now_ny.date()
    end_date = d_cursor + timedelta(days=90)
    while d_cursor <= end_date:
        if d_cursor.weekday() == 5:  # Saturday week-ending
            wk_s = d_cursor.isoformat()
            if wk_s in holiday_overrides:
                rel_et_str = holiday_overrides[wk_s]
                note = "遇美國假日調整公布日"
            else:
                rel_d = d_cursor + timedelta(days=5)
                rel_et_str = f"{rel_d.isoformat()}T08:30:00"
                note = "每週例行"
            rel_dt = _parse_et_iso(rel_et_str)
            if rel_dt.astimezone(UTC_TZ) > now_utc:
                upcoming.append({
                    "period": wk_s,
                    "release_et": rel_et_str,
                    "stars": 2,
                    "note": note,
                })
        d_cursor += timedelta(days=1)
    return past_releases, upcoming


def _build_fomc_card(
    fred_upper: list[tuple[str, float | None]],
    fred_lower: list[tuple[str, float | None]],
    cal_cfg: dict[str, Any],
    indexed_bars: dict[str, list[tuple[date, OHLCVBar]]],
    now_utc: datetime,
    source_used: str = "FRED",
    source_detail: str | None = None,
) -> dict[str, Any]:
    u_clean = [(d, v) for d, v in fred_upper if v is not None]
    l_clean = [(d, v) for d, v in fred_lower if v is not None]
    l_map = dict(l_clean)
    latest_d, latest_u = u_clean[-1]
    latest_l = l_map.get(latest_d, latest_u - 0.25)

    # Month-end 24 points for sparkline
    monthly_u: dict[str, float] = {}
    monthly_l: dict[str, float] = {}
    for d, v in u_clean:
        monthly_u[d[:7]] = v
    for d, v in l_clean:
        monthly_l[d[:7]] = v
    months = sorted(monthly_u.keys())[-24:]
    chart_u = [monthly_u[m] for m in months]
    chart_l = [monthly_l.get(m, monthly_u[m] - 0.25) for m in months]

    fomc_cfg = cal_cfg.get("fomc") or {}
    past_decs = list(fomc_cfg.get("past_decisions") or [])[-24:]

    rows: list[dict[str, Any]] = []
    for i, dec in enumerate(past_decs):
        rel_dt = _parse_et_iso(dec["release_et"])
        t_info = _fmt_tpe(rel_dt)
        r_hi = float(dec["rate_high"])
        r_lo = float(dec["rate_low"])
        prev_hi = float(past_decs[i - 1]["rate_high"]) if i > 0 else r_hi
        prev_lo = float(past_decs[i - 1]["rate_low"]) if i > 0 else r_lo
        prev_str = f"{prev_lo:.2f}%–{prev_hi:.2f}%"
        cons_str = dec.get("consensus")
        cons_hi = dec.get("consensus_high")
        if cons_str and cons_hi is not None:
            if math.isclose(r_hi, float(cons_hi), abs_tol=1e-6):
                surp, basis = "符合預期", "consensus"
            elif r_hi > float(cons_hi):
                surp, basis = "高於預期", "consensus"
            else:
                surp, basis = "低於預期", "consensus"
        else:
            if r_hi > prev_hi:
                surp, basis = "高於前值", "prev"
            elif r_hi < prev_hi:
                surp, basis = "低於前值", "prev"
            else:
                surp, basis = "持平前值", "prev"
        if r_hi > prev_hi:
            prev_cmp = "高於前值"
        elif r_hi < prev_hi:
            prev_cmp = "低於前值"
        else:
            prev_cmp = "持平前值"

        reactions = compute_market_reactions(rel_dt, indexed_bars)
        rows.append({
            "period": dec["period"],
            "period_zh": _period_zh(dec["period"]),
            "release_taipei": t_info["time_taipei"],
            "release_utc": t_info["time_utc"],
            "actual_str": dec.get("action") or f"{r_lo:.2f}%–{r_hi:.2f}%",
            "actual_num": r_hi,
            "prev_str": prev_str,
            "prev_num": prev_hi,
            "consensus_str": cons_str,
            "consensus_source": dec.get("consensus_source"),
            "surprise": surp,
            "surprise_basis": basis,
            "prev_cmp": prev_cmp,
            "reactions": reactions,
            "note": dec.get("note", ""),
        })

    rows_desc = list(reversed(rows))
    last_dec = past_decs[-1] if past_decs else {}
    prev_dec = past_decs[-2] if len(past_decs) >= 2 else last_dec
    last_hi = float(last_dec.get("rate_high", latest_u))
    prev_hi = float(prev_dec.get("rate_high", last_hi))
    prev_lo = float(prev_dec.get("rate_low", latest_l))
    diff_bp = round((latest_u - prev_hi) * 100)
    if diff_bp > 0:
        chg_str, chg_dir = f"+{diff_bp} bp（升息）", "up"
        verdict, vtext = "bear", "🔴 偏鷹"
    elif diff_bp < 0:
        chg_str, chg_dir = f"{diff_bp} bp（降息）", "down"
        verdict, vtext = "bull", "🟢 偏鴿"
    else:
        # Check if prior hike in recent 2 meetings makes Fed hawkish
        recent_hike = any(
            float(past_decs[j]["rate_high"]) > float(past_decs[j - 1]["rate_high"])
            for j in range(max(1, len(past_decs) - 2), len(past_decs))
        )
        chg_str, chg_dir = "0 bp（維持不變）", "flat"
        verdict, vtext = ("bear", "🔴 偏鷹") if recent_hike else ("neut", "🟡 中性")

    last_rel_tpe = _fmt_tpe(_parse_et_iso(last_dec["release_et"]))["time_taipei"] if last_dec.get("release_et") else latest_d
    next_up = _find_next_upcoming(list(fomc_cfg.get("upcoming") or []), now_utc)

    sub_info_str = (
        f"FRED 最新目標區間截至 {latest_d}｜{last_dec.get('action', '')}"
        if source_used == "FRED"
        else f"FOMC 決議目標區間（截至 {latest_d}）｜{last_dec.get('action', '')}"
    )
    src_str = source_detail or (
        f"FRED（DFEDTARU / DFEDTARL，截至 {latest_d}）＋ Federal Reserve FOMC"
        if source_used == "FRED"
        else f"Federal Reserve FOMC 決議紀錄（截至 {latest_d}，備用）"
    )

    return {
        "id": "FED_RATE",
        "category": "rates",
        "name": "聯邦基金利率",
        "code": "DFEDTARU / DFEDTARL",
        "period": _period_zh(last_dec.get("period", latest_d[:7])),
        "value_str": f"{latest_l:.2f}%–{latest_u:.2f}%",
        "value_num": latest_u,
        "unit": "",
        "prev_str": f"{prev_lo:.2f}%–{prev_hi:.2f}%（前次決議）",
        "change_str": chg_str,
        "change_dir": chg_dir,
        "release_date_taipei": last_rel_tpe,
        "sub_info": sub_info_str,
        "consensus_str": last_dec.get("consensus"),
        "consensus_source": last_dec.get("consensus_source"),
        "surprise": rows[-1]["surprise"] if rows else "—",
        "surprise_basis": rows[-1]["surprise_basis"] if rows else "none",
        "verdict": verdict,
        "verdict_text": vtext,
        "chart_values": chart_u,
        "chart_values_secondary": chart_l,
        "chart_label": "近 24 個月目標區間上／下限（%）",
        "next_release": next_up,
        "why": "聯準會基準利率決定全市場資金成本。升息或維持鷹派立場通常壓抑科技股與加密貨幣估值；降息週期則有助於風險偏好回溫。",
        "source": src_str,
        "source_used": source_used,
        "data_status": "ok",
        "updating": False,
        "modal_title": "FOMC 利率決議",
        "modal_foot_extra": "會前預期為決議前 CME FedWatch 最高機率結果（僅最近 12 次有可靠記錄，較早期標示無可靠預期值）。",
        "history_rows": rows_desc,
        "history_stats": _build_modal_stats(rows_desc),
    }


def _build_daily_yield_card(
    indicator_id: str,
    name: str,
    fred_id: str,
    why_text: str,
    fred_rows: list[tuple[str, float | None]],
    indexed_bars: dict[str, list[tuple[date, OHLCVBar]]],
    source_used: str = "FRED",
    source_detail: str | None = None,
) -> dict[str, Any]:
    clean = [(d, v) for d, v in fred_rows if v is not None]
    if len(clean) < 2:
        raise ValueError(f"Insufficient yield observations for {indicator_id}")
    latest_d, latest_v = clean[-1]
    prev_d, prev_v = clean[-2]
    chg_str, chg_dir = _fmt_change(indicator_id, latest_v, prev_v)

    monthly: dict[str, float] = {}
    for d, v in clean:
        monthly[d[:7]] = v
    months = sorted(monthly.keys())[-24:]
    chart_vals = [monthly[m] for m in months]

    # H.15 daily Treasury yield closes at ~15:30 ET
    rel_dt = _parse_et_iso(f"{latest_d}T15:30:00")
    rel_tpe = _fmt_tpe(rel_dt)

    rows: list[dict[str, Any]] = []
    for idx in range(max(1, len(clean) - 24), len(clean)):
        d_s, val = clean[idx]
        _, p_val = clean[idx - 1]
        dt_et = _parse_et_iso(f"{d_s}T15:30:00")
        t_info = _fmt_tpe(dt_et)
        surp, basis = _classify_surprise(indicator_id, val, None, p_val)
        reactions = compute_market_reactions(dt_et, indexed_bars)
        rows.append({
            "period": d_s,
            "period_zh": d_s,
            "release_taipei": t_info["time_taipei"],
            "release_utc": t_info["time_utc"],
            "actual_str": f"{val:.2f}%",
            "actual_num": round(val, 2),
            "prev_str": f"{p_val:.2f}%",
            "prev_num": round(p_val, 2),
            "consensus_str": None,
            "consensus_source": None,
            "surprise": surp,
            "surprise_basis": basis,
            "prev_cmp": surp,
            "reactions": reactions,
            "note": "",
        })
    rows_desc = list(reversed(rows))

    verdict = "bear" if latest_v >= 4.5 else ("neut" if latest_v >= 3.5 else "bull")
    vtext = "🔴 偏高承壓" if verdict == "bear" else ("🟡 中性" if verdict == "neut" else "🟢 偏低寬鬆")

    return {
        "id": indicator_id,
        "category": "rates",
        "name": name,
        "code": fred_id,
        "period": latest_d,
        "value_str": f"{latest_v:.2f}%",
        "value_num": round(latest_v, 2),
        "unit": "",
        "prev_str": f"{prev_v:.2f}%（{prev_d}）",
        "change_str": chg_str,
        "change_dir": chg_dir,
        "release_date_taipei": rel_tpe["time_taipei"],
        "sub_info": f"每日美債市場殖利率（H.15 日收盤，美東 15:30 定盤）",
        "consensus_str": None,
        "consensus_source": None,
        "surprise": rows[-1]["surprise"],
        "surprise_basis": "prev",
        "verdict": verdict,
        "verdict_text": vtext,
        "chart_values": chart_vals,
        "chart_label": "近 24 個月月末殖利率（%）",
        "next_release": None,
        "why": why_text,
        "source": source_detail or (f"FRED（{fred_id}，截至 {latest_d}）" if source_used == "FRED" else f"{source_used}（截至 {latest_d}）"),
        "source_used": source_used,
        "data_status": "ok",
        "updating": False,
        "modal_title": f"{name}（近 24 個交易日）",
        "modal_foot_extra": "美債殖利率為交易日日頻利率，無固定公布預期值；本表統計最近 24 個交易日殖利率上升／下降當日與隔日市場表現。",
        "history_rows": rows_desc,
        "history_stats": _build_modal_stats(rows_desc),
    }


def _compute_fred_period_values(
    indicator_id: str,
    fred_rows: list[tuple[str, float | None]],
) -> tuple[dict[str, dict[str, Any]], list[float]]:
    """Convert raw FRED rows into period-indexed actual/prev/yoy values + 24-point chart series."""
    by_period: dict[str, dict[str, Any]] = {}
    chart_pts: list[float] = []

    if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI", "AVG_HOURLY_EARNINGS", "RETAIL_SALES"):
        # Monthly index -> compute MoM % and YoY %
        idx_by_ym: dict[str, float | None] = {d[:7]: v for d, v in fred_rows}
        for i, (d_s, val) in enumerate(fred_rows):
            ym = d_s[:7]
            yr, mo = int(ym[:4]), int(ym[5:7])
            prev_ym = f"{yr - 1:04d}-12" if mo == 1 else f"{yr:04d}-{mo - 1:02d}"
            yoy_ym = f"{yr - 1:04d}-{mo:02d}"
            prev_idx = idx_by_ym.get(prev_ym)
            yoy_idx = idx_by_ym.get(yoy_ym)
            mom = ((val / prev_idx) - 1.0) * 100.0 if (val is not None and prev_idx) else None
            yoy = ((val / yoy_idx) - 1.0) * 100.0 if (val is not None and yoy_idx) else None
            by_period[ym] = {"actual": mom, "index": val, "yoy": yoy, "date": d_s}
            if mom is not None:
                chart_pts.append(round(mom, 2))
        # Fill prev_actual from prior available period
        sorted_p = sorted(by_period.keys())
        for j, p in enumerate(sorted_p):
            prev_mom = None
            for k in range(j - 1, -1, -1):
                cand = by_period[sorted_p[k]]["actual"]
                if cand is not None:
                    prev_mom = cand
                    break
            by_period[p]["prev"] = prev_mom
        return by_period, chart_pts[-24:]

    if indicator_id == "NFP":
        clean = [(d, v) for d, v in fred_rows if v is not None]
        for i in range(1, len(clean)):
            d_s, val = clean[i]
            _, prev_level = clean[i - 1]
            diff_wan = (val - prev_level) / 10.0
            ym = d_s[:7]
            by_period[ym] = {"actual": diff_wan, "level_k": val, "date": d_s}
            chart_pts.append(round(diff_wan, 1))
        sorted_p = sorted(by_period.keys())
        for j, p in enumerate(sorted_p):
            by_period[p]["prev"] = by_period[sorted_p[j - 1]]["actual"] if j > 0 else None
        return by_period, chart_pts[-24:]

    if indicator_id == "UNRATE":
        for i, (d_s, val) in enumerate(fred_rows):
            ym = d_s[:7]
            by_period[ym] = {"actual": val, "date": d_s}
            if val is not None:
                chart_pts.append(round(val, 1))
        sorted_p = sorted(by_period.keys())
        for j, p in enumerate(sorted_p):
            prev_v = None
            for k in range(j - 1, -1, -1):
                cand = by_period[sorted_p[k]]["actual"]
                if cand is not None:
                    prev_v = cand
                    break
            by_period[p]["prev"] = prev_v
        return by_period, chart_pts[-24:]

    if indicator_id == "JOLTS":
        # FRED JTSJOL 與 BLS JTS000000000000000JOL 原始單位均為「千個」，換算為百萬個（保留一位小數顯示）
        clean = [(d, v) for d, v in fred_rows if v is not None]
        for d_s, val_k in clean:
            ym = d_s[:7]
            val_m = val_k / 1000.0
            by_period[ym] = {"actual": val_m, "level_k": val_k, "date": d_s}
            chart_pts.append(round(val_m, 2))
        sorted_p = sorted(by_period.keys())
        for j, p in enumerate(sorted_p):
            by_period[p]["prev"] = by_period[sorted_p[j - 1]]["actual"] if j > 0 else None
        return by_period, chart_pts[-24:]

    if indicator_id == "GDP":
        clean = [(d, v) for d, v in fred_rows if v is not None]
        for i, (d_s, val) in enumerate(clean):
            yr, mo = int(d_s[:4]), int(d_s[5:7])
            q = (mo - 1) // 3 + 1
            q_key = f"{yr:04d}-Q{q}"
            prev_v = clean[i - 1][1] if i > 0 else None
            by_period[q_key] = {"actual": val, "prev": prev_v, "date": d_s}
            chart_pts.append(round(val, 1))
        return by_period, chart_pts[-12:]

    return by_period, chart_pts


INDICATOR_SPECS: dict[str, dict[str, Any]] = {
    "CPI": {
        "category": "inflation",
        "name": "CPI 消費者物價",
        "code": "CPIAUCSL",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "一般家庭消費籃子的物價漲幅。通膨高於預期代表物價黏著、聯準會升息壓力上升；溫和降溫則有利美股與加密貨幣。",
        "hawkish_when_high": True,
    },
    "CORE_CPI": {
        "category": "inflation",
        "name": "核心 CPI",
        "code": "CPILFESL",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "剔除波動較大的食品與能源價格，最能反映服務與居住等核心通膨黏性，是聯準會緊盯的重點。",
        "hawkish_when_high": True,
    },
    "PCE": {
        "category": "inflation",
        "name": "PCE 物價指數",
        "code": "PCEPI",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "聯準會官方設定 2% 長期目標的基準通膨指標，涵蓋更廣泛消費替代效應，直接牽動 FOMC 利率路徑。",
        "hawkish_when_high": True,
    },
    "CORE_PCE": {
        "category": "inflation",
        "name": "核心 PCE",
        "code": "PCEPILFE",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "聯準會決策時最核心的通膨尺標；若環比長期高於 +0.2%（年率逾 2.4%），政策利率難以放鬆。",
        "hawkish_when_high": True,
    },
    "PPI": {
        "category": "inflation",
        "name": "PPI 生產者物價",
        "code": "PPIFIS",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "衡量企業批發與生產端成本變化，常作為 CPI 的上游領先訊號；上游成本攀升容易轉嫁至終端物價。",
        "hawkish_when_high": True,
    },
    "NFP": {
        "category": "jobs",
        "name": "非農就業新增",
        "code": "PAYEMS",
        "unit": "",
        "chart_label": "近 24 個月每月新增（萬人）",
        "why": "每月新增非農就業人數。在高利率環境中，就業過熱增加緊縮理由；有序放緩最理想，驟降至負值則引發衰退擔憂。",
        "hawkish_when_high": True,
    },
    "UNRATE": {
        "category": "jobs",
        "name": "失業率",
        "code": "UNRATE",
        "unit": "",
        "chart_label": "近 24 個月失業率 %",
        "why": "勞動市場鬆緊溫度計。失業率低表示勞動市場緊俏、薪資不易降溫；溫和緩升有助通膨回落，急升則為衰退警訊。",
        "hawkish_when_high": False,
    },
    "AVG_HOURLY_EARNINGS": {
        "category": "jobs",
        "name": "平均時薪",
        "code": "CES0500000003",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "反映薪資成長動能。薪資漲幅過快容易形成「薪資—物價螺旋」，推升核心服務通膨。",
        "hawkish_when_high": True,
    },
    "ICSA": {
        "category": "jobs",
        "name": "初領失業金人數",
        "code": "ICSA",
        "unit": "",
        "chart_label": "近 24 週初領人數（萬人）",
        "why": "每週公布的高頻就業指標。人數溫和上升顯示裁員增加與就業降溫，高利率期反而舒緩緊縮壓力；暴增則反映衰退風險。",
        "hawkish_when_high": False,
    },
    "JOLTS": {
        "category": "jobs",
        "name": "JOLTS 職位空缺",
        "code": "JTSJOL",
        "unit": "",
        "chart_label": "近 24 個月職位空缺（百萬個）",
        "why": "衡量美國企業招募需求與勞動市場供需缺口。職缺居高代表搶人壓力大、薪資與通膨不易降溫；適度回落有助聯準會評估降息，但驟降須留意景氣轉弱。",
        "hawkish_when_high": True,
    },
    "GDP": {
        "category": "growth",
        "name": "GDP 成長率",
        "code": "A191RL1Q225SBEA",
        "unit": "年化季增",
        "chart_label": "近 12 季年化季增 %",
        "why": "美國整體經濟成長速度。強勁成長支撐企業獲利，但也讓聯準會更有底氣維持高利率；轉負則引發衰退避險。",
        "hawkish_when_high": True,
    },
    "ISM_MFG": {
        "category": "growth",
        "name": "ISM 製造業 PMI",
        "code": "ISM Manufacturing",
        "unit": "點",
        "chart_label": "近 24 個月製造業 PMI",
        "why": "以 50 為榮枯線的美國製造業景氣指標。讀數與物價分項偏高代表景氣擴張且成本壓力仍在。",
        "hawkish_when_high": True,
    },
    "ISM_SVC": {
        "category": "growth",
        "name": "ISM 服務業 PMI",
        "code": "ISM Services",
        "unit": "點",
        "chart_label": "近 24 個月服務業 PMI",
        "why": "服務業占美國經濟產出大宗，50 以上為擴張；若服務業與物價分項維持高檔，核心通膨較難快速降溫。",
        "hawkish_when_high": True,
    },
    "RETAIL_SALES": {
        "category": "growth",
        "name": "零售銷售",
        "code": "RSAFS",
        "unit": "環比",
        "chart_label": "近 24 個月環比 %",
        "why": "美國民間消費動能指標（消費約占 GDP 七成）。穩健成長支撐景氣，過熱則強化高利率延續預期。",
        "hawkish_when_high": True,
    },
}


def _evaluate_verdict(
    indicator_id: str,
    actual: float | None,
    consensus: float | None,
    prev: float | None,
    cal_cfg: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if actual is None:
        return "neut", "🟡 資料不足"
    if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI"):
        if consensus is not None and round(actual, 1) > round(consensus, 1):
            return "bear", "🔴 偏熱利空"
        if actual >= 0.35:
            return "bear", "🔴 偏熱利空"
        if actual <= 0.15:
            return "bull", "🟢 降溫利多"
        return "neut", "🟡 中性偏黏"
    if indicator_id == "NFP":
        if consensus is not None and actual - consensus > 2.0:
            return "bear", "🔴 過熱利空"
        if actual < 0:
            return "bear", "🔴 轉負警訊"
        if actual >= 15.0:
            return "bear", "🔴 就業偏強"
        return "bull", "🟢 溫和降溫"
    if indicator_id == "UNRATE":
        if prev is not None and actual < prev:
            return "bear", "🔴 就業偏緊"
        if actual >= 4.5:
            return "neut", "🟡 關注放緩"
        return "bull", "🟢 有序降溫"
    if indicator_id == "ICSA":
        if prev is not None and actual > prev:
            return "bull", "🟢 略為降溫"
        return "neut", "🟡 就業仍穩"
    if indicator_id == "JOLTS":
        jrules = dict(DEFAULT_JOLTS_RULES)
        if cal_cfg:
            jrules.update((cal_cfg.get("indicator_rules") or {}).get("JOLTS") or {})
        thresh = float(jrules.get("diff_threshold_m", 0.2))
        sharp_drop = float(jrules.get("sharp_drop_m", 0.6))
        ref_val = consensus if consensus is not None else prev
        if ref_val is not None:
            diff = actual - ref_val
            if diff >= thresh - 1e-9:
                return "bear", "🔴 職缺偏緊"
            if diff <= -thresh + 1e-9:
                drop_from_prev = (prev - actual) if prev is not None else 0.0
                if drop_from_prev >= sharp_drop - 1e-9 or (-diff) >= sharp_drop - 1e-9:
                    return "bull", "🟢 降溫（留意景氣放緩）"
                return "bull", "🟢 溫和降溫"
            return "neut", "🟡 就業平穩"
        return "neut", "🟡 中性"
    if indicator_id in ("ISM_MFG", "ISM_SVC"):
        if actual >= 55.0:
            return "bear", "🔴 景氣偏熱"
        if actual < 50.0:
            return "neut", "🟡 收縮區間"
        if prev is not None and actual < prev:
            return "bull", "🟢 溫和降溫"
        return "neut", "🟡 溫和擴張"
    if indicator_id == "JOLTS":
        # FRED JTSJOL 與 BLS JTS000000000000000JOL 原始單位均為「千個」，換算為百萬個（保留一位小數顯示）
        clean = [(d, v) for d, v in fred_rows if v is not None]
        for d_s, val_k in clean:
            ym = d_s[:7]
            val_m = val_k / 1000.0
            by_period[ym] = {"actual": val_m, "level_k": val_k, "date": d_s}
            chart_pts.append(round(val_m, 2))
        sorted_p = sorted(by_period.keys())
        for j, p in enumerate(sorted_p):
            by_period[p]["prev"] = by_period[sorted_p[j - 1]]["actual"] if j > 0 else None
        return by_period, chart_pts[-24:]

    if indicator_id == "GDP":
        if actual >= 3.0:
            return "bear", "🔴 成長偏熱"
        if actual < 0:
            return "bear", "🔴 負成長警訊"
        return "neut", "🟡 溫和成長"
    return "neut", "🟡 中性"


def _build_standard_indicator_card(
    indicator_id: str,
    cal_cfg: dict[str, Any],
    fred_rows: list[tuple[str, float | None]] | None,
    te_latest: dict[str, Any] | None,
    indexed_bars: dict[str, list[tuple[date, OHLCVBar]]],
    now_utc: datetime,
    source_used: str | None = None,
    source_detail: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build card + calendar past events for one standard (monthly/quarterly/weekly) indicator."""
    spec = INDICATOR_SPECS[indicator_id]
    ind_cfg = (cal_cfg.get("indicators") or {}).get(indicator_id) or {}
    stars = int(ind_cfg.get("stars", 2))

    # Build past release entries
    if indicator_id == "ICSA":
        assert fred_rows is not None
        past_entries, upcoming_entries = _build_icsa_schedule_map(fred_rows, cal_cfg, now_utc)
        clean_icsa = [round(v / 10000.0, 1) for _, v in fred_rows if v is not None]
        chart_vals = clean_icsa[-24:]
        latest_period = past_entries[-1]["period"]
        latest_actual = past_entries[-1]["actual"]
        latest_prev = past_entries[-1]["prev"]
        sub_info = "週頻率初請人數（單位：萬人）"
    elif indicator_id in ("ISM_MFG", "ISM_SVC"):
        past_entries = [dict(x) for x in (ind_cfg.get("past_releases") or [])]
        upcoming_entries = [dict(x) for x in (ind_cfg.get("upcoming_releases") or [])]
        # Merge newer TE reading if available
        known_periods = {e["period"]: e for e in past_entries}
        if te_latest and te_latest.get("period") and te_latest["period"] not in known_periods:
            # Match against upcoming_entries if present
            matched_up = next((u for u in upcoming_entries if u.get("period") == te_latest["period"]), None)
            rel_et = matched_up["release_et"] if matched_up else f"{te_latest['period']}-03T10:00:00"
            past_entries.append({
                "period": te_latest["period"],
                "release_et": rel_et,
                "actual": te_latest["actual"],
                "consensus": te_latest.get("consensus"),
                "consensus_source": te_latest.get("consensus_source"),
            })
            upcoming_entries = [u for u in upcoming_entries if u.get("period") != te_latest["period"]]
        elif te_latest and te_latest.get("period") in known_periods:
            # Refresh consensus if TE had it and YAML didn't
            entry = known_periods[te_latest["period"]]
            if entry.get("consensus") is None and te_latest.get("consensus") is not None:
                entry["consensus"] = te_latest["consensus"]
                entry["consensus_source"] = te_latest.get("consensus_source")

        past_entries.sort(key=lambda e: str(e.get("period", "")))
        for i, e in enumerate(past_entries):
            e["prev"] = past_entries[i - 1]["actual"] if i > 0 else None
        chart_vals = [float(e["actual"]) for e in past_entries if e.get("actual") is not None][-24:]
        latest_entry = past_entries[-1]
        latest_period = latest_entry["period"]
        latest_actual = float(latest_entry["actual"])
        latest_prev = float(latest_entry["prev"]) if latest_entry.get("prev") is not None else None
        sub_info = f"50 為榮枯線｜{latest_entry.get('note') or 'ISM 官方月度報告'}"
    else:
        assert fred_rows is not None
        by_period, chart_vals = _compute_fred_period_values(indicator_id, fred_rows)
        past_entries = [dict(x) for x in (ind_cfg.get("past_releases") or [])]
        if indicator_id == "JOLTS":
            upcoming_entries = _build_jolts_upcoming_releases(cal_cfg, now_utc)
        else:
            upcoming_entries = [dict(x) for x in (ind_cfg.get("upcoming_releases") or [])]

        # Auto-promote any upcoming release whose period already appeared in FRED!
        known_periods = {e["period"] for e in past_entries}
        remaining_up: list[dict[str, Any]] = []
        for u in upcoming_entries:
            p = str(u.get("period", ""))
            if p not in known_periods and p in by_period and by_period[p].get("actual") is not None:
                past_entries.append({
                    "period": p,
                    "release_et": u["release_et"],
                    "consensus": u.get("consensus"),
                    "consensus_source": u.get("consensus_source"),
                    "note": u.get("note", ""),
                })
                known_periods.add(p)
            else:
                remaining_up.append(u)
        # For JOLTS, also auto-append any newer period in FRED/BLS not listed in YAML
        if indicator_id == "JOLTS":
            max_known = max(known_periods) if known_periods else ""
            for p in sorted(by_period.keys()):
                if p not in known_periods and p > max_known and by_period[p].get("actual") is not None:
                    rel_et = _estimate_jolts_release_et(p, ind_cfg.get("schedule_overrides") or {})
                    past_entries.append({
                        "period": p,
                        "release_et": rel_et,
                        "consensus": None,
                        "consensus_source": None,
                        "note": "",
                    })
                    known_periods.add(p)
        upcoming_entries = remaining_up

        for e in past_entries:
            p_data = by_period.get(str(e.get("period", ""))) or {}
            e["actual"] = p_data.get("actual")
            e["prev"] = p_data.get("prev")
            e["yoy"] = p_data.get("yoy")
            e["index"] = p_data.get("index")
            e["level_k"] = p_data.get("level_k")

        # Latest period from FRED directly
        valid_periods = [p for p, d in sorted(by_period.items()) if d.get("actual") is not None]
        latest_period = valid_periods[-1]
        latest_d = by_period[latest_period]
        latest_actual = latest_d["actual"]
        latest_prev = latest_d.get("prev")
        if indicator_id in ("CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI"):
            yoy_s = f" · 同比 {latest_d['yoy']:+.2f}%" if latest_d.get("yoy") is not None else ""
            idx_s = f"指數 {latest_d['index']:.3f}" if latest_d.get("index") is not None else ""
            sub_info = f"{idx_s}{yoy_s}".strip(" ·")
        elif indicator_id == "AVG_HOURLY_EARNINGS":
            yoy_s = f" · 同比 {latest_d['yoy']:+.2f}%" if latest_d.get("yoy") is not None else ""
            idx_s = f"時薪 ${latest_d['index']:.2f}" if latest_d.get("index") is not None else ""
            sub_info = f"{idx_s}{yoy_s}".strip(" ·")
        elif indicator_id == "NFP":
            lvl = latest_d.get("level_k")
            sub_info = f"總就業 {lvl:,.0f} 千人" if lvl is not None else "每月非農就業人口變動"
        elif indicator_id == "JOLTS":
            lvl = latest_d.get("level_k")
            sub_info = f"職缺總數 {lvl:,.0f} 千個（單位：百萬個）" if lvl is not None else "BLS JOLTS 非農職位空缺"
        elif indicator_id == "RETAIL_SALES":
            lvl = latest_d.get("index")
            sub_info = f"總額 {lvl:,.0f} 百萬美元" if lvl is not None else "美國零售與餐飲銷售"
        elif indicator_id == "GDP":
            sub_info = "實質 GDP 季增年化率"
        else:
            sub_info = "BLS 官方月度調查"

    past_entries = past_entries[-24:]
    rows: list[dict[str, Any]] = []
    cal_past_items: list[dict[str, Any]] = []

    for e in past_entries:
        rel_dt = _parse_et_iso(e["release_et"])
        t_info = _fmt_tpe(rel_dt)
        fred_act = e.get("actual")
        act_for_surp = e.get("actual_as_released") if e.get("actual_as_released") is not None else fred_act
        act_display = act_for_surp if act_for_surp is not None else fred_act
        prev_val = e.get("prev")
        cons_val = e.get("consensus")
        surp, basis = _classify_surprise(indicator_id, act_for_surp, cons_val, prev_val)
        prev_cmp, _ = _classify_surprise(indicator_id, act_display, None, prev_val)
        reactions = compute_market_reactions(rel_dt, indexed_bars)

        note_parts = [str(e.get("note", "")).strip()] if e.get("note") else []
        if e.get("actual_as_released") is not None and fred_act is not None:
            if abs(float(e["actual_as_released"]) - float(fred_act)) >= 0.08:
                note_parts.append(f"FRED 修正後最新值 {_fmt_val_unit(indicator_id, float(fred_act))}")

        row = {
            "period": str(e["period"]),
            "period_zh": _period_zh(str(e["period"])),
            "release_taipei": t_info["time_taipei"],
            "release_utc": t_info["time_utc"],
            "actual_str": _fmt_val_unit(indicator_id, float(act_display)) if act_display is not None else "資料不足",
            "actual_num": round(float(act_display), 2) if act_display is not None else None,
            "prev_str": _fmt_val_unit(indicator_id, float(prev_val)) if prev_val is not None else "資料不足",
            "prev_num": round(float(prev_val), 2) if prev_val is not None else None,
            "consensus_str": _fmt_consensus(indicator_id, float(cons_val)) if cons_val is not None else None,
            "consensus_source": e.get("consensus_source"),
            "surprise": surp,
            "surprise_basis": basis,
            "prev_cmp": prev_cmp,
            "reactions": reactions,
            "note": "；".join(p for p in note_parts if p),
        }
        rows.append(row)

        # Build past calendar entry (avoid duplicating UNRATE/AVG_HOURLY_EARNINGS onto NFP date in main calendar)
        if indicator_id not in ("UNRATE", "AVG_HOURLY_EARNINGS", "CORE_CPI", "CORE_PCE"):
            cal_past_items.append({
                "id": f"{indicator_id.lower()}-{e['period']}",
                "title": f"{spec['name']}（{_period_zh(str(e['period']))}）",
                "indicator_id": indicator_id,
                "period": str(e["period"]),
                "stars": stars,
                "time_utc": t_info["time_utc"],
                "time_taipei": t_info["time_taipei"],
                "time_taipei_iso": t_info["time_taipei_iso"],
                "short_taipei": t_info["short_taipei"],
                "actual_str": row["actual_str"],
                "prev_str": row["prev_str"],
                "consensus_str": row["consensus_str"],
                "consensus_source": row["consensus_source"],
                "surprise": row["surprise"],
                "surprise_basis": row["surprise_basis"],
                "reactions": reactions,
                "note": row["note"],
            })

    rows_desc = list(reversed(rows))
    last_row = rows[-1] if rows else {}
    chg_str, chg_dir = _fmt_change(indicator_id, latest_actual, latest_prev)
    latest_cons = past_entries[-1].get("consensus") if past_entries else None
    latest_cons_src = past_entries[-1].get("consensus_source") if past_entries else None
    verdict, vtext = _evaluate_verdict(indicator_id, latest_actual, latest_cons, latest_prev, cal_cfg=cal_cfg)

    next_up = _find_next_upcoming(upcoming_entries, now_utc)
    if indicator_id in ("UNRATE", "AVG_HOURLY_EARNINGS"):
        next_up = _find_next_upcoming(list((cal_cfg.get("indicators", {}).get("NFP") or {}).get("upcoming_releases") or []), now_utc)

    eff_source_used = source_used or (
        "TradingEconomics ＋ ISM" if indicator_id in ("ISM_MFG", "ISM_SVC") else "FRED"
    )
    source_str = source_detail or (
        "TradingEconomics 公開頁面 ＋ ISM 報告"
        if indicator_id in ("ISM_MFG", "ISM_SVC")
        else (f"FRED（{spec['code']}）" if eff_source_used == "FRED" else f"{eff_source_used}（{spec['code']}）")
    )
    if indicator_id in ("UNRATE", "AVG_HOURLY_EARNINGS"):
        modal_foot_extra = f"BLS 就業報告固定與非農就業同日公布。歷史實際值取自 {eff_source_used} 最新官方序列。"
    elif indicator_id in ("ISM_MFG", "ISM_SVC"):
        modal_foot_extra = "ISM 製造業／服務業 PMI 來自 ISM 官方報告與 TradingEconomics 公開存檔。"
    else:
        modal_foot_extra = f"歷史實際值以 {eff_source_used} 最新官方序列為基準；部分前期經官方修正之項目於備註標示公布當下初值與意外比較。"

    card = {
        "id": indicator_id,
        "category": spec["category"],
        "name": spec["name"],
        "code": spec["code"],
        "period": _period_zh(latest_period),
        "value_str": _fmt_val_unit(indicator_id, latest_actual),
        "value_num": round(float(latest_actual), 2) if latest_actual is not None else None,
        "unit": spec["unit"],
        "prev_str": _fmt_val_unit(indicator_id, latest_prev) if latest_prev is not None else "資料不足",
        "change_str": chg_str,
        "change_dir": chg_dir,
        "release_date_taipei": last_row.get("release_taipei", latest_period),
        "sub_info": sub_info,
        "consensus_str": _fmt_consensus(indicator_id, float(latest_cons)) if latest_cons is not None else None,
        "consensus_source": latest_cons_src,
        "surprise": last_row.get("surprise", "—"),
        "surprise_basis": last_row.get("surprise_basis", "none"),
        "verdict": verdict,
        "verdict_text": vtext,
        "chart_values": chart_vals,
        "chart_label": spec["chart_label"],
        "next_release": next_up,
        "why": spec["why"],
        "source": source_str,
        "source_used": eff_source_used,
        "data_status": "ok",
        "updating": False,
        "modal_title": spec["name"],
        "modal_foot_extra": modal_foot_extra,
        "history_rows": rows_desc,
        "history_stats": _build_modal_stats(rows_desc),
    }
    return card, cal_past_items


def compute_fomc_blackout_start(decision_date: date) -> date:
    """Compute Fed FOMC blackout period start date (2nd Saturday before the meeting starts)."""
    meeting_start = decision_date - timedelta(days=1)
    days_since_sat = (meeting_start.weekday() - 5) % 7
    first_sat = meeting_start - timedelta(days=7 if days_since_sat == 0 else days_since_sat)
    return first_sat - timedelta(days=7)


def _estimate_jolts_release_et(period_ym: str, overrides: dict[str, Any] | None = None) -> str:
    """Estimate BLS JOLTS release timestamp (10:00 ET on first Tuesday of M+2)."""
    if overrides and period_ym in overrides:
        return str(overrides[period_ym])
    yr, mo = int(period_ym[:4]), int(period_ym[5:7])
    r_mo = mo + 2
    r_yr = yr
    while r_mo > 12:
        r_mo -= 12
        r_yr += 1
    d = date(r_yr, r_mo, 1)
    while d.weekday() != 1:
        d += timedelta(days=1)
    if (d.month, d.day) in ((1, 1), (7, 4), (11, 11), (12, 25)):
        d += timedelta(days=1)
    return f"{d.isoformat()}T10:00:00"


def _build_jolts_upcoming_releases(cal_cfg: dict[str, Any], now_utc: datetime) -> list[dict[str, Any]]:
    """Build upcoming JOLTS releases using config overrides or BLS-convention estimates."""
    ind_cfg = (cal_cfg.get("indicators") or {}).get("JOLTS") or {}
    past_periods = {str(e.get("period", "")) for e in (ind_cfg.get("past_releases") or [])}
    overrides = ind_cfg.get("schedule_overrides") or {}
    explicit_by_p = {
        str(u.get("period", "")): dict(u)
        for u in (ind_cfg.get("upcoming_releases") or [])
        if u.get("period")
    }
    stars = int(ind_cfg.get("stars", 2))
    out: list[dict[str, Any]] = []
    base_y, base_m = now_utc.year, now_utc.month - 2
    while base_m <= 0:
        base_m += 12
        base_y -= 1
    for offset in range(14):
        mo = base_m + offset
        yr = base_y + (mo - 1) // 12
        mo = ((mo - 1) % 12) + 1
        p = f"{yr:04d}-{mo:02d}"
        if p in past_periods:
            continue
        if p in explicit_by_p:
            item = explicit_by_p[p]
            item.setdefault("stars", stars)
            out.append(item)
            continue
        has_override = p in overrides
        rel_et = _estimate_jolts_release_et(p, overrides)
        try:
            dt_utc = _parse_et_iso(rel_et).astimezone(UTC_TZ)
        except Exception:
            continue
        if dt_utc <= now_utc:
            continue
        out.append({
            "period": p,
            "release_et": rel_et,
            "stars": stars,
            "estimated": not has_override,
            "note": "" if has_override else "預估日期（BLS 慣例：隔隔月首個週二美東 10:00）",
        })
    return out


def zq_contract_symbol(year: int, month: int) -> str:
    """Return yfinance CBOT 30-Day Fed Funds futures symbol (e.g. ZQZ26.CBT, ZQF27.CBT)."""
    code = ZQ_MONTH_CODES[int(month)]
    return f"ZQ{code}{year % 100:02d}.CBT"


def fetch_yfinance_history(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
    """Fetch daily closing prices via yfinance without API key."""
    import yfinance as yf  # lazy import so tests without network stay fast

    df = yf.Ticker(symbol).history(period=period)
    if df is None or len(df) == 0 or "Close" not in df:
        return []
    rows: list[tuple[str, float]] = []
    for idx, val in df["Close"].items():
        if val is None or (isinstance(val, float) and math.isnan(val)):
            continue
        d_s = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        rows.append((d_s, float(val)))
    rows.sort(key=lambda x: x[0])
    return rows


def _fedwatch_buckets_from_steps(delta_steps: float) -> dict[str, float]:
    """Decompose expected 25bp step change from current rate into 5 probability buckets."""
    raw: dict[int, float] = {}
    low_step = math.floor(delta_steps)
    frac = delta_steps - low_step
    if abs(frac) < 1e-9:
        raw[low_step] = 100.0
    elif abs(1.0 - frac) < 1e-9:
        raw[low_step + 1] = 100.0
    else:
        raw[low_step] = (1.0 - frac) * 100.0
        raw[low_step + 1] = frac * 100.0

    buckets = {"cut_2_plus": 0.0, "cut_1": 0.0, "hold": 0.0, "hike_1": 0.0, "hike_2_plus": 0.0}
    for step, prob in raw.items():
        if step <= -2:
            buckets["cut_2_plus"] += prob
        elif step == -1:
            buckets["cut_1"] += prob
        elif step == 0:
            buckets["hold"] += prob
        elif step == 1:
            buckets["hike_1"] += prob
        else:
            buckets["hike_2_plus"] += prob

    # Round to 1 decimal while preserving sum == 100.0
    rounded = {k: round(v, 1) for k, v in buckets.items()}
    diff = round(100.0 - sum(rounded.values()), 1)
    if abs(diff) >= 0.05:
        top_k = max(rounded.keys(), key=lambda k: rounded[k])
        rounded[top_k] = round(rounded[top_k] + diff, 1)
    return rounded


def _fmt_prob_pct(val: float) -> str:
    if abs(val - round(val)) < 0.05:
        return f"{int(round(val))}%"
    return f"{val:.1f}%"


def _build_fedwatch_meeting_summary(probabilities: dict[str, float], date_mmdd: str) -> str:
    """Format top-2 FedWatch probability summary line (e.g. '市場押注 12/10 會議：升 1 碼 93%｜維持 7%')."""
    label_map = dict(FEDWATCH_BUCKETS)
    ranked = [
        (label_map[k], float(probabilities.get(k, 0.0)))
        for k, _ in FEDWATCH_BUCKETS
        if float(probabilities.get(k, 0.0)) > 0.5
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    if not ranked:
        return f"市場押注 {date_mmdd} 會議：資料計算中"
    parts = [f"{lbl} {_fmt_prob_pct(p)}" for lbl, p in ranked[:2]]
    return f"市場押注 {date_mmdd} 會議：{'｜'.join(parts)}"


def compute_fedwatch_from_prices(
    meetings_meta: list[dict[str, Any]],
    current_low: float,
    current_high: float,
    prices_by_symbol: dict[str, float],
    as_of_taipei: str,
    source_used: str = "yfinance (CBOT ZQ)",
) -> dict[str, Any]:
    """Compute CME FedWatch-style probabilities sequentially for up to 2 FOMC meetings."""
    current_mid = (float(current_low) + float(current_high)) / 2.0
    current_range = f"{float(current_low):.2f}%–{float(current_high):.2f}%"
    r_pre = current_mid
    meetings_out: list[dict[str, Any]] = []

    for m in meetings_meta[:2]:
        dt_et = _parse_et_iso(m["release_et"])
        m_date = dt_et.date()
        t_info = _fmt_tpe(dt_et)
        days_in_month = calendar.monthrange(m_date.year, m_date.month)[1]
        in_last_7_days = (days_in_month - m_date.day) < 7

        if in_last_7_days:
            c_yr = m_date.year + (1 if m_date.month == 12 else 0)
            c_mo = 1 if m_date.month == 12 else m_date.month + 1
            sym = zq_contract_symbol(c_yr, c_mo)
        else:
            sym = zq_contract_symbol(m_date.year, m_date.month)

        if sym not in prices_by_symbol:
            raise RuntimeError(f"Missing ZQ futures price for {sym}")
        f_price = float(prices_by_symbol[sym])
        implied_avg = 100.0 - f_price

        if in_last_7_days:
            # 會議落於月底最後 7 天內，改用下個月期貨合約推算會後利率
            r_post = implied_avg
        else:
            days_before = m_date.day
            days_after = days_in_month - m_date.day
            r_post = (implied_avg * days_in_month - r_pre * days_before) / days_after

        delta_steps = (r_post - current_mid) / 0.25
        probs = _fedwatch_buckets_from_steps(delta_steps)
        mmdd = f"{int(t_info['date_taipei'][5:7])}/{int(t_info['date_taipei'][8:10])}"
        summary_text = _build_fedwatch_meeting_summary(probs, mmdd)

        meetings_out.append({
            "period": str(m.get("period", m_date.strftime("%Y-%m"))),
            "title": str(m.get("title", "FOMC 利率決議")),
            "date": m_date.isoformat(),
            "date_taipei": t_info["date_taipei"],
            "time_taipei": t_info["time_taipei"],
            "contract_symbol": sym,
            "used_next_month_contract": in_last_7_days,
            "futures_price": round(f_price, 4),
            "implied_rate": round(r_post, 3),
            "current_range": current_range,
            "probabilities": probs,
            "summary_text": summary_text,
            "source_used": source_used,
            "as_of_taipei": as_of_taipei,
        })
        r_pre = r_post

    return {
        "meetings": meetings_out,
        "current_range": current_range,
        "current_mid": round(current_mid, 3),
        "source_used": source_used,
        "as_of_taipei": as_of_taipei,
        "data_status": "ok",
        "updating": False,
        "disclaimer": "機率由聯邦基金期貨價格推算，方法同 CME FedWatch；僅代表市場押注，不是預測。",
    }


def _select_zq_symbol_for_meeting(release_et: str) -> str:
    m_date = _parse_et_iso(release_et).date()
    days_in_month = calendar.monthrange(m_date.year, m_date.month)[1]
    if (days_in_month - m_date.day) < 7:
        c_yr = m_date.year + (1 if m_date.month == 12 else 0)
        c_mo = 1 if m_date.month == 12 else m_date.month + 1
        return zq_contract_symbol(c_yr, c_mo)
    return zq_contract_symbol(m_date.year, m_date.month)


def build_fedwatch_data(
    cal_cfg: dict[str, Any],
    now_utc: datetime,
    prev_doc: dict[str, Any],
    yf_fetcher: Callable[[str, str], list[tuple[str, float]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch CBOT ZQ futures for next 2 FOMC meetings with manual (<7d) and prev-doc fallback."""
    t0 = time_mod.monotonic()
    fetcher = yf_fetcher or fetch_yfinance_history
    now_tpe = now_utc.astimezone(TPE_TZ)
    as_of_tpe = now_tpe.strftime("%Y-%m-%d %H:%M")

    fomc_cfg = cal_cfg.get("fomc") or {}
    past_decs = list(fomc_cfg.get("past_decisions") or [])
    last_dec = past_decs[-1] if past_decs else {"rate_low": 3.75, "rate_high": 4.00}
    cur_lo = float(last_dec.get("rate_low", 3.75))
    cur_hi = float(last_dec.get("rate_high", 4.00))

    upcoming = []
    for item in (fomc_cfg.get("upcoming") or []):
        try:
            dt = _parse_et_iso(item["release_et"]).astimezone(UTC_TZ)
        except Exception:
            continue
        if dt > now_utc:
            upcoming.append(item)
        if len(upcoming) >= 2:
            break

    prices: dict[str, float] = {}
    fetch_errs: list[str] = []
    for item in upcoming:
        sym = _select_zq_symbol_for_meeting(item["release_et"])
        if sym in prices:
            continue
        try:
            rows = fetcher(sym, "1mo")
            if rows:
                prices[sym] = float(rows[-1][1])
            else:
                fetch_errs.append(f"{sym}: empty")
        except Exception as exc:
            fetch_errs.append(f"{sym}: {exc}")

    # 1) Primary: computed from ZQ futures
    if upcoming and _select_zq_symbol_for_meeting(upcoming[0]["release_et"]) in prices:
        usable_meetings = [m for m in upcoming if _select_zq_symbol_for_meeting(m["release_et"]) in prices]
        try:
            fw = compute_fedwatch_from_prices(
                usable_meetings, cur_lo, cur_hi, prices, as_of_taipei=as_of_tpe, source_used="yfinance (CBOT ZQ)"
            )
            rep = {
                "status": "ok" if not fetch_errs else "partial",
                "source_used": "yfinance (CBOT ZQ)",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "contracts": sorted(prices.keys()),
                "error": "; ".join(fetch_errs) if fetch_errs else None,
            }
            return fw, rep
        except Exception as exc:
            fetch_errs.append(str(exc))

    # 2) Manual fallback (< 7 days)
    manual_cfg = cal_cfg.get("fedwatch_manual") or {}
    if isinstance(manual_cfg, dict) and manual_cfg.get("meetings") and manual_cfg.get("as_of_date"):
        try:
            as_of_d = date.fromisoformat(str(manual_cfg["as_of_date"])[:10])
            age_days = (now_tpe.date() - as_of_d).days
            if -1 <= age_days <= 7:
                man_src = str(manual_cfg.get("source") or "CME FedWatch 手動更新")
                src_label = f"手動更新（{man_src}）"
                m_out: list[dict[str, Any]] = []
                for idx, mm in enumerate(manual_cfg["meetings"][:2]):
                    probs = {k: float((mm.get("probabilities") or {}).get(k, 0.0)) for k, _ in FEDWATCH_BUCKETS}
                    m_date_s = str(mm.get("date", ""))
                    tpe_date = m_date_s
                    if idx < len(upcoming):
                        t_info = _fmt_tpe(_parse_et_iso(upcoming[idx]["release_et"]))
                        tpe_date = t_info["date_taipei"]
                    mmdd = f"{int(tpe_date[5:7])}/{int(tpe_date[8:10])}" if len(tpe_date) >= 10 else m_date_s
                    m_out.append({
                        "period": str(mm.get("period", m_date_s[:7])),
                        "title": str(mm.get("title") or (upcoming[idx].get("title") if idx < len(upcoming) else "FOMC 利率決議")),
                        "date": m_date_s,
                        "date_taipei": tpe_date,
                        "implied_rate": mm.get("implied_rate"),
                        "current_range": str(mm.get("current_range") or f"{cur_lo:.2f}%–{cur_hi:.2f}%"),
                        "probabilities": probs,
                        "summary_text": _build_fedwatch_meeting_summary(probs, mmdd),
                        "source_used": src_label,
                        "as_of_taipei": str(manual_cfg["as_of_date"]),
                    })
                fw_man = {
                    "meetings": m_out,
                    "current_range": f"{cur_lo:.2f}%–{cur_hi:.2f}%",
                    "current_mid": round((cur_lo + cur_hi) / 2.0, 3),
                    "source_used": src_label,
                    "as_of_taipei": str(manual_cfg["as_of_date"]),
                    "data_status": "ok",
                    "updating": False,
                    "disclaimer": "機率由聯邦基金期貨價格推算，方法同 CME FedWatch；僅代表市場押注，不是預測。",
                }
                rep = {
                    "status": "ok",
                    "source_used": src_label,
                    "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                    "error": "; ".join(fetch_errs) if fetch_errs else None,
                }
                return fw_man, rep
        except Exception as exc:
            fetch_errs.append(f"manual parse: {exc}")

    # 3) Preserve previous macro.json fedwatch with 資料更新中
    err_str = "; ".join(fetch_errs) or "期貨報價無法取得"
    prev_fw = prev_doc.get("fedwatch")
    if isinstance(prev_fw, dict) and prev_fw.get("meetings"):
        preserved = dict(prev_fw)
        preserved["data_status"] = "資料更新中"
        preserved["updating"] = True
        preserved["updating_notice"] = f"資料更新中（{err_str}，暫保留前次資料）"
        preserved["meetings"] = [
            {**m, "data_status": "資料更新中", "updating": True}
            for m in prev_fw["meetings"]
        ]
        rep = {
            "status": "failed",
            "source_used": prev_fw.get("source_used") or "前次資料（資料更新中）",
            "elapsed_sec": round(time_mod.monotonic() - t0, 3),
            "error": err_str,
        }
        return preserved, rep

    empty_fw = {
        "meetings": [],
        "current_range": f"{cur_lo:.2f}%–{cur_hi:.2f}%",
        "current_mid": round((cur_lo + cur_hi) / 2.0, 3),
        "source_used": None,
        "as_of_taipei": as_of_tpe,
        "data_status": "資料更新中",
        "updating": True,
        "updating_notice": f"資料更新中（{err_str}）",
        "disclaimer": "機率由聯邦基金期貨價格推算，方法同 CME FedWatch；僅代表市場押注，不是預測。",
    }
    return empty_fw, {
        "status": "failed",
        "source_used": None,
        "elapsed_sec": round(time_mod.monotonic() - t0, 3),
        "error": err_str,
    }


def _eval_snapshot_item(
    item_id: str,
    rows: list[tuple[str, float]],
    rule: dict[str, Any],
    as_of_taipei: str,
    source_used: str,
) -> dict[str, Any]:
    """Compute snapshot metrics, 1Y range position, 90d closes, and verdict for one asset."""
    clean = [(d, float(v)) for d, v in rows if v is not None]
    if len(clean) < 2:
        raise ValueError(f"Insufficient rows for market snapshot {item_id}")
    closes_1y = [v for _, v in clean[-252:]]
    last_v = closes_1y[-1]
    prev_1d = closes_1y[-2]
    ref_20d = closes_1y[-21] if len(closes_1y) >= 21 else closes_1y[0]

    chg_1d_pct = ((last_v / prev_1d) - 1.0) * 100.0 if prev_1d else 0.0
    chg_20d_pct = ((last_v / ref_20d) - 1.0) * 100.0 if ref_20d else 0.0
    chg_1d_pp = last_v - prev_1d
    chg_20d_pp = last_v - ref_20d

    lo_1y = min(closes_1y)
    hi_1y = max(closes_1y)
    range_pos = round(((last_v - lo_1y) / (hi_1y - lo_1y)) * 100.0, 1) if hi_1y > lo_1y else 50.0
    range_pos = max(0.0, min(100.0, range_pos))

    dec = int(rule.get("decimals", 2))
    closes_90d = [round(v, dec) for v in closes_1y[-90:]]

    if item_id == "DXY":
        bear_th = float(rule.get("chg_20d_bear_pct", 1.5))
        bull_th = float(rule.get("chg_20d_bull_pct", -1.5))
        if chg_20d_pct > bear_th:
            verdict, vtag = "bear", "🔴 偏空（美元轉強）"
            reason = f"美元指數近 20 日上漲 {chg_20d_pct:+.2f}%（超過 +{bear_th:.1f}% 門檻），美元轉強易對台股外資與加密貨幣流動性構成壓力。"
        elif chg_20d_pct < bull_th:
            verdict, vtag = "bull", "🟢 偏多（美元轉弱）"
            reason = f"美元指數近 20 日下跌 {chg_20d_pct:+.2f}%（低於 {bull_th:.1f}% 門檻），美元轉弱有利新興市場資金與風險資產。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"美元指數近 20 日變動 {chg_20d_pct:+.2f}%，處於 ±{bear_th:.1f}% 中性區間。"
    elif item_id == "USDTWD":
        bear_th = float(rule.get("chg_20d_bear_pct", 1.0))
        bull_th = float(rule.get("chg_20d_bull_pct", -1.0))
        if chg_20d_pct > bear_th:
            verdict, vtag = "bear", "🔴 偏空（台幣貶值）"
            reason = f"美元／台幣近 20 日上漲 {chg_20d_pct:+.2f}%（台幣貶值逾 {bear_th:.1f}%），反映外資匯出壓力，對台股偏空。"
        elif chg_20d_pct < bull_th:
            verdict, vtag = "bull", "🟢 偏多（台幣升值）"
            reason = f"美元／台幣近 20 日下跌 {chg_20d_pct:+.2f}%（台幣升值逾 {abs(bull_th):.1f}%），代表外資匯入動能，對台股偏多。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"美元／台幣近 20 日變動 {chg_20d_pct:+.2f}%，匯率維持於 ±{bear_th:.1f}% 區間整理。"
    elif item_id == "HSI":
        bull_th = float(rule.get("chg_20d_bull_pct", 3.0))
        bear_th = float(rule.get("chg_20d_bear_pct", -3.0))
        if chg_20d_pct > bull_th:
            verdict, vtag = "bull", "🟢 偏多（亞股轉強）"
            reason = f"恒生指數近 20 日上漲 {chg_20d_pct:+.2f}%（超過 +{bull_th:.1f}% 門檻），顯示亞洲風險偏好與中港資金情緒回溫，對台股偏正面。"
        elif chg_20d_pct < bear_th:
            verdict, vtag = "bear", "🔴 偏空（亞股轉弱）"
            reason = f"恒生指數近 20 日下跌 {chg_20d_pct:+.2f}%（低於 {bear_th:.1f}% 門檻），反映亞洲風險情緒承壓，需留意對台股與亞股氣氛的拖累。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"恒生指數近 20 日變動 {chg_20d_pct:+.2f}%，處於 ±{bull_th:.1f}% 中性區間。"
    elif item_id == "GOLD":
        bull_th = float(rule.get("chg_20d_bull_pct", 3.0))
        bear_th = float(rule.get("chg_20d_bear_pct", -3.0))
        if chg_20d_pct > bull_th:
            verdict, vtag = "bull", "🟢 偏多（避險／抗通膨）"
            reason = f"黃金近 20 日上漲 {chg_20d_pct:+.2f}%（超過 +{bull_th:.1f}%），反映寬鬆預期或避險買盤（對非美／抗通膨題材偏多，亦留意避險情緒）。"
        elif chg_20d_pct < bear_th:
            verdict, vtag = "bear", "🔴 偏空（強美元承壓）"
            reason = f"黃金近 20 日下跌 {chg_20d_pct:+.2f}%（低於 {bear_th:.1f}%），反映實質利率上行或美元走強壓抑無息資產。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"黃金近 20 日變動 {chg_20d_pct:+.2f}%，位於一年區間 {range_pos:.0f}% 位置，走勢偏震盪。"
    elif item_id == "WTI":
        bear_th = float(rule.get("chg_20d_bear_pct", 5.0))
        bull_th = float(rule.get("chg_20d_bull_pct", -5.0))
        if chg_20d_pct > bear_th:
            verdict, vtag = "bear", "🔴 偏空（通膨壓力）"
            reason = f"WTI 原油近 20 日上漲 {chg_20d_pct:+.2f}%（超過 +{bear_th:.1f}%），能源價格走高易推升通膨預期並壓抑降息空間。"
        elif chg_20d_pct < bull_th:
            verdict, vtag = "bull", "🟢 偏多（通膨舒緩）"
            reason = f"WTI 原油近 20 日下跌 {chg_20d_pct:+.2f}%（低於 {bull_th:.1f}%），油價回落有助通膨降溫，舒緩緊縮預期。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"WTI 原油近 20 日變動 {chg_20d_pct:+.2f}%，位於一年區間 {range_pos:.0f}% 位置，通膨邊際影響溫和。"
    else:  # US10Y
        bear_pp = float(rule.get("chg_20d_bear_pp", 0.25))
        bull_pp = float(rule.get("chg_20d_bull_pp", -0.25))
        if chg_20d_pp > bear_pp:
            verdict, vtag = "bear", "🔴 偏空（殖利率急升）"
            reason = f"10 年期殖利率近 20 日上升 {chg_20d_pp:+.2f} 個百分點（逾 +{bear_pp:.2f}），無風險利率攀升壓抑股幣估值。"
        elif chg_20d_pp < bull_pp:
            verdict, vtag = "bull", "🟢 偏多（殖利率回落）"
            reason = f"10 年期殖利率近 20 日回落 {chg_20d_pp:+.2f} 個百分點（低於 {bull_pp:.2f}），估值折現壓力減輕。"
        else:
            verdict, vtag = "neut", "🟡 中性"
            reason = f"10 年期殖利率近 20 日變動 {chg_20d_pp:+.2f} 個百分點（最新 {last_v:.2f}%），短期波動於 ±{bear_pp:.2f} 個百分點內。"

    return {
        "id": item_id,
        "symbol": str(rule.get("symbol", item_id)),
        "name_zh": str(rule.get("name_zh", item_id)),
        "unit": str(rule.get("unit", "")),
        "last": round(last_v, dec),
        "last_str": f"{last_v:,.{dec}f}{('%' if item_id == 'US10Y' else '')}",
        "chg_1d_pct": round(chg_1d_pct, 2),
        "chg_20d_pct": round(chg_20d_pct, 2),
        "chg_1d_pp": round(chg_1d_pp, 2) if item_id == "US10Y" else None,
        "chg_20d_pp": round(chg_20d_pp, 2) if item_id == "US10Y" else None,
        "range_1y_pos_pct": range_pos,
        "range_1y_low": round(lo_1y, dec),
        "range_1y_high": round(hi_1y, dec),
        "closes_90d": closes_90d,
        "verdict": verdict,
        "verdict_tag": vtag,
        "verdict_reason": reason,
        "what_is": str(rule.get("what_is", "")),
        "rule_rationale": str(rule.get("rationale", "")),
        "as_of_date": clean[-1][0],
        "as_of_taipei": as_of_taipei,
        "source_used": source_used,
        "data_status": "ok",
        "updating": False,
    }


def build_market_snapshot(
    cal_cfg: dict[str, Any],
    dgs10_rows: list[tuple[str, float | None]] | None,
    dgs10_source: str | None,
    now_utc: datetime,
    prev_doc: dict[str, Any],
    yf_fetcher: Callable[[str, str], list[tuple[str, float]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build 6-item Market Snapshot (DXY, USDTWD, HSI, GOLD, WTI, US10Y) with prev-doc fallback."""
    t0 = time_mod.monotonic()
    fetcher = yf_fetcher or fetch_yfinance_history
    as_of_tpe = now_utc.astimezone(TPE_TZ).strftime("%Y-%m-%d %H:%M")

    cfg_rules = cal_cfg.get("market_snapshot_rules") or {}
    prev_items = {
        it["id"]: it
        for it in ((prev_doc.get("market_snapshot") or {}).get("items") or [])
        if isinstance(it, dict) and it.get("id")
    }

    items_out: list[dict[str, Any]] = []
    ok_ids: list[str] = []
    fail_ids: list[str] = []
    errs: list[str] = []

    for item_id in ("DXY", "USDTWD", "HSI", "GOLD", "WTI", "US10Y"):
        rule = dict(DEFAULT_MARKET_SNAPSHOT_RULES[item_id])
        rule.update(cfg_rules.get(item_id) or {})
        sym = str(rule["symbol"])
        try:
            rows: list[tuple[str, float]] = []
            src_used = f"yfinance ({sym})"
            if item_id == "US10Y":
                clean_dgs10 = [(d, float(v)) for d, v in (dgs10_rows or []) if v is not None]
                if len(clean_dgs10) >= 200:
                    rows = clean_dgs10[-260:]
                    src_used = f"沿用 DGS10（{dgs10_source or 'FRED'}）"
                else:
                    try:
                        raw_tnx = fetcher(sym, "1y")
                        # Yahoo ^TNX 在部分版本回傳 10 倍殖利率（如 49.12 代表 4.912%），yfinance 0.2.66 實測已為 4.912，
                        # 故僅在數值 > 15 時除以 10，避免將 4.91% 誤除成 0.49%。
                        rows = [(d, (v / 10.0 if v > 15.0 else v)) for d, v in raw_tnx]
                        src_used = "yfinance (^TNX)"
                    except Exception:
                        rows = []
                    if len(rows) < 2 and len(clean_dgs10) >= 2:
                        rows = clean_dgs10
                        src_used = f"沿用 DGS10（{dgs10_source or '備用'}）"
            else:
                rows = fetcher(sym, "1y")

            item = _eval_snapshot_item(item_id, rows, rule, as_of_tpe, src_used)
            items_out.append(item)
            ok_ids.append(item_id)
        except Exception as exc:
            fail_ids.append(item_id)
            errs.append(f"{item_id}: {exc}")
            if item_id in prev_items:
                preserved = _mark_preserved_updating(prev_items[item_id], str(exc))
                items_out.append(preserved)
            else:
                items_out.append({
                    "id": item_id,
                    "symbol": sym,
                    "name_zh": str(rule["name_zh"]),
                    "unit": str(rule.get("unit", "")),
                    "last": None,
                    "last_str": "資料更新中",
                    "chg_1d_pct": None,
                    "chg_20d_pct": None,
                    "chg_1d_pp": None,
                    "chg_20d_pp": None,
                    "range_1y_pos_pct": None,
                    "closes_90d": [],
                    "verdict": "na",
                    "verdict_tag": "資料更新中",
                    "verdict_reason": "來源暫時無法取得，資料更新中。",
                    "what_is": str(rule.get("what_is", "")),
                    "rule_rationale": str(rule.get("rationale", "")),
                    "as_of_taipei": as_of_tpe,
                    "source_used": None,
                    "data_status": "資料更新中",
                    "updating": True,
                })

    snapshot_obj = {
        "as_of_taipei": as_of_tpe,
        "hhmm_taipei": now_utc.astimezone(TPE_TZ).strftime("%H:%M"),
        "items": items_out,
    }
    rep = {
        "status": "ok" if not fail_ids else ("partial" if ok_ids else "failed"),
        "elapsed_sec": round(time_mod.monotonic() - t0, 3),
        "series_ok": ok_ids,
        "series_failed": fail_ids,
        "error": "; ".join(errs) if errs else None,
    }
    return snapshot_obj, rep


def _build_upcoming_calendar(
    cal_cfg: dict[str, Any],
    icsa_upcoming: list[dict[str, Any]],
    now_utc: datetime,
    published_keys: set[tuple[str, str]],
    fedwatch: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect and sort upcoming calendar events across all indicators + FOMC + extra events."""
    events: list[dict[str, Any]] = []

    def _add(ev_id: str, title: str, ind_id: str, period: str, rel_et: str, stars: int, note: str = "", cons: Any = None, cons_src: str | None = None) -> None:
        if (ind_id, period) in published_keys:
            return
        dt = _parse_et_iso(rel_et)
        if dt.astimezone(UTC_TZ) <= now_utc:
            return
        t_info = _fmt_tpe(dt)
        events.append({
            "id": ev_id,
            "title": title,
            "indicator_id": ind_id,
            "period": period,
            "period_zh": _period_zh(period),
            "stars": stars,
            "time_utc": t_info["time_utc"],
            "time_taipei": t_info["time_taipei"],
            "time_taipei_iso": t_info["time_taipei_iso"],
            "date_taipei": t_info["date_taipei"],
            "short_taipei": t_info["short_taipei"],
            "consensus_str": _fmt_consensus(ind_id, float(cons)) if isinstance(cons, (int, float)) else (str(cons) if cons else None),
            "consensus_source": cons_src,
            "note": note,
        })

    fw_meetings = (fedwatch or {}).get("meetings") or []
    fw_by_period = {str(m.get("period", "")): m for m in fw_meetings if isinstance(m, dict)}

    fomc_up = (cal_cfg.get("fomc") or {}).get("upcoming") or []
    for item in fomc_up:
        p_str = str(item["period"])
        base_note = str(item.get("note", "")).strip()
        fw_m = fw_by_period.get(p_str)
        if fw_m and fw_m.get("summary_text") and not fw_m.get("updating"):
            fw_note = str(fw_m["summary_text"])
            full_note = f"{base_note}；{fw_note}" if base_note else fw_note
        else:
            full_note = base_note
        _add(
            f"fomc-{p_str}",
            item.get("title", "FOMC 利率決議"),
            "FED_RATE",
            p_str,
            item["release_et"],
            int(item.get("stars", 3)),
            full_note,
        )
        # 聯準會靜默期開始：FOMC 會議前第二個週六（美東 00:00）
        try:
            dec_d = _parse_et_iso(item["release_et"]).date()
            bo_d = compute_fomc_blackout_start(dec_d)
            _add(
                f"fomc-blackout-{p_str}",
                f"聯準會靜默期開始（{_period_zh(p_str)} FOMC）",
                "FED_RATE",
                f"{p_str}-blackout",
                f"{bo_d.isoformat()}T00:00:00",
                1,
                "靜默期內聯準會官員不公開談貨幣政策，市場消息來源變少",
            )
        except Exception:
            pass

    for ind_id, ind_cfg in (cal_cfg.get("indicators") or {}).items():
        if ind_id in ("ICSA", "CORE_CPI", "CORE_PCE", "UNRATE", "AVG_HOURLY_EARNINGS"):
            continue
        spec = INDICATOR_SPECS.get(ind_id) or {}
        cal_names = {
            "CPI": "CPI／核心 CPI 消費者物價",
            "PCE": "PCE／核心 PCE 物價指數",
            "NFP": "非農就業／失業率／時薪",
        }
        name = cal_names.get(ind_id, spec.get("name", ind_id))
        up_list = _build_jolts_upcoming_releases(cal_cfg, now_utc) if ind_id == "JOLTS" else (ind_cfg.get("upcoming_releases") or [])
        for item in up_list:
            p = str(item.get("period", ""))
            ev_note = str(item.get("note", "") or "")
            if item.get("estimated") and "預估日期" not in ev_note:
                ev_note = f"預估日期；{ev_note}".strip("；")
            _add(
                f"{ind_id.lower()}-{p}",
                f"{name}（{_period_zh(p)}）",
                ind_id,
                p,
                item["release_et"],
                int(item.get("stars", ind_cfg.get("stars", 2))),
                ev_note,
                item.get("consensus"),
                item.get("consensus_source"),
            )

    for item in icsa_upcoming:
        p = str(item.get("period", ""))
        _add(
            f"icsa-{p}",
            f"初領失業金人數（{_period_zh(p)}）",
            "ICSA",
            p,
            item["release_et"],
            int(item.get("stars", 2)),
            item.get("note", ""),
        )

    for item in ((cal_cfg.get("extra_events") or {}).get("upcoming") or []):
        _add(
            str(item["id"]),
            str(item["title"]),
            str(item.get("indicator_id", "FED_RATE")),
            str(item.get("period", "")),
            str(item["release_et"]),
            int(item.get("stars", 1)),
            str(item.get("note", "")),
        )

    events.sort(key=lambda e: e["time_utc"])
    return events


def _score_to_impact(score: float) -> tuple[str, str]:
    if score >= 0.75:
        return "bull", "偏多"
    if score <= -0.75:
        return "bear", "偏空"
    return "neut", "中性"


def _build_overview_summary(
    cards_by_id: dict[str, dict[str, Any]],
    fedwatch: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
    cal_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive macro stance summary & 7-asset impact table from 17 indicators, FedWatch, and market snapshot."""
    fed = cards_by_id.get("FED_RATE") or {}
    cpi = cards_by_id.get("CPI") or {}
    unrate = cards_by_id.get("UNRATE") or {}
    dgs2 = cards_by_id.get("DGS2") or {}
    dgs10 = cards_by_id.get("DGS10") or {}
    jolts = cards_by_id.get("JOLTS") or {}
    ism_mfg = cards_by_id.get("ISM_MFG") or {}

    fed_v = fed.get("verdict", "neut")
    cpi_v = cpi.get("verdict", "neut")
    if fed_v == "bear" or cpi_v == "bear":
        stance = "偏鷹 · 利率高檔承壓"
        stance_cls = "bear"
        stance_desc = (
            f"目前聯邦基金利率位於 {fed.get('value_str', '--')}，"
            f"CPI（{cpi.get('period', '')}）{cpi.get('value_str', '--')}（{cpi.get('sub_info', '')}），"
            f"10 年期美債殖利率 {dgs10.get('value_str', '--')}。在通膨未明顯回落前，緊縮預期對高估值科技股與加密貨幣偏逆風。"
        )
    elif fed_v == "bull" and cpi_v == "bull":
        stance = "偏鴿 · 通膨降溫"
        stance_cls = "bull"
        stance_desc = (
            f"通膨壓力收斂且聯準會偏向寬鬆（基準利率 {fed.get('value_str', '--')}），"
            f"10 年期殖利率 {dgs10.get('value_str', '--')}，有利風險資產估值修復。"
        )
    else:
        stance = "中性觀察 · 數據依賴"
        stance_cls = "neut"
        stance_desc = (
            f"聯邦基金利率 {fed.get('value_str', '--')}、失業率 {unrate.get('value_str', '--')}、"
            f"10 年期美債殖利率 {dgs10.get('value_str', '--')}，市場密切關注後續通膨與就業走勢。"
        )

    snap_items = {
        it["id"]: it
        for it in ((market_snapshot or {}).get("items") or [])
        if isinstance(it, dict) and it.get("id") and it.get("last") is not None
    }
    dxy = snap_items.get("DXY")
    twd = snap_items.get("USDTWD")
    gold = snap_items.get("GOLD")
    wti = snap_items.get("WTI")
    us10y = snap_items.get("US10Y")

    # FedWatch bias helper
    fw_meetings = (fedwatch or {}).get("meetings") or []
    fw_m0 = fw_meetings[0] if fw_meetings and not (fedwatch or {}).get("updating") else None
    fw_bias = 0.0
    fw_phrase = ""
    if fw_m0 and isinstance(fw_m0.get("probabilities"), dict):
        p = fw_m0["probabilities"]
        hike_p = float(p.get("hike_1", 0.0)) + float(p.get("hike_2_plus", 0.0))
        cut_p = float(p.get("cut_1", 0.0)) + float(p.get("cut_2_plus", 0.0))
        if hike_p >= 50.0:
            fw_bias = -1.0
        elif cut_p >= 50.0:
            fw_bias = 1.0
        if fw_m0.get("summary_text"):
            fw_phrase = str(fw_m0["summary_text"])

    rules_cfg = (cal_cfg or {}).get("asset_impact_rules", {}).get("rules") or {}

    def _sub(asset: str) -> str:
        return str((rules_cfg.get(asset) or {}).get("sub") or ASSET_IMPACT_SUBS.get(asset, ""))

    def _na_row(asset: str) -> dict[str, str]:
        return {
            "asset": asset,
            "sub": _sub(asset),
            "cls": "na",
            "tag": "資料不足",
            "note": "資料不足，暫不判斷",
        }

    # 1. 美股
    us10_val = dgs10.get("value_num") if dgs10.get("value_num") is not None else (us10y["last"] if us10y else None)
    if sum(1 for x in (fed.get("value_str"), cpi.get("value_str"), us10_val) if x is not None) < 2:
        us_row = _na_row("美股")
    else:
        s = 0.0
        if fed_v == "bear":
            s -= 1.0
        elif fed_v == "bull":
            s += 1.0
        if cpi_v == "bear":
            s -= 1.0
        elif cpi_v == "bull":
            s += 1.0
        if us10_val is not None:
            if us10_val >= 4.5:
                s -= 1.0
            elif us10_val <= 3.5:
                s += 1.0
        if us10y and us10y.get("verdict") == "bear":
            s -= 0.5
        elif us10y and us10y.get("verdict") == "bull":
            s += 0.5
        s += fw_bias * 0.8
        if jolts.get("verdict") == "bull":
            s += 0.4
        elif jolts.get("verdict") == "bear":
            s -= 0.4
        cls, tag = _score_to_impact(s)
        parts = []
        if us10_val is not None:
            parts.append(f"10 年期殖利率 {us10_val:.2f}%")
        if cpi.get("value_str"):
            parts.append(f"CPI 環比 {cpi['value_str']}")
        if fw_phrase:
            parts.append(fw_phrase)
        if jolts.get("value_str"):
            parts.append(f"JOLTS {jolts['value_str']}")
        tail = "估值倍數承壓、高敏感科技股波動放大。" if cls == "bear" else ("估值折現壓力舒緩，有利成長股修復。" if cls == "bull" else "多空力道拉鋸，靜待後續數據指引。")
        us_row = {"asset": "美股", "sub": _sub("美股"), "cls": cls, "tag": tag, "note": f"{'、'.join(parts)}；{tail}"}

    # 2. 台股
    if not twd and (not dxy or us10_val is None):
        tw_row = _na_row("台股")
    else:
        s = 0.0
        if twd:
            s += 1.5 if twd["verdict"] == "bull" else (-1.5 if twd["verdict"] == "bear" else 0.0)
        if dxy:
            s += 0.8 if dxy["verdict"] == "bull" else (-0.8 if dxy["verdict"] == "bear" else 0.0)
        if us_row["cls"] == "bear":
            s -= 0.7
        elif us_row["cls"] == "bull":
            s += 0.7
        cls, tag = _score_to_impact(s)
        parts = []
        if twd:
            parts.append(f"美元／台幣現報 {twd['last_str']}（近 20 日 {twd['chg_20d_pct']:+.2f}%）")
        if dxy:
            parts.append(f"美元指數近 20 日 {dxy['chg_20d_pct']:+.2f}%")
        if us10_val is not None:
            parts.append(f"10Y 殖利率 {us10_val:.2f}%")
        tail = "外資匯出與權值股調節壓力偏高。" if cls == "bear" else ("台幣偏強與美債壓力趨緩有利外資回補電子權值。" if cls == "bull" else "資金面中性，隨美股科技股與匯率波動。")
        tw_row = {"asset": "台股", "sub": _sub("台股"), "cls": cls, "tag": tag, "note": f"{'，'.join(parts)}，{tail}"}

    # 3. 加密貨幣
    if not dxy and not (fed.get("value_str") and us10_val is not None):
        crypto_row = _na_row("加密貨幣")
    else:
        s = 0.0
        if dxy:
            s += 1.5 if dxy["verdict"] == "bull" else (-1.5 if dxy["verdict"] == "bear" else 0.0)
        s += fw_bias * 0.8
        if fed_v == "bear":
            s -= 0.8
        elif fed_v == "bull":
            s += 0.8
        if us10y and us10y.get("verdict") == "bear":
            s -= 0.6
        elif us10y and us10y.get("verdict") == "bull":
            s += 0.6
        cls, tag = _score_to_impact(s)
        parts = []
        if dxy:
            parts.append(f"美元指數近 20 日 {dxy['chg_20d_pct']:+.2f}%（現報 {dxy['last_str']}）")
        if fed.get("value_str"):
            parts.append(f"基準利率 {fed['value_str']}")
        if fw_phrase:
            parts.append(fw_phrase)
        tail = "美元流動性偏緊易放大短線回檔。" if cls == "bear" else ("流動性與寬鬆預期支撐風險偏好。" if cls == "bull" else "流動性訊號分歧，維持區間震盪。")
        crypto_row = {"asset": "加密貨幣", "sub": _sub("加密貨幣"), "cls": cls, "tag": tag, "note": f"{'、'.join(parts)}；{tail}"}

    # 4. 美債（債價視角）
    if us10_val is None and dgs2.get("value_num") is None:
        bond_row = _na_row("美債")
    else:
        s = 0.0
        if us10y:
            s += 1.5 if us10y["verdict"] == "bull" else (-1.5 if us10y["verdict"] == "bear" else 0.0)
        s += fw_bias * 0.9
        if cpi_v == "bear":
            s -= 0.8
        elif cpi_v == "bull":
            s += 0.8
        cls, tag = _score_to_impact(s)
        parts = [f"2Y {dgs2.get('value_str', '--')} / 10Y {us10_val:.2f}%" if us10_val is not None else f"2Y {dgs2.get('value_str', '--')}"]
        if us10y and us10y.get("chg_20d_pp") is not None:
            parts.append(f"10Y 近 20 日 {us10y['chg_20d_pp']:+.2f} 個百分點")
        if cpi.get("value_str"):
            parts.append(f"CPI {cpi['value_str']}")
        tail = "通膨與緊縮預期壓抑債價（殖利率高檔）。" if cls == "bear" else ("殖利率回落與降溫訊號支撐美債價格。" if cls == "bull" else "高息收買盤與政策利率預期拉鋸。")
        bond_row = {"asset": "美債", "sub": _sub("美債"), "cls": cls, "tag": tag, "note": f"{'、'.join(parts)}；{tail}"}

    # 5. 黃金
    if not gold:
        gold_row = _na_row("黃金")
    else:
        s = 0.0
        if gold["chg_20d_pct"] > 3.0:
            s += 1.2
        elif gold["chg_20d_pct"] < -3.0:
            s -= 1.2
        if dxy:
            s += 0.8 if dxy["verdict"] == "bull" else (-0.8 if dxy["verdict"] == "bear" else 0.0)
        if us10y:
            s += 0.7 if us10y["verdict"] == "bull" else (-0.7 if us10y["verdict"] == "bear" else 0.0)
        cls, tag = _score_to_impact(s)
        dxy_s = f"、美元指數近 20 日 {dxy['chg_20d_pct']:+.2f}%" if dxy else ""
        y_s = f"、10Y 殖利率 {us10_val:.2f}%" if us10_val is not None else ""
        tail = "避險與央行買盤支撐金價走勢。" if cls == "bull" else ("強美元或高利率壓抑無息資產表現。" if cls == "bear" else "高名目殖利率壓抑與避險需求彼此拉鋸。")
        gold_row = {
            "asset": "黃金",
            "sub": _sub("黃金"),
            "cls": cls,
            "tag": tag,
            "note": f"黃金現報 {gold['last_str']} 美元（近 20 日 {gold['chg_20d_pct']:+.2f}%、一年區間 {gold['range_1y_pos_pct']:.0f}%）{dxy_s}{y_s}；{tail}",
        }

    # 6. 原油
    if not wti:
        oil_row = _na_row("原油")
    else:
        s = 0.0
        if wti["chg_20d_pct"] >= 3.0:
            s += 1.2
        elif wti["chg_20d_pct"] <= -3.0:
            s -= 1.2
        if ism_mfg.get("value_num") is not None:
            s += 0.8 if float(ism_mfg["value_num"]) >= 50.0 else -0.8
        if dxy:
            s += 0.4 if dxy["verdict"] == "bull" else (-0.4 if dxy["verdict"] == "bear" else 0.0)
        cls, tag = _score_to_impact(s)
        ism_s = f"、ISM 製造業 PMI {ism_mfg['value_str']}" if ism_mfg.get("value_str") else ""
        tail = "需求支撐與短期動能偏強。" if cls == "bull" else ("供給寬鬆或高利率壓抑能源需求預期。" if cls == "bear" else "供需與景氣訊號交錯，油價維持區間震盪。")
        oil_row = {
            "asset": "原油",
            "sub": _sub("原油"),
            "cls": cls,
            "tag": tag,
            "note": f"WTI 原油現報 {wti['last_str']} 美元（近 20 日 {wti['chg_20d_pct']:+.2f}%、一年區間 {wti['range_1y_pos_pct']:.0f}%）{ism_s}；{tail}",
        }

    # 7. 美元／台幣（台幣升貶與外資視角）
    if not twd:
        fx_row = _na_row("美元／台幣")
    else:
        s = 0.0
        s += 1.5 if twd["verdict"] == "bull" else (-1.5 if twd["verdict"] == "bear" else 0.0)
        if dxy:
            s += 0.8 if dxy["verdict"] == "bull" else (-0.8 if dxy["verdict"] == "bear" else 0.0)
        s += fw_bias * 0.4
        cls, tag = _score_to_impact(s)
        dxy_s = f"、美元指數近 20 日 {dxy['chg_20d_pct']:+.2f}%" if dxy else ""
        tail = "台幣偏升有利外資資金匯入。" if cls == "bull" else ("台幣偏貶反映外資匯出與美元強勢壓力。" if cls == "bear" else "匯率隨美元波動於區間整理。")
        fx_row = {
            "asset": "美元／台幣",
            "sub": _sub("美元／台幣"),
            "cls": cls,
            "tag": tag,
            "note": f"美元／台幣現報 {twd['last_str']}（近 20 日 {twd['chg_20d_pct']:+.2f}%）{dxy_s}；{tail}",
        }

    asset_impacts = [us_row, tw_row, crypto_row, bond_row, gold_row, oil_row, fx_row]

    return {
        "stance": stance,
        "stance_cls": stance_cls,
        "stance_desc": stance_desc,
        "asset_impacts": asset_impacts,
    }


def _load_existing_macro_json(out_dir: Path) -> dict[str, Any]:
    for candidate in (out_dir / "macro.json", ROOT_DIR / "data" / "macro.json"):
        if candidate.exists():
            try:
                doc = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and isinstance(doc.get("indicators"), list):
                    return doc
            except Exception:
                pass
    return {}


def _mark_preserved_updating(prev_card: dict[str, Any], err_msg: str) -> dict[str, Any]:
    preserved = dict(prev_card)
    preserved["data_status"] = "資料更新中"
    preserved["updating"] = True
    preserved["source_used"] = prev_card.get("source_used") or prev_card.get("source") or "前次資料（資料更新中）"
    preserved["updating_notice"] = f"資料更新中（來源暫時無法取得：{err_msg}，暫保留前次資料）"
    return preserved


def generate_macro_file(
    out_dir: Path,
    calendar_path: Path | None = None,
    http_get: Any = None,
    http_post: Any = None,
    now_utc: datetime | None = None,
    use_local_cache: bool = True,
    fred_base_url: str | None = None,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
    yf_fetcher: Callable[[str, str], list[tuple[str, float]]] | None = None,
) -> dict[str, Any]:
    """Fetch public macro data (FRED primary + no-key fallbacks), compute market reactions, and write `out_dir/macro.json`."""
    run_t0 = time_mod.monotonic()
    eff_timeout = max(1, min(int(timeout_sec), 10))
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_tpe = now.astimezone(TPE_TZ)
    cal_cfg = load_macro_calendar(calendar_path)
    prev_doc = _load_existing_macro_json(out_dir)
    prev_cards_by_id = {
        c["id"]: c
        for c in (prev_doc.get("indicators") or [])
        if isinstance(c, dict) and c.get("id")
    }
    prev_cal_past = list((prev_doc.get("calendar") or {}).get("past") or [])

    raw_bars = _load_market_bars(out_dir, use_local_cache=use_local_cache)
    indexed_bars = {sym: _index_bars_with_dates(bars) for sym, bars in raw_bars.items()}

    # 1) Primary source: FRED (circuit-breaker fast-fail on 1st failure or any transport/limit error)
    fred_cache: dict[str, list[tuple[str, float | None]]] = {}
    fred_errors: dict[str, str] = {}
    needed_fred = [
        "DFEDTARU", "DFEDTARL", "DGS2", "DGS10",
        "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIFIS",
        "PAYEMS", "UNRATE", "CES0500000003", "ICSA", "JTSJOL",
        "A191RL1Q225SBEA", "RSAFS",
    ]
    fred_t0 = time_mod.monotonic()
    fred_attempts = 0
    fred_aborted = False
    fred_abort_reason: str | None = None
    fred_ok: list[str] = []
    fred_failed: list[str] = []
    fred_skipped: list[str] = []

    for sid in needed_fred:
        if fred_aborted:
            fred_skipped.append(sid)
            fred_errors[sid] = f"FRED skipped after fast-fail ({fred_abort_reason})"
            continue
        fred_attempts += 1
        try:
            fred_cache[sid] = fetch_fred_series(
                sid,
                http_get=http_get,
                timeout_sec=eff_timeout,
                fred_url_template=fred_base_url,
            )
            fred_ok.append(sid)
        except Exception as exc:
            fred_errors[sid] = str(exc)
            fred_failed.append(sid)
            if fred_attempts == 1 or isinstance(exc, FredTransportError):
                fred_aborted = True
                fred_abort_reason = str(exc)

    fred_elapsed = round(time_mod.monotonic() - fred_t0, 3)
    sources_report: dict[str, Any] = {
        "FRED": {
            "status": "ok" if not fred_failed and not fred_skipped else ("partial" if fred_ok else "failed"),
            "elapsed_sec": fred_elapsed,
            "attempts": fred_attempts,
            "aborted": fred_aborted,
            "series_ok": fred_ok,
            "series_failed": fred_failed,
            "series_skipped": fred_skipped,
            "error": fred_abort_reason or ("; ".join(f"{k}: {v}" for k, v in fred_errors.items()) if fred_errors else None),
        }
    }
    indicators_report: dict[str, Any] = {}

    # 2) Fallback sources ONLY for series not obtained from FRED
    fallback_series: dict[str, dict[str, Any]] = {}
    fallback_errors: dict[str, str] = {}

    # 2a. FED_RATE fallback from FOMC YAML history
    if "DFEDTARU" not in fred_cache or "DFEDTARL" not in fred_cache:
        t0 = time_mod.monotonic()
        try:
            u_rows, l_rows = build_fomc_fallback_series(cal_cfg, now)
            fallback_series["FED_RATE"] = {
                "upper": u_rows,
                "lower": l_rows,
                "source_used": "FOMC 決議紀錄（備用）",
                "source_detail": "Federal Reserve FOMC 決議紀錄（config/macro_calendar.yaml，備用）",
            }
            sources_report["FOMC_YAML"] = {
                "status": "ok",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": ["FED_RATE"],
                "series_failed": [],
                "error": None,
            }
        except Exception as exc:
            fallback_errors["FED_RATE"] = str(exc)
            sources_report["FOMC_YAML"] = {
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": [],
                "series_failed": ["FED_RATE"],
                "error": str(exc),
            }

    # 2b. US Treasury daily yield curve fallback (DGS2, DGS10)
    needed_yields = [yid for yid in ("DGS2", "DGS10") if yid not in fred_cache]
    if needed_yields:
        t0 = time_mod.monotonic()
        try:
            tr_data = fetch_treasury_yield_curve(now, http_get=http_get, timeout_sec=eff_timeout)
            ok_y: list[str] = []
            for yid in needed_yields:
                if tr_data.get(yid):
                    fallback_series[yid] = {
                        "rows": tr_data[yid],
                        "source_used": "美國財政部（備用）",
                        "source_detail": "U.S. Treasury Daily Par Yield Curve（備用）",
                    }
                    ok_y.append(yid)
            sources_report["US_TREASURY"] = {
                "status": "ok" if len(ok_y) == len(needed_yields) else "partial",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": ok_y,
                "series_failed": [y for y in needed_yields if y not in ok_y],
                "error": None,
            }
        except Exception as exc:
            for yid in needed_yields:
                fallback_errors[yid] = str(exc)
            sources_report["US_TREASURY"] = {
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": [],
                "series_failed": needed_yields,
                "error": str(exc),
            }

    # 2c. BLS Public API v1 batch fallback
    needed_bls = [
        ind_id for ind_id in ("CPI", "CORE_CPI", "PPI", "NFP", "UNRATE", "AVG_HOURLY_EARNINGS", "JOLTS")
        if INDICATOR_SPECS[ind_id]["code"] not in fred_cache
    ]
    if needed_bls:
        t0 = time_mod.monotonic()
        bls_sids = [BLS_FALLBACK_SERIES[ind_id] for ind_id in needed_bls]
        try:
            bls_map = fetch_bls_batch_v1(
                bls_sids,
                start_year=now.year - 3,
                end_year=now.year,
                http_get=http_get,
                http_post=http_post,
                timeout_sec=eff_timeout,
            )
            ok_b: list[str] = []
            fail_b: list[str] = []
            for ind_id in needed_bls:
                bsid = BLS_FALLBACK_SERIES[ind_id]
                if bls_map.get(bsid):
                    fallback_series[ind_id] = {
                        "rows": bls_map[bsid],
                        "source_used": "BLS（備用）",
                        "source_detail": f"BLS Public API v1（{bsid}，備用）",
                    }
                    ok_b.append(ind_id)
                else:
                    fail_b.append(ind_id)
                    fallback_errors[ind_id] = f"BLS series {bsid} empty"
            sources_report["BLS_API_V1"] = {
                "status": "ok" if not fail_b else ("partial" if ok_b else "failed"),
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": ok_b,
                "series_failed": fail_b,
                "error": None if not fail_b else f"Missing BLS series: {fail_b}",
            }
        except Exception as exc:
            for ind_id in needed_bls:
                fallback_errors[ind_id] = str(exc)
            sources_report["BLS_API_V1"] = {
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": [],
                "series_failed": needed_bls,
                "error": str(exc),
            }

    # 2d. DBnomics BEA fallback (PCE, CORE_PCE, GDP)
    needed_bea = [
        ind_id for ind_id in ("PCE", "CORE_PCE", "GDP")
        if INDICATOR_SPECS[ind_id]["code"] not in fred_cache
    ]
    if needed_bea:
        t0 = time_mod.monotonic()
        ok_bea: list[str] = []
        fail_bea: list[str] = []
        err_bea: list[str] = []
        for ind_id in needed_bea:
            db_path = DBNOMICS_BEA_SERIES[ind_id]
            try:
                rows = fetch_dbnomics_series(db_path, http_get=http_get, timeout_sec=eff_timeout)
                fallback_series[ind_id] = {
                    "rows": rows,
                    "source_used": "DBnomics BEA（備用）",
                    "source_detail": f"DBnomics（{db_path}，備用）",
                }
                ok_bea.append(ind_id)
            except Exception as exc:
                fail_bea.append(ind_id)
                fallback_errors[ind_id] = str(exc)
                err_bea.append(f"{ind_id}: {exc}")
        sources_report["DBNOMICS_BEA"] = {
            "status": "ok" if not fail_bea else ("partial" if ok_bea else "failed"),
            "elapsed_sec": round(time_mod.monotonic() - t0, 3),
            "series_ok": ok_bea,
            "series_failed": fail_bea,
            "error": "; ".join(err_bea) if err_bea else None,
        }

    # 2e. US Census MRTS fallback (RETAIL_SALES)
    if INDICATOR_SPECS["RETAIL_SALES"]["code"] not in fred_cache:
        t0 = time_mod.monotonic()
        try:
            rs_rows = fetch_census_retail_sales(http_get=http_get, timeout_sec=eff_timeout)
            fallback_series["RETAIL_SALES"] = {
                "rows": rs_rows,
                "source_used": "美國普查局 Census（備用）",
                "source_detail": "U.S. Census Bureau MRTS adv44X72（備用）",
            }
            sources_report["CENSUS_MRTS"] = {
                "status": "ok",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": ["RETAIL_SALES"],
                "series_failed": [],
                "error": None,
            }
        except Exception as exc:
            fallback_errors["RETAIL_SALES"] = str(exc)
            sources_report["CENSUS_MRTS"] = {
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": [],
                "series_failed": ["RETAIL_SALES"],
                "error": str(exc),
            }

    # 2f. DOL OUI XML + TradingEconomics fallback (ICSA)
    if INDICATOR_SPECS["ICSA"]["code"] not in fred_cache:
        t0 = time_mod.monotonic()
        try:
            icsa_rows, icsa_src_used, icsa_src_detail = fetch_icsa_fallback(
                now,
                http_get=http_get,
                http_post=http_post,
                timeout_sec=eff_timeout,
            )
            fallback_series["ICSA"] = {
                "rows": icsa_rows,
                "source_used": icsa_src_used,
                "source_detail": icsa_src_detail,
            }
            sources_report["DOL_TE_ICSA"] = {
                "status": "ok",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": ["ICSA"],
                "series_failed": [],
                "error": None,
            }
        except Exception as exc:
            fallback_errors["ICSA"] = str(exc)
            sources_report["DOL_TE_ICSA"] = {
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "series_ok": [],
                "series_failed": ["ICSA"],
                "error": str(exc),
            }

    cards_out: list[dict[str, Any]] = []
    cal_past_all: list[dict[str, Any]] = []
    updating_ids: list[str] = []
    icsa_upcoming: list[dict[str, Any]] = []

    # 3) Assemble indicator cards
    # 3a. FED_RATE
    if "DFEDTARU" in fred_cache and "DFEDTARL" in fred_cache:
        fomc_card = _build_fomc_card(
            fred_cache["DFEDTARU"], fred_cache["DFEDTARL"], cal_cfg, indexed_bars, now,
            source_used="FRED",
        )
        cards_out.append(fomc_card)
        indicators_report["FED_RATE"] = {"source_used": "FRED", "fallback_used": False, "status": "ok"}
    elif "FED_RATE" in fallback_series:
        fb = fallback_series["FED_RATE"]
        fomc_card = _build_fomc_card(
            fb["upper"], fb["lower"], cal_cfg, indexed_bars, now,
            source_used=fb["source_used"],
            source_detail=fb["source_detail"],
        )
        cards_out.append(fomc_card)
        indicators_report["FED_RATE"] = {"source_used": fb["source_used"], "fallback_used": True, "status": "ok"}
    elif "FED_RATE" in prev_cards_by_id:
        err = f"FRED: {fred_errors.get('DFEDTARU') or fred_errors.get('DFEDTARL')}; 備用: {fallback_errors.get('FED_RATE', '失敗')}"
        preserved = _mark_preserved_updating(prev_cards_by_id["FED_RATE"], err)
        cards_out.append(preserved)
        updating_ids.append("FED_RATE")
        indicators_report["FED_RATE"] = {"source_used": preserved["source_used"], "fallback_used": True, "status": "failed", "error": err}
        fomc_card = None
    else:
        updating_ids.append("FED_RATE")
        indicators_report["FED_RATE"] = {"source_used": None, "fallback_used": True, "status": "failed"}
        fomc_card = None

    if fomc_card is not None:
        for r in fomc_card["history_rows"]:
            cal_past_all.append({
                "id": f"fomc-{r['period']}",
                "title": f"FOMC 利率決議（{r['period_zh']}）",
                "indicator_id": "FED_RATE",
                "period": r["period"],
                "stars": 3,
                "time_utc": r["release_utc"],
                "time_taipei": r["release_taipei"],
                "time_taipei_iso": _fmt_tpe(datetime.fromisoformat(r["release_utc"].replace("Z", "+00:00")))["time_taipei_iso"],
                "short_taipei": _fmt_tpe(datetime.fromisoformat(r["release_utc"].replace("Z", "+00:00")))["short_taipei"],
                "actual_str": r["actual_str"],
                "prev_str": r["prev_str"],
                "consensus_str": r["consensus_str"],
                "consensus_source": r["consensus_source"],
                "surprise": r["surprise"],
                "surprise_basis": r["surprise_basis"],
                "reactions": r["reactions"],
                "note": r.get("note", ""),
            })

    # 3b. DGS2 & DGS10
    for yid, yname, fid, ywhy in (
        (
            "DGS2",
            "2 年期美債殖利率",
            "DGS2",
            "反映市場對未來兩年聯準會利率路徑的定價，對 FOMC 與通膨數據最為敏感。",
        ),
        (
            "DGS10",
            "10 年期美債殖利率",
            "DGS10",
            "全球風險資產的定價錨，影響企業融資成本、房貸利率與科技股折現估值。",
        ),
    ):
        if fid in fred_cache:
            cards_out.append(_build_daily_yield_card(yid, yname, fid, ywhy, fred_cache[fid], indexed_bars, source_used="FRED"))
            indicators_report[yid] = {"source_used": "FRED", "fallback_used": False, "status": "ok"}
        elif yid in fallback_series:
            fb = fallback_series[yid]
            cards_out.append(_build_daily_yield_card(
                yid, yname, fid, ywhy, fb["rows"], indexed_bars,
                source_used=fb["source_used"], source_detail=fb["source_detail"],
            ))
            indicators_report[yid] = {"source_used": fb["source_used"], "fallback_used": True, "status": "ok"}
        elif yid in prev_cards_by_id:
            err = f"FRED: {fred_errors.get(fid, '失敗')}; 備用: {fallback_errors.get(yid, '失敗')}"
            preserved = _mark_preserved_updating(prev_cards_by_id[yid], err)
            cards_out.append(preserved)
            updating_ids.append(yid)
            indicators_report[yid] = {"source_used": preserved["source_used"], "fallback_used": True, "status": "failed", "error": err}
        else:
            updating_ids.append(yid)
            indicators_report[yid] = {"source_used": None, "fallback_used": True, "status": "failed"}

    # 3c. Remaining 14 standard indicators (including JOLTS in jobs group)
    std_order = [
        "CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI",
        "NFP", "UNRATE", "AVG_HOURLY_EARNINGS", "ICSA", "JOLTS",
        "GDP", "ISM_MFG", "ISM_SVC", "RETAIL_SALES",
    ]
    ism_t0 = time_mod.monotonic()
    ism_ok: list[str] = []
    ism_fail: list[str] = []
    ism_errs: list[str] = []

    for ind_id in std_order:
        spec = INDICATOR_SPECS[ind_id]
        if ind_id in ("ISM_MFG", "ISM_SVC"):
            try:
                te_latest = fetch_ism_te(ind_id, http_get=http_get, timeout_sec=eff_timeout)
                card, cal_items = _build_standard_indicator_card(
                    ind_id, cal_cfg, None, te_latest, indexed_bars, now,
                    source_used="TradingEconomics ＋ ISM",
                )
                cards_out.append(card)
                cal_past_all.extend(cal_items)
                ism_ok.append(ind_id)
                indicators_report[ind_id] = {"source_used": "TradingEconomics ＋ ISM", "fallback_used": False, "status": "ok"}
            except Exception as exc:
                ism_fail.append(ind_id)
                ism_errs.append(f"{ind_id}: {exc}")
                if ind_id in prev_cards_by_id:
                    preserved = _mark_preserved_updating(prev_cards_by_id[ind_id], str(exc))
                    cards_out.append(preserved)
                    indicators_report[ind_id] = {"source_used": preserved["source_used"], "fallback_used": False, "status": "failed", "error": str(exc)}
                else:
                    card, cal_items = _build_standard_indicator_card(
                        ind_id, cal_cfg, None, None, indexed_bars, now,
                        source_used="TradingEconomics ＋ ISM",
                    )
                    preserved = _mark_preserved_updating(card, str(exc))
                    cards_out.append(preserved)
                    cal_past_all.extend(cal_items)
                    indicators_report[ind_id] = {"source_used": preserved["source_used"], "fallback_used": False, "status": "failed", "error": str(exc)}
                updating_ids.append(ind_id)
        else:
            fid = spec["code"]
            if fid in fred_cache:
                card, cal_items = _build_standard_indicator_card(
                    ind_id, cal_cfg, fred_cache[fid], None, indexed_bars, now,
                    source_used="FRED",
                )
                cards_out.append(card)
                cal_past_all.extend(cal_items)
                if ind_id == "ICSA":
                    _, icsa_upcoming = _build_icsa_schedule_map(fred_cache[fid], cal_cfg, now)
                indicators_report[ind_id] = {"source_used": "FRED", "fallback_used": False, "status": "ok"}
            elif ind_id in fallback_series:
                fb = fallback_series[ind_id]
                try:
                    card, cal_items = _build_standard_indicator_card(
                        ind_id, cal_cfg, fb["rows"], None, indexed_bars, now,
                        source_used=fb["source_used"],
                        source_detail=fb["source_detail"],
                    )
                    cards_out.append(card)
                    cal_past_all.extend(cal_items)
                    if ind_id == "ICSA":
                        _, icsa_upcoming = _build_icsa_schedule_map(fb["rows"], cal_cfg, now)
                    indicators_report[ind_id] = {"source_used": fb["source_used"], "fallback_used": True, "status": "ok"}
                except Exception as exc:
                    err = f"備用換算失敗: {exc}"
                    if ind_id in prev_cards_by_id:
                        preserved = _mark_preserved_updating(prev_cards_by_id[ind_id], err)
                        cards_out.append(preserved)
                        cal_past_all.extend([e for e in prev_cal_past if e.get("indicator_id") == ind_id])
                    updating_ids.append(ind_id)
                    indicators_report[ind_id] = {"source_used": None, "fallback_used": True, "status": "failed", "error": err}
            elif ind_id in prev_cards_by_id:
                err = f"FRED: {fred_errors.get(fid, '失敗')}; 備用: {fallback_errors.get(ind_id, '失敗')}"
                preserved = _mark_preserved_updating(prev_cards_by_id[ind_id], err)
                cards_out.append(preserved)
                updating_ids.append(ind_id)
                cal_past_all.extend([e for e in prev_cal_past if e.get("indicator_id") == ind_id])
                indicators_report[ind_id] = {"source_used": preserved["source_used"], "fallback_used": True, "status": "failed", "error": err}
            else:
                updating_ids.append(ind_id)
                indicators_report[ind_id] = {
                    "source_used": None,
                    "fallback_used": True,
                    "status": "failed",
                    "error": f"FRED: {fred_errors.get(fid, '失敗')}; 備用: {fallback_errors.get(ind_id, '失敗')}",
                }

    sources_report["TE_ISM"] = {
        "status": "ok" if not ism_fail else ("partial" if ism_ok else "failed"),
        "elapsed_sec": round(time_mod.monotonic() - ism_t0, 3),
        "series_ok": ism_ok,
        "series_failed": ism_fail,
        "error": "; ".join(ism_errs) if ism_errs else None,
    }

    # Extra past calendar events (e.g., FOMC minutes)
    for ex in ((cal_cfg.get("extra_events") or {}).get("past") or []):
        rel_dt = _parse_et_iso(ex["release_et"])
        t_info = _fmt_tpe(rel_dt)
        cal_past_all.append({
            "id": str(ex["id"]),
            "title": str(ex["title"]),
            "indicator_id": str(ex.get("indicator_id", "FED_RATE")),
            "period": str(ex.get("period", "")),
            "stars": int(ex.get("stars", 2)),
            "time_utc": t_info["time_utc"],
            "time_taipei": t_info["time_taipei"],
            "time_taipei_iso": t_info["time_taipei_iso"],
            "short_taipei": t_info["short_taipei"],
            "actual_str": str(ex.get("actual_str", "—")),
            "prev_str": str(ex.get("prev_str", "—")),
            "consensus_str": ex.get("consensus_str"),
            "consensus_source": None,
            "surprise": str(ex.get("surprise", "中性")),
            "surprise_basis": str(ex.get("surprise_basis", "text")),
            "reactions": compute_market_reactions(rel_dt, indexed_bars),
            "note": str(ex.get("note", "")),
        })

    # Deduplicate and sort calendar past items newest-first
    dedup_past: dict[str, dict[str, Any]] = {}
    for item in cal_past_all:
        if item.get("time_utc"):
            dedup_past[item["id"]] = item
    past_sorted = sorted(dedup_past.values(), key=lambda x: x["time_utc"], reverse=True)

    # Only keep past items whose release_utc <= now_utc and within ~400 days
    cutoff_past = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    past_filtered = [p for p in past_sorted if cutoff_past <= p["time_utc"] <= now_iso]

    # 4) FedWatch probabilities (CBOT ZQ via yfinance -> manual <7d -> prev_doc)
    fedwatch, fedwatch_report = build_fedwatch_data(cal_cfg, now, prev_doc, yf_fetcher=yf_fetcher)
    sources_report["YFINANCE_FEDWATCH"] = fedwatch_report

    # Dynamically update FED_RATE card's next_release note with FedWatch summary (omit if unavailable)
    if fomc_card is not None and isinstance(fomc_card.get("next_release"), dict):
        fw_m_map = {str(m.get("period", "")): m for m in (fedwatch.get("meetings") or []) if isinstance(m, dict)}
        nr_p = str(fomc_card["next_release"].get("period", ""))
        fw_m = fw_m_map.get(nr_p)
        if fw_m and fw_m.get("summary_text") and not fw_m.get("updating"):
            base_nr_note = str(fomc_card["next_release"].get("note") or "").strip()
            fw_txt = str(fw_m["summary_text"])
            if fw_txt not in base_nr_note:
                fomc_card["next_release"]["note"] = f"{base_nr_note}；{fw_txt}" if base_nr_note else fw_txt

    # 5) Market Snapshot (DXY, USDTWD, GOLD, WTI, US10Y)
    dgs10_rows: list[tuple[str, float | None]] | None = None
    dgs10_source: str | None = None
    if "DGS10" in fred_cache:
        dgs10_rows = fred_cache["DGS10"]
        dgs10_source = "FRED"
    elif "DGS10" in fallback_series:
        dgs10_rows = fallback_series["DGS10"]["rows"]
        dgs10_source = fallback_series["DGS10"]["source_used"]
    market_snapshot, snapshot_report = build_market_snapshot(
        cal_cfg, dgs10_rows, dgs10_source, now, prev_doc, yf_fetcher=yf_fetcher
    )
    sources_report["YFINANCE_SNAPSHOT"] = snapshot_report

    published_keys = {(p.get("indicator_id", ""), p.get("period", "")) for p in past_filtered}
    upcoming_sorted = _build_upcoming_calendar(cal_cfg, icsa_upcoming, now, published_keys, fedwatch=fedwatch)

    cards_by_id = {c["id"]: c for c in cards_out}
    overview = _build_overview_summary(
        cards_by_id,
        fedwatch=fedwatch,
        market_snapshot=market_snapshot,
        cal_cfg=cal_cfg,
    )

    payload = {
        "version": 1,
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M"),
        "status": "partial" if updating_ids else "ok",
        "updating_indicators": updating_ids,
        "fetch_report": {
            "total_elapsed_sec": round(time_mod.monotonic() - run_t0, 3),
            "fred_aborted": fred_aborted,
            "fred_abort_reason": fred_abort_reason,
            "sources": sources_report,
            "indicators": indicators_report,
            "fedwatch": fedwatch_report,
            "market_snapshot": snapshot_report,
        },
        "timezone_notes": cal_cfg.get("timezone_notes") or {},
        "reaction_methodology": {
            "source": "本站 data 分支 history_1d.json.gz（約 10 年日線）",
            "NAS100_SP500": "公布當日美股收盤（若於美東 16:00 後公布則取次一交易日）對前一交易日收盤報酬率",
            "TWII": "公布後第一個台股交易日收盤（台北 13:30 收盤截點）對前一交易日收盤報酬率",
            "BTCUSDT": "公布前最後一根 UTC 日 K 收盤（08:00 台北）對公布後第一根 UTC 日 K 收盤報酬率",
        },
        "overview": overview,
        "fedwatch": fedwatch,
        "market_snapshot": market_snapshot,
        "indicators": cards_out,
        "calendar": {
            "upcoming": upcoming_sorted,
            "past": past_filtered,
        },
    }

    out_path = out_dir / "macro.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate macro economic data/macro.json")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--calendar", type=Path, default=None)
    parser.add_argument("--no-local-cache", action="store_true", help="Disable .cache fallback when history_1d.json.gz missing")
    parser.add_argument("--fred-base-url", type=str, default=None, help="Optional FRED URL template override (e.g. for fallback testing)")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SEC, help="Per-request HTTP timeout in seconds (capped at 10)")
    args = parser.parse_args()

    res = generate_macro_file(
        out_dir=args.out_dir,
        calendar_path=args.calendar,
        use_local_cache=not args.no_local_cache,
        fred_base_url=args.fred_base_url,
        timeout_sec=args.timeout,
    )
    print(
        f"Generated {args.out_dir / 'macro.json'}: "
        f"{len(res['indicators'])} indicators, "
        f"{len(res['calendar']['upcoming'])} upcoming, "
        f"{len(res['calendar']['past'])} past events, "
        f"status={res['status']}, "
        f"elapsed={res['fetch_report']['total_elapsed_sec']}s."
    )


if __name__ == "__main__":
    main()

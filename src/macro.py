"""Daily macro economic data generator producing `data/macro.json`."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
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


def fetch_fred_series(
    series_id: str,
    http_get: Any = None,
    timeout_sec: int = 12,
) -> list[tuple[str, float | None]]:
    """Fetch public FRED CSV (no API key) and parse `(YYYY-MM-DD, float|None)` rows."""
    getter = http_get or requests.get
    url = FRED_CSV_URL.format(series_id=series_id)
    last_err: Exception | None = None
    for _attempt in range(2):
        try:
            resp = getter(url, timeout=timeout_sec, headers={"User-Agent": "SignalDesk-Macro/1.0"})
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
    raise RuntimeError(str(last_err or f"Failed fetching FRED {series_id}"))


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
    timeout_sec: int = 12,
) -> dict[str, Any]:
    """Fetch latest ISM Manufacturing / Services PMI reading from TradingEconomics public page."""
    url = TE_ISM_URLS[indicator_id]
    getter = http_get or requests.get
    resp = getter(
        url,
        timeout=timeout_sec,
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
        "sub_info": f"FRED 最新目標區間截至 {latest_d}｜{last_dec.get('action', '')}",
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
        "source": f"FRED（DFEDTARU / DFEDTARL，截至 {latest_d}）＋ Federal Reserve FOMC",
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
) -> dict[str, Any]:
    clean = [(d, v) for d, v in fred_rows if v is not None]
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
        "source": f"FRED（{fred_id}，截至 {latest_d}）",
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


def _evaluate_verdict(indicator_id: str, actual: float | None, consensus: float | None, prev: float | None) -> tuple[str, str]:
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
    if indicator_id in ("ISM_MFG", "ISM_SVC"):
        if actual >= 55.0:
            return "bear", "🔴 景氣偏熱"
        if actual < 50.0:
            return "neut", "🟡 收縮區間"
        if prev is not None and actual < prev:
            return "bull", "🟢 溫和降溫"
        return "neut", "🟡 溫和擴張"
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
    verdict, vtext = _evaluate_verdict(indicator_id, latest_actual, latest_cons, latest_prev)

    next_up = _find_next_upcoming(upcoming_entries, now_utc)
    if indicator_id in ("UNRATE", "AVG_HOURLY_EARNINGS"):
        next_up = _find_next_upcoming(list((cal_cfg.get("indicators", {}).get("NFP") or {}).get("upcoming_releases") or []), now_utc)

    source_str = (
        "TradingEconomics 公開頁面 ＋ ISM 報告"
        if indicator_id in ("ISM_MFG", "ISM_SVC")
        else f"FRED（{spec['code']}）"
    )
    if indicator_id in ("UNRATE", "AVG_HOURLY_EARNINGS"):
        modal_foot_extra = "BLS 就業報告固定與非農就業同日公布。歷史實際值取自 FRED 最新官方序列。"
    elif indicator_id in ("ISM_MFG", "ISM_SVC"):
        modal_foot_extra = "ISM 製造業／服務業 PMI 來自 ISM 官方報告與 TradingEconomics 公開存檔。"
    else:
        modal_foot_extra = "歷史實際值以 FRED 最新官方序列為基準；部分前期經官方修正之項目於備註標示公布當下初值與意外比較。"

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
        "data_status": "ok",
        "updating": False,
        "modal_title": spec["name"],
        "modal_foot_extra": modal_foot_extra,
        "history_rows": rows_desc,
        "history_stats": _build_modal_stats(rows_desc),
    }
    return card, cal_past_items


def _build_upcoming_calendar(
    cal_cfg: dict[str, Any],
    icsa_upcoming: list[dict[str, Any]],
    now_utc: datetime,
    published_keys: set[tuple[str, str]],
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

    fomc_up = (cal_cfg.get("fomc") or {}).get("upcoming") or []
    for item in fomc_up:
        _add(
            f"fomc-{item['period']}",
            item.get("title", "FOMC 利率決議"),
            "FED_RATE",
            str(item["period"]),
            item["release_et"],
            int(item.get("stars", 3)),
            item.get("note", ""),
        )

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
        for item in (ind_cfg.get("upcoming_releases") or []):
            p = str(item.get("period", ""))
            _add(
                f"{ind_id.lower()}-{p}",
                f"{name}（{_period_zh(p)}）",
                ind_id,
                p,
                item["release_et"],
                int(item.get("stars", ind_cfg.get("stars", 2))),
                item.get("note", ""),
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


def _build_overview_summary(cards_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Derive macro stance summary & asset impact framing from latest card readings."""
    fed = cards_by_id.get("FED_RATE") or {}
    cpi = cards_by_id.get("CPI") or {}
    unrate = cards_by_id.get("UNRATE") or {}
    dgs10 = cards_by_id.get("DGS10") or {}

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

    is_hawk = stance_cls == "bear"
    asset_impacts = [
        {
            "asset": "美股",
            "sub": "標普 500 · 那斯達克 100",
            "cls": "bear" if is_hawk else ("bull" if stance_cls == "bull" else "neut"),
            "tag": "偏利空" if is_hawk else ("偏利多" if stance_cls == "bull" else "中性"),
            "note": (
                f"10 年期殖利率處於 {dgs10.get('value_str', '--')} 高檔，估值倍數承壓；利率敏感度高的科技成長股波動較大。"
                if is_hawk
                else "利率與通膨壓力趨緩時，有利成長股與大盤估值修復。"
            ),
        },
        {
            "asset": "台股",
            "sub": "加權指數 · 電子權值股",
            "cls": "bear" if is_hawk else ("bull" if stance_cls == "bull" else "neut"),
            "tag": "偏利空" if is_hawk else ("偏利多" if stance_cls == "bull" else "中性"),
            "note": "台股與美股科技股高度連動，美債殖利率走高時外資易調節電子權值與高本益比族群。",
        },
        {
            "asset": "加密貨幣",
            "sub": "BTC · 主流幣",
            "cls": "bear" if is_hawk else ("bull" if stance_cls == "bull" else "neut"),
            "tag": "偏利空" if is_hawk else ("偏利多" if stance_cls == "bull" else "中性"),
            "note": "加密市場對美元流動性與實質利率最敏感，數據過熱推高緊縮預期時短線震盪易放大。",
        },
        {
            "asset": "美債與黃金",
            "sub": "2Y / 10Y 殖利率 · 黃金",
            "cls": "neut",
            "tag": "拉鋸觀察",
            "note": f"名目殖利率高檔（2Y {cards_by_id.get('DGS2', {}).get('value_str', '--')} / 10Y {dgs10.get('value_str', '--')}）壓抑無息資產，但避險需求與央行買盤提供下檔支撐。",
        },
    ]

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
    preserved["updating_notice"] = f"資料更新中（來源暫時無法取得：{err_msg}，暫保留前次資料）"
    return preserved


def generate_macro_file(
    out_dir: Path,
    calendar_path: Path | None = None,
    http_get: Any = None,
    now_utc: datetime | None = None,
    use_local_cache: bool = True,
) -> dict[str, Any]:
    """Fetch public macro data, compute market reactions from history_1d.json.gz, and write `out_dir/macro.json`."""
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

    # Fetch FRED series cache (deduplicated)
    fred_cache: dict[str, list[tuple[str, float | None]]] = {}
    fred_errors: dict[str, str] = {}
    needed_fred = [
        "DFEDTARU", "DFEDTARL", "DGS2", "DGS10",
        "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIFIS",
        "PAYEMS", "UNRATE", "CES0500000003", "ICSA",
        "A191RL1Q225SBEA", "RSAFS",
    ]
    for sid in needed_fred:
        try:
            fred_cache[sid] = fetch_fred_series(sid, http_get=http_get)
        except Exception as exc:
            fred_errors[sid] = str(exc)

    cards_out: list[dict[str, Any]] = []
    cal_past_all: list[dict[str, Any]] = []
    updating_ids: list[str] = []
    icsa_upcoming: list[dict[str, Any]] = []

    # 1. FED_RATE
    if "DFEDTARU" in fred_cache and "DFEDTARL" in fred_cache:
        cards_out.append(
            _build_fomc_card(fred_cache["DFEDTARU"], fred_cache["DFEDTARL"], cal_cfg, indexed_bars, now)
        )
        # Also populate FOMC past decisions into calendar past
        fomc_card = cards_out[-1]
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
    elif "FED_RATE" in prev_cards_by_id:
        err = fred_errors.get("DFEDTARU") or fred_errors.get("DFEDTARL") or "FRED 連線失敗"
        cards_out.append(_mark_preserved_updating(prev_cards_by_id["FED_RATE"], err))
        updating_ids.append("FED_RATE")
    else:
        updating_ids.append("FED_RATE")

    # 2. DGS2 & DGS10
    for yid, yname, fid, ywhy in (
        (
            "DGS2",
            "2 年期美債殖利率",
            "DGS2",
            "反映市場對未來兩年聯準會政策利率路徑的定價，對 FOMC 與通膨數據最為敏感。",
        ),
        (
            "DGS10",
            "10 年期美債殖利率",
            "DGS10",
            "全球風險資產的定價錨，影響企業融資成本、房貸利率與科技股折現估值。",
        ),
    ):
        if fid in fred_cache:
            cards_out.append(_build_daily_yield_card(yid, yname, fid, ywhy, fred_cache[fid], indexed_bars))
        elif yid in prev_cards_by_id:
            cards_out.append(_mark_preserved_updating(prev_cards_by_id[yid], fred_errors.get(fid, "FRED 連線失敗")))
            updating_ids.append(yid)
        else:
            updating_ids.append(yid)

    # 3. Remaining 13 standard indicators
    std_order = [
        "CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI",
        "NFP", "UNRATE", "AVG_HOURLY_EARNINGS", "ICSA",
        "GDP", "ISM_MFG", "ISM_SVC", "RETAIL_SALES",
    ]
    for ind_id in std_order:
        spec = INDICATOR_SPECS[ind_id]
        if ind_id in ("ISM_MFG", "ISM_SVC"):
            try:
                te_latest = fetch_ism_te(ind_id, http_get=http_get)
                card, cal_items = _build_standard_indicator_card(
                    ind_id, cal_cfg, None, te_latest, indexed_bars, now
                )
                cards_out.append(card)
                cal_past_all.extend(cal_items)
            except Exception as exc:
                if ind_id in prev_cards_by_id:
                    cards_out.append(_mark_preserved_updating(prev_cards_by_id[ind_id], str(exc)))
                else:
                    # Fallback to verified YAML snapshot while clearly flagging updating
                    card, cal_items = _build_standard_indicator_card(
                        ind_id, cal_cfg, None, None, indexed_bars, now
                    )
                    cards_out.append(_mark_preserved_updating(card, str(exc)))
                    cal_past_all.extend(cal_items)
                updating_ids.append(ind_id)
        else:
            fid = spec["code"]
            if fid in fred_cache:
                card, cal_items = _build_standard_indicator_card(
                    ind_id, cal_cfg, fred_cache[fid], None, indexed_bars, now
                )
                cards_out.append(card)
                cal_past_all.extend(cal_items)
                if ind_id == "ICSA":
                    _, icsa_upcoming = _build_icsa_schedule_map(fred_cache[fid], cal_cfg, now)
            elif ind_id in prev_cards_by_id:
                cards_out.append(_mark_preserved_updating(prev_cards_by_id[ind_id], fred_errors.get(fid, "FRED 連線失敗")))
                updating_ids.append(ind_id)
                cal_past_all.extend([e for e in prev_cal_past if e.get("indicator_id") == ind_id])
            else:
                updating_ids.append(ind_id)

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

    published_keys = {(p.get("indicator_id", ""), p.get("period", "")) for p in past_filtered}
    upcoming_sorted = _build_upcoming_calendar(cal_cfg, icsa_upcoming, now, published_keys)

    cards_by_id = {c["id"]: c for c in cards_out}
    overview = _build_overview_summary(cards_by_id)

    payload = {
        "version": 1,
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M"),
        "status": "partial" if updating_ids else "ok",
        "updating_indicators": updating_ids,
        "timezone_notes": cal_cfg.get("timezone_notes") or {},
        "reaction_methodology": {
            "source": "本站 data 分支 history_1d.json.gz（約 10 年日線）",
            "NAS100_SP500": "公布當日美股收盤（若於美東 16:00 後公布則取次一交易日）對前一交易日收盤報酬率",
            "TWII": "公布後第一個台股交易日收盤（台北 13:30 收盤截點）對前一交易日收盤報酬率",
            "BTCUSDT": "公布前最後一根 UTC 日 K 收盤（08:00 台北）對公布後第一根 UTC 日 K 收盤報酬率",
        },
        "overview": overview,
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
    args = parser.parse_args()

    res = generate_macro_file(
        out_dir=args.out_dir,
        calendar_path=args.calendar,
        use_local_cache=not args.no_local_cache,
    )
    print(
        f"Generated {args.out_dir / 'macro.json'}: "
        f"{len(res['indicators'])} indicators, "
        f"{len(res['calendar']['upcoming'])} upcoming, "
        f"{len(res['calendar']['past'])} past events, "
        f"status={res['status']}."
    )


if __name__ == "__main__":
    main()

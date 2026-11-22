"""News, on-chain, derivatives, and exchange announcement generator (`src.news`).

Produces:
- `data/news/YYYY-MM.jsonl`: append-only permanent monthly archive (never modifies/deletes old lines)
- `data/news_latest.json`: latest issue items + recent 7-day items + fetch_report
- `data/news_index.json`: index of all issues and monthly JSONL archives
"""

from __future__ import annotations

import argparse
import difflib
import email.utils
import hashlib
import html
import json
import re
import time as time_mod
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"
DEFAULT_NEWS_CONFIG_PATH = ROOT_DIR / "config" / "news_config.yaml"

TPE_TZ = ZoneInfo("Asia/Taipei")
UTC_TZ = timezone.utc

MAX_TIMEOUT_CAP_SEC = 10
MAX_RETRIES_CAP = 1

VALID_CATEGORIES = (
    "總經",
    "聯準會",
    "美股",
    "台股",
    "加密貨幣",
    "鏈上資金",
    "衍生品",
    "交易所公告",
    "安全事件",
)

VALID_MARKETS = (
    "台股",
    "美股",
    "加密貨幣",
    "總經",
)

CRON_MINUTES = (2, 17, 32, 47)
MAX_REACTIONS_PER_RUN = 40

_TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "ref",
    "source",
    "guccounter",
    "guce_referrer",
    "guce_referrer_sig",
}

_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "that", "this", "it", "its", "after", "before", "over", "under", "into",
    "says", "say", "said", "amid", "ahead", "new", "will", "may", "can",
}

_CASE_SENSITIVE_SHORT_TICKERS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "LTC", "TRX", "BCH", "NEAR", "SUI", "TON", "APT", "UNI", "ATOM", "ETC",
    "FIL", "ARB", "OP", "INJ", "AAVE", "XLM", "HBAR", "ICP", "SEI", "TIA",
    "PEPE", "WLD", "RNDR", "NDX", "SPX", "WTI", "XAU", "XAG", "AAPL", "TSLA",
    "GOOGL", "GOOG", "SNDK", "SPCX",
}


def load_news_config(path: Path | None = None) -> dict[str, Any]:
    """Load `config/news_config.yaml`."""
    target = path or DEFAULT_NEWS_CONFIG_PATH
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid news config format in {target}")
    return data


def load_tracked_instruments(path: Path | None = None) -> list[dict[str, Any]]:
    """Load enabled instruments from `config/config.yaml`."""
    target = path or DEFAULT_CONFIG_PATH
    with target.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    raw = cfg.get("instruments") or []
    return [inst for inst in raw if isinstance(inst, dict) and inst.get("enabled", True)]


def compute_issue_id(dt_utc: datetime) -> str:
    """Compute issue ID in Asia/Taipei: 08:00-19:59 -> same day AM; 20:00-07:59 -> 20:00 day PM."""
    tpe = dt_utc.astimezone(TPE_TZ)
    if 8 <= tpe.hour < 20:
        return f"{tpe.strftime('%Y-%m-%d')}-AM"
    if tpe.hour >= 20:
        return f"{tpe.strftime('%Y-%m-%d')}-PM"
    prev_day = (tpe - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{prev_day}-PM"


def compute_next_update_hint(dt_utc: datetime, cron_minutes: tuple[int, ...] = CRON_MINUTES) -> str:
    """Compute next approximate Taipei update time string based on 15m cron slots."""
    tpe = dt_utc.astimezone(TPE_TZ)
    next_min = next((m for m in sorted(cron_minutes) if m > tpe.minute), None)
    if next_min is not None:
        next_tpe = tpe.replace(minute=next_min, second=0, microsecond=0)
    else:
        next_hour = tpe.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        next_tpe = next_hour.replace(minute=min(cron_minutes))
    return f"約台北時間 {next_tpe.strftime('%H:%M')}（每 15 分鐘更新）"


def _fmt_timestamps(dt_utc: datetime) -> dict[str, str]:
    utc = dt_utc.astimezone(UTC_TZ)
    tpe = dt_utc.astimezone(TPE_TZ)
    return {
        "published_at_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at_taipei": tpe.strftime("%Y-%m-%d %H:%M"),
        "published_at_taipei_iso": tpe.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "month": tpe.strftime("%Y-%m"),
    }


def _parse_datetime(raw: Any) -> datetime | None:
    """Parse RFC 2822, ISO 8601, or unix timestamp into timezone-aware UTC datetime."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        try:
            return datetime.fromtimestamp(val, tz=UTC_TZ)
        except Exception:
            return None
    s = str(raw).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return _parse_datetime(float(s))
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC_TZ)
            return dt.astimezone(UTC_TZ)
    except Exception:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(UTC_TZ)
    except Exception:
        return None


def canonicalize_url(url: str) -> str:
    """Normalize URL for duplicate comparison by stripping tracking parameters."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        p = urllib.parse.urlsplit(raw)
        scheme = "https" if p.scheme in ("http", "https", "") else p.scheme.lower()
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = re.sub(r"/+$", "", p.path) or "/"
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
        filtered_qs = [(k, v) for k, v in qs if k.lower() not in _TRACKING_QUERY_PARAMS]
        query = urllib.parse.urlencode(filtered_qs)
        return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return raw.rstrip("/")


def _clean_html_text(text: str) -> str:
    if not text:
        return ""
    s = html.unescape(str(text))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_title_for_compare(title: str) -> str:
    s = html.unescape(str(title or "")).strip()
    # Strip common trailing outlet attributions like " - CNBC" or " | Reuters"
    s = re.sub(r"\s+[-|｜]\s+[A-Za-z0-9\u4e00-\u9fff .&]{2,25}$", "", s)
    s = s.lower()
    s = re.sub(r"[\"'`“”‘’()\[\]{}【】（）<>《》,.;:!?，。；：！？/\\|@#%^&*_+=~]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _title_tokens(norm_title: str) -> set[str]:
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]{2,}", norm_title):
        if word not in _EN_STOPWORDS:
            tokens.add(word)
    # Chinese character bigrams for CJK title comparison
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", norm_title)
    for run in cjk_runs:
        if len(run) == 1:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
    return tokens


def title_similarity(title_a: str, title_b: str) -> float:
    """Compute similarity in [0.0, 1.0] across English and Traditional Chinese headlines."""
    na = _normalize_title_for_compare(title_a)
    nb = _normalize_title_for_compare(title_b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta = _title_tokens(na)
    tb = _title_tokens(nb)
    if not ta or not tb:
        return seq_ratio
    inter = len(ta & tb)
    union = len(ta | tb)
    jaccard = inter / union if union else 0.0
    overlap_min = inter / min(len(ta), len(tb)) if min(len(ta), len(tb)) else 0.0
    return max(seq_ratio, 0.55 * jaccard + 0.45 * overlap_min)


def build_symbol_alias_map(
    instruments: list[dict[str, Any]],
    news_cfg: dict[str, Any],
) -> dict[str, list[str]]:
    """Combine `config.yaml` instruments with `news_config.yaml` symbol_aliases."""
    cfg_aliases = news_cfg.get("symbol_aliases") or {}
    out: dict[str, list[str]] = {}
    for inst in instruments:
        sym = str(inst.get("symbol") or "").strip()
        if not sym:
            continue
        aliases: list[str] = [sym]
        disp = str(inst.get("display_name") or "").strip()
        # Extract base name before parentheses, e.g. "台積電 (2330)" -> "台積電"
        base_disp = re.sub(r"\s*\([^)]*\)\s*", "", disp).strip()
        if base_disp and base_disp.lower() not in ("gold", "silver", "copper"):
            aliases.append(base_disp)
        if sym.endswith("USDT") and len(sym) > 4:
            aliases.append(sym[:-4])
        for extra in cfg_aliases.get(sym, []) or []:
            s_extra = str(extra).strip()
            if s_extra and s_extra not in aliases:
                aliases.append(s_extra)
        out[sym] = aliases
    return out


def _alias_matches_text(alias: str, text: str, text_lower: str) -> bool:
    al = alias.strip()
    if not al:
        return False
    # Special guard for Gold vs Goldman Sachs
    if al.lower() == "gold":
        return bool(re.search(r"\bgold\b(?!man)", text_lower))
    # Pure ASCII ticker / short word
    if re.fullmatch(r"[A-Za-z0-9.\-&]+", al):
        upper = al.upper()
        if upper in _CASE_SENSITIVE_SHORT_TICKERS and len(al) <= 5:
            # Match uppercase ticker or $ticker with boundaries
            pattern = rf"(?<![A-Za-z0-9]){re.escape(upper)}(?![A-Za-z0-9])"
            if re.search(pattern, text):
                return True
            if al.lower() != upper and len(al) >= 4:
                return False
            return False
        if re.fullmatch(r"\d{4}", al):
            return bool(re.search(rf"(?<!\d){re.escape(al)}(?!\d)", text))
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(al.lower())}(?![A-Za-z0-9])", text_lower))
    # CJK or phrase containing spaces/non-ASCII
    return al.lower() in text_lower


def match_symbols(
    title: str,
    summary: str,
    symbol_aliases: dict[str, list[str]],
) -> list[str]:
    """Match tracked `config.yaml` instrument symbols mentioned in title/summary."""
    combined = f"{title or ''} \n {summary or ''}"
    combined_lower = combined.lower()
    matched: list[str] = []
    for sym, aliases in symbol_aliases.items():
        for al in aliases:
            if _alias_matches_text(al, combined, combined_lower):
                matched.append(sym)
                break
    return matched


def is_trading_relevant(
    title: str,
    summary: str,
    symbols: list[str],
    news_cfg: dict[str, Any],
    filter_trading: bool = True,
) -> bool:
    """Return True if item is trading-relevant and not lifestyle/non-market fluff."""
    text = f"{title or ''} {summary or ''}".lower()
    tf = news_cfg.get("trading_filter") or {}
    for bad in tf.get("exclude_keywords") or []:
        if str(bad).lower() in text:
            return False
    if not filter_trading:
        return True
    if symbols:
        return True
    for kw in tf.get("include_keywords") or []:
        k = str(kw).lower().strip()
        if not k:
            continue
        if re.fullmatch(r"[a-z0-9& ]+", k) and len(k) <= 5:
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text):
                return True
        elif k in text:
            return True
    return False


def classify_category(
    title: str,
    summary: str,
    default_category: str,
    source_id: str,
    symbols: list[str],
) -> str:
    """Classify item into one of the 9 required Chinese categories using explicit rules."""
    text = f"{title or ''} {summary or ''}".lower()

    if source_id == "defillama_hacks" or any(
        kw in text for kw in ("hack", "hacked", "exploit", "exploited", "drained", "stolen", "security breach", "駭客", "遭駭", "被盜", "漏洞攻擊")
    ):
        return "安全事件"

    if source_id.endswith("_announcements") or any(
        kw in text for kw in ("will list", "new listing", "delist", "delisting", "上幣", "下幣", "下架公告", "移除交易對")
    ):
        return "交易所公告"

    if source_id.endswith("_derivatives") or any(
        kw in text for kw in ("funding rate", "open interest", "perpetual", "liquidation", "資金費率", "未平倉", "爆倉", "清算")
    ):
        return "衍生品"

    if source_id in ("defillama_stablecoins", "defillama_chains", "bitbo_btc_etf", "mempool_space") or any(
        kw in text for kw in ("stablecoin supply", "tvl", "spot etf", "etf inflow", "etf outflow", "mempool", "穩定幣總市值", "淨流入", "淨流出", "鏈上鎖倉")
    ):
        return "鏈上資金"

    if source_id == "fed_press" or any(
        kw in text for kw in ("fomc", "federal reserve", "jerome powell", "fed chair", "fed rate", "聯準會", "鮑爾", "鮑威爾", "fed")
    ):
        # Ensure bare "fed" matches word boundary
        if source_id == "fed_press" or re.search(r"(?<![a-z])fed(?![a-z])", text) or any(
            kw in text for kw in ("fomc", "federal reserve", "powell", "聯準會", "鮑爾", "鮑威爾")
        ):
            return "聯準會"

    tw_syms = {"TWII", "2330", "2454", "3711", "2317", "2382", "2308", "2881", "2882", "2891", "2603", "1301", "2412"}
    if source_id in ("cnyes_tw_stock", "udn_money_stock", "yahoo_tw_stock") or any(s in tw_syms for s in symbols) or any(
        kw in text for kw in ("台股", "加權指數", "台積電", "外資買超", "外資賣超", "國安基金", "金管會")
    ):
        return "台股"

    if source_id in ("cnbc_economy", "cnyes_macro") or any(
        kw in text for kw in ("cpi", "ppi", "pce", "gdp", "nonfarm", "payroll", "unemployment", "jobless", "tariff", "inflation", "treasury yield", "opec", "crude oil", "通膨", "非農", "關稅", "失業率", "殖利率", "美債", "原油", "黃金")
    ):
        return "總經"

    us_syms = {"NAS100", "SP500", "TSLA", "GOOGL", "AAPL", "SNDK", "SPCX"}
    if ("HSI" in symbols or any(kw in text for kw in ("hang seng", "恒生", "恆生", "恒指", "恆指", "港股"))) and not any(
        s in us_syms for s in symbols
    ):
        return "總經"
    if source_id in ("cnyes_us_stock", "cnbc_top", "marketwatch_top", "yahoo_finance_us") and not any(
        s.endswith("USDT") for s in symbols
    ):
        return "美股"
    if any(s in us_syms for s in symbols) or any(kw in text for kw in ("美股", "那斯達克", "標普", "道瓊", "nasdaq", "s&p 500", "wall street")):
        return "美股"

    if any(s.endswith("USDT") for s in symbols) or source_id in ("coindesk", "cointelegraph", "theblock", "decrypt", "cnyes_crypto") or any(
        kw in text for kw in ("bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "defi", "比特幣", "以太坊", "加密貨幣")
    ):
        return "加密貨幣"

    return default_category if default_category in VALID_CATEGORIES else "總經"


def score_importance(
    title: str,
    category: str,
    source_id: str,
    source_tier: str,
    symbols: list[str],
    news_cfg: dict[str, Any],
    *,
    is_data_alert: bool = False,
    data_severity: int | None = None,
) -> tuple[int, str, str]:
    """Determine importance 1..3 (★..★★★) using config rules."""
    if is_data_alert and data_severity in (1, 2, 3):
        stars = "★" * data_severity
        return data_severity, stars, "數據門檻規則"

    rules = news_cfg.get("importance_rules") or {}
    text = str(title or "").lower()

    if category in (rules.get("star3_categories") or []):
        return 3, "★★★", f"分類規則（{category}）"

    if source_id in (rules.get("star3_sources") or []) and any(
        k in text for k in ("fomc", "monetary", "rate", "discount rate", "statement", "beigebook")
    ):
        return 3, "★★★", "官方央行決策/聲明"

    for kw in rules.get("star3_keywords") or []:
        k = str(kw).lower().strip()
        if not k:
            continue
        matched = (
            bool(re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text))
            if re.fullmatch(r"[a-z0-9 ]+", k) and len(k) <= 4
            else (k in text)
        )
        if matched:
            return 3, "★★★", f"重大關鍵字（{kw}）"

    for kw in rules.get("star2_keywords") or []:
        k = str(kw).lower().strip()
        if k and k in text:
            return 2, "★★", f"市場重點（{kw}）"

    major_syms = set(rules.get("star3_major_symbols") or [])
    if any(s in major_syms for s in symbols) and source_tier in ("official", "major", "exchange"):
        return 2, "★★", "核心追蹤標的"

    if symbols or source_tier in ("official", "major"):
        return 2 if symbols else 1, ("★★" if symbols else "★"), ("追蹤標的相關" if symbols else "一般交易消息")

    return 1, "★", "一般交易消息"


def derive_markets(
    title: str,
    summary: str,
    category: str,
    symbols: list[str],
    news_cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Derive market super-categories from `台股 | 美股 | 加密貨幣 | 總經` (multi-select)."""
    cfg = (news_cfg or {}).get("market_mapping") or {}
    cat_map = cfg.get("category_to_markets") or {
        "台股": ["台股"],
        "美股": ["美股"],
        "加密貨幣": ["加密貨幣"],
        "鏈上資金": ["加密貨幣"],
        "衍生品": ["加密貨幣"],
        "交易所公告": ["加密貨幣"],
        "安全事件": ["加密貨幣"],
        "總經": ["總經"],
        "聯準會": ["總經"],
    }
    sym_rules = cfg.get("symbol_market_rules") or {}
    tw_syms = set(sym_rules.get("tw_symbols") or [
        "TWII", "2330", "2454", "3711", "2317", "2382", "2308", "2881", "2882", "2891", "2603", "1301", "2412",
    ])
    us_syms = set(sym_rules.get("us_symbols") or ["NAS100", "SP500", "TSLA", "GOOGL", "AAPL", "SNDK", "SPCX"])
    macro_syms = set(sym_rules.get("macro_symbols") or ["HSI", "JP225"])
    crypto_suffix = str(sym_rules.get("crypto_suffix") or "USDT")

    found: set[str] = set()
    for m in cat_map.get(category, []):
        if m in VALID_MARKETS:
            found.add(m)

    for sym in symbols or []:
        s = str(sym).strip()
        if not s:
            continue
        if s in tw_syms:
            found.add("台股")
        elif s in us_syms:
            found.add("美股")
        elif s in macro_syms:
            found.add("總經")
        elif crypto_suffix and s.endswith(crypto_suffix):
            found.add("加密貨幣")

    kw_map = cfg.get("keyword_to_market") or {
        "台股": ["台股", "加權指數", "台積電", "台灣股市", "taiex", "taiwan stock", "tsmc"],
        "美股": ["美股", "那斯達克", "標普", "道瓊", "華爾街", "wall street", "us stocks", "u.s. stocks", "nasdaq", "s&p 500", "dow jones"],
        "加密貨幣": ["比特幣", "以太坊", "加密貨幣", "幣圈", "穩定幣", "bitcoin", "btc", "ethereum", "eth", "crypto"],
    }
    text_lower = f"{title or ''} {summary or ''}".lower()
    # Macro / Fed news (or cross-market news) also picks up clearly affected markets
    for mkt, kws in kw_map.items():
        if mkt not in VALID_MARKETS:
            continue
        for kw in kws or []:
            k = str(kw).lower().strip()
            if not k:
                continue
            if re.fullmatch(r"[a-z0-9.\- ]+", k) and len(k) <= 5:
                if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text_lower):
                    found.add(mkt)
                    break
            elif k in text_lower:
                found.add(mkt)
                break

    ordered = [m for m in VALID_MARKETS if m in found]
    return ordered or ["總經"]


def derive_sentiment_targets(
    symbols: list[str],
    markets: list[str],
    category: str,
    news_cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve affected target symbols, falling back to representative market tickers."""
    cleaned = list(dict.fromkeys(str(s).strip() for s in (symbols or []) if str(s).strip()))
    if cleaned:
        return cleaned

    rules = (news_cfg or {}).get("sentiment_rules") or {}
    by_mkt = rules.get("default_targets_by_market") or {
        "加密貨幣": ["BTCUSDT"],
        "台股": ["TWII"],
        "美股": ["NAS100"],
        "總經": ["NAS100"],
    }
    by_cat = rules.get("default_targets_by_category") or {}
    for m in markets or []:
        t = by_mkt.get(m)
        if t:
            return list(t)
    if category in by_cat and by_cat[category]:
        return list(by_cat[category])
    return ["NAS100"]


def _keyword_matches_in_text(text_lower: str, kw_lower: str) -> list[tuple[int, int]]:
    if not kw_lower:
        return []
    if re.fullmatch(r"[a-z0-9' \-]+", kw_lower):
        pattern = rf"(?<![a-z0-9]){re.escape(kw_lower)}(?![a-z0-9])"
        return [(m.start(), m.end()) for m in re.finditer(pattern, text_lower)]
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text_lower.find(kw_lower, start)
        if idx < 0:
            break
        spans.append((idx, idx + len(kw_lower)))
        start = idx + len(kw_lower)
    return spans


def _is_span_negated(
    text_lower: str,
    start: int,
    kw_lower: str,
    neg_en: list[str],
    neg_zh: list[str],
    false_zh_spans: list[tuple[int, int]],
) -> bool:
    is_ascii = bool(re.fullmatch(r"[a-z0-9' \-]+", kw_lower))
    if is_ascii:
        window = text_lower[max(0, start - 36) : start]
        # Only look within the current clause
        clause = re.split(r"[,.;:!?—\-]|(?:\band\b|\bbut\b|\bamid\b)", window)[-1]
        for nw in neg_en:
            nwl = nw.lower().strip()
            if nwl and re.search(rf"(?<![a-z0-9]){re.escape(nwl)}(?![a-z0-9])", clause):
                return True
        return False

    # CJK / mixed phrase: check up to 6 chars before start inside same clause
    win_start = max(0, start - 6)
    window = text_lower[win_start:start]
    clause = re.split(r"[，。；：、！？（）「」\[\]]", window)[-1]
    clause_offset = start - len(clause)
    for np in neg_zh:
        npl = np.lower().strip()
        if not npl:
            continue
        pos = clause.rfind(npl)
        if pos < 0:
            continue
        abs_pos = clause_offset + pos
        abs_end = abs_pos + len(npl)
        # Skip if negation token sits inside a false-negation compound like 未平倉 / 不如預期
        if any(fs <= abs_pos < fe for fs, fe in false_zh_spans):
            continue
        # Only trigger if close enough to the matched keyword (gap <= 3 chars)
        if start - abs_end <= 3:
            return True
    return False


def evaluate_sentiment(
    title: str,
    summary: str,
    category: str,
    markets: list[str],
    symbols: list[str],
    source_id: str,
    news_cfg: dict[str, Any] | None = None,
    *,
    is_data_alert: bool = False,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rule-based bullish/bearish/neutral sentiment classifier (no API keys)."""
    cfg = news_cfg or {}
    rules = cfg.get("sentiment_rules") or {}
    targets = derive_sentiment_targets(symbols, markets, category, cfg)

    def _pack(sent: str, score: int, reason: str) -> dict[str, Any]:
        lbl_map = {"bullish": "利多", "bearish": "利空", "neutral": "中性"}
        return {
            "sentiment": sent,
            "sentiment_label": lbl_map[sent],
            "sentiment_score": int(score),
            "sentiment_reason": reason,
            "sentiment_targets": targets,
            "sentiment_method": "rule",
        }

    # 1. Forced category rules (e.g. 安全事件一律利空)
    by_cat = rules.get("by_category") or {}
    if category in by_cat or source_id == "defillama_hacks":
        cat_rule = by_cat.get(category) or {}
        forced = str(cat_rule.get("force_sentiment") or "bearish")
        sc = int(cat_rule.get("score", -3))
        rsn = str(cat_rule.get("reason") or "安全事件一律偏空（駭客／漏洞風險）")
        return _pack(forced, sc, rsn)

    # 2. Exchange announcements
    title_str = str(title or "")
    if source_id.endswith("_announcements"):
        if "[下架]" in title_str or "delist" in title_str.lower():
            return _pack("bearish", -3, "命中：交易所下架公告")
        if "[上幣]" in title_str or "list" in title_str.lower():
            return _pack("bullish", 2, "命中：交易所上幣公告")

    # 3. Deterministic data alerts based on numeric direction
    if is_data_alert and isinstance(metrics, dict):
        if source_id == "bitbo_btc_etf" and "net_flow_usd_m" in metrics:
            flow = float(metrics.get("net_flow_usd_m") or 0.0)
            th = float(((cfg.get("data_thresholds") or {}).get("btc_etf_net_flow_abs_usd_m") or {}).get("value", 150.0))
            sev = 3 if abs(flow) >= th * 2.5 else 2
            if flow > 0:
                return _pack("bullish", sev, "數據：BTC 現貨 ETF 淨流入")
            if flow < 0:
                return _pack("bearish", -sev, "數據：BTC 現貨 ETF 淨流出")
        if source_id == "defillama_stablecoins" and "change_1d_usd_m" in metrics:
            chg_m = float(metrics.get("change_1d_usd_m") or 0.0)
            if chg_m > 0:
                return _pack("bullish", 2, "數據：穩定幣供給大增（流動性增加）")
            if chg_m < 0:
                return _pack("bearish", -2, "數據：穩定幣供給大減（流動性流出）")
        if source_id == "defillama_chains" and "change_1d_pct" in metrics:
            pct = float(metrics.get("change_1d_pct") or 0.0)
            if pct > 0:
                return _pack("bullish", 2, "數據：公鏈 TVL 資金淨流入")
            if pct < 0:
                return _pack("bearish", -2, "數據：公鏈 TVL 資金淨流出")
        if source_id.endswith("_derivatives"):
            if "funding_rate_pct" in metrics and metrics["funding_rate_pct"] is not None:
                fr = float(metrics["funding_rate_pct"])
                fr_high = float(((cfg.get("data_thresholds") or {}).get("funding_rate_high_pct") or {}).get("value", 0.05))
                if fr >= fr_high:
                    return _pack("bearish", -2, "數據：偏空（多頭擁擠，資金費率過高）")
                return _pack("bullish", 2, "數據：偏多（空頭極端，具軋空條件）")
            if "oi_24h_pct" in metrics:
                return _pack("neutral", 0, "數據：未平倉量劇烈變動（方向中性）")
        if source_id == "mempool_space":
            return _pack("neutral", 0, "數據：鏈上擁擠度升高（方向中性）")

    # 4. General rule-based keyword & inversion/negation matching
    text_lower = f"{title or ''} {summary or ''}".lower()
    if not text_lower.strip():
        return _pack("neutral", 0, "未命中明確多空關鍵字（中性）")

    consumed: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        return any(not (e <= cs or s >= ce) for cs, ce in consumed)

    false_zh_spans: list[tuple[int, int]] = []
    for fw in rules.get("negation_false_words_zh") or ["未平倉", "不如預期", "不斷", "不僅"]:
        false_zh_spans.extend(_keyword_matches_in_text(text_lower, str(fw).lower()))

    score = 0
    reasons: list[str] = []

    # 4a. Macro / inflation inversion (e.g. 通膨低於預期 -> bullish; 通膨高於預期 -> bearish)
    macro_topics = [
        str(t).lower().strip()
        for t in (rules.get("macro_inversion_topics_zh") or []) + (rules.get("macro_inversion_topics_en") or [])
        if str(t).strip()
    ]
    has_macro_topic = any(_keyword_matches_in_text(text_lower, t) for t in macro_topics)
    if has_macro_topic:
        cooling_kws = sorted(
            [str(k).lower().strip() for k in (rules.get("cooling_words_zh") or []) + (rules.get("cooling_words_en") or []) if str(k).strip()],
            key=len,
            reverse=True,
        )
        heating_kws = sorted(
            [str(k).lower().strip() for k in (rules.get("heating_words_zh") or []) + (rules.get("heating_words_en") or []) if str(k).strip()],
            key=len,
            reverse=True,
        )
        cool_hit = None
        for ck in cooling_kws:
            spans = _keyword_matches_in_text(text_lower, ck)
            if spans:
                for s, e in spans:
                    if not _overlaps(s, e):
                        consumed.append((s, e))
                cool_hit = ck
                break
        heat_hit = None
        if cool_hit is None:
            for hk in heating_kws:
                spans = _keyword_matches_in_text(text_lower, hk)
                if spans:
                    for s, e in spans:
                        if not _overlaps(s, e):
                            consumed.append((s, e))
                    heat_hit = hk
                    break
        if cool_hit:
            score += 2
            reasons.append(f"反轉：通膨／就業降溫（{cool_hit}）")
        elif heat_hit:
            score -= 2
            reasons.append(f"反轉：通膨／就業升溫（{heat_hit}）")

    # 4b. Collect bullish & bearish keyword lists (plus market-specific overrides)
    bull_cfg = rules.get("bullish") or {}
    bear_cfg = rules.get("bearish") or {}
    by_mkt_rules = rules.get("by_market") or {}

    bull_kws: list[str] = list(bull_cfg.get("en") or []) + list(bull_cfg.get("zh") or [])
    bear_kws: list[str] = list(bear_cfg.get("en") or []) + list(bear_cfg.get("zh") or [])
    for m in markets or []:
        m_rule = by_mkt_rules.get(m) or {}
        bull_kws.extend(m_rule.get("bullish") or [])
        bear_kws.extend(m_rule.get("bearish") or [])

    neg_en = [str(x) for x in (rules.get("negation_words_en") or [])]
    neg_zh = [str(x) for x in (rules.get("negation_prefixes_zh") or [])]

    tagged_kws: list[tuple[str, str]] = []
    seen_kw_pairs: set[tuple[str, str]] = set()
    for pol, kws in (("bullish", bull_kws), ("bearish", bear_kws)):
        for raw_kw in kws:
            kl = str(raw_kw).strip().lower()
            if kl and (pol, kl) not in seen_kw_pairs:
                seen_kw_pairs.add((pol, kl))
                tagged_kws.append((pol, str(raw_kw).strip()))
    # Longest phrases first so specific phrases consume before shorter substrings
    tagged_kws.sort(key=lambda pair: len(pair[1]), reverse=True)

    for pol, orig_kw in tagged_kws:
        kl = orig_kw.lower()
        for s, e in _keyword_matches_in_text(text_lower, kl):
            if _overlaps(s, e):
                continue
            consumed.append((s, e))
            negated = _is_span_negated(text_lower, s, kl, neg_en, neg_zh, false_zh_spans)
            if pol == "bullish":
                if negated:
                    score -= 1
                    reasons.append(f"反轉：否定「{orig_kw}」")
                else:
                    score += 1
                    reasons.append(f"命中：{orig_kw}")
            else:
                if negated:
                    score += 1
                    reasons.append(f"反轉：否定「{orig_kw}」")
                else:
                    score -= 1
                    reasons.append(f"命中：{orig_kw}")

    # Deduplicate reason labels while preserving order
    uniq_reasons = list(dict.fromkeys(reasons))
    if score > 0:
        return _pack("bullish", min(3, score), "；".join(uniq_reasons[:3]))
    if score < 0:
        return _pack("bearish", max(-3, score), "；".join(uniq_reasons[:3]))
    if uniq_reasons:
        return _pack("neutral", 0, f"多空訊號抵銷（{'；'.join(uniq_reasons[:2])}）")
    return _pack("neutral", 0, "未命中明確多空關鍵字（中性）")


def _http_get_with_retry(
    url: str,
    *,
    http_get: Callable[..., requests.Response] | None = None,
    timeout_sec: int = 8,
    max_retries: int = 1,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    getter = http_get or requests.get
    eff_timeout = min(max(1, int(timeout_sec)), MAX_TIMEOUT_CAP_SEC)
    eff_retries = min(max(0, int(max_retries)), MAX_RETRIES_CAP)
    last_exc: Exception | None = None
    for attempt in range(eff_retries + 1):
        try:
            resp = getter(url, headers=headers, timeout=eff_timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < eff_retries:
                time_mod.sleep(0.25)
    assert last_exc is not None
    raise last_exc


def _make_item(
    *,
    issue: str,
    dt_utc: datetime,
    title: str,
    url: str,
    source: str,
    source_id: str,
    category: str,
    symbols: list[str],
    importance: int,
    importance_reason: str,
    is_data_alert: bool = False,
    metrics: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
    summary: str = "",
    news_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = _fmt_timestamps(dt_utc)
    seen_dt = (now_utc or dt_utc).astimezone(UTC_TZ)
    seen_tpe = seen_dt.astimezone(TPE_TZ)
    canon = canonicalize_url(url)
    hash_key = f"{issue}|{canon}|{title}" if is_data_alert else f"{canon}|{title}"
    digest = hashlib.sha1(hash_key.encode("utf-8")).hexdigest()[:10]
    item_id = f"news-{dt_utc.astimezone(TPE_TZ).strftime('%Y%m%d')}-{digest}"
    markets = derive_markets(title, summary, category, symbols, news_cfg)
    sent = evaluate_sentiment(
        title,
        summary,
        category,
        markets,
        symbols,
        source_id,
        news_cfg,
        is_data_alert=is_data_alert,
        metrics=metrics,
    )
    return {
        "id": item_id,
        "issue": issue,
        "published_at_utc": ts["published_at_utc"],
        "published_at_taipei": ts["published_at_taipei"],
        "published_at_taipei_iso": ts["published_at_taipei_iso"],
        "first_seen_at_utc": seen_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_seen_at_taipei": seen_tpe.strftime("%Y-%m-%d %H:%M"),
        "month": ts["month"],
        "title": title.strip(),
        "url": url.strip(),
        "canonical_url": canon,
        "source": source,
        "source_id": source_id,
        "category": category,
        "markets": markets,
        "symbols": symbols,
        "importance": int(importance),
        "importance_stars": "★" * int(importance),
        "importance_reason": importance_reason,
        "is_data_alert": bool(is_data_alert),
        "metrics": metrics,
        "followup_of": None,
        **sent,
    }


def _parse_rss_or_atom(xml_bytes: bytes) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom XML bytes into list of raw entry dicts."""
    # Strip UTF-8 BOM if present
    cleaned = xml_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")
    root = ET.fromstring(cleaned)
    entries: list[dict[str, str]] = []

    # RSS 2.0
    rss_items = root.findall(".//item")
    if rss_items:
        for item in rss_items:
            title = _clean_html_text(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            if not link:
                guid = item.findtext("guid") or ""
                if guid.startswith("http"):
                    link = guid.strip()
            pub = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
            desc = _clean_html_text(item.findtext("description") or "")
            if title and link:
                entries.append({"title": title, "url": link, "published": pub, "summary": desc})
        return entries

    # Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    atom_entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")
    for ent in atom_entries:
        title = _clean_html_text(ent.findtext("atom:title", namespaces=ns) or ent.findtext("title") or "")
        link = ""
        for link_el in ent.findall("atom:link", ns) + ent.findall("link"):
            rel = link_el.attrib.get("rel", "alternate")
            href = link_el.attrib.get("href", "").strip()
            if href and rel == "alternate":
                link = href
                break
            if href and not link:
                link = href
        pub = (
            ent.findtext("atom:published", namespaces=ns)
            or ent.findtext("atom:updated", namespaces=ns)
            or ent.findtext("published")
            or ent.findtext("updated")
            or ""
        )
        desc = _clean_html_text(
            ent.findtext("atom:summary", namespaces=ns)
            or ent.findtext("atom:content", namespaces=ns)
            or ent.findtext("summary")
            or ""
        )
        if title and link:
            entries.append({"title": title, "url": link, "published": pub, "summary": desc})
    return entries


def _fetch_rss_source(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    symbol_aliases: dict[str, list[str]],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    raw_entries = _parse_rss_or_atom(resp.content)
    max_items = int((news_cfg.get("schedule") or {}).get("max_items_per_feed", 25))
    max_age_h = int((news_cfg.get("schedule") or {}).get("max_item_age_hours", 72))
    cutoff = now_utc - timedelta(hours=max_age_h)

    kept: list[dict[str, Any]] = []
    for ent in raw_entries[:max_items]:
        dt = _parse_datetime(ent.get("published")) or now_utc
        if dt < cutoff or dt > now_utc + timedelta(hours=2):
            continue
        title = ent["title"]
        summary = ent.get("summary", "")
        syms = match_symbols(title, summary, symbol_aliases)
        if not is_trading_relevant(title, summary, syms, news_cfg, filter_trading=bool(src.get("filter_trading", True))):
            continue
        cat = classify_category(title, summary, str(src.get("default_category", "總經")), src["id"], syms)
        imp, _, reason = score_importance(
            title, cat, src["id"], str(src.get("tier", "media")), syms, news_cfg
        )
        kept.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url=ent["url"],
                source=src["name"],
                source_id=src["id"],
                category=cat,
                symbols=syms,
                importance=imp,
                importance_reason=reason,
                now_utc=now_utc,
                summary=summary,
                news_cfg=news_cfg,
            )
        )
    return kept, len(raw_entries)


def _fetch_cnyes_source(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    symbol_aliases: dict[str, list[str]],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    payload = resp.json()
    rows = ((payload.get("items") or {}).get("data")) or []
    max_items = int((news_cfg.get("schedule") or {}).get("max_items_per_feed", 25))
    max_age_h = int((news_cfg.get("schedule") or {}).get("max_item_age_hours", 72))
    cutoff = now_utc - timedelta(hours=max_age_h)

    kept: list[dict[str, Any]] = []
    for row in rows[:max_items]:
        title = _clean_html_text(row.get("title") or "")
        nid = row.get("newsId")
        if not title or not nid:
            continue
        dt = _parse_datetime(row.get("publishAt")) or now_utc
        if dt < cutoff:
            continue
        summary = _clean_html_text(row.get("summary") or "")
        url = f"https://news.cnyes.com/news/id/{nid}"
        syms = match_symbols(title, summary, symbol_aliases)
        if not is_trading_relevant(title, summary, syms, news_cfg, filter_trading=bool(src.get("filter_trading", True))):
            continue
        cat = classify_category(title, summary, str(src.get("default_category", "台股")), src["id"], syms)
        imp, _, reason = score_importance(
            title, cat, src["id"], str(src.get("tier", "major")), syms, news_cfg
        )
        kept.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url=url,
                source=src["name"],
                source_id=src["id"],
                category=cat,
                symbols=syms,
                importance=imp,
                importance_reason=reason,
                now_utc=now_utc,
                summary=summary,
                news_cfg=news_cfg,
            )
        )
    return kept, len(rows)


def _fetch_defillama_stablecoins(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    rows = resp.json()
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("DefiLlama stablecoincharts returned < 2 rows")

    prev = rows[-2]
    curr = rows[-1]

    def _sum_usd(entry: dict[str, Any]) -> float:
        circ = entry.get("totalCirculatingUSD") or entry.get("totalCirculating") or {}
        if "peggedUSD" in circ and len(circ) == 1:
            return float(circ["peggedUSD"] or 0.0)
        return sum(float(v or 0.0) for v in circ.values())

    prev_usd = _sum_usd(prev)
    curr_usd = _sum_usd(curr)
    diff_usd = curr_usd - prev_usd
    diff_m = diff_usd / 1e6
    pct = (diff_usd / prev_usd * 100.0) if prev_usd > 0 else 0.0

    th = news_cfg.get("data_thresholds") or {}
    pct_th = float((th.get("stablecoin_1d_change_pct") or {}).get("value", 0.5))
    usd_th_m = float((th.get("stablecoin_1d_change_usd_m") or {}).get("value", 500.0))

    metrics = {
        "total_usd_b": round(curr_usd / 1e9, 2),
        "change_1d_usd_m": round(diff_m, 1),
        "change_1d_pct": round(pct, 2),
        "alert_key": f"stablecoins_supply:{'up' if diff_m >= 0 else 'down'}",
    }

    items: list[dict[str, Any]] = []
    if abs(pct) >= pct_th or abs(diff_m) >= usd_th_m:
        direction = "大增" if diff_m >= 0 else "大減"
        sign = "+" if diff_m >= 0 else ""
        usd_str = f"+${diff_m:,.1f}M" if diff_m >= 0 else f"-${abs(diff_m):,.1f}M"
        sev = 3 if (abs(pct) >= pct_th * 2.5 or abs(diff_m) >= usd_th_m * 3) else 2
        title = (
            f"全網穩定幣總供給單日{direction} {usd_str}"
            f"（{sign}{pct:.2f}%，總市值 ${curr_usd / 1e9:.2f}B）｜DefiLlama"
        )
        dt = _parse_datetime(curr.get("date")) or now_utc
        imp, _, reason = score_importance(
            title, "鏈上資金", src["id"], "onchain", ["BTCUSDT", "ETHUSDT"], news_cfg,
            is_data_alert=True, data_severity=sev,
        )
        items.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url="https://defillama.com/stablecoins",
                source=src["name"],
                source_id=src["id"],
                category="鏈上資金",
                symbols=["BTCUSDT", "ETHUSDT"],
                importance=imp,
                importance_reason=reason,
                is_data_alert=True,
                metrics=metrics,
                now_utc=now_utc,
                news_cfg=news_cfg,
            )
        )
    return items, metrics


def _fetch_defillama_chains(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    chains = resp.json()
    if not isinstance(chains, list):
        raise ValueError("DefiLlama chains response is not a list")

    th_cfg = (news_cfg.get("data_thresholds") or {}).get("chain_tvl_1d_change_pct") or {}
    pct_th = float(th_cfg.get("value", 5.0))
    min_tvl_usd = float(th_cfg.get("min_tvl_usd_m", 1000.0)) * 1e6

    major_chains = sorted(
        [c for c in chains if float(c.get("tvl") or 0.0) >= min_tvl_usd and c.get("name")],
        key=lambda c: float(c.get("tvl") or 0.0),
        reverse=True,
    )[:6]

    chain_sym_map = {
        "Bitcoin": ["BTCUSDT"],
        "Ethereum": ["ETHUSDT"],
        "Solana": ["SOLUSDT"],
        "BSC": ["BNBUSDT"],
        "Arbitrum": ["ARBUSDT", "ETHUSDT"],
        "Base": ["ETHUSDT"],
        "Tron": ["TRXUSDT"],
        "Sui": ["SUIUSDT"],
        "Avalanche": ["AVAXUSDT"],
    }

    hist_tpl = str(src.get("history_url_template") or "https://api.llama.fi/v2/historicalChainTvl/{chain}")
    items: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}

    for ch in major_chains:
        cname = str(ch["name"])
        try:
            h_resp = _http_get_with_retry(
                hist_tpl.format(chain=urllib.parse.quote(cname)),
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            hist = h_resp.json()
            if not isinstance(hist, list) or len(hist) < 2:
                continue
            prev_tvl = float(hist[-2].get("tvl") or 0.0)
            curr_tvl = float(hist[-1].get("tvl") or 0.0)
            if prev_tvl <= 0:
                continue
            pct = (curr_tvl / prev_tvl - 1.0) * 100.0
            snapshot[cname] = {"tvl_b": round(curr_tvl / 1e9, 2), "change_1d_pct": round(pct, 2)}

            if abs(pct) >= pct_th:
                sign = "+" if pct >= 0 else ""
                direction = "大增" if pct >= 0 else "大減"
                sev = 3 if abs(pct) >= pct_th * 2 else 2
                syms = chain_sym_map.get(cname, [])
                title = f"{cname} 鏈上 TVL 24h {direction} {sign}{pct:.2f}%（現為 ${curr_tvl / 1e9:.2f}B）｜DefiLlama"
                dt = _parse_datetime(hist[-1].get("date")) or now_utc
                imp, _, reason = score_importance(
                    title, "鏈上資金", src["id"], "onchain", syms, news_cfg,
                    is_data_alert=True, data_severity=sev,
                )
                items.append(
                    _make_item(
                        issue=issue,
                        dt_utc=dt,
                        title=title,
                        url=f"https://defillama.com/chain/{urllib.parse.quote(cname)}",
                        source=src["name"],
                        source_id=src["id"],
                        category="鏈上資金",
                        symbols=syms,
                        importance=imp,
                        importance_reason=reason,
                        is_data_alert=True,
                        metrics={
                            "chain": cname,
                            "tvl_b": round(curr_tvl / 1e9, 2),
                            "change_1d_pct": round(pct, 2),
                            "alert_key": f"chain_tvl:{cname}:{'up' if pct >= 0 else 'down'}",
                        },
                        now_utc=now_utc,
                        news_cfg=news_cfg,
                    )
                )
        except Exception:
            continue

    return items, snapshot


def _fetch_defillama_hacks(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    symbol_aliases: dict[str, list[str]],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    hacks = resp.json()
    if not isinstance(hacks, list):
        raise ValueError("DefiLlama hacks response is not a list")

    th = (news_cfg.get("data_thresholds") or {}).get("hack_min_amount_usd") or {}
    min_amt = float(th.get("value", 1_000_000))
    lookback_h = int(th.get("lookback_hours", 72))
    cutoff = now_utc - timedelta(hours=lookback_h)

    items: list[dict[str, Any]] = []
    for h in sorted(hacks, key=lambda x: float(x.get("date") or 0), reverse=True)[:30]:
        dt = _parse_datetime(h.get("date"))
        if dt is None or dt < cutoff:
            continue
        amt = float(h.get("amount") or 0.0)
        if amt < min_amt:
            continue
        name = str(h.get("name") or "Unknown Protocol").strip()
        chains = ", ".join(h.get("chain") or []) or "On-chain"
        tech = str(h.get("technique") or h.get("classification") or "Exploit").strip()
        title = f"[Security] {name} ({chains}) {tech}: ${amt / 1e6:.2f}M drained"
        url = str(h.get("source") or f"https://defillama.com/hacks?q={urllib.parse.quote(name)}").strip()
        syms = match_symbols(f"{name} {chains}", tech, symbol_aliases)
        sev = 3 if amt >= 5_000_000 else 2
        imp, _, reason = score_importance(
            title, "安全事件", src["id"], "onchain", syms, news_cfg,
            is_data_alert=True, data_severity=sev,
        )
        items.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url=url,
                source=src["name"],
                source_id=src["id"],
                category="安全事件",
                symbols=syms,
                importance=imp,
                importance_reason=reason,
                is_data_alert=True,
                metrics={"amount_usd": amt, "chains": h.get("chain") or []},
                now_utc=now_utc,
                news_cfg=news_cfg,
            )
        )
    return items, len(hacks)


def _fetch_bitbo_btc_etf(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resp = _http_get_with_retry(
        src["url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    tables = re.findall(r"<table.*?</table>", resp.text, re.S)
    if not tables:
        raise ValueError("Bitbo ETF page has no HTML table")
    rows_raw = re.findall(r"<tr.*?</tr>", tables[0], re.S)
    if len(rows_raw) < 2:
        raise ValueError("Bitbo ETF table has < 2 rows")

    def _cells(row_html: str) -> list[str]:
        return [
            re.sub(r"<[^>]+>", "", html.unescape(c)).strip()
            for c in re.findall(r"<t[dh].*?</t[dh]>", row_html, re.S)
        ]

    header = _cells(rows_raw[0])
    latest_row = _cells(rows_raw[1])
    if len(latest_row) < 2:
        raise ValueError("Bitbo ETF latest row malformed")

    date_str = latest_row[0]
    total_idx = len(latest_row) - 1
    for i, col in enumerate(header):
        if "total" in col.lower():
            total_idx = i
            break
    total_str = latest_row[total_idx].replace(",", "").replace("$", "").strip()
    total_m = float(total_str)

    # Parse date like "Nov 19, 2026"
    dt = now_utc
    try:
        parsed_d = datetime.strptime(date_str, "%b %d, %Y")
        dt = parsed_d.replace(hour=22, minute=0, tzinfo=UTC_TZ)
    except Exception:
        pass

    th = (news_cfg.get("data_thresholds") or {}).get("btc_etf_net_flow_abs_usd_m") or {}
    abs_th = float(th.get("value", 150.0))

    metrics = {
        "date": date_str,
        "net_flow_usd_m": round(total_m, 1),
        "alert_key": f"btc_spot_etf:{'inflow' if total_m >= 0 else 'outflow'}",
    }
    items: list[dict[str, Any]] = []
    # Only alert if recent (within 5 days) and exceeds threshold
    if abs(total_m) >= abs_th and (now_utc - dt).total_seconds() <= 5 * 86400:
        direction = "淨流入" if total_m >= 0 else "淨流出"
        flow_str = f"+${total_m:,.1f}M" if total_m >= 0 else f"-${abs(total_m):,.1f}M"
        sev = 3 if abs(total_m) >= abs_th * 2.5 else 2
        title = f"美國比特幣現貨 ETF 單日{direction} {flow_str}（{date_str}）｜Bitbo"
        imp, _, reason = score_importance(
            title, "鏈上資金", src["id"], "onchain", ["BTCUSDT"], news_cfg,
            is_data_alert=True, data_severity=sev,
        )
        items.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url=src["url"],
                source=src["name"],
                source_id=src["id"],
                category="鏈上資金",
                symbols=["BTCUSDT"],
                importance=imp,
                importance_reason=reason,
                is_data_alert=True,
                metrics=metrics,
                now_utc=now_utc,
                news_cfg=news_cfg,
            )
        )
    return items, metrics


def _fetch_mempool_space(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fees_resp = _http_get_with_retry(
        src["fees_url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    mp_resp = _http_get_with_retry(
        src["mempool_url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    fees = fees_resp.json()
    mp = mp_resp.json()

    fastest = int(fees.get("fastestFee") or 0)
    half_hour = int(fees.get("halfHourFee") or 0)
    vsize_mb = float(mp.get("vsize") or 0.0) / 1e6
    tx_count = int(mp.get("count") or 0)

    th = news_cfg.get("data_thresholds") or {}
    fee_th = float((th.get("mempool_fastest_fee_sat_vb") or {}).get("value", 50))
    vsize_th = float((th.get("mempool_vsize_mb") or {}).get("value", 80.0))

    metrics = {
        "fastest_fee_sat_vb": fastest,
        "half_hour_fee_sat_vb": half_hour,
        "mempool_vsize_mb": round(vsize_mb, 1),
        "tx_count": tx_count,
        "alert_key": "btc_mempool:congested",
    }
    items: list[dict[str, Any]] = []
    if fastest >= fee_th or vsize_mb >= vsize_th:
        sev = 3 if (fastest >= fee_th * 2 or vsize_mb >= vsize_th * 2) else 2
        title = (
            f"比特幣鏈上擁擠度升高：最速手續費 {fastest} sat/vB、"
            f"Mempool 積壓 {vsize_mb:.1f} vMB（{tx_count:,} 筆待確認）｜mempool.space"
        )
        imp, _, reason = score_importance(
            title, "鏈上資金", src["id"], "onchain", ["BTCUSDT"], news_cfg,
            is_data_alert=True, data_severity=sev,
        )
        items.append(
            _make_item(
                issue=issue,
                dt_utc=now_utc,
                title=title,
                url="https://mempool.space/",
                source=src["name"],
                source_id=src["id"],
                category="鏈上資金",
                symbols=["BTCUSDT"],
                importance=imp,
                importance_reason=reason,
                is_data_alert=True,
                metrics=metrics,
                now_utc=now_utc,
                news_cfg=news_cfg,
            )
        )
    return items, metrics


def _top_crypto_symbols(instruments: list[dict[str, Any]], limit: int = 10) -> list[str]:
    priority = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    ]
    enabled_crypto = {str(i["symbol"]) for i in instruments if i.get("market") == "crypto"}
    ordered = [s for s in priority if s in enabled_crypto]
    for s in sorted(enabled_crypto):
        if s not in ordered:
            ordered.append(s)
    return ordered[:limit]


def _evaluate_derivative_alerts(
    exchange_name: str,
    source_id: str,
    source_url: str,
    records: list[dict[str, Any]],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn exchange funding/OI records into alert items when thresholds are crossed."""
    th = news_cfg.get("data_thresholds") or {}
    fr_high = float((th.get("funding_rate_high_pct") or {}).get("value", 0.05))
    fr_low = float((th.get("funding_rate_low_pct") or {}).get("value", -0.03))
    oi_th = float((th.get("open_interest_24h_change_pct") or {}).get("value", 10.0))

    items: list[dict[str, Any]] = []
    for rec in records:
        sym = rec["symbol"]
        fr_pct = rec.get("funding_rate_pct")
        oi_pct = rec.get("oi_24h_pct")
        oi_usd = rec.get("oi_usd")

        if fr_pct is not None and (fr_pct >= fr_high or fr_pct <= fr_low):
            sign = "+" if fr_pct >= 0 else ""
            bias = "多頭極端擁擠" if fr_pct >= fr_high else "空頭費率極端偏低"
            sev = 3 if (fr_pct >= fr_high * 1.6 or fr_pct <= fr_low * 2.0) else 2
            title = f"{sym} 永續資金費率極端（{bias}）：{sign}{fr_pct:.4f}% / 8h｜{exchange_name}"
            imp, _, reason = score_importance(
                title, "衍生品", source_id, "exchange", [sym], news_cfg,
                is_data_alert=True, data_severity=sev,
            )
            items.append(
                _make_item(
                    issue=issue,
                    dt_utc=now_utc,
                    title=title,
                    url=source_url,
                    source=exchange_name,
                    source_id=source_id,
                    category="衍生品",
                    symbols=[sym],
                    importance=imp,
                    importance_reason=reason,
                    is_data_alert=True,
                    metrics={
                        "symbol": sym,
                        "exchange": exchange_name,
                        "funding_rate_pct": round(fr_pct, 4),
                        "alert_key": f"funding_rate:{sym}:{'high' if fr_pct >= fr_high else 'low'}",
                    },
                    now_utc=now_utc,
                    news_cfg=news_cfg,
                )
            )

        if oi_pct is not None and abs(oi_pct) >= oi_th:
            sign = "+" if oi_pct >= 0 else ""
            direction = "暴增" if oi_pct >= 0 else "劇減"
            sev = 3 if abs(oi_pct) >= oi_th * 2.0 else 2
            oi_str = f"（現為 ${oi_usd / 1e9:.2f}B）" if oi_usd and oi_usd >= 1e8 else ""
            title = f"{sym} 24h 未平倉量{direction} {sign}{oi_pct:.2f}%{oi_str}｜{exchange_name}"
            imp, _, reason = score_importance(
                title, "衍生品", source_id, "exchange", [sym], news_cfg,
                is_data_alert=True, data_severity=sev,
            )
            items.append(
                _make_item(
                    issue=issue,
                    dt_utc=now_utc,
                    title=title,
                    url=source_url,
                    source=exchange_name,
                    source_id=source_id,
                    category="衍生品",
                    symbols=[sym],
                    importance=imp,
                    importance_reason=reason,
                    is_data_alert=True,
                    metrics={
                        "symbol": sym,
                        "exchange": exchange_name,
                        "oi_24h_pct": round(oi_pct, 2),
                        "oi_usd": round(oi_usd, 0) if oi_usd is not None else None,
                        "alert_key": f"open_interest:{sym}:{'up' if oi_pct >= 0 else 'down'}",
                    },
                    now_utc=now_utc,
                    news_cfg=news_cfg,
                )
            )
    return items


def _fetch_binance_derivatives(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    target_symbols: list[str],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prem_resp = _http_get_with_retry(
        src["funding_url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    prem_rows = prem_resp.json()
    if not isinstance(prem_rows, list):
        raise ValueError("Binance premiumIndex response invalid")

    fr_by_sym = {
        str(r.get("symbol")): float(r.get("lastFundingRate") or 0.0) * 100.0
        for r in prem_rows
        if r.get("symbol") in target_symbols
    }

    records: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    oi_base = str(src["oi_hist_url"])
    for sym in target_symbols[:6]:
        fr_pct = fr_by_sym.get(sym)
        oi_pct = None
        oi_usd = None
        try:
            oi_resp = _http_get_with_retry(
                f"{oi_base}?symbol={sym}&period=1d&limit=2",
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            hist = oi_resp.json()
            if isinstance(hist, list) and len(hist) >= 2:
                prev_v = float(hist[-2].get("sumOpenInterestValue") or 0.0)
                curr_v = float(hist[-1].get("sumOpenInterestValue") or 0.0)
                if prev_v > 0:
                    oi_pct = (curr_v / prev_v - 1.0) * 100.0
                    oi_usd = curr_v
        except Exception:
            pass
        records.append({"symbol": sym, "funding_rate_pct": fr_pct, "oi_24h_pct": oi_pct, "oi_usd": oi_usd})
        if sym in ("BTCUSDT", "ETHUSDT"):
            snapshot[sym] = {
                "funding_rate_pct": round(fr_pct, 4) if fr_pct is not None else None,
                "oi_24h_pct": round(oi_pct, 2) if oi_pct is not None else None,
            }

    items = _evaluate_derivative_alerts(
        "Binance",
        src["id"],
        "https://www.binance.com/en/futures/funding-history/perpetual/funding-fee-history",
        records,
        issue=issue,
        now_utc=now_utc,
        news_cfg=news_cfg,
    )
    return items, snapshot


def _fetch_bybit_derivatives(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    target_symbols: list[str],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tick_resp = _http_get_with_retry(
        src["tickers_url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    tick_list = ((tick_resp.json().get("result") or {}).get("list")) or []
    by_sym = {str(r.get("symbol")): r for r in tick_list if r.get("symbol") in target_symbols}

    records: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    oi_base = str(src["oi_url"])
    for sym in target_symbols[:6]:
        tk = by_sym.get(sym) or {}
        fr_raw = tk.get("fundingRate")
        fr_pct = float(fr_raw) * 100.0 if fr_raw not in (None, "") else None
        oi_usd = float(tk.get("openInterestValue")) if tk.get("openInterestValue") not in (None, "") else None
        oi_pct = None
        try:
            oi_resp = _http_get_with_retry(
                f"{oi_base}?category=linear&symbol={sym}&intervalTime=1d&limit=2",
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            hist = ((oi_resp.json().get("result") or {}).get("list")) or []
            if len(hist) >= 2:
                # Bybit returns newest first
                curr_oi = float(hist[0].get("openInterest") or 0.0)
                prev_oi = float(hist[1].get("openInterest") or 0.0)
                if prev_oi > 0:
                    oi_pct = (curr_oi / prev_oi - 1.0) * 100.0
        except Exception:
            pass
        records.append({"symbol": sym, "funding_rate_pct": fr_pct, "oi_24h_pct": oi_pct, "oi_usd": oi_usd})
        if sym in ("BTCUSDT", "ETHUSDT"):
            snapshot[sym] = {
                "funding_rate_pct": round(fr_pct, 4) if fr_pct is not None else None,
                "oi_24h_pct": round(oi_pct, 2) if oi_pct is not None else None,
            }

    items = _evaluate_derivative_alerts(
        "Bybit",
        src["id"],
        "https://www.bybit.com/en/announcement-info/fund-rate",
        records,
        issue=issue,
        now_utc=now_utc,
        news_cfg=news_cfg,
    )
    return items, snapshot


def _fetch_okx_derivatives(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    target_symbols: list[str],
    prev_oi_state: dict[str, Any],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    oi_resp = _http_get_with_retry(
        src["oi_url"],
        http_get=http_get,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        headers=headers,
    )
    oi_rows = oi_resp.json().get("data") or []
    oi_by_inst = {str(r.get("instId")): r for r in oi_rows}

    records: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    new_oi_state: dict[str, Any] = dict(prev_oi_state)
    fr_base = str(src["funding_url"])

    for sym in target_symbols[:6]:
        inst_id = f"{sym[:-4]}-USDT-SWAP"
        fr_pct = None
        try:
            fr_resp = _http_get_with_retry(
                f"{fr_base}?instId={inst_id}",
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            fr_data = fr_resp.json().get("data") or []
            if fr_data:
                fr_pct = float(fr_data[0].get("fundingRate") or 0.0) * 100.0
        except Exception:
            pass

        oi_row = oi_by_inst.get(inst_id) or {}
        oi_usd = float(oi_row.get("oiUsd")) if oi_row.get("oiUsd") not in (None, "") else None
        oi_pct = None
        if oi_usd is not None:
            prev_rec = prev_oi_state.get(inst_id)
            hist_entries: list[dict[str, Any]] = []
            if isinstance(prev_rec, dict):
                raw_hist = prev_rec.get("history")
                if isinstance(raw_hist, list):
                    for h in raw_hist:
                        if isinstance(h, dict) and float(h.get("oi_usd") or 0.0) > 0:
                            hist_entries.append({"oi_usd": float(h["oi_usd"]), "ts_utc": str(h.get("ts_utc") or "")})
                elif float(prev_rec.get("oi_usd") or 0.0) > 0 and prev_rec.get("ts_utc"):
                    hist_entries.append({"oi_usd": float(prev_rec["oi_usd"]), "ts_utc": str(prev_rec["ts_utc"])})

            # Pick reference snapshot >= 6h old (closest to 24h) for 15-minute run compatibility
            eligible_refs: list[tuple[float, float]] = []
            kept_hist: list[dict[str, Any]] = []
            for h in hist_entries:
                h_dt = _parse_datetime(h.get("ts_utc"))
                if h_dt is None:
                    continue
                age_sec = (now_utc - h_dt).total_seconds()
                if age_sec <= 36 * 3600:
                    kept_hist.append(h)
                if 6 * 3600 <= age_sec <= 36 * 3600:
                    eligible_refs.append((abs(age_sec - 24 * 3600), float(h["oi_usd"])))

            if eligible_refs:
                _, ref_oi = min(eligible_refs, key=lambda pair: pair[0])
                if ref_oi > 0:
                    oi_pct = (oi_usd / ref_oi - 1.0) * 100.0

            now_iso_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            last_dt = _parse_datetime(kept_hist[-1]["ts_utc"]) if kept_hist else None
            if last_dt is None or (now_utc - last_dt).total_seconds() >= 600:
                kept_hist.append({"oi_usd": oi_usd, "ts_utc": now_iso_str})
            new_oi_state[inst_id] = {
                "oi_usd": oi_usd,
                "ts_utc": now_iso_str,
                "history": kept_hist[-160:],
            }

        records.append({"symbol": sym, "funding_rate_pct": fr_pct, "oi_24h_pct": oi_pct, "oi_usd": oi_usd})
        if sym in ("BTCUSDT", "ETHUSDT"):
            snapshot[sym] = {
                "funding_rate_pct": round(fr_pct, 4) if fr_pct is not None else None,
                "oi_usd_b": round(oi_usd / 1e9, 2) if oi_usd is not None else None,
            }

    items = _evaluate_derivative_alerts(
        "OKX",
        src["id"],
        "https://www.okx.com/trade-swap/funding-rate",
        records,
        issue=issue,
        now_utc=now_utc,
        news_cfg=news_cfg,
    )
    return items, snapshot, new_oi_state


def _fetch_exchange_announcements(
    src: dict[str, Any],
    *,
    issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
    symbol_aliases: dict[str, list[str]],
    http_get: Callable[..., requests.Response] | None,
    timeout_sec: int,
    max_retries: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    kind = src["kind"]
    max_age_h = int((news_cfg.get("schedule") or {}).get("max_item_age_hours", 72))
    cutoff = now_utc - timedelta(hours=max_age_h)
    raw_items: list[dict[str, Any]] = []

    if kind == "binance_announcements":
        for label, url_key in (("上幣", "listing_url"), ("下架", "delisting_url")):
            resp = _http_get_with_retry(
                src[url_key],
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            cats = ((resp.json().get("data") or {}).get("catalogs")) or []
            for cat in cats:
                for art in cat.get("articles") or []:
                    code = str(art.get("code") or "")
                    title = _clean_html_text(art.get("title") or "")
                    if title and code:
                        raw_items.append({
                            "title": f"[{label}] {title}",
                            "url": f"https://www.binance.com/en/support/announcement/{code}",
                            "dt": _parse_datetime(art.get("releaseDate")) or now_utc,
                            "is_delist": label == "下架",
                        })
    elif kind == "okx_announcements":
        for label, url_key in (("上幣", "listing_url"), ("下架", "delisting_url")):
            resp = _http_get_with_retry(
                src[url_key],
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            data = resp.json().get("data") or []
            for grp in data:
                for det in grp.get("details") or []:
                    title = _clean_html_text(det.get("title") or "")
                    url = str(det.get("url") or "").strip()
                    if title and url:
                        raw_items.append({
                            "title": f"[{label}] {title}",
                            "url": url,
                            "dt": _parse_datetime(det.get("pTime")) or now_utc,
                            "is_delist": label == "下架",
                        })
    elif kind == "bybit_announcements":
        for label, url_key in (("上幣", "listing_url"), ("下架", "delisting_url")):
            resp = _http_get_with_retry(
                src[url_key],
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
            rows = ((resp.json().get("result") or {}).get("list")) or []
            for row in rows:
                title = _clean_html_text(row.get("title") or "")
                url = str(row.get("url") or "").strip()
                if title and url:
                    raw_items.append({
                        "title": f"[{label}] {title}",
                        "url": url,
                        "dt": _parse_datetime(row.get("dateTimestamp") or row.get("publishTime")) or now_utc,
                        "is_delist": label == "下架",
                    })

    kept: list[dict[str, Any]] = []
    for raw in raw_items:
        dt = raw["dt"]
        if dt < cutoff:
            continue
        title = raw["title"]
        syms = match_symbols(title, "", symbol_aliases)
        sev_override = 3 if raw["is_delist"] else None
        imp, _, reason = score_importance(
            title, "交易所公告", src["id"], "exchange", syms, news_cfg,
            is_data_alert=bool(sev_override), data_severity=sev_override,
        )
        kept.append(
            _make_item(
                issue=issue,
                dt_utc=dt,
                title=title,
                url=raw["url"],
                source=src["name"],
                source_id=src["id"],
                category="交易所公告",
                symbols=syms,
                importance=imp,
                importance_reason=reason,
                now_utc=now_utc,
                news_cfg=news_cfg,
            )
        )
    return kept, len(raw_items)


def _load_existing_archive(news_dir: Path) -> list[dict[str, Any]]:
    """Read all existing monthly `news/YYYY-MM.jsonl` files without modifying them."""
    items: list[dict[str, Any]] = []
    if not news_dir.exists():
        return items
    for p in sorted(news_dir.glob("*.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if isinstance(obj, dict) and obj.get("id"):
                    items.append(obj)
        except Exception:
            continue
    return items


def _get_data_alert_key(item: dict[str, Any]) -> str | None:
    """Return canonical `(indicator, direction)` key for data alerts to prevent same-issue repeats."""
    metrics = item.get("metrics")
    if isinstance(metrics, dict) and metrics.get("alert_key"):
        return str(metrics["alert_key"])
    sid = str(item.get("source_id") or "")
    title = str(item.get("title") or "")
    if sid == "defillama_stablecoins":
        return f"stablecoins_supply:{'up' if '大增' in title else 'down'}"
    if sid == "defillama_chains":
        chain = str(metrics.get("chain")) if isinstance(metrics, dict) and metrics.get("chain") else title.split(" ")[0]
        return f"chain_tvl:{chain}:{'up' if '大增' in title else 'down'}"
    if sid == "bitbo_btc_etf":
        return f"btc_spot_etf:{'inflow' if '淨流入' in title else 'outflow'}"
    if sid == "mempool_space":
        return "btc_mempool:congested"
    if item.get("category") == "衍生品":
        sym = str(metrics.get("symbol")) if isinstance(metrics, dict) and metrics.get("symbol") else (item.get("symbols") or [""])[0]
        if "資金費率" in title:
            return f"funding_rate:{sym}:{'high' if '多頭' in title else 'low'}"
        return f"open_interest:{sym}:{'up' if '暴增' in title else 'down'}"
    return None


def deduplicate_and_link_items(
    candidates: list[dict[str, Any]],
    existing_items: list[dict[str, Any]],
    *,
    current_issue: str,
    now_utc: datetime,
    news_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deduplicate by URL/title similarity (current issue + recent 7d) and link follow-up developments."""
    dedup_cfg = news_cfg.get("dedup") or {}
    dup_th = float(dedup_cfg.get("title_similarity_threshold", 0.78))
    followup_min = float(dedup_cfg.get("followup_similarity_min", 0.46))
    lookback_days = int(dedup_cfg.get("followup_lookback_days", 7))
    sim_lookback_days = max(7, lookback_days)
    lookback_cutoff = now_utc - timedelta(days=lookback_days)
    sim_cutoff = now_utc - timedelta(days=sim_lookback_days)

    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    seen_alert_keys_in_issue: set[str] = set()
    history_pool: list[dict[str, Any]] = []
    data_alert_titles: set[str] = set()

    for ex in existing_items:
        if ex.get("id"):
            seen_ids.add(str(ex["id"]))
        if ex.get("is_data_alert"):
            ex_title = str(ex.get("title") or "").strip()
            if ex_title:
                data_alert_titles.add(ex_title)
            if ex.get("issue") == current_issue and ex.get("source_id") != "defillama_hacks":
                ak = _get_data_alert_key(ex)
                if ak:
                    seen_alert_keys_in_issue.add(ak)
            continue

        # Regular news: block duplicate URLs across all archive
        cu = canonicalize_url(str(ex.get("canonical_url") or ex.get("url") or ""))
        if cu:
            seen_urls.add(cu)

        ex_dt = _parse_datetime(ex.get("first_seen_at_utc") or ex.get("published_at_utc"))
        if ex.get("issue") == current_issue or ex_dt is None or ex_dt >= sim_cutoff:
            history_pool.append(ex)

    # Sort candidates: higher importance & newer first
    ordered_cands = sorted(
        candidates,
        key=lambda x: (int(x.get("importance", 1)), str(x.get("published_at_utc", ""))),
        reverse=True,
    )

    accepted: list[dict[str, Any]] = []
    for cand in ordered_cands:
        cid = str(cand.get("id") or "")
        if cid and cid in seen_ids:
            continue

        if cand.get("is_data_alert"):
            cand_title = str(cand.get("title") or "").strip()
            if cand_title in data_alert_titles:
                continue
            if cand.get("source_id") != "defillama_hacks":
                ak = _get_data_alert_key(cand)
                if ak and ak in seen_alert_keys_in_issue:
                    continue
                if ak:
                    seen_alert_keys_in_issue.add(ak)
            if cand_title:
                data_alert_titles.add(cand_title)
            if cid:
                seen_ids.add(cid)
            accepted.append(dict(cand))
            continue

        cu = canonicalize_url(str(cand.get("canonical_url") or cand.get("url") or ""))
        if cu and cu in seen_urls:
            continue

        # Check title similarity against current issue + recent 7d archive + already accepted
        is_dup = False
        best_followup: tuple[float, dict[str, Any]] | None = None
        cand_title = str(cand.get("title") or "")
        cand_syms = set(cand.get("symbols") or [])

        for prev in history_pool + accepted:
            if prev.get("is_data_alert"):
                continue
            prev_title = str(prev.get("title") or "")
            sim = title_similarity(cand_title, prev_title)
            if sim >= dup_th:
                is_dup = True
                break
            # Follow-up candidate: strictly from an earlier issue within lookback window
            if prev.get("issue") and prev.get("issue") != current_issue and sim >= followup_min:
                prev_dt = _parse_datetime(prev.get("published_at_utc"))
                if prev_dt is not None and prev_dt >= lookback_cutoff:
                    prev_syms = set(prev.get("symbols") or [])
                    # Require shared symbol when both items have symbols
                    if not (cand_syms and prev_syms and not (cand_syms & prev_syms)):
                        if best_followup is None or sim > best_followup[0]:
                            best_followup = (sim, prev)

        if is_dup:
            continue

        new_item = dict(cand)
        if best_followup is not None:
            old = best_followup[1]
            new_item["followup_of"] = {
                "id": old.get("id"),
                "issue": old.get("issue"),
                "title": old.get("title"),
                "published_at_taipei": old.get("published_at_taipei"),
                "url": old.get("url"),
            }

        if cu:
            seen_urls.add(cu)
        if cid:
            seen_ids.add(cid)
        accepted.append(new_item)

    return accepted


def _resolve_reaction_threshold(
    target: str,
    instruments: list[dict[str, Any]],
    news_cfg: dict[str, Any],
) -> float:
    """Resolve 60-minute significance threshold (%) for a given target symbol."""
    rt = news_cfg.get("reaction_thresholds") or {}
    inst_map = {str(i.get("symbol")): i for i in instruments if isinstance(i, dict) and i.get("symbol")}
    inst = inst_map.get(target) or {}
    market = str(inst.get("market") or "")

    if target in ("TWII", "HSI"):
        key = "tw_index"
    elif target in ("NAS100", "SP500", "JP225") or market == "index":
        key = "us_index"
    elif target.endswith("USDT") or market == "crypto":
        key = "crypto"
    elif target in ("XAUUSD", "XAGUSD", "WTI", "NATGAS", "COPPER") or market == "commodity":
        key = "commodity"
    else:
        key = "stock"

    defaults = {"crypto": 0.5, "us_index": 0.3, "tw_index": 0.3, "stock": 1.0, "commodity": 0.4}
    return float((rt.get(key) or {}).get("threshold_60m_pct", defaults[key]))


def _resolve_okx_inst_id(symbol: str, instruments: list[dict[str, Any]]) -> str | None:
    for inst in instruments:
        if inst.get("symbol") == symbol:
            okx_code = (inst.get("sources") or {}).get("okx")
            if okx_code:
                return str(okx_code)
    if symbol.endswith("USDT") and len(symbol) > 4:
        return f"{symbol[:-4]}-USDT"
    return None


def _resolve_yfinance_ticker(symbol: str, instruments: list[dict[str, Any]]) -> str | None:
    for inst in instruments:
        if inst.get("symbol") == symbol:
            yf_code = (inst.get("sources") or {}).get("yfinance")
            if yf_code:
                return str(yf_code)
    builtin = {
        "NAS100": "^NDX",
        "SP500": "^GSPC",
        "JP225": "^N225",
        "TWII": "^TWII",
        "HSI": "^HSI",
        "XAUUSD": "GC=F",
        "XAGUSD": "SI=F",
        "WTI": "CL=F",
        "NATGAS": "NG=F",
        "COPPER": "HG=F",
    }
    if symbol in builtin:
        return builtin[symbol]
    if re.fullmatch(r"\d{4}", symbol):
        return f"{symbol}.TW"
    if re.fullmatch(r"[A-Z]{1,6}", symbol) and not symbol.endswith("USDT"):
        return symbol
    return None


def _fetch_okx_15m_candles(
    inst_id: str,
    *,
    now_utc: datetime,
    http_get: Callable[..., requests.Response] | None = None,
    timeout_sec: int = 8,
    max_retries: int = 1,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch closed 15m candles from OKX public history-candles API (up to ~50h)."""
    base_url = "https://www.okx.com/api/v5/market/history-candles"
    cutoff_ms = int((now_utc - timedelta(hours=50)).timestamp() * 1000)
    now_sec = now_utc.timestamp()
    by_ts: dict[datetime, dict[str, Any]] = {}
    after_ms: int | None = None

    for _ in range(3):
        url = f"{base_url}?instId={urllib.parse.quote(inst_id)}&bar=15m&limit=100"
        if after_ms is not None:
            url += f"&after={after_ms}"
        resp = _http_get_with_retry(
            url,
            http_get=http_get,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            headers=headers,
        )
        payload = resp.json()
        if str(payload.get("code")) != "0":
            break
        rows = payload.get("data") or []
        if not rows:
            break
        min_ms: int | None = None
        for row in rows:
            open_ms = int(row[0])
            if min_ms is None or open_ms < min_ms:
                min_ms = open_ms
            confirm = str(row[8]) if len(row) > 8 else "1"
            if confirm != "1" and (open_ms / 1000.0 + 900.0) > now_sec:
                continue
            dt = datetime.fromtimestamp(open_ms / 1000.0, tz=UTC_TZ)
            by_ts[dt] = {
                "ts_utc": dt,
                "open": float(row[1]),
                "close": float(row[4]),
            }
        if min_ms is None or min_ms <= cutoff_ms or len(rows) < 100:
            break
        after_ms = min_ms

    return [by_ts[k] for k in sorted(by_ts.keys())]


def _fetch_yfinance_15m_candles(
    yf_ticker: str,
    *,
    now_utc: datetime,
    timeout_sec: int = 8,
) -> list[dict[str, Any]]:
    """Fetch closed 15m candles from yfinance (no API key)."""
    import pandas as pd
    import yfinance as yf

    df = yf.download(
        tickers=yf_ticker,
        period="5d",
        interval="15m",
        progress=False,
        auto_adjust=False,
        threads=False,
        timeout=min(max(1, int(timeout_sec)), MAX_TIMEOUT_CAP_SEC),
    )
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    df = df.copy()
    df.index = idx

    now_sec = now_utc.timestamp()
    bars: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        open_dt = ts.to_pydatetime().astimezone(UTC_TZ)
        if open_dt.timestamp() + 900.0 > now_sec:
            continue
        o_val = row.get("Open")
        c_val = row.get("Close")
        if pd.isna(c_val):
            continue
        open_f = float(c_val) if pd.isna(o_val) else float(o_val)
        bars.append({"ts_utc": open_dt, "open": open_f, "close": float(c_val)})
    bars.sort(key=lambda b: b["ts_utc"])
    return bars


def fetch_15m_candles_for_symbol(
    symbol: str,
    *,
    now_utc: datetime,
    instruments: list[dict[str, Any]],
    http_get: Callable[..., requests.Response] | None = None,
    timeout_sec: int = 8,
    max_retries: int = 1,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch 15m OHLC candles for a tracked symbol via OKX (crypto) or yfinance (TradFi)."""
    inst_id = _resolve_okx_inst_id(symbol, instruments)
    is_crypto = symbol.endswith("USDT") or any(
        i.get("symbol") == symbol and i.get("market") == "crypto" for i in instruments
    )
    try:
        if is_crypto and inst_id:
            return _fetch_okx_15m_candles(
                inst_id,
                now_utc=now_utc,
                http_get=http_get,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                headers=headers,
            )
        yf_ticker = _resolve_yfinance_ticker(symbol, instruments)
        if yf_ticker:
            return _fetch_yfinance_15m_candles(yf_ticker, now_utc=now_utc, timeout_sec=timeout_sec)
    except Exception:
        return []
    return []


def compute_single_news_reaction(
    item: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    target: str,
    now_utc: datetime,
    instruments: list[dict[str, Any]],
    news_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Compute +15m / +60m market price reaction for a single news item."""
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _unavailable(closed_flag: bool | None = None) -> dict[str, Any]:
        return {
            "reaction_status": "unavailable",
            "reaction_label": None,
            "reaction_direction": None,
            "ret_15m_pct": None,
            "ret_60m_pct": None,
            "target": target,
            "base_time_taipei": None,
            "market_closed_at_event": closed_flag,
            "agree_with_rule": None,
            "computed_at_utc": now_iso,
        }

    if not bars:
        return _unavailable(None)

    norm_bars: list[dict[str, Any]] = []
    for b in bars:
        bt = b.get("ts_utc")
        dt = bt.astimezone(UTC_TZ) if isinstance(bt, datetime) else _parse_datetime(bt)
        if dt is None:
            continue
        close_px = float(b.get("close") or 0.0)
        if close_px <= 0:
            continue
        norm_bars.append({"ts_utc": dt, "close": close_px})
    norm_bars.sort(key=lambda x: x["ts_utc"])
    if not norm_bars:
        return _unavailable(None)

    t_pub = _parse_datetime(item.get("published_at_utc"))
    t_seen = _parse_datetime(item.get("first_seen_at_utc"))
    if t_pub and t_seen:
        t_event = max(t_pub, t_seen)
    else:
        t_event = t_seen or t_pub
    if t_event is None:
        return _unavailable(None)

    bar_dur = timedelta(minutes=15)
    base_bar: dict[str, Any] | None = None
    market_closed = False

    # First check if an active 15m bar contains t_event
    for b in norm_bars:
        if b["ts_utc"] <= t_event < b["ts_utc"] + bar_dur:
            base_bar = b
            market_closed = False
            break

    if base_bar is None:
        next_bar = next((b for b in norm_bars if b["ts_utc"] >= t_event), None)
        if next_bar is None:
            return _unavailable(None)
        base_bar = next_bar
        market_closed = (next_bar["ts_utc"] - t_event) > timedelta(minutes=30)

    base_ts = base_bar["ts_utc"]
    base_px = base_bar["close"]

    b_15 = next(
        (b for b in norm_bars if base_ts + timedelta(minutes=15) <= b["ts_utc"] <= base_ts + timedelta(hours=24)),
        None,
    )
    b_60 = next(
        (b for b in norm_bars if base_ts + timedelta(minutes=60) <= b["ts_utc"] <= base_ts + timedelta(hours=24)),
        None,
    )
    if b_60 is None:
        return _unavailable(market_closed)

    ret_15m = round((b_15["close"] / base_px - 1.0) * 100.0, 2) if b_15 is not None else None
    ret_60m = round((b_60["close"] / base_px - 1.0) * 100.0, 2)

    th = _resolve_reaction_threshold(target, instruments, news_cfg)
    if ret_60m > th:
        r_label = "市場解讀偏多"
        r_dir = "bullish"
    elif ret_60m < -th:
        r_label = "市場解讀偏空"
        r_dir = "bearish"
    else:
        r_label = "市場反應不明顯"
        r_dir = "neutral"

    rule_sent = item.get("sentiment")
    if rule_sent in ("bullish", "bearish") and r_dir in ("bullish", "bearish"):
        agree: bool | None = (rule_sent == r_dir)
    else:
        agree = None

    return {
        "reaction_status": "ok",
        "reaction_label": r_label,
        "reaction_direction": r_dir,
        "ret_15m_pct": ret_15m,
        "ret_60m_pct": ret_60m,
        "target": target,
        "base_time_taipei": base_ts.astimezone(TPE_TZ).strftime("%Y-%m-%d %H:%M"),
        "market_closed_at_event": market_closed,
        "agree_with_rule": agree,
        "computed_at_utc": now_iso,
    }


def compute_news_reactions(
    all_items: list[dict[str, Any]],
    prev_reactions: dict[str, Any],
    *,
    now_utc: datetime,
    instruments: list[dict[str, Any]],
    news_cfg: dict[str, Any],
    http_get: Callable[..., requests.Response] | None = None,
    candle_fetcher: Callable[[str, datetime], list[dict[str, Any]]] | None = None,
    timeout_sec: int = 8,
    max_retries: int = 1,
    headers: dict[str, str] | None = None,
    max_items_per_run: int = MAX_REACTIONS_PER_RUN,
) -> tuple[dict[str, Any], int]:
    """Backfill market reactions for items aged 60m..48h (up to 40 per run, importance-first)."""
    reactions = dict(prev_reactions)
    eligible: list[tuple[int, int, str, dict[str, Any]]] = []

    for it in all_items:
        iid = str(it.get("id") or "")
        if not iid:
            continue
        existing = reactions.get(iid)
        if isinstance(existing, dict) and existing.get("reaction_status") == "ok":
            continue

        ref_dt = _parse_datetime(it.get("first_seen_at_utc") or it.get("published_at_utc"))
        if ref_dt is None:
            continue
        age_sec = (now_utc - ref_dt).total_seconds()
        if age_sec < 3600 or age_sec >= 48 * 3600:
            continue

        # Prioritize items never computed yet over previously unavailable retries
        is_uncomputed = 1 if existing is None else 0
        if isinstance(existing, dict) and existing.get("reaction_status") == "unavailable":
            prev_comp = _parse_datetime(existing.get("computed_at_utc"))
            if prev_comp is not None and (now_utc - prev_comp).total_seconds() < 3600:
                continue

        imp = int(it.get("importance", 1))
        eligible.append((is_uncomputed, imp, ref_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), it))

    eligible.sort(key=lambda tup: (tup[0], tup[1], tup[2]), reverse=True)
    batch = [tup[3] for tup in eligible[:max_items_per_run]]

    candle_cache: dict[str, list[dict[str, Any]]] = {}
    updated_count = 0

    for it in batch:
        iid = str(it["id"])
        targets = list(it.get("sentiment_targets") or [])
        if not targets:
            mkts = it.get("markets") or derive_markets(
                str(it.get("title") or ""), "", str(it.get("category") or "總經"), list(it.get("symbols") or []), news_cfg
            )
            targets = derive_sentiment_targets(list(it.get("symbols") or []), mkts, str(it.get("category") or "總經"), news_cfg)

        chosen_result: dict[str, Any] | None = None
        for tgt in targets:
            if tgt not in candle_cache:
                if candle_fetcher is not None:
                    try:
                        candle_cache[tgt] = candle_fetcher(tgt, now_utc) or []
                    except Exception:
                        candle_cache[tgt] = []
                else:
                    candle_cache[tgt] = fetch_15m_candles_for_symbol(
                        tgt,
                        now_utc=now_utc,
                        instruments=instruments,
                        http_get=http_get,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        headers=headers,
                    )
            res = compute_single_news_reaction(
                it,
                candle_cache[tgt],
                target=tgt,
                now_utc=now_utc,
                instruments=instruments,
                news_cfg=news_cfg,
            )
            if chosen_result is None:
                chosen_result = res
            if res["reaction_status"] == "ok":
                chosen_result = res
                break

        if chosen_result is not None:
            reactions[iid] = chosen_result
            updated_count += 1

    return reactions, updated_count


def generate_news_files(
    out_dir: Path,
    *,
    config_path: Path | None = None,
    news_config_path: Path | None = None,
    issue_override: str | None = None,
    now_utc: datetime | None = None,
    timeout_sec: int | None = None,
    http_get: Callable[..., requests.Response] | None = None,
    candle_fetcher: Callable[[str, datetime], list[dict[str, Any]]] | None = None,
    probe_only: bool = False,
) -> dict[str, Any]:
    """Fetch all enabled news/data sources, deduplicate, append monthly JSONL, and emit index files."""
    run_t0 = time_mod.monotonic()
    now = (now_utc or datetime.now(UTC_TZ)).astimezone(UTC_TZ)
    now_tpe = now.astimezone(TPE_TZ)
    issue = issue_override or compute_issue_id(now)

    news_cfg = load_news_config(news_config_path)
    instruments = load_tracked_instruments(config_path)
    symbol_aliases = build_symbol_alias_map(instruments, news_cfg)
    target_crypto = _top_crypto_symbols(instruments, limit=10)

    http_cfg = news_cfg.get("http") or {}
    eff_timeout = min(int(timeout_sec or http_cfg.get("request_timeout_sec", 8)), MAX_TIMEOUT_CAP_SEC)
    max_retries = min(int(http_cfg.get("max_retries", 1)), MAX_RETRIES_CAP)
    headers = {
        "User-Agent": str(http_cfg.get("user_agent") or "SignalDeskNewsBot/1.0"),
        "Accept": "application/json, application/xml, text/xml, application/rss+xml, */*",
    }

    state_path = out_dir / "news_state.json"
    prev_state: dict[str, Any] = {}
    if state_path.exists():
        try:
            prev_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}
    okx_prev_oi = prev_state.get("okx_oi") if isinstance(prev_state.get("okx_oi"), dict) else {}

    sources_report: dict[str, dict[str, Any]] = {}
    failed_sources: list[dict[str, str]] = []
    candidates: list[dict[str, Any]] = []
    metrics_snapshot: dict[str, Any] = {}
    new_okx_oi = dict(okx_prev_oi)

    for src in news_cfg.get("sources") or []:
        if not src.get("enabled", True):
            continue
        sid = str(src["id"])
        sname = str(src["name"])
        kind = str(src["kind"])
        t0 = time_mod.monotonic()
        try:
            items: list[dict[str, Any]] = []
            raw_count = 0
            if kind == "rss":
                items, raw_count = _fetch_rss_source(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    symbol_aliases=symbol_aliases, http_get=http_get,
                    timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
            elif kind == "cnyes":
                items, raw_count = _fetch_cnyes_source(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    symbol_aliases=symbol_aliases, http_get=http_get,
                    timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
            elif kind == "defillama_stablecoins":
                items, m = _fetch_defillama_stablecoins(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = 1
                metrics_snapshot["stablecoins"] = m
            elif kind == "defillama_chains":
                items, m = _fetch_defillama_chains(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = len(m)
                metrics_snapshot["chains_tvl"] = m
            elif kind == "defillama_hacks":
                items, raw_count = _fetch_defillama_hacks(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    symbol_aliases=symbol_aliases, http_get=http_get,
                    timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
            elif kind == "bitbo_btc_etf":
                items, m = _fetch_bitbo_btc_etf(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = 1
                metrics_snapshot["btc_spot_etf"] = m
            elif kind == "binance_derivatives":
                items, m = _fetch_binance_derivatives(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg, target_symbols=target_crypto,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = len(target_crypto[:6])
                metrics_snapshot["binance_derivatives"] = m
            elif kind == "bybit_derivatives":
                items, m = _fetch_bybit_derivatives(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg, target_symbols=target_crypto,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = len(target_crypto[:6])
                metrics_snapshot["bybit_derivatives"] = m
            elif kind == "okx_derivatives":
                items, m, new_okx_oi = _fetch_okx_derivatives(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    target_symbols=target_crypto, prev_oi_state=okx_prev_oi,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = len(target_crypto[:6])
                metrics_snapshot["okx_derivatives"] = m
            elif kind == "mempool":
                items, m = _fetch_mempool_space(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    http_get=http_get, timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
                raw_count = 1
                metrics_snapshot["mempool"] = m
            elif kind in ("binance_announcements", "okx_announcements", "bybit_announcements"):
                items, raw_count = _fetch_exchange_announcements(
                    src, issue=issue, now_utc=now, news_cfg=news_cfg,
                    symbol_aliases=symbol_aliases, http_get=http_get,
                    timeout_sec=eff_timeout, max_retries=max_retries, headers=headers,
                )
            else:
                raise ValueError(f"Unknown news source kind: {kind}")

            candidates.extend(items)
            sources_report[sid] = {
                "name": sname,
                "kind": kind,
                "status": "ok",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "raw_count": raw_count,
                "items_kept": len(items),
                "error": None,
            }
        except Exception as exc:
            err_msg = str(exc)
            sources_report[sid] = {
                "name": sname,
                "kind": kind,
                "status": "failed",
                "elapsed_sec": round(time_mod.monotonic() - t0, 3),
                "raw_count": 0,
                "items_kept": 0,
                "error": err_msg,
            }
            failed_sources.append({"id": sid, "name": sname, "error": err_msg})

    # Deduplicate same-symbol derivative alerts across exchanges in the same run (keep strongest magnitude)
    non_deriv = [c for c in candidates if not (c.get("is_data_alert") and c.get("category") == "衍生品")]
    deriv = [c for c in candidates if c.get("is_data_alert") and c.get("category") == "衍生品"]
    best_deriv: dict[tuple[str, str], dict[str, Any]] = {}
    for d in deriv:
        m = d.get("metrics") or {}
        sym = str(m.get("symbol") or "")
        metric_kind = "funding" if "funding_rate_pct" in m else "oi"
        mag = abs(float(m.get("funding_rate_pct") if metric_kind == "funding" else m.get("oi_24h_pct") or 0.0))
        key = (sym, metric_kind)
        if key not in best_deriv or mag > best_deriv[key][0]:
            best_deriv[key] = (mag, d)
    candidates = non_deriv + [pair[1] for pair in best_deriv.values()]

    news_dir = out_dir / "news"
    existing_items = _load_existing_archive(news_dir)

    accepted_items = deduplicate_and_link_items(
        candidates,
        existing_items,
        current_issue=issue,
        now_utc=now,
        news_cfg=news_cfg,
    )

    if not probe_only:
        news_dir.mkdir(parents=True, exist_ok=True)
        # Append-only per-month JSONL write: never touch existing lines!
        by_month: dict[str, list[dict[str, Any]]] = {}
        for item in accepted_items:
            m_key = str(item.get("month") or now_tpe.strftime("%Y-%m"))
            by_month.setdefault(m_key, []).append(item)

        for m_key, new_rows in by_month.items():
            # Sort newly appended rows chronologically within the append batch
            new_rows_sorted = sorted(new_rows, key=lambda x: str(x.get("published_at_utc", "")))
            m_path = news_dir / f"{m_key}.jsonl"
            append_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_rows_sorted)
            with m_path.open("a", encoding="utf-8") as f:
                f.write(append_text)

    all_items = existing_items + accepted_items
    # Build latest issue & recent 7-day window
    recent_days = int((news_cfg.get("schedule") or {}).get("recent_days_window", 7))
    recent_cutoff_iso = (now - timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    latest_items = sorted(
        [it for it in all_items if it.get("issue") == issue],
        key=lambda x: (int(x.get("importance", 1)), str(x.get("published_at_utc", ""))),
        reverse=True,
    )
    recent_7d_items = sorted(
        [it for it in all_items if str(it.get("published_at_utc", "")) >= recent_cutoff_iso],
        key=lambda x: str(x.get("published_at_utc", "")),
        reverse=True,
    )

    # Rebuild issues index from existing news_index.json + all_items so no issue metadata is lost
    index_path = out_dir / "news_index.json"
    prev_issue_meta: dict[str, dict[str, Any]] = {}
    if index_path.exists():
        try:
            prev_idx = json.loads(index_path.read_text(encoding="utf-8"))
            for row in prev_idx.get("issues") or []:
                if isinstance(row, dict) and row.get("issue"):
                    prev_issue_meta[str(row["issue"])] = row
        except Exception:
            pass

    items_by_issue: dict[str, list[dict[str, Any]]] = {}
    month_counts: dict[str, int] = {}
    for it in all_items:
        iss = str(it.get("issue") or "")
        if iss:
            items_by_issue.setdefault(iss, []).append(it)
        mk = str(it.get("month") or str(it.get("published_at_taipei", ""))[:7])
        if mk:
            month_counts[mk] = month_counts.get(mk, 0) + 1

    # Ensure current issue appears in issues index even if 0 items matched
    items_by_issue.setdefault(issue, [])

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_tpe_str = now_tpe.strftime("%Y-%m-%d %H:%M")

    issues_list: list[dict[str, Any]] = []
    for iss, iss_items in items_by_issue.items():
        prev_meta = prev_issue_meta.get(iss) or {}
        parts = iss.split("-")
        iss_date = "-".join(parts[:3]) if len(parts) >= 3 else iss[:10]
        iss_month = iss_date[:7]
        iss_half = parts[3] if len(parts) >= 4 else ""
        is_current = iss == issue
        issues_list.append({
            "issue": iss,
            "month": iss_month,
            "date": iss_date,
            "half": iss_half,
            "updated_at_taipei": now_tpe_str if is_current else str(prev_meta.get("updated_at_taipei") or iss_date),
            "count": len(iss_items),
            "star3_count": sum(1 for x in iss_items if int(x.get("importance", 1)) >= 3),
            "failed_sources": failed_sources if is_current else (prev_meta.get("failed_sources") or []),
        })
    issues_list.sort(key=lambda r: (str(r.get("date", "")), 1 if r.get("half") == "PM" else 0), reverse=True)

    months_list = [
        {"month": mk, "file": f"news/{mk}.jsonl", "count": cnt}
        for mk, cnt in sorted(month_counts.items(), reverse=True)
    ]

    reactions_path = out_dir / "news_reactions.json"
    prev_reactions: dict[str, Any] = {}
    if reactions_path.exists():
        try:
            prev_react_doc = json.loads(reactions_path.read_text(encoding="utf-8"))
            if isinstance(prev_react_doc, dict) and isinstance(prev_react_doc.get("reactions"), dict):
                prev_reactions = prev_react_doc["reactions"]
        except Exception:
            prev_reactions = {}

    reactions_map, reactions_computed = compute_news_reactions(
        all_items,
        prev_reactions,
        now_utc=now,
        instruments=instruments,
        news_cfg=news_cfg,
        http_get=http_get,
        candle_fetcher=candle_fetcher,
        timeout_sec=eff_timeout,
        max_retries=max_retries,
        headers=headers,
    )

    ok_sources = sum(1 for r in sources_report.values() if r["status"] == "ok")
    status = "ok" if not failed_sources else ("partial" if ok_sources > 0 else "failed")
    next_hint = compute_next_update_hint(now)

    latest_payload = {
        "version": 1,
        "latest_issue": issue,
        "is_current_issue_updating": True,
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe_str,
        "last_updated_utc": now_iso,
        "last_updated_taipei": now_tpe_str,
        "next_update_hint": next_hint,
        "status": status,
        "failed_sources": failed_sources,
        "fetch_report": {
            "total_elapsed_sec": round(time_mod.monotonic() - run_t0, 3),
            "sources_ok": ok_sources,
            "sources_failed": len(failed_sources),
            "new_items_added": len(accepted_items),
            "reactions_computed": reactions_computed,
            "sources": sources_report,
        },
        "data_metrics_snapshot": metrics_snapshot,
        "latest_items": latest_items,
        "recent_7d_items": recent_7d_items,
        "recent_issues": issues_list[:14],
    }

    index_payload = {
        "version": 1,
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe_str,
        "latest_issue": issue,
        "months": months_list,
        "issues": issues_list,
    }

    reactions_payload = {
        "version": 1,
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe_str,
        "count": len(reactions_map),
        "reactions": reactions_map,
    }

    if not probe_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "news_latest.json").write_text(
            json.dumps(latest_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        index_path.write_text(
            json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reactions_path.write_text(
            json.dumps(reactions_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        state_path.write_text(
            json.dumps(
                {
                    "updated_at_utc": now_iso,
                    "latest_issue": issue,
                    "okx_oi": new_okx_oi,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return latest_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Signal Desk news archive and indices")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--news-config", type=Path, default=None)
    parser.add_argument("--issue", type=str, default=None, help="Optional issue override e.g. 2026-11-21-PM")
    parser.add_argument("--timeout", type=int, default=None, help="Per-request HTTP timeout in seconds (<= 10)")
    parser.add_argument("--probe-only", action="store_true", help="Probe all sources without writing data files")
    args = parser.parse_args()

    res = generate_news_files(
        out_dir=args.out_dir,
        config_path=args.config,
        news_config_path=args.news_config,
        issue_override=args.issue,
        timeout_sec=args.timeout,
        probe_only=args.probe_only,
    )
    fr = res["fetch_report"]
    print(
        f"News issue {res['latest_issue']} ({res['status']}): "
        f"{len(res['latest_items'])} items in issue, "
        f"sources ok={fr['sources_ok']}/failed={fr['sources_failed']}, "
        f"elapsed={fr['total_elapsed_sec']}s."
    )


if __name__ == "__main__":
    main()

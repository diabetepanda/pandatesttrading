"""Tests for news generator (`src.news`), workflows, and frontend `#/news` & signal news."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from src.jobs.data_sync import merge_conflicted_content
from src.news import (
    build_symbol_alias_map,
    canonicalize_url,
    classify_category,
    compute_issue_id,
    compute_news_reactions,
    compute_next_update_hint,
    compute_single_news_reaction,
    deduplicate_and_link_items,
    derive_markets,
    evaluate_sentiment,
    generate_news_files,
    is_trading_relevant,
    load_news_config,
    load_tracked_instruments,
    match_symbols,
    score_importance,
    title_similarity,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", json_data: Any = None) -> None:
        self.status_code = status_code
        if json_data is not None:
            self._text = json.dumps(json_data, ensure_ascii=False)
        else:
            self._text = text
        self.content = self._text.encode("utf-8")

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        return json.loads(self._text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_symbol_matching_and_false_positive_guards() -> None:
    news_cfg = load_news_config()
    instruments = load_tracked_instruments()
    aliases = build_symbol_alias_map(instruments, news_cfg)

    assert "BTCUSDT" in match_symbols("Bitcoin rallies as BTC ETF inflows surge", "", aliases)
    assert "BTCUSDT" in match_symbols("比特幣突破關鍵壓力區", "", aliases)
    assert "2330" in match_symbols("台積電法說會釋出樂觀展望", "", aliases)
    assert "2330" in match_symbols("TSMC (2330) raises capital expenditure guidance", "", aliases)
    assert "XAUUSD" in match_symbols("Spot Gold hits fresh high amid rate cut bets", "", aliases)

    # False-positive guards: Goldman Sachs != XAUUSD; "link to" != LINKUSDT; "top stocks" != OPUSDT
    assert "XAUUSD" not in match_symbols("Goldman Sachs raises S&P 500 year-end target", "", aliases)
    assert "LINKUSDT" not in match_symbols("Here is a link to the latest Federal Reserve report", "", aliases)
    assert "OPUSDT" not in match_symbols("Top tech stocks climb ahead of earnings", "", aliases)


def test_importance_rules_and_trading_filter() -> None:
    news_cfg = load_news_config()
    instruments = load_tracked_instruments()
    aliases = build_symbol_alias_map(instruments, news_cfg)

    # 1. Non-trading fluff filtered out
    assert not is_trading_relevant("林內農會推芋頭零食 3款新品進軍北部", "", [], news_cfg, filter_trading=True)
    assert not is_trading_relevant("Top 5 travel destinations and horoscope for November", "", [], news_cfg, filter_trading=True)

    # 2. Trading news kept & scored properly
    syms_fed = match_symbols("FOMC announces 25 bps rate cut as Powell cites cooling inflation", "", aliases)
    assert is_trading_relevant("FOMC announces 25 bps rate cut", "", syms_fed, news_cfg, filter_trading=True)
    cat_fed = classify_category("FOMC announces 25 bps rate cut", "", "總經", "cnbc_top", syms_fed)
    assert cat_fed == "聯準會"
    imp_fed, stars_fed, _ = score_importance("FOMC announces 25 bps rate cut", cat_fed, "cnbc_top", "major", syms_fed, news_cfg)
    assert imp_fed == 3 and stars_fed == "★★★"

    # Security incident -> ★★★
    imp_sec, stars_sec, _ = score_importance("Protocol drained in $10M exploit", "安全事件", "coindesk", "major", ["ETHUSDT"], news_cfg)
    assert imp_sec == 3 and stars_sec == "★★★"

    # Tracked symbol regular earnings/news -> ★★
    syms_tsla = match_symbols("Tesla quarterly deliveries beat estimates", "", aliases)
    imp_tsla, stars_tsla, _ = score_importance("Tesla quarterly deliveries beat estimates", "美股", "cnbc_top", "major", syms_tsla, news_cfg)
    assert imp_tsla == 2 and stars_tsla == "★★"

    # General trading news without tracked symbol or major keyword -> ★
    imp_gen, stars_gen, _ = score_importance("European bond market sees steady session", "總經", "cointelegraph", "media", [], news_cfg)
    assert imp_gen == 1 and stars_gen == "★"


def test_dedup_followup_linking_and_append_only_monthly_archive(tmp_path: Path) -> None:
    news_cfg = load_news_config()

    # URL canonicalization removes utm_* params
    assert canonicalize_url("https://www.coindesk.com/markets/btc-rally/?utm_source=rss&utm_medium=feed") == \
        canonicalize_url("http://coindesk.com/markets/btc-rally")

    # Title similarity detects cross-outlet duplicates and ongoing updates
    assert title_similarity(
        "Federal Reserve cuts interest rates by 25 basis points - CNBC",
        "Federal Reserve cuts interest rates by 25 basis points | MarketWatch",
    ) >= 0.78

    now_am = datetime(2026, 11, 21, 0, 5, tzinfo=timezone.utc)  # 08:05 TPE -> 2026-11-21-AM
    now_pm = datetime(2026, 11, 21, 12, 5, tzinfo=timezone.utc)  # 20:05 TPE -> 2026-11-21-PM
    assert compute_issue_id(now_am) == "2026-11-21-AM"
    assert compute_issue_id(now_pm) == "2026-11-21-PM"

    # Issue 1 (AM) via mocked http_get
    rss_am = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
      <item>
        <title>Federal Reserve signals pause on rate hikes amid cooling CPI inflation</title>
        <link>https://www.cnbc.com/2026/11/20/fed-signals-pause.html?utm_source=rss</link>
        <pubDate>Fri, 20 Nov 2026 22:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Federal Reserve signals pause on rate hikes amid cooling CPI inflation - MarketWatch</title>
        <link>https://www.marketwatch.com/story/fed-signals-pause-dup</link>
        <pubDate>Fri, 20 Nov 2026 22:10:00 GMT</pubDate>
      </item>
      <item>
        <title>Bitcoin surges past $95,000 as spot ETF inflows accelerate</title>
        <link>https://www.coindesk.com/markets/2026/11/20/btc-etf-inflows/</link>
        <pubDate>Fri, 20 Nov 2026 23:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    def http_am(url: str, headers: Any = None, timeout: int = 8) -> _FakeResponse:
        if "cnbc" in url:
            return _FakeResponse(200, text=rss_am)
        if "coindesk" in url:
            return _FakeResponse(200, text=rss_am)
        raise requests.ConnectionError("Simulated offline source")

    res_am = generate_news_files(tmp_path, now_utc=now_am, http_get=http_am)
    assert res_am["latest_issue"] == "2026-11-21-AM"
    assert res_am["status"] == "partial"
    assert len(res_am["failed_sources"]) > 0
    # Dedup across CNBC & MarketWatch + across cnbc_top & cnbc_economy + coindesk => only 2 unique items!
    assert len(res_am["latest_items"]) == 2

    month_file = tmp_path / "news" / "2026-11.jsonl"
    assert month_file.exists()
    original_lines = month_file.read_text(encoding="utf-8").splitlines()
    assert len(original_lines) == 2

    # Issue 2 (PM): same URL should be skipped; related follow-up event should be appended and linked
    rss_pm = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
      <item>
        <title>Federal Reserve signals pause on rate hikes amid cooling CPI inflation</title>
        <link>https://www.cnbc.com/2026/11/20/fed-signals-pause.html</link>
        <pubDate>Fri, 20 Nov 2026 22:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Bitcoin extends rally above $98,000 as spot ETF inflows hit weekly record</title>
        <link>https://www.coindesk.com/markets/2026/11/21/btc-extends-rally-etf/</link>
        <pubDate>Sat, 21 Nov 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    def http_pm(url: str, headers: Any = None, timeout: int = 8) -> _FakeResponse:
        if "coindesk" in url:
            return _FakeResponse(200, text=rss_pm)
        if "cnbc" in url:
            return _FakeResponse(200, text=rss_pm)
        raise requests.ConnectionError("Simulated offline source")

    res_pm = generate_news_files(tmp_path, now_utc=now_pm, http_get=http_pm)
    assert res_pm["latest_issue"] == "2026-11-21-PM"
    assert len(res_pm["latest_items"]) == 1
    followup_item = res_pm["latest_items"][0]
    assert "Bitcoin extends rally" in followup_item["title"]
    assert followup_item["followup_of"] is not None
    assert followup_item["followup_of"]["issue"] == "2026-11-21-AM"
    assert "Bitcoin surges past" in followup_item["followup_of"]["title"]

    # Append-only verification: original 2 lines at top of 2026-11.jsonl are byte-for-byte intact
    updated_lines = month_file.read_text(encoding="utf-8").splitlines()
    assert len(updated_lines) == 3
    assert updated_lines[:2] == original_lines

    # news_index.json contains both PM and AM issues
    idx_doc = json.loads((tmp_path / "news_index.json").read_text(encoding="utf-8"))
    assert [x["issue"] for x in idx_doc["issues"]] == ["2026-11-21-PM", "2026-11-21-AM"]


def test_source_failure_isolation_and_data_threshold_alerts(tmp_path: Path) -> None:
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    def http_mixed(url: str, headers: Any = None, timeout: int = 8) -> _FakeResponse:
        if "fapi.binance.com" in url:
            raise requests.HTTPError("451 Unavailable For Legal Reasons")
        if "api.bybit.com/v5/market/tickers" in url:
            return _FakeResponse(200, json_data={
                "retCode": 0,
                "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.00065", "openInterestValue": "5200000000"}]},
            })
        if "api.bybit.com/v5/market/open-interest" in url:
            return _FakeResponse(200, json_data={
                "retCode": 0,
                "result": {"list": [{"openInterest": "120000"}, {"openInterest": "100000"}]},
            })
        if "mempool.space/api/v1/fees/recommended" in url:
            return _FakeResponse(200, json_data={"fastestFee": 72, "halfHourFee": 55})
        if "mempool.space/api/mempool" in url:
            return _FakeResponse(200, json_data={"vsize": 95_000_000, "count": 120000})
        if "stablecoins.llama.fi" in url:
            return _FakeResponse(200, json_data=[
                {"date": "1795132800", "totalCirculatingUSD": {"peggedUSD": 280_000_000_000}},
                {"date": "1795219200", "totalCirculatingUSD": {"peggedUSD": 282_000_000_000}},
            ])
        raise requests.Timeout("Simulated timeout")

    res = generate_news_files(tmp_path, now_utc=now, http_get=http_mixed)
    assert res["status"] == "partial"
    failed_ids = {f["id"] for f in res["failed_sources"]}
    assert "binance_derivatives" in failed_ids
    assert res["fetch_report"]["sources"]["bybit_derivatives"]["status"] == "ok"
    assert res["fetch_report"]["sources"]["mempool_space"]["status"] == "ok"
    assert res["fetch_report"]["sources"]["defillama_stablecoins"]["status"] == "ok"

    titles = [it["title"] for it in res["latest_items"]]
    assert any("資金費率極端" in t and "0.0650%" in t for t in titles)
    assert any("未平倉量" in t and "+20.00%" in t for t in titles)
    assert any("72 sat/vB" in t for t in titles)
    assert any("穩定幣總供給單日大增" in t and "+$2,000.0M" in t for t in titles)


def test_news_workflows_and_data_sync_merge() -> None:
    news_yml_raw = (ROOT_DIR / ".github" / "workflows" / "news.yml").read_text(encoding="utf-8")
    news_yml = yaml.safe_load(news_yml_raw)
    on_cfg = news_yml.get("on") or news_yml.get(True)
    crons = [c["cron"] for c in (on_cfg.get("schedule") or [])]
    assert "2,17,32,47 * * * *" in crons
    assert "0 0 * * *" not in crons
    assert "0 12 * * *" not in crons
    assert "workflow_dispatch" in on_cfg
    assert news_yml["concurrency"]["group"] == "data-writer-news"
    assert news_yml["concurrency"]["cancel-in-progress"] is False
    assert "news_reactions.json" in news_yml_raw
    assert "data_sync.py" in news_yml_raw
    assert "--max-retries 3" in news_yml_raw

    probe_raw = (ROOT_DIR / ".github" / "workflows" / "news-probe.yml").read_text(encoding="utf-8")
    probe_yml = yaml.safe_load(probe_raw)
    probe_on = probe_yml.get("on") or probe_yml.get(True)
    assert "workflow_dispatch" in probe_on
    assert "schedule" not in probe_on
    assert "git push" not in probe_raw
    assert "git commit" not in probe_raw

    # Check data_sync.py merges news_index.json and news_reactions.json safely
    idx_a = json.dumps({"latest_issue": "2026-11-21-AM", "months": [{"month": "2026-11", "count": 5}], "issues": [{"issue": "2026-11-21-AM", "date": "2026-11-21", "half": "AM", "count": 5}]})
    idx_b = json.dumps({"latest_issue": "2026-11-21-PM", "months": [{"month": "2026-11", "count": 8}], "issues": [{"issue": "2026-11-21-PM", "date": "2026-11-21", "half": "PM", "count": 3}]})
    merged_idx = json.loads(merge_conflicted_content("news_index.json", idx_a, idx_b))
    assert [x["issue"] for x in merged_idx["issues"]] == ["2026-11-21-PM", "2026-11-21-AM"]

    rx_a = json.dumps({"version": 1, "reactions": {"n1": {"reaction_status": "ok"}}})
    rx_b = json.dumps({"version": 1, "reactions": {"n2": {"reaction_status": "ok"}}})
    merged_rx = json.loads(merge_conflicted_content("news_reactions.json", rx_a, rx_b))
    assert set(merged_rx["reactions"].keys()) == {"n1", "n2"}
    assert merged_rx["count"] == 2


def test_issue_id_taipei_schedule_and_next_update_hint() -> None:
    tpe_tz = timezone(timedelta(hours=8))
    assert compute_issue_id(datetime(2026, 11, 22, 7, 59, tzinfo=tpe_tz).astimezone(timezone.utc)) == "2026-11-21-PM"
    assert compute_issue_id(datetime(2026, 11, 22, 8, 0, tzinfo=tpe_tz).astimezone(timezone.utc)) == "2026-11-22-AM"
    assert compute_issue_id(datetime(2026, 11, 22, 19, 59, tzinfo=tpe_tz).astimezone(timezone.utc)) == "2026-11-22-AM"
    assert compute_issue_id(datetime(2026, 11, 22, 20, 0, tzinfo=tpe_tz).astimezone(timezone.utc)) == "2026-11-22-PM"
    assert compute_issue_id(datetime(2026, 11, 22, 3, 0, tzinfo=tpe_tz).astimezone(timezone.utc)) == "2026-11-21-PM"

    hint = compute_next_update_hint(datetime(2026, 11, 21, 23, 17, tzinfo=tpe_tz).astimezone(timezone.utc))
    assert "約" in hint and "23:32" in hint
    hint_wrap = compute_next_update_hint(datetime(2026, 11, 21, 23, 50, tzinfo=tpe_tz).astimezone(timezone.utc))
    assert "約" in hint_wrap and "00:02" in hint_wrap


def test_consecutive_15m_runs_dedup_and_data_alert_same_issue(tmp_path: Path) -> None:
    tpe_tz = timezone(timedelta(hours=8))
    run1_utc = datetime(2026, 11, 21, 20, 2, tzinfo=tpe_tz).astimezone(timezone.utc)
    run2_utc = datetime(2026, 11, 21, 20, 17, tzinfo=tpe_tz).astimezone(timezone.utc)
    run3_utc = datetime(2026, 11, 22, 8, 2, tzinfo=tpe_tz).astimezone(timezone.utc)

    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test</title>
      <item>
        <title>Federal Reserve cuts rates by 25 bps as inflation cools</title>
        <link>https://www.cnbc.com/2026/11/21/fed-cut.html</link>
        <pubDate>Sat, 21 Nov 2026 11:30:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    state = {"fee": 72, "fr": "0.00065"}

    def http_mock(url: str, headers: Any = None, timeout: int = 8) -> _FakeResponse:
        if "cnbc" in url:
            return _FakeResponse(200, text=rss_xml)
        if "api.bybit.com/v5/market/tickers" in url:
            return _FakeResponse(200, json_data={
                "retCode": 0,
                "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": state["fr"], "openInterestValue": "5200000000"}]},
            })
        if "mempool.space/api/v1/fees/recommended" in url:
            return _FakeResponse(200, json_data={"fastestFee": state["fee"], "halfHourFee": 55})
        if "mempool.space/api/mempool" in url:
            return _FakeResponse(200, json_data={"vsize": 95_000_000, "count": 120000})
        raise requests.ConnectionError("Simulated offline")

    res1 = generate_news_files(tmp_path, now_utc=run1_utc, http_get=http_mock, candle_fetcher=lambda *_: [])
    assert res1["latest_issue"] == "2026-11-21-PM"
    assert res1["last_updated_utc"] == run1_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert res1["last_updated_taipei"] == "2026-11-21 20:02"
    assert "約" in res1["next_update_hint"]
    assert len(res1["latest_items"]) == 3  # 1 RSS + 1 funding + 1 mempool
    for it in res1["latest_items"]:
        assert it["first_seen_at_utc"] == run1_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert it["first_seen_at_taipei"] == "2026-11-21 20:02"
        assert it["markets"]
        assert it["sentiment"] in ("bullish", "bearish", "neutral")

    month_file = tmp_path / "news" / "2026-11.jsonl"
    lines_after_run1 = month_file.read_text(encoding="utf-8").splitlines()
    assert len(lines_after_run1) == 3

    # Run 2 (15m later, SAME issue): metrics fluctuate slightly -> still MUST NOT trigger duplicate alerts or RSS
    state["fee"] = 75
    state["fr"] = "0.00068"
    res2 = generate_news_files(tmp_path, now_utc=run2_utc, http_get=http_mock, candle_fetcher=lambda *_: [])
    assert res2["fetch_report"]["new_items_added"] == 0
    assert len(res2["latest_items"]) == 3
    lines_after_run2 = month_file.read_text(encoding="utf-8").splitlines()
    assert lines_after_run2 == lines_after_run1

    # Run 3 (next issue 2026-11-22-AM): data alerts ARE allowed again, while RSS stays deduplicated & old lines intact
    state["fee"] = 78
    state["fr"] = "0.00070"
    res3 = generate_news_files(tmp_path, now_utc=run3_utc, http_get=http_mock, candle_fetcher=lambda *_: [])
    assert res3["latest_issue"] == "2026-11-22-AM"
    assert res3["fetch_report"]["new_items_added"] == 2
    lines_after_run3 = month_file.read_text(encoding="utf-8").splitlines()
    assert len(lines_after_run3) == 5
    assert lines_after_run3[:3] == lines_after_run1


def test_sentiment_rules_negation_inversion_and_markets() -> None:
    news_cfg = load_news_config()

    # English cases (>= 5, incl. negation & inflation inversion)
    en_cases = [
        ("Federal Reserve announces 25 bps rate cut", "聯準會", ["NAS100"], "bullish"),
        ("Fed signals it will not cut rates this year", "聯準會", ["NAS100"], "bearish"),
        ("US CPI inflation comes in lower than expected at 2.4%", "總經", ["NAS100"], "bullish"),
        ("Core PCE inflation hotter than expected, Fed hawkish", "聯準會", ["NAS100"], "bearish"),
        ("TSMC beats estimates and raises guidance on AI demand", "美股", ["2330"], "bullish"),
        ("Retailer misses estimates and files for bankruptcy", "美股", [], "bearish"),
        ("Fed rules out rate hike amid cooling inflation", "聯準會", ["NAS100"], "bullish"),
        ("European bond market sees steady session", "總經", [], "neutral"),
    ]
    for title, cat, syms, expected in en_cases:
        mkts = derive_markets(title, "", cat, syms, news_cfg)
        res = evaluate_sentiment(title, "", cat, mkts, syms, "cnbc_top", news_cfg)
        assert res["sentiment"] == expected, f"EN sentiment mismatch for {title!r}: got {res}"
        assert -3 <= res["sentiment_score"] <= 3
        assert res["sentiment_method"] == "rule"
        assert res["sentiment_targets"]

    # Traditional Chinese cases (>= 5, incl. negation & inflation inversion & 未平倉 guard)
    zh_cases = [
        ("美國 CPI 通膨低於預期，市場押注降息", "總經", [], "bullish"),
        ("美國通膨高於預期，聯準會立場維持鷹派", "聯準會", [], "bearish"),
        ("台積電財報優於預期並上調財測，外資買超", "台股", ["2330"], "bullish"),
        ("某電子廠財報不如預期並下修財測，外資賣超", "台股", [], "bearish"),
        ("聯準會官員表態今年不降息，暫緩寬鬆", "聯準會", [], "bearish"),
        ("BTC 未平倉量觀察，市場等待方向", "衍生品", ["BTCUSDT"], "neutral"),
        ("美國宣布解除制裁並撤銷相關訴訟", "總經", [], "bullish"),
        ("某協議遭駭客漏洞攻擊被盜 2000 萬美元", "安全事件", ["ETHUSDT"], "bearish"),
        ("台北股市今日量縮整理，關注法說會", "台股", ["TWII"], "neutral"),
    ]
    for title, cat, syms, expected in zh_cases:
        mkts = derive_markets(title, "", cat, syms, news_cfg)
        res = evaluate_sentiment(title, "", cat, mkts, syms, "cnyes_tw_stock", news_cfg)
        assert res["sentiment"] == expected, f"ZH sentiment mismatch for {title!r}: got {res}"

    # Data alert sentiment checks
    etf_in = evaluate_sentiment("BTC ETF", "", "鏈上資金", ["加密貨幣"], ["BTCUSDT"], "bitbo_btc_etf", news_cfg, is_data_alert=True, metrics={"net_flow_usd_m": 250.0})
    assert etf_in["sentiment"] == "bullish"
    etf_out = evaluate_sentiment("BTC ETF", "", "鏈上資金", ["加密貨幣"], ["BTCUSDT"], "bitbo_btc_etf", news_cfg, is_data_alert=True, metrics={"net_flow_usd_m": -250.0})
    assert etf_out["sentiment"] == "bearish"
    fr_hi = evaluate_sentiment("Funding high", "", "衍生品", ["加密貨幣"], ["BTCUSDT"], "okx_derivatives", news_cfg, is_data_alert=True, metrics={"funding_rate_pct": 0.08})
    assert fr_hi["sentiment"] == "bearish" and "多頭擁擠" in fr_hi["sentiment_reason"]

    # Markets derivation checks
    assert derive_markets("台積電法說會", "", "台股", ["2330"], news_cfg) == ["台股"]
    assert derive_markets("Tesla deliveries", "", "美股", ["TSLA"], news_cfg) == ["美股"]
    assert derive_markets("OKX 上幣公告", "", "交易所公告", ["SOLUSDT"], news_cfg) == ["加密貨幣"]
    fed_multi = derive_markets("FOMC 利率決議牽動美股與比特幣走勢", "", "聯準會", [], news_cfg)
    assert set(fed_multi) == {"美股", "加密貨幣", "總經"}


def test_market_reactions_and_40_cap(tmp_path: Path) -> None:
    news_cfg = load_news_config()
    instruments = load_tracked_instruments()
    t0 = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
    now = t0 + timedelta(hours=2)

    def _mk_bars(start_dt: datetime, closes: list[float]) -> list[dict[str, Any]]:
        return [{"ts_utc": start_dt + timedelta(minutes=15 * i), "open": c, "close": c} for i, c in enumerate(closes)]

    # 1. Bullish reaction (+0.8% > 0.5% crypto threshold) agreeing with bullish rule
    it_bull = {
        "id": "r-bull",
        "published_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_seen_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sentiment": "bullish",
        "importance": 3,
        "sentiment_targets": ["BTCUSDT"],
    }
    res_bull = compute_single_news_reaction(
        it_bull, _mk_bars(t0, [100.0, 100.3, 100.5, 100.7, 100.8]),
        target="BTCUSDT", now_utc=now, instruments=instruments, news_cfg=news_cfg,
    )
    assert res_bull["reaction_status"] == "ok"
    assert res_bull["reaction_label"] == "市場解讀偏多"
    assert res_bull["ret_15m_pct"] == 0.3
    assert res_bull["ret_60m_pct"] == 0.8
    assert res_bull["agree_with_rule"] is True
    assert res_bull["market_closed_at_event"] is False

    # 2. Bearish reaction (-0.9%) disagreeing with bullish rule -> agree_with_rule is False
    res_div = compute_single_news_reaction(
        it_bull, _mk_bars(t0, [100.0, 99.7, 99.4, 99.2, 99.1]),
        target="BTCUSDT", now_utc=now, instruments=instruments, news_cfg=news_cfg,
    )
    assert res_div["reaction_label"] == "市場解讀偏空"
    assert res_div["agree_with_rule"] is False

    # 3. Insignificant reaction (+0.1% within ±0.3% us_index threshold) -> agree_with_rule is None
    it_flat = {
        "id": "r-flat",
        "published_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_seen_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sentiment": "bullish",
        "importance": 2,
        "sentiment_targets": ["NAS100"],
    }
    res_flat = compute_single_news_reaction(
        it_flat, _mk_bars(t0, [1000.0, 1000.5, 1000.8, 1000.9, 1001.0]),
        target="NAS100", now_utc=now, instruments=instruments, news_cfg=news_cfg,
    )
    assert res_flat["reaction_label"] == "市場反應不明顯"
    assert res_flat["agree_with_rule"] is None

    # 4. Market closed at event (event at Sat 10:00 TPE, next open Mon 09:00 TPE)
    tpe_tz = timezone(timedelta(hours=8))
    sat_event = datetime(2026, 11, 21, 10, 0, tzinfo=tpe_tz).astimezone(timezone.utc)
    mon_open = datetime(2026, 11, 23, 9, 0, tzinfo=tpe_tz).astimezone(timezone.utc)
    it_closed = {
        "id": "r-closed",
        "published_at_utc": sat_event.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_seen_at_utc": sat_event.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sentiment": "bullish",
        "importance": 3,
        "sentiment_targets": ["2330"],
    }
    res_closed = compute_single_news_reaction(
        it_closed, _mk_bars(mon_open, [1000.0, 1005.0, 1010.0, 1012.0, 1015.0]),
        target="2330", now_utc=mon_open + timedelta(hours=2), instruments=instruments, news_cfg=news_cfg,
    )
    assert res_closed["reaction_status"] == "ok"
    assert res_closed["market_closed_at_event"] is True
    assert res_closed["base_time_taipei"] == "2026-11-23 09:00"
    assert res_closed["reaction_label"] == "市場解讀偏多"

    # 5. Price unavailable
    res_unavail = compute_single_news_reaction(
        it_bull, [], target="BTCUSDT", now_utc=now, instruments=instruments, news_cfg=news_cfg,
    )
    assert res_unavail["reaction_status"] == "unavailable"

    # 6. Cap at 40 per run, prioritizing importance=3, skipping <60m and >=48h
    items_pool: list[dict[str, Any]] = [
        {
            "id": "too-young",
            "first_seen_at_utc": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "importance": 3,
            "sentiment": "bullish",
            "sentiment_targets": ["BTCUSDT"],
        },
        {
            "id": "too-old",
            "first_seen_at_utc": (now - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "importance": 3,
            "sentiment": "bullish",
            "sentiment_targets": ["BTCUSDT"],
        },
    ]
    for i in range(45):
        items_pool.append({
            "id": f"elig-{i:02d}",
            "first_seen_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "importance": 3 if i < 5 else 1,
            "sentiment": "bullish",
            "sentiment_targets": ["BTCUSDT"],
        })
    rx_map, count = compute_news_reactions(
        items_pool,
        {},
        now_utc=now,
        instruments=instruments,
        news_cfg=news_cfg,
        candle_fetcher=lambda _sym, _now: _mk_bars(t0, [100.0, 100.2, 100.4, 100.6, 100.8]),
    )
    assert count == 40
    assert len(rx_map) == 40
    assert "too-young" not in rx_map
    assert "too-old" not in rx_map
    for i in range(5):
        assert f"elig-{i:02d}" in rx_map


def test_frontend_news_defaults_filters_and_signal_related_news() -> None:
    index_html = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT_DIR / "assets" / "app.js").read_text(encoding="utf-8")
    style_css = (ROOT_DIR / "assets" / "style.css").read_text(encoding="utf-8")

    assert "#/news" in index_html
    assert 'id="view-news"' in index_html
    assert 'id="news-market-tabs"' in index_html
    assert 'id="news-sentiment-tabs"' in index_html
    assert 'id="news-new-banner"' in index_html
    assert 'id="news-updating-pill"' in index_html
    assert 'id="news-issue-select"' in index_html
    assert 'id="news-filter-category"' in index_html
    assert 'id="news-filter-symbol"' in index_html
    assert 'id="news-filter-importance"' in index_html
    assert "相關消息（最近 3 天）" in app_js
    assert ".news-row" in style_css
    assert ".signal-news-box" in style_css
    assert ".news-market-tabs" in style_css

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    old_2h_iso = (now_dt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_4d_iso = (now_dt - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev_visit_ms = int((now_dt - timedelta(minutes=30)).timestamp() * 1000)

    latest_mock = {
        "version": 1,
        "latest_issue": "2026-11-21-PM",
        "is_current_issue_updating": True,
        "updated_at_utc": now_iso,
        "updated_at_taipei": "2026-11-21 20:05",
        "last_updated_utc": now_iso,
        "last_updated_taipei": "2026-11-21 20:05",
        "next_update_hint": "約台北時間 20:17（每 15 分鐘更新）",
        "status": "partial",
        "failed_sources": [{"id": "binance_derivatives", "name": "Binance 永續合約", "error": "451"}],
        "latest_items": [
            {
                "id": "n-pm-1",
                "issue": "2026-11-21-PM",
                "published_at_utc": old_2h_iso,
                "first_seen_at_utc": old_2h_iso,
                "published_at_taipei": "2026-11-21 19:50",
                "title": "FOMC minutes show rate cut path",
                "url": "https://example.com/pm-1",
                "source": "CNBC",
                "category": "聯準會",
                "markets": ["總經"],
                "symbols": ["NAS100"],
                "importance": 3,
                "importance_stars": "★★★",
                "sentiment": "bullish",
                "sentiment_label": "利多",
                "sentiment_score": 2,
                "sentiment_reason": "命中：rate cut",
            },
            {
                "id": "n-pm-2",
                "issue": "2026-11-21-PM",
                "published_at_utc": now_iso,
                "first_seen_at_utc": now_iso,
                "published_at_taipei": "2026-11-21 19:40",
                "title": "Bitcoin ETF net inflow hits $210M",
                "url": "https://example.com/pm-2",
                "source": "CoinDesk",
                "category": "加密貨幣",
                "symbols": ["BTCUSDT"],
                "importance": 2,
                "importance_stars": "★★",
                "followup_of": {"id": "n-am-1", "issue": "2026-11-21-AM", "title": "Prior BTC rally", "url": "https://example.com/am-1"},
            },
        ],
        "recent_7d_items": [
            {
                "id": "n-pm-1",
                "issue": "2026-11-21-PM",
                "published_at_utc": old_2h_iso,
                "first_seen_at_utc": old_2h_iso,
                "title": "FOMC minutes show rate cut path",
                "url": "https://example.com/pm-1",
                "source": "CNBC",
                "category": "聯準會",
                "symbols": ["NAS100"],
                "importance": 3,
                "sentiment": "bullish",
                "sentiment_label": "利多",
            },
            {
                "id": "n-pm-2",
                "issue": "2026-11-21-PM",
                "published_at_utc": now_iso,
                "first_seen_at_utc": now_iso,
                "title": "Bitcoin ETF net inflow hits $210M",
                "url": "https://example.com/pm-2",
                "source": "CoinDesk",
                "category": "加密貨幣",
                "symbols": ["BTCUSDT"],
                "importance": 2,
            },
            {
                "id": "n-am-old",
                "issue": "2026-11-21-AM",
                "published_at_utc": now_iso,
                "title": "AM-ONLY-SHOULD-NOT-SHOW-IN-DEFAULT-PM",
                "url": "https://example.com/am-old",
                "source": "MarketWatch",
                "category": "美股",
                "symbols": ["SP500"],
                "importance": 2,
            },
            {
                "id": "n-4d-old",
                "issue": "2026-11-17-AM",
                "published_at_utc": old_4d_iso,
                "title": "BTC-4-DAYS-AGO-OUTSIDE-3D-WINDOW",
                "url": "https://example.com/4d",
                "source": "Decrypt",
                "category": "加密貨幣",
                "symbols": ["BTCUSDT"],
                "importance": 2,
            },
        ],
        "recent_issues": [
            {"issue": "2026-11-21-PM", "month": "2026-11", "count": 2},
            {"issue": "2026-11-21-AM", "month": "2026-11", "count": 1},
        ],
    }
    index_mock = {
        "version": 1,
        "latest_issue": "2026-11-21-PM",
        "issues": [
            {"issue": "2026-11-21-PM", "month": "2026-11", "count": 2, "updated_at_taipei": "2026-11-21 20:05"},
            {"issue": "2026-11-21-AM", "month": "2026-11", "count": 1, "updated_at_taipei": "2026-11-21 08:05"},
        ],
    }
    reactions_mock = {
        "version": 1,
        "reactions": {
            "n-pm-1": {
                "reaction_status": "ok",
                "reaction_label": "市場解讀偏空",
                "reaction_direction": "bearish",
                "ret_15m_pct": -0.2,
                "ret_60m_pct": -0.6,
                "target": "NAS100",
                "base_time_taipei": "2026-11-21 19:50",
                "market_closed_at_event": False,
                "agree_with_rule": False,
            },
        },
    }

    script = f"""
        function makeEl(id, extra = {{}}) {{
          const cls = new Set(extra.classes || []);
          return {{
            id,
            dataset: extra.dataset || {{}},
            style: {{}},
            innerHTML: '',
            textContent: '',
            value: extra.value || 'all',
            disabled: false,
            clientWidth: 800,
            clientHeight: 340,
            classList: {{
              add: (c) => cls.add(c),
              remove: (c) => cls.delete(c),
              contains: (c) => cls.has(c),
              toggle: (c, f) => f ? cls.add(c) : cls.delete(c),
            }},
            setAttribute: () => {{}},
            addEventListener: () => {{}},
            querySelectorAll: () => [],
            querySelector: () => null,
          }};
        }}
        const byId = {{
          'news-filter-category': makeEl('news-filter-category', {{ value: 'all' }}),
          'news-filter-symbol': makeEl('news-filter-symbol', {{ value: 'all' }}),
          'news-filter-importance': makeEl('news-filter-importance', {{ value: 'all' }}),
          'news-issue-select': makeEl('news-issue-select', {{ value: '' }}),
        }};
        const routeViews = ['view-wall', 'view-signal', 'view-account', 'view-data', 'view-research', 'view-news'].map((id) => {{
          const e = makeEl(id, {{ classes: ['route-view'] }});
          byId[id] = e;
          return e;
        }});
        const navLinks = ['wall', 'account', 'data', 'research', 'news'].map((r) => makeEl('nav-' + r, {{ classes: ['nav-link'], dataset: {{ route: r }} }}));
        var navigator = {{}};
        var window = {{ location: {{ hash: '#/news' }}, addEventListener: () => {{}}, SIGNAL_DESK_CONFIG: {{}} }};
        var document = {{
          getElementById: (id) => byId[id] || (byId[id] = makeEl(id)),
          querySelectorAll: (sel) => sel === '.route-view' ? routeViews : (sel === '.nav-link' ? navLinks : []),
          querySelector: (sel) => {{
            const m = sel.match(/^\\.nav-link\\[data-route="([^"]+)"\\]$/);
            return m ? navLinks.find((n) => n.dataset.route === m[1]) || null : null;
          }},
          body: {{ appendChild: () => {{}}, removeChild: () => {{}} }},
          createElement: () => makeEl('tmp'),
        }};
        function setInterval() {{}}
        function setTimeout() {{ return 1; }}
        function clearTimeout() {{}}
        async function fetch() {{ return {{ ok: false }}; }}

        {app_js}

        const u = window.__SIGNAL_DESK_UTILS__;
        u.setNewsDataForTest({json.dumps(latest_mock)}, {json.dumps(index_mock)}, null, {json.dumps(reactions_mock)}, {prev_visit_ms});
        u.applyRoute();

        // 1. Default view renders ONLY latest issue (2026-11-21-PM), NOT AM issue
        const listHtml = byId['news-list'].innerHTML;
        if (!listHtml.includes('FOMC minutes show rate cut path') || !listHtml.includes('Bitcoin ETF net inflow')) {{
          throw new Error('Default news-list missing latest issue items');
        }}
        if (listHtml.includes('AM-ONLY-SHOULD-NOT-SHOW-IN-DEFAULT-PM')) {{
          throw new Error('Default news-list leaked older issue items');
        }}
        if (!listHtml.includes('同事件後續')) {{
          throw new Error('Followup badge missing');
        }}
        if (!listHtml.includes('利多') || !listHtml.includes('—')) {{
          throw new Error('Sentiment badges (new + legacy dash) missing');
        }}
        if (!listHtml.includes('市場：偏空') || !listHtml.includes('判讀分歧')) {{
          throw new Error('Market reaction or divergence badge missing');
        }}
        if (!listHtml.includes('市場反應計算中')) {{
          throw new Error('Pending reaction (<60m) badge missing');
        }}
        if (!listHtml.includes('news-new-pill')) {{
          throw new Error('New-since-last-visit badge missing');
        }}
        if (!byId['news-issue-badge'].textContent.includes('更新中')) {{
          throw new Error('Current issue updating indicator missing');
        }}
        if (!byId['news-updated-at'].textContent.includes('約每 15 分鐘更新')) {{
          throw new Error('Last updated header missing 15m text');
        }}

        // 2. Legacy market derivation + hash URL market selection (#/news?m=加密貨幣)
        const derivedCrypto = u.deriveNewsMarkets({{ category: '鏈上資金', symbols: [], title: 'BTC inflow' }});
        if (!derivedCrypto.includes('加密貨幣')) {{
          throw new Error('JS deriveNewsMarkets failed');
        }}
        window.location.hash = '#/news?m=' + encodeURIComponent('加密貨幣');
        u.applyRoute();
        if (u.getNewsSelectedMarket() !== '加密貨幣') {{
          throw new Error('Hash market parsing failed');
        }}
        const cryptoOnlyHtml = byId['news-list'].innerHTML;
        if (!cryptoOnlyHtml.includes('Bitcoin ETF net inflow') || cryptoOnlyHtml.includes('FOMC minutes show rate cut path')) {{
          throw new Error('Market filter failed to filter out non-crypto news');
        }}
        window.location.hash = '#/news';
        u.applyRoute();

        // 3. Highlights card shows ★★★ item
        if (!byId['news-highlights-list'].innerHTML.includes('FOMC minutes show rate cut path')) {{
          throw new Error('Highlights missing ★★★ item');
        }}

        // 4. Failed sources banner visible and non-breaking
        if (byId['news-failed-banner'].classList.contains('hidden') || !byId['news-failed-banner'].innerHTML.includes('Binance 永續合約')) {{
          throw new Error('Failed sources banner not shown properly');
        }}

        // 5. Signal card related news (3 days): includes sentiment/reaction & excludes 4d old
        const nasHtml = u.renderSignalRelatedNewsHtml('NAS100');
        if (!nasHtml.includes('利多') || !nasHtml.includes('判讀分歧')) {{
          throw new Error('Signal related news missing sentiment or reaction');
        }}
        const btcHtml = u.renderSignalRelatedNewsHtml('BTCUSDT');
        if (!btcHtml.includes('相關消息（最近 3 天）') || !btcHtml.includes('Bitcoin ETF net inflow')) {{
          throw new Error('BTCUSDT related news missing');
        }}
        if (btcHtml.includes('BTC-4-DAYS-AGO-OUTSIDE-3D-WINDOW')) {{
          throw new Error('4-day-old news leaked into 3-day signal window');
        }}
        if (u.renderSignalRelatedNewsHtml('2330') !== '') {{
          throw new Error('Expected empty string when symbol has no related news');
        }}
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"Frontend news JS test failed: {proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)


def test_hsi_news_matching_markets_and_reaction_config() -> None:
    from src.news import (
        _resolve_reaction_threshold,
        _resolve_yfinance_ticker,
        build_symbol_alias_map,
        classify_category,
        load_news_config,
        load_tracked_instruments,
        match_symbols,
    )

    news_cfg = load_news_config()
    instruments = load_tracked_instruments()
    aliases = build_symbol_alias_map(instruments, news_cfg)
    assert "HSI" in aliases

    for headline in ("恒生指數高開逾 2%，科技股領漲", "恒生今早反彈", "Hang Seng climbs on stimulus hopes"):
        syms = match_symbols(headline, "", aliases)
        assert "HSI" in syms, f"Expected HSI matched for {headline!r}, got {syms}"
        cat = classify_category(headline, "", "美股", "yahoo_finance_us", syms)
        mkts = derive_markets(headline, "", cat, syms, news_cfg)
        assert "總經" in mkts

    assert _resolve_yfinance_ticker("HSI", instruments) == "^HSI"
    assert _resolve_reaction_threshold("HSI", instruments, news_cfg) == 0.3


def test_news_frontend_pure_time_sorting_and_highlights() -> None:
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    app_js = (ROOT_DIR / "assets" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT_DIR / "assets" / "style.css").read_text(encoding="utf-8")

    # CSS and HTML structural assertions
    assert "news-layout-grid" in index_html
    assert "news-main-column" in index_html
    assert "news-highlights-column" in index_html
    assert "news-highlights-toggle" in index_html
    assert "news-highlights-title" in index_html
    assert ".news-layout-grid" in style_css
    assert "68fr 32fr" in style_css
    assert "position: sticky" in style_css
    assert "max-width: 1099.98px" in style_css
    assert "min-width: 1100px" in style_css

    test_items = [
        {
            "id": "item-older-3",
            "issue": "2026-11-22-AM",
            "published_at_utc": "2026-11-22T08:00:00Z",
            "first_seen_at_utc": "2026-11-22T08:00:00Z",
            "title": "Older 3-Star News (08:00)",
            "url": "https://example.com/item-older-3",
            "source": "SourceA",
            "category": "總經",
            "symbols": ["SP500"],
            "importance": 3,
            "importance_stars": "★★★",
            "sentiment": "neutral",
        },
        {
            "id": "item-newer-2",
            "issue": "2026-11-22-AM",
            "published_at_utc": "2026-11-22T10:00:00Z",
            "first_seen_at_utc": "2026-11-22T10:00:00Z",
            "title": "Newer 2-Star News (10:00)",
            "url": "https://example.com/item-newer-2",
            "source": "SourceB",
            "category": "美股",
            "symbols": ["AAPL"],
            "importance": 2,
            "importance_stars": "★★",
            "sentiment": "bullish",
        },
        {
            "id": "item-newest-3",
            "issue": "2026-11-22-AM",
            "published_at_utc": "2026-11-22T11:00:00Z",
            "first_seen_at_utc": "2026-11-22T11:00:00Z",
            "title": "Newest 3-Star News (11:00)",
            "url": "https://example.com/item-newest-3",
            "source": "SourceC",
            "category": "加密貨幣",
            "symbols": ["BTCUSDT"],
            "importance": 3,
            "importance_stars": "★★★",
            "sentiment": "bullish",
        },
        {
            "id": "item-fallback-1",
            "issue": "2026-11-22-AM",
            "published_at_utc": None,
            "first_seen_at_utc": "2026-11-22T09:00:00Z",
            "title": "Fallback First Seen 1-Star News (09:00)",
            "url": "https://example.com/item-fallback-1",
            "source": "SourceD",
            "category": "總經",
            "symbols": ["TWII"],
            "importance": 1,
            "importance_stars": "★",
            "sentiment": "neutral",
        },
        {
            "id": "item-no-time",
            "issue": "2026-11-22-AM",
            "published_at_utc": None,
            "first_seen_at_utc": None,
            "title": "No Time News (Sort to End)",
            "url": "https://example.com/item-no-time",
            "source": "SourceE",
            "category": "總經",
            "symbols": [],
            "importance": 3,
            "importance_stars": "★★★",
            "sentiment": "neutral",
        },
    ]

    mock_doc = {
        "version": 1,
        "latest_issue": "2026-11-22-AM",
        "is_current_issue_updating": True,
        "updated_at_utc": "2026-11-22T11:05:00Z",
        "updated_at_taipei": "2026-11-22 19:05",
        "last_updated_utc": "2026-11-22T11:05:00Z",
        "last_updated_taipei": "2026-11-22 19:05",
        "status": "ok",
        "failed_sources": [],
        "latest_items": test_items,
        "recent_7d_items": test_items,
    }

    mock_index = {
        "version": 1,
        "latest_issue": "2026-11-22-AM",
        "issues": [
            {"issue": "2026-11-22-AM", "month": "2026-11", "count": 5, "updated_at_taipei": "2026-11-22 19:05"}
        ],
    }

    script = f"""
        function makeEl(id, extra = {{}}) {{
          const cls = new Set(extra.classes || []);
          return {{
            id,
            dataset: extra.dataset || {{}},
            style: {{}},
            innerHTML: '',
            textContent: '',
            value: extra.value || 'all',
            disabled: false,
            clientWidth: 1200,
            clientHeight: 700,
            classList: {{
              add: (c) => cls.add(c),
              remove: (c) => cls.delete(c),
              contains: (c) => cls.has(c),
              toggle: (c, f) => f !== undefined ? (f ? cls.add(c) : cls.delete(c)) : (cls.has(c) ? (cls.delete(c), false) : (cls.add(c), true)),
            }},
            setAttribute: () => {{}},
            addEventListener: () => {{}},
            querySelectorAll: () => [],
            querySelector: () => null,
          }};
        }}
        const byId = {{
          'news-filter-category': makeEl('news-filter-category', {{ value: 'all' }}),
          'news-filter-symbol': makeEl('news-filter-symbol', {{ value: 'all' }}),
          'news-filter-importance': makeEl('news-filter-importance', {{ value: 'all' }}),
          'news-issue-select': makeEl('news-issue-select', {{ value: '2026-11-22-AM' }}),
          'news-highlights-card': makeEl('news-highlights-card', {{ classes: ['collapsed'] }}),
          'news-highlights-toggle': makeEl('news-highlights-toggle'),
          'news-highlights-title': makeEl('news-highlights-title'),
          'news-highlights-list': makeEl('news-highlights-list'),
          'news-list': makeEl('news-list'),
        }};
        const routeViews = ['view-wall', 'view-signal', 'view-account', 'view-data', 'view-research', 'view-news'].map((id) => {{
          const e = makeEl(id, {{ classes: ['route-view'] }});
          byId[id] = e;
          return e;
        }});
        const navLinks = ['wall', 'account', 'data', 'research', 'news'].map((r) => makeEl('nav-' + r, {{ classes: ['nav-link'], dataset: {{ route: r }} }}));
        var navigator = {{}};
        var window = {{ location: {{ hash: '#/news' }}, addEventListener: () => {{}}, SIGNAL_DESK_CONFIG: {{}} }};
        var document = {{
          getElementById: (id) => byId[id] || (byId[id] = makeEl(id)),
          querySelectorAll: (sel) => sel === '.route-view' ? routeViews : (sel === '.nav-link' ? navLinks : []),
          querySelector: (sel) => null,
          body: {{ appendChild: () => {{}}, removeChild: () => {{}} }},
          createElement: () => makeEl('tmp'),
        }};
        function setInterval() {{}}
        function setTimeout() {{ return 1; }}
        function clearTimeout() {{}}
        async function fetch() {{ return {{ ok: false }}; }}

        {app_js}

        const u = window.__SIGNAL_DESK_UTILS__;

        // 1. Test compareNewsItemsByTimeDesc unit comparisons
        const itemNew = {{ published_at_utc: '2026-11-22T10:00:00Z', importance: 2 }};
        const itemOld = {{ published_at_utc: '2026-11-22T08:00:00Z', importance: 3 }};
        const itemSeen = {{ first_seen_at_utc: '2026-11-22T09:00:00Z', importance: 1 }};
        const itemNone = {{ importance: 3 }};

        if (u.compareNewsItemsByTimeDesc(itemNew, itemOld) >= 0) {{
          throw new Error('Newer item (10:00) must sort before older item (08:00)');
        }}
        if (u.compareNewsItemsByTimeDesc(itemOld, itemNew) <= 0) {{
          throw new Error('Older item (08:00) must sort after newer item (10:00)');
        }}
        if (u.compareNewsItemsByTimeDesc(itemNew, itemNone) >= 0) {{
          throw new Error('Item with timestamp must sort before item missing timestamp');
        }}
        if (u.compareNewsItemsByTimeDesc(itemNone, itemNew) <= 0) {{
          throw new Error('Item missing timestamp must sort after item with timestamp');
        }}
        if (u.compareNewsItemsByTimeDesc(itemNone, itemNone) !== 0) {{
          throw new Error('Two items with missing timestamps should compare as 0');
        }}
        if (u.compareNewsItemsByTimeDesc(itemSeen, itemOld) >= 0) {{
          throw new Error('Item with first_seen_at_utc 09:00 must sort before published_at_utc 08:00');
        }}

        // 2. Full render test
        u.setNewsDataForTest({json.dumps(mock_doc)}, {json.dumps(mock_index)}, null, {{}}, 0);
        u.applyRoute();

        const mainListHtml = byId['news-list'].innerHTML;
        const hlListHtml = byId['news-highlights-list'].innerHTML;

        // Verify main list contains all 5 items
        const idxNewest3 = mainListHtml.indexOf('Newest 3-Star News');
        const idxNewer2 = mainListHtml.indexOf('Newer 2-Star News');
        const idxFallback1 = mainListHtml.indexOf('Fallback First Seen');
        const idxOlder3 = mainListHtml.indexOf('Older 3-Star News');
        const idxNoTime = mainListHtml.indexOf('No Time News');

        if (idxNewest3 === -1 || idxNewer2 === -1 || idxFallback1 === -1 || idxOlder3 === -1 || idxNoTime === -1) {{
          throw new Error('Main news list is missing some items');
        }}

        // Pure time order check: 11:00 < 10:00 < 09:00 (first_seen) < 08:00 < no_time
        if (!(idxNewest3 < idxNewer2 && idxNewer2 < idxFallback1 && idxFallback1 < idxOlder3 && idxOlder3 < idxNoTime)) {{
          throw new Error('Main news list is NOT in pure time order: expected 11:00 < 10:00 < 09:00 < 08:00 < no_time');
        }}

        // Specifically verify: ★★★ old news does NOT rank ahead of newer ★★ news
        if (idxOlder3 < idxNewer2) {{
          throw new Error('Older 3-star news (08:00) was improperly ranked before newer 2-star news (10:00)!');
        }}

        // Verify missing timestamp item is at the very end
        if (idxNoTime < idxOlder3) {{
          throw new Error('Item without timestamp was not sorted to the end');
        }}

        // 3. Highlights column verification
        // Must contain ★★★ items only
        if (!hlListHtml.includes('Newest 3-Star News') || !hlListHtml.includes('Older 3-Star News') || !hlListHtml.includes('No Time News')) {{
          throw new Error('Highlights list missing 3-star items');
        }}
        if (hlListHtml.includes('Newer 2-Star News') || hlListHtml.includes('Fallback First Seen')) {{
          throw new Error('Highlights list leaked 1-star or 2-star news items!');
        }}

        // Highlights column time order check
        const hlIdxNewest3 = hlListHtml.indexOf('Newest 3-Star News');
        const hlIdxOlder3 = hlListHtml.indexOf('Older 3-Star News');
        const hlIdxNoTime = hlListHtml.indexOf('No Time News');
        if (!(hlIdxNewest3 < hlIdxOlder3 && hlIdxOlder3 < hlIdxNoTime)) {{
          throw new Error('Highlights column is NOT in time order');
        }}

        // Highlights title reflects count (3 items)
        if (byId['news-highlights-title'].textContent !== '重點（3 則）') {{
          throw new Error('Highlights title count mismatch: ' + byId['news-highlights-title'].textContent);
        }}

        // 4. Test filtering applied to highlights column
        // Set importance filter to '1' (★ 一般), so 0 items in highlights column
        byId['news-filter-importance'].value = '1';
        u.renderNewsView();
        if (!byId['news-highlights-list'].innerHTML.includes('目前沒有重點消息')) {{
          throw new Error('Expected "目前沒有重點消息" when no highlights match filters');
        }}
        if (byId['news-highlights-title'].textContent !== '重點（0 則）') {{
          throw new Error('Highlights title should be 重點（0 則） when 0 items match');
        }}

        // Reset filter
        byId['news-filter-importance'].value = 'all';
        u.renderNewsView();
        if (byId['news-highlights-title'].textContent !== '重點（3 則）') {{
          throw new Error('Highlights title failed to reset count');
        }}
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"Frontend time sorting test failed: {proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)



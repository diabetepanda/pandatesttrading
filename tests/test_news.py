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
    deduplicate_and_link_items,
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
    assert "0 0 * * *" in crons
    assert "0 12 * * *" in crons
    assert "workflow_dispatch" in on_cfg
    assert news_yml["concurrency"]["group"] == "data-writer-news"
    assert news_yml["concurrency"]["cancel-in-progress"] is False
    assert "data_sync.py" in news_yml_raw
    assert "--max-retries 3" in news_yml_raw

    probe_raw = (ROOT_DIR / ".github" / "workflows" / "news-probe.yml").read_text(encoding="utf-8")
    probe_yml = yaml.safe_load(probe_raw)
    probe_on = probe_yml.get("on") or probe_yml.get(True)
    assert "workflow_dispatch" in probe_on
    assert "schedule" not in probe_on
    assert "git push" not in probe_raw
    assert "git commit" not in probe_raw

    # Check data_sync.py merges news_index.json and news/*.jsonl safely
    idx_a = json.dumps({"latest_issue": "2026-11-21-AM", "months": [{"month": "2026-11", "count": 5}], "issues": [{"issue": "2026-11-21-AM", "date": "2026-11-21", "half": "AM", "count": 5}]})
    idx_b = json.dumps({"latest_issue": "2026-11-21-PM", "months": [{"month": "2026-11", "count": 8}], "issues": [{"issue": "2026-11-21-PM", "date": "2026-11-21", "half": "PM", "count": 3}]})
    merged_idx = json.loads(merge_conflicted_content("news_index.json", idx_a, idx_b))
    assert [x["issue"] for x in merged_idx["issues"]] == ["2026-11-21-PM", "2026-11-21-AM"]


def test_frontend_news_defaults_filters_and_signal_related_news() -> None:
    index_html = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT_DIR / "assets" / "app.js").read_text(encoding="utf-8")
    style_css = (ROOT_DIR / "assets" / "style.css").read_text(encoding="utf-8")

    assert "#/news" in index_html
    assert 'id="view-news"' in index_html
    assert 'id="news-issue-select"' in index_html
    assert 'id="news-filter-category"' in index_html
    assert 'id="news-filter-symbol"' in index_html
    assert 'id="news-filter-importance"' in index_html
    assert "相關消息（最近 3 天）" in app_js
    assert ".news-row" in style_css
    assert ".signal-news-box" in style_css

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_4d_iso = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

    latest_mock = {
        "version": 1,
        "latest_issue": "2026-11-21-PM",
        "updated_at_utc": now_iso,
        "updated_at_taipei": "2026-11-21 20:05",
        "status": "partial",
        "failed_sources": [{"id": "binance_derivatives", "name": "Binance 永續合約", "error": "451"}],
        "latest_items": [
            {
                "id": "n-pm-1",
                "issue": "2026-11-21-PM",
                "published_at_utc": now_iso,
                "published_at_taipei": "2026-11-21 19:50",
                "title": "FOMC minutes show rate cut path",
                "url": "https://example.com/pm-1",
                "source": "CNBC",
                "category": "聯準會",
                "symbols": ["NAS100"],
                "importance": 3,
                "importance_stars": "★★★",
            },
            {
                "id": "n-pm-2",
                "issue": "2026-11-21-PM",
                "published_at_utc": now_iso,
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
                "published_at_utc": now_iso,
                "title": "FOMC minutes show rate cut path",
                "url": "https://example.com/pm-1",
                "source": "CNBC",
                "category": "聯準會",
                "symbols": ["NAS100"],
                "importance": 3,
            },
            {
                "id": "n-pm-2",
                "issue": "2026-11-21-PM",
                "published_at_utc": now_iso,
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
        u.setNewsDataForTest({json.dumps(latest_mock)}, {json.dumps(index_mock)});
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

        // 2. Highlights card shows ★★★ item
        if (!byId['news-highlights-list'].innerHTML.includes('FOMC minutes show rate cut path')) {{
          throw new Error('Highlights missing ★★★ item');
        }}

        // 3. Failed sources banner visible and non-breaking
        if (byId['news-failed-banner'].classList.contains('hidden') || !byId['news-failed-banner'].innerHTML.includes('Binance 永續合約')) {{
          throw new Error('Failed sources banner not shown properly');
        }}

        // 4. Signal card related news (3 days): BTCUSDT has 1 recent item (excludes 4d old), SP500 has 1, 2330 has none -> empty
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

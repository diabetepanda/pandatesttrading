"""Tests for macro economic data generator, market reaction conventions, and macro.html frontend."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import run_js

from src.data.models import OHLCVBar
from src.jobs.ta import _save_history_1d_store
from src.macro import (
    _build_overview_summary,
    _evaluate_verdict,
    build_fedwatch_data,
    build_market_snapshot,
    compute_fedwatch_from_prices,
    compute_fomc_blackout_start,
    compute_market_reactions,
    generate_macro_file,
    load_macro_calendar,
    zq_contract_symbol,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)


def _build_synthetic_fred_csv(series_id: str) -> str:
    if series_id == "DFEDTARU":
        lines = ["observation_date,DFEDTARU"]
        d = date(2024, 1, 1)
        while d <= date(2026, 11, 20):
            val = 5.50 if d < date(2024, 9, 18) else (4.00 if d >= date(2026, 9, 16) else 3.75)
            lines.append(f"{d.isoformat()},{val:.2f}")
            d += timedelta(days=1)
        return "\n".join(lines) + "\n"
    if series_id == "DFEDTARL":
        lines = ["observation_date,DFEDTARL"]
        d = date(2024, 1, 1)
        while d <= date(2026, 11, 20):
            val = 5.25 if d < date(2024, 9, 18) else (3.75 if d >= date(2026, 9, 16) else 3.50)
            lines.append(f"{d.isoformat()},{val:.2f}")
            d += timedelta(days=1)
        return "\n".join(lines) + "\n"
    if series_id in ("DGS2", "DGS10"):
        lines = [f"observation_date,{series_id}"]
        base = 4.60 if series_id == "DGS2" else 4.85
        d = date(2024, 1, 2)
        i = 0
        while d <= date(2026, 11, 19):
            if d.weekday() < 5:
                v = base + ((i % 7) - 3) * 0.04
                lines.append(f"{d.isoformat()},{v:.2f}")
                i += 1
            d += timedelta(days=1)
        return "\n".join(lines) + "\n"
    if series_id == "ICSA":
        lines = ["observation_date,ICSA"]
        d = date(2025, 11, 1)
        i = 0
        while d <= date(2026, 11, 14):
            lines.append(f"{d.isoformat()},{195000 + (i % 5) * 2000}")
            d += timedelta(days=7)
            i += 1
        return "\n".join(lines) + "\n"
    if series_id == "A191RL1Q225SBEA":
        lines = ["observation_date,A191RL1Q225SBEA"]
        for yr in range(2020, 2027):
            for mo in (1, 4, 7, 10):
                if yr == 2026 and mo > 7:
                    continue
                lines.append(f"{yr:04d}-{mo:02d}-01,{2.0 + (mo % 3) * 0.6:.1f}")
        return "\n".join(lines) + "\n"

    # Monthly series from 2023-01 to 2026-10
    lines = [f"observation_date,{series_id}"]
    idx = 100.0
    for yr in range(2023, 2027):
        for mo in range(1, 13):
            if yr == 2026 and mo > 10:
                continue
            if series_id == "PAYEMS":
                idx += 120.0 + ((mo % 4) - 1) * 35.0
                lines.append(f"{yr:04d}-{mo:02d}-01,{idx:.0f}")
            elif series_id == "UNRATE":
                val = 4.0 + (mo % 3) * 0.1
                lines.append(f"{yr:04d}-{mo:02d}-01,{val:.1f}")
            elif series_id == "JTSJOL":
                if yr == 2026 and mo > 9:
                    continue
                val = 7400 + (mo % 4) * 150
                lines.append(f"{yr:04d}-{mo:02d}-01,{val:.0f}")
            elif series_id in ("CPIAUCSL",) and yr == 2025 and mo == 10:
                lines.append(f"{yr:04d}-{mo:02d}-01,")
            else:
                idx *= 1.003 if mo % 2 == 0 else 1.002
                lines.append(f"{yr:04d}-{mo:02d}-01,{idx:.3f}")
    return "\n".join(lines) + "\n"


def _build_synthetic_te_html(indicator_id: str) -> str:
    if indicator_id == "ISM_MFG":
        return (
            '<html><body><div id="historical-desc">The ISM Manufacturing PMI rose to 55.5 in October 2026 '
            'compared to 54.5 in September and forecasts of 54.6.</div></body></html>'
        )
    return (
        '<html><body><div id="historical-desc">The ISM Services PMI decreased to 54.5 in October 2026 '
        'compared to 54.9 in September and forecasts of 54.8.</div></body></html>'
    )


def _write_synthetic_history_1d(out_dir: Path, start_year: int = 2020) -> None:
    store: dict[str, dict[str, Any]] = {}
    d0 = date(start_year, 1, 1)
    d1 = date(2026, 11, 20)
    for sym, base in (("NAS100", 18000.0), ("SP500", 5000.0), ("TWII", 22000.0), ("BTCUSDT", 60000.0)):
        bars: list[OHLCVBar] = []
        d = d0
        px = base
        step = 0
        while d <= d1:
            is_trading = (sym == "BTCUSDT") or (d.weekday() < 5)
            if is_trading:
                px = round(px * (1.002 if step % 3 != 0 else 0.997), 2)
                bars.append(
                    OHLCVBar(
                        ts_utc=f"{d.isoformat()}T00:00:00Z",
                        open=px,
                        high=px * 1.01,
                        low=px * 0.99,
                        close=px,
                        volume=1000.0,
                    )
                )
                step += 1
            d += timedelta(days=1)
        store[sym] = {"short_listing": False, "full_fetch_at_utc": "2026-11-20T00:00:00Z", "bars": bars}
    _save_history_1d_store(out_dir, store, "2026-11-20T00:00:00Z")


def test_market_reaction_calculation_exact_conventions() -> None:
    # Build controlled daily bars for 2026-10-26 .. 2026-11-12
    # US: Oct 27=100, Oct 28=103 (+3%), Nov 9=200, Nov 10=196 (-2%)
    # TW: Oct 28=1000, Oct 29=980 (-2%), Nov 10=2000, Nov 11=2060 (+3%)
    # BTC: Oct 27=50000, Oct 28=52500 (+5%), Nov 9=70000, Nov 10=68600 (-2%)
    def mk(rows: list[tuple[str, float]]) -> list[tuple[date, OHLCVBar]]:
        return [
            (
                date.fromisoformat(d),
                OHLCVBar(ts_utc=f"{d}T00:00:00Z", open=c, high=c, low=c, close=c, volume=1.0),
            )
            for d, c in rows
        ]

    bars_map = {
        "NAS100": mk([("2026-10-27", 100.0), ("2026-10-28", 103.0), ("2026-10-29", 104.0), ("2026-11-09", 200.0), ("2026-11-10", 196.0), ("2026-11-11", 198.0)]),
        "SP500": mk([("2026-10-27", 100.0), ("2026-10-28", 101.5), ("2026-10-29", 102.0), ("2026-11-09", 200.0), ("2026-11-10", 198.0), ("2026-11-11", 199.0)]),
        "TWII": mk([("2026-10-28", 1000.0), ("2026-10-29", 980.0), ("2026-11-10", 2000.0), ("2026-11-11", 2060.0)]),
        "BTCUSDT": mk([("2026-10-27", 50000.0), ("2026-10-28", 52500.0), ("2026-10-29", 53000.0), ("2026-11-09", 70000.0), ("2026-11-10", 68600.0), ("2026-11-11", 69000.0)]),
    }

    # Case 1: CPI at 2026-11-10 08:30 EST (= 2026-11-10 21:30 Taipei = 13:30 UTC)
    cpi_dt = datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)
    rx_cpi = compute_market_reactions(cpi_dt, bars_map)
    assert rx_cpi["NAS100"] == -2.0   # Nov 10 vs Nov 9
    assert rx_cpi["SP500"] == -1.0    # Nov 10 vs Nov 9
    assert rx_cpi["TWII"] == 3.0      # Nov 11 (next TW day after 21:30 TPE) vs Nov 10
    assert rx_cpi["BTCUSDT"] == -2.0  # Nov 10 bar (closes Nov 11 00:00Z) vs Nov 9 bar (closed Nov 10 00:00Z)

    # Case 2: FOMC at 2026-10-28 14:00 EDT (= 2026-10-29 02:00 Taipei = 18:00 UTC)
    fomc_dt = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    rx_fomc = compute_market_reactions(fomc_dt, bars_map)
    assert rx_fomc["NAS100"] == 3.0   # Oct 28 US close vs Oct 27
    assert rx_fomc["SP500"] == 1.5    # Oct 28 US close vs Oct 27
    assert rx_fomc["TWII"] == -2.0    # Oct 29 TW close (02:00 TPE < 13:30 TW close) vs Oct 28
    assert rx_fomc["BTCUSDT"] == 5.0  # Oct 28 bar (closes Oct 29 00:00Z) vs Oct 27 bar (closed Oct 28 00:00Z)


def _make_fake_yf_fetcher(fail_all: bool = False) -> Any:
    def _fetch(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
        if fail_all:
            raise ConnectionError("yfinance blocked")
        if symbol == "ZQZ26.CBT":
            return [("2026-11-20", 95.96)]
        if symbol == "ZQG27.CBT":
            return [("2026-11-20", 95.755)]
        if symbol.startswith("ZQ"):
            return [("2026-11-20", 95.80)]
        base_map = {
            "DX-Y.NYB": (101.0, 103.5),
            "TWD=X": (32.2, 32.9),
            "^HSI": (21500.0, 22450.0),
            "GC=F": (3350.0, 3520.0),
            "CL=F": (82.0, 84.0),
            "^TNX": (4.55, 4.92),
        }
        lo, hi = base_map.get(symbol, (100.0, 102.0))
        d0 = date(2025, 11, 20)
        out: list[tuple[str, float]] = []
        for i in range(252):
            d = d0 + timedelta(days=int(i * 1.44))
            val = lo + (hi - lo) * (i / 251.0)
            out.append((d.isoformat(), round(val, 4)))
        return out

    return _fetch


def test_macro_json_generation_and_failure_preservation(tmp_path: Path) -> None:
    _write_synthetic_history_1d(tmp_path)

    def _mock_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "fredgraph.csv?id=" in url:
            sid = url.split("id=")[-1]
            return _FakeResponse(200, _build_synthetic_fred_csv(sid))
        if "business-confidence" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_MFG"))
        if "non-manufacturing-pmi" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_SVC"))
        return _FakeResponse(404, "not found")

    now = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)
    doc = generate_macro_file(
        out_dir=tmp_path,
        http_get=_mock_get,
        now_utc=now,
        use_local_cache=False,
        yf_fetcher=_make_fake_yf_fetcher(),
    )
    assert (tmp_path / "macro.json").exists()
    assert doc["status"] == "ok"
    assert doc["updating_indicators"] == []
    assert len(doc["indicators"]) == 17

    by_id = {c["id"]: c for c in doc["indicators"]}
    assert set(by_id.keys()) == {
        "FED_RATE", "DGS2", "DGS10",
        "CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI",
        "NFP", "UNRATE", "AVG_HOURLY_EARNINGS", "ICSA", "JOLTS",
        "GDP", "ISM_MFG", "ISM_SVC", "RETAIL_SALES",
    }
    assert len(doc["overview"]["asset_impacts"]) == 7
    assert len(doc["fedwatch"]["meetings"]) == 2
    assert len(doc["market_snapshot"]["items"]) == 6
    for cid, card in by_id.items():
        assert card["value_str"], f"{cid} missing value_str"
        assert card["prev_str"], f"{cid} missing prev_str"
        assert card["change_str"], f"{cid} missing change_str"
        assert card["release_date_taipei"], f"{cid} missing release_date_taipei"
        assert len(card["history_rows"]) == 24, f"{cid} expected 24 rows"
        assert card["data_status"] == "ok"
        assert card["updating"] is False

    # DGS2 & CORE_CPI lack sufficient consensus -> fallback to prev grouping
    assert by_id["DGS2"]["history_stats"]["primary_grouping"] == "prev"
    assert by_id["DGS2"]["consensus_str"] is None
    assert by_id["CORE_CPI"]["history_stats"]["primary_grouping"] == "prev"
    assert by_id["CPI"]["history_stats"]["primary_grouping"] == "consensus"

    # Now simulate CPI and ISM_MFG failing on next run -> old data preserved and flagged 資料更新中
    prev_cpi_val = by_id["CPI"]["value_str"]

    def _failing_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "id=CPIAUCSL" in url:
            return _FakeResponse(503, "Service Unavailable")
        if "business-confidence" in url:
            raise ConnectionError("network timeout")
        return _mock_get(url, **kwargs)

    doc2 = generate_macro_file(
        out_dir=tmp_path,
        http_get=_failing_get,
        now_utc=now + timedelta(days=1),
        use_local_cache=False,
        yf_fetcher=_make_fake_yf_fetcher(),
    )
    assert doc2["status"] == "partial"
    assert set(doc2["updating_indicators"]) == {"CPI", "ISM_MFG"}
    by_id2 = {c["id"]: c for c in doc2["indicators"]}
    assert by_id2["CPI"]["data_status"] == "資料更新中"
    assert by_id2["CPI"]["updating"] is True
    assert by_id2["CPI"]["value_str"] == prev_cpi_val
    assert len(by_id2["CPI"]["history_rows"]) == 24
    assert by_id2["ISM_MFG"]["data_status"] == "資料更新中"
    assert by_id2["ISM_MFG"]["updating"] is True
    assert by_id2["NFP"]["data_status"] == "ok"
    assert by_id2["NFP"]["updating"] is False


def test_frontend_macro_html_nav_versions_and_calendar_defaults() -> None:
    index_html = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    macro_html = (ROOT_DIR / "macro.html").read_text(encoding="utf-8")
    style_css = (ROOT_DIR / "assets" / "style.css").read_text(encoding="utf-8")
    macro_js = (ROOT_DIR / "assets" / "macro.js").read_text(encoding="utf-8")
    daily_yml = (ROOT_DIR / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    scan_yml = (ROOT_DIR / ".github" / "workflows" / "scan.yml").read_text(encoding="utf-8")

    # Nav & cache-busting checks
    assert "./macro.html" in index_html
    assert "宏觀數據" in index_html
    assert "?v=20261127b" in index_html
    assert "?v=20261124a" in macro_html
    assert "./assets/style.css?v=20261124a" in macro_html
    assert "./assets/config.js?v=20261124a" in macro_html
    assert "./assets/glossary.js?v=20261124a" in macro_html
    assert "./assets/flow_tw.js?v=20261124a" in macro_html
    assert "./assets/macro.js?v=20261124a" in macro_html
    assert "./assets/glossary.js?v=20261127b" in index_html
    assert "./assets/flow_tw.js?v=20261127b" in index_html
    assert "./index.html#/wall" in macro_html
    assert 'href="/' not in macro_html
    assert 'src="/' not in macro_html
    assert ".macro-ind-card" in style_css
    assert ".macro-cal-past-summary" in style_css
    assert "src.macro" in daily_yml
    assert "src.macro" not in scan_yml
    probe_yml = (ROOT_DIR / ".github" / "workflows" / "macro-probe.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in probe_yml
    assert "src.macro" in probe_yml
    assert "git push" not in probe_yml
    assert "git commit" not in probe_yml

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    script = f"""
        const elements = {{}};
        const stubEl = (id) => {{
          if (!elements[id]) {{
            elements[id] = {{
              id: id,
              classList: {{
                _set: new Set(),
                add(c) {{ this._set.add(c); }},
                remove(c) {{ this._set.delete(c); }},
                toggle(c, force) {{
                  if (force === undefined) {{ if (this._set.has(c)) this._set.delete(c); else this._set.add(c); }}
                  else if (force) this._set.add(c); else this._set.delete(c);
                }},
                contains(c) {{ return this._set.has(c); }},
              }},
              addEventListener: () => {{}},
              setAttribute: () => {{}},
              getAttribute: () => null,
              focus: () => {{}},
              innerHTML: '',
              textContent: '',
              value: '',
              options: [],
              style: {{}},
            }};
          }}
          return elements[id];
        }};
        var window = {{ location: {{ hash: '' }}, addEventListener: () => {{}}, SIGNAL_DESK_CONFIG: {{}} }};
        var document = {{
          getElementById: stubEl,
          querySelectorAll: () => [],
          querySelector: () => stubEl('_q'),
          addEventListener: () => {{}},
          body: {{ appendChild: () => {{}}, removeChild: () => {{}}, style: {{}} }},
          createElement: () => ({{ style: {{}}, setAttribute: () => {{}}, click: () => {{}} }}),
        }};
        var history = {{ replaceState: () => {{}} }};
        function setInterval() {{}}
        function setTimeout() {{ return 1; }}
        function clearTimeout() {{}}
        async function fetch() {{ return {{ ok: false }}; }}
        {macro_js}

        const u = window.__MACRO_DESK_UTILS__;
        const defs = u.getCalendarDefaults();
        if (defs.calRange !== 'week2' || defs.calStars !== '3' || defs.calPastMonth !== 'recent7') {{
          throw new Error('Unexpected calendar defaults: ' + JSON.stringify(defs));
        }}

        // Fixed reference: Saturday 2026-11-21 10:00 Taipei = 2026-11-21T02:00:00Z
        const nowMs = Date.parse('2026-11-21T02:00:00Z');
        const upcoming = [
          {{ id: 'u-nextwk-3', title: 'PCE', indicator_id: 'PCE', stars: 3, time_utc: '2026-11-25T13:30:00Z', time_taipei_iso: '2026-11-25T21:30:00+08:00' }},
          {{ id: 'u-nextwk-2', title: 'Claims', indicator_id: 'ICSA', stars: 2, time_utc: '2026-11-25T13:30:00Z', time_taipei_iso: '2026-11-25T21:30:00+08:00' }},
          {{ id: 'u-later-3', title: 'FOMC Dec', indicator_id: 'FED_RATE', stars: 3, time_utc: '2026-12-09T19:00:00Z', time_taipei_iso: '2026-12-10T03:00:00+08:00' }},
          {{ id: 'u-far-3', title: 'CPI Jan', indicator_id: 'CPI', stars: 3, time_utc: '2027-01-13T13:30:00Z', time_taipei_iso: '2027-01-13T21:30:00+08:00' }},
        ];
        // Default week2 + stars=3: only u-nextwk-3 (excludes u-nextwk-2, u-later-3, u-far-3)
        const defUp = u.filterUpcomingEvents(upcoming, {{ nowMs }});
        if (defUp.length !== 1 || defUp[0].id !== 'u-nextwk-3') {{
          throw new Error('Default week2+3star failed: ' + JSON.stringify(defUp));
        }}
        // Switching to 60d + all stars shows all 4
        const all60 = u.filterUpcomingEvents(upcoming, {{ nowMs, range: '60d', stars: 'all' }});
        if (all60.length !== 4) {{
          throw new Error('60d+all failed: ' + all60.length);
        }}

        const past = [
          {{ id: 'p-3d', title: 'Retail', indicator_id: 'RETAIL_SALES', stars: 3, time_utc: '2026-11-17T13:30:00Z', time_taipei: '2026-11-17 21:30' }},
          {{ id: 'p-2d-2star', title: 'Claims', indicator_id: 'ICSA', stars: 2, time_utc: '2026-11-19T13:30:00Z', time_taipei: '2026-11-19 21:30' }},
          {{ id: 'p-20d', title: 'FOMC Oct', indicator_id: 'FED_RATE', stars: 3, time_utc: '2026-10-28T18:00:00Z', time_taipei: '2026-10-29 02:00' }},
        ];
        // Default recent7 + stars=3 -> only p-3d
        const defPast = u.filterPastEvents(past, {{ nowMs }});
        if (defPast.length !== 1 || defPast[0].id !== 'p-3d') {{
          throw new Error('Default recent7+3star failed: ' + JSON.stringify(defPast));
        }}
        // Month selector 2026-10 -> shows p-20d
        const octPast = u.filterPastEvents(past, {{ nowMs, pastMonth: '2026-10', stars: '3' }});
        if (octPast.length !== 1 || octPast[0].id !== 'p-20d') {{
          throw new Error('Month selector 2026-10 failed: ' + JSON.stringify(octPast));
        }}

        // Verify card HTML & modal HTML with missing consensus + updating flag
        const testCard = {{
          id: 'DGS2', name: '2 年期美債殖利率', code: 'DGS2', period: '2026-11-19',
          value_str: '4.76%', prev_str: '4.80%', change_str: '-0.04 個百分點', change_dir: 'down',
          release_date_taipei: '2026-11-20 04:30', sub_info: '每日美債',
          consensus_str: null, surprise: '低於前值', surprise_basis: 'prev',
          verdict: 'bear', verdict_text: '🔴 偏高承壓', chart_values: [4.5, 4.76], chart_label: '近24月',
          why: '說明', source: 'U.S. Treasury Daily Par Yield Curve（備用）', source_used: '美國財政部（備用）',
          updating: true, updating_notice: '來源失敗',
          history_rows: [{{
            period: '2026-11-19', period_zh: '2026-11-19', release_taipei: '2026-11-20 04:30',
            actual_str: '4.76%', prev_str: '4.80%', consensus_str: null, surprise: '低於前值',
            reactions: {{ NAS100: -0.5, SP500: -0.3, TWII: 0.4, BTCUSDT: 1.2 }},
          }}],
          history_stats: {{
            primary_grouping: 'prev', consensus_rows_count: 0, total_rows_count: 1,
            by_prev: [{{ group: '低於前值', n_rows: 1, assets: {{ NAS100: {{ label: '那指 100', n: 1, avg_pct: -0.5, up_prob_pct: 0 }} }} }}],
          }},
        }};
        const cHtml = u.renderIndicatorCardHtml(testCard);
        if (!cHtml.includes('📊 過去公布後市場怎麼走') || !cHtml.includes('資料更新中') || !cHtml.includes('無可靠預期值') || !cHtml.includes('來源：美國財政部（備用）')) {{
          throw new Error('Card HTML missing expected markers: ' + cHtml);
        }}
        const mHtml = u.buildHistModalBodyHtml(testCard, {{}});
        if (!mHtml.includes('無可靠預期值') || !mHtml.includes('高於／低於前值') || !mHtml.includes('-0.50%')) {{
          throw new Error('Modal HTML missing expected markers: ' + mHtml);
        }}
    """
    proc = run_js(script, js_bin=js_bin)
    assert proc.returncode == 0, f"Macro JS check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_real_data_macro_json_and_calendar_yaml() -> None:
    cal = load_macro_calendar()
    assert len((cal.get("fomc") or {}).get("past_decisions") or []) == 24
    assert len((cal.get("fomc") or {}).get("upcoming") or []) >= 8

    macro_path = ROOT_DIR / "data" / "macro.json"
    assert macro_path.exists(), "Expected committed data/macro.json"
    doc = json.loads(macro_path.read_text(encoding="utf-8"))
    assert len(doc["indicators"]) == 17
    assert len(doc["overview"]["asset_impacts"]) == 7
    assert "fedwatch" in doc and "market_snapshot" in doc
    assert len(doc["calendar"]["upcoming"]) >= 20
    assert len(doc["calendar"]["past"]) >= 50
    for c in doc["indicators"]:
        assert c["value_str"] and c["prev_str"] and c["change_str"] and c["release_date_taipei"]
        assert 12 <= len(c["history_rows"]) <= 24


def _build_synthetic_bls_json() -> str:
    months: list[tuple[int, int]] = []
    y, m = 2023, 1
    while (y, m) <= (2026, 10):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1

    series_defs = {
        "CUSR0000SA0": lambda i: f"{300.0 + i * 0.85:.3f}",
        "CUSR0000SA0L1E": lambda i: f"{305.0 + i * 0.78:.3f}",
        "WPSFD4": lambda i: f"{140.0 + i * 0.45:.3f}",
        "CES0000000001": lambda i: f"{154000 + i * 120}",
        "LNS14000000": lambda i: f"{3.9 if i % 2 == 0 else 4.1:.1f}",
        "CES0500000003": lambda i: f"{34.00 + i * 0.09:.2f}",
        "JTS000000000000000JOL": lambda i: f"{7400 + (i % 4) * 150}",
    }
    series_list = []
    for sid, fn in series_defs.items():
        data_rows = []
        for idx, (yr, mo) in enumerate(months):
            if sid == "JTS000000000000000JOL" and (yr, mo) > (2026, 9):
                continue
            data_rows.append({"year": str(yr), "period": f"M{mo:02d}", "value": fn(idx)})
        series_list.append({"seriesID": sid, "data": list(reversed(data_rows))})
    return json.dumps({"status": "REQUEST_SUCCEEDED", "Results": {"series": series_list}})


def _build_synthetic_treasury_csv() -> str:
    lines = ['Date,"1 Mo","2 Yr","10 Yr"']
    d = date(2026, 9, 1)
    idx = 0
    while d <= date(2026, 11, 20):
        if d.weekday() < 5:
            y2 = 4.50 + idx * 0.01
            y10 = 4.70 + idx * 0.01
            lines.append(f"{d.month:02d}/{d.day:02d}/{d.year},4.20,{y2:.2f},{y10:.2f}")
            idx += 1
        d += timedelta(days=1)
    return "\n".join(lines)


def _build_synthetic_census_retail() -> str:
    lines = [
        "RETAIL & FOOD SERVICES",
        "",
        "YEAR      JAN       FEB       MAR       APR       MAY       JUN       JUL       AUG       SEP       OCT       NOV       DEC",
    ]
    base = 650000
    for yr in range(2024, 2027):
        cols = []
        max_m = 10 if yr == 2026 else 12
        for mo in range(1, max_m + 1):
            base += 1800
            cols.append(str(base))
        lines.append(f"{yr}   " + "   ".join(cols))
    lines.append("")
    lines.append("SEASONAL FACTORS")
    lines.append("2026   0.910   0.879   1.010   1.000   1.044   1.009   1.028   1.016   0.977   1.019   0.998   1.109")
    return "\n".join(lines)


def _build_synthetic_dol_xml() -> str:
    parts = ['<r539cyNational rundate="11/20/2026">']
    d = date(2024, 1, 6)
    idx = 0
    while d <= date(2026, 10, 24):
        sa = 195000 + (idx % 5) * 2000
        parts.append(
            f"<week><weekEnded>{d.strftime('%m/%d/%Y')}</weekEnded>"
            f"<InitialClaims><SA>{sa:,}</SA></InitialClaims></week>"
        )
        d += timedelta(days=7)
        idx += 1
    parts.append(
        "<week><weekEnded>11/14/2026</weekEnded>"
        "<InitialClaims><SA>&#160;</SA></InitialClaims></week>"
    )
    parts.append("</r539cyNational>")
    return "\n".join(parts)


def _build_synthetic_te_icsa_html() -> str:
    return """
    <html><head>
      <meta name="description" content="Initial Jobless Claims in the United States increased to 203 thousand in the week ending November 14 of 2026 from 202 thousand in the previous week." />
    </head><body>
      <table>
        <tr><th>Calendar</th><th>GMT</th><th>Name</th><th>Reference</th><th>Actual</th><th>Previous</th><th>Consensus</th></tr>
        <tr><td>2026-11-12</td><td>01:30 PM</td><td>Initial Jobless Claims</td><td>Nov/07</td><td>200K</td><td>191K</td><td>195K</td></tr>
        <tr><td>2026-11-19</td><td>01:30 PM</td><td>Initial Jobless Claims</td><td>Nov/14</td><td>203K</td><td>202K</td><td>197K</td></tr>
      </table>
    </body></html>
    """


def test_fred_timeout_triggers_fallback_without_retrying_remaining_fred(tmp_path: Path) -> None:
    _write_synthetic_history_1d(tmp_path)
    fred_calls: list[str] = []
    seen_timeouts: list[int] = []

    def _timeout_fred_handler(url: str, **kwargs: Any) -> _FakeResponse:
        if "timeout" in kwargs:
            seen_timeouts.append(int(kwargs["timeout"]))
        if "fredgraph.csv" in url:
            sid = url.split("id=")[-1]
            fred_calls.append(sid)
            raise TimeoutError("HTTPSConnectionPool: Read timed out. (read timeout=8)")
        if "api.bls.gov/publicAPI/v1/timeseries/data" in url:
            return _FakeResponse(200, _build_synthetic_bls_json())
        if "home.treasury.gov" in url:
            return _FakeResponse(200, _build_synthetic_treasury_csv())
        if "api.db.nomics.world/v22/series/BEA/NIPA-T20804/DPCERG-M" in url:
            periods = [f"{yr:04d}-{mo:02d}" for yr in range(2023, 2027) for mo in range(1, 13) if (yr, mo) <= (2026, 9)]
            vals = [round(120.0 + i * 0.28, 3) for i in range(len(periods))]
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": periods, "value": vals}]}}))
        if "api.db.nomics.world/v22/series/BEA/NIPA-T20804/DPCCRG-M" in url:
            periods = [f"{yr:04d}-{mo:02d}" for yr in range(2023, 2027) for mo in range(1, 13) if (yr, mo) <= (2026, 9)]
            vals = [round(118.0 + i * 0.25, 3) for i in range(len(periods))]
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": periods, "value": vals}]}}))
        if "api.db.nomics.world/v22/series/BEA/NIPA-T10101/A191RL-Q" in url:
            periods = [f"{yr:04d}-Q{q}" for yr in range(2020, 2027) for q in range(1, 5) if (yr, q) <= (2026, 3)]
            vals = [2.2 if i % 2 == 0 else 3.4 for i in range(len(periods))]
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": periods, "value": vals}]}}))
        if "api.db.nomics.world/v22/series/FED/H15/" in url:
            return _FakeResponse(404, "optional backfill unavailable")
        if "adv44X72.txt" in url:
            return _FakeResponse(200, _build_synthetic_census_retail())
        if "wkclaims/report.asp" in url:
            return _FakeResponse(200, _build_synthetic_dol_xml())
        if "jobless-claims" in url:
            return _FakeResponse(200, _build_synthetic_te_icsa_html())
        if "business-confidence" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_MFG"))
        if "non-manufacturing-pmi" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_SVC"))
        return _FakeResponse(404, "not found")

    now = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)
    doc = generate_macro_file(
        out_dir=tmp_path,
        http_get=_timeout_fred_handler,
        now_utc=now,
        use_local_cache=False,
        yf_fetcher=_make_fake_yf_fetcher(),
    )

    # First FRED series timed out (retried at most once = 2 attempts), zero calls to remaining 15 FRED series
    assert fred_calls == ["DFEDTARU", "DFEDTARU"]
    assert all(t <= 10 for t in seen_timeouts)
    assert doc["status"] == "ok"
    assert doc["updating_indicators"] == []
    assert len(doc["indicators"]) == 17
    assert doc["fetch_report"]["fred_aborted"] is True
    assert doc["fetch_report"]["sources"]["FRED"]["status"] == "failed"
    assert doc["fetch_report"]["sources"]["FRED"]["attempts"] == 1
    assert len(doc["fetch_report"]["sources"]["FRED"]["series_skipped"]) == 15

    by_id = {c["id"]: c for c in doc["indicators"]}
    assert by_id["FED_RATE"]["source_used"] == "FOMC 決議紀錄（備用）"
    assert by_id["DGS2"]["source_used"] == "美國財政部（備用）"
    assert by_id["DGS10"]["source_used"] == "美國財政部（備用）"
    assert by_id["CPI"]["source_used"] == "BLS（備用）"
    assert by_id["NFP"]["source_used"] == "BLS（備用）"
    assert by_id["JOLTS"]["source_used"] == "BLS（備用）"
    assert by_id["PCE"]["source_used"] == "DBnomics BEA（備用）"
    assert by_id["GDP"]["source_used"] == "DBnomics BEA（備用）"
    assert by_id["RETAIL_SALES"]["source_used"] == "美國普查局 Census（備用）"
    assert by_id["ICSA"]["source_used"] == "DOL / TradingEconomics（備用）"
    assert by_id["ICSA"]["value_str"] == "20.3 萬人"
    assert by_id["ICSA"]["prev_str"] == "20.2 萬人"
    assert by_id["ISM_MFG"]["source_used"] == "TradingEconomics ＋ ISM"


def test_fallback_conversion_matches_fred_conventions(tmp_path: Path) -> None:
    """Fallback index/level/CSV conversions must produce identical indicator formatting to FRED CSV."""
    _write_synthetic_history_1d(tmp_path)

    # Run A: FRED primary using synthetic CSV
    def _fred_only(url: str, **kwargs: Any) -> _FakeResponse:
        if "fredgraph.csv?id=" in url:
            sid = url.split("id=")[-1]
            return _FakeResponse(200, _build_synthetic_fred_csv(sid))
        if "business-confidence" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_MFG"))
        if "non-manufacturing-pmi" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_SVC"))
        return _FakeResponse(404, "not found")

    dir_a = tmp_path / "run_fred"
    _write_synthetic_history_1d(dir_a)
    now = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)
    doc_a = generate_macro_file(out_dir=dir_a, http_get=_fred_only, now_utc=now, use_local_cache=False, yf_fetcher=_make_fake_yf_fetcher())

    # Extract exact FRED observations from synthetic CSVs to mirror them inside fallback payloads
    def _csv_to_ym_vals(sid: str) -> list[tuple[str, float]]:
        rows = []
        for ln in _build_synthetic_fred_csv(sid).splitlines()[1:]:
            if "," not in ln:
                continue
            d_s, v_s = ln.split(",", 1)
            d_s, v_s = d_s.strip(), v_s.strip()
            if not d_s or not v_s or v_s == ".":
                continue
            rows.append((d_s, float(v_s)))
        return rows

    bls_payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": []}}
    for ind_id, fred_sid, bls_sid in (
        ("CPI", "CPIAUCSL", "CUSR0000SA0"),
        ("CORE_CPI", "CPILFESL", "CUSR0000SA0L1E"),
        ("PPI", "PPIFIS", "WPSFD4"),
        ("NFP", "PAYEMS", "CES0000000001"),
        ("UNRATE", "UNRATE", "LNS14000000"),
        ("AVG_HOURLY_EARNINGS", "CES0500000003", "CES0500000003"),
        ("JOLTS", "JTSJOL", "JTS000000000000000JOL"),
    ):
        data_rows = []
        for d_s, val in _csv_to_ym_vals(fred_sid):
            yr, mo = d_s[:4], d_s[5:7]
            data_rows.append({"year": yr, "period": f"M{mo}", "value": str(val)})
        bls_payload["Results"]["series"].append({"seriesID": bls_sid, "data": list(reversed(data_rows))})

    dgs2_map = dict(_csv_to_ym_vals("DGS2"))
    dgs10_map = dict(_csv_to_ym_vals("DGS10"))
    tr_lines = ['Date,"2 Yr","10 Yr"']
    for d_s in sorted(dgs2_map.keys()):
        yr, mo, dy = d_s.split("-")
        tr_lines.append(f"{mo}/{dy}/{yr},{dgs2_map[d_s]:.2f},{dgs10_map[d_s]:.2f}")
    tr_csv = "\n".join(tr_lines)

    pce_rows = _csv_to_ym_vals("PCEPI")
    cpce_rows = _csv_to_ym_vals("PCEPILFE")
    gdp_rows = _csv_to_ym_vals("A191RL1Q225SBEA")
    rs_rows = _csv_to_ym_vals("RSAFS")

    rs_by_yr: dict[int, list[str]] = {}
    for d_s, v in rs_rows:
        rs_by_yr.setdefault(int(d_s[:4]), []).append(f"{v:.4f}")
    rs_txt = "\n".join(
        ["RETAIL & FOOD SERVICES", "YEAR JAN FEB"] + [f"{yr} " + " ".join(vals) for yr, vals in sorted(rs_by_yr.items())]
    )

    icsa_rows = _csv_to_ym_vals("ICSA")
    xml_parts = ['<r539cyNational rundate="11/20/2026">']
    for d_s, v in icsa_rows:
        yr, mo, dy = d_s.split("-")
        xml_parts.append(f"<week><weekEnded>{mo}/{dy}/{yr}</weekEnded><InitialClaims><SA>{v:.0f}</SA></InitialClaims></week>")
    xml_parts.append("</r539cyNational>")

    def _fallback_identical(url: str, **kwargs: Any) -> _FakeResponse:
        if "fredgraph.csv" in url:
            raise ConnectionError("FRED blocked")
        if "api.bls.gov" in url:
            return _FakeResponse(200, json.dumps(bls_payload))
        if "home.treasury.gov" in url:
            return _FakeResponse(200, tr_csv)
        if "FED/H15/" in url:
            return _FakeResponse(404, "none")
        if "DPCERG-M" in url:
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": [d[:7] for d, _ in pce_rows], "value": [v for _, v in pce_rows]}]}}))
        if "DPCCRG-M" in url:
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": [d[:7] for d, _ in cpce_rows], "value": [v for _, v in cpce_rows]}]}}))
        if "A191RL-Q" in url:
            q_p = [f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}" for d, _ in gdp_rows]
            return _FakeResponse(200, json.dumps({"series": {"docs": [{"period": q_p, "value": [v for _, v in gdp_rows]}]}}))
        if "adv44X72.txt" in url:
            return _FakeResponse(200, rs_txt)
        if "wkclaims/report.asp" in url:
            return _FakeResponse(200, "\n".join(xml_parts))
        if "jobless-claims" in url:
            return _FakeResponse(404, "skip te")
        if "business-confidence" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_MFG"))
        if "non-manufacturing-pmi" in url:
            return _FakeResponse(200, _build_synthetic_te_html("ISM_SVC"))
        return _FakeResponse(404, "not found")

    dir_b = tmp_path / "run_fb"
    _write_synthetic_history_1d(dir_b)
    doc_b = generate_macro_file(out_dir=dir_b, http_get=_fallback_identical, now_utc=now, use_local_cache=False, yf_fetcher=_make_fake_yf_fetcher())
    assert doc_b["status"] == "ok"

    by_a = {c["id"]: c for c in doc_a["indicators"]}
    by_b = {c["id"]: c for c in doc_b["indicators"]}
    for ind_id in ("DGS2", "DGS10", "CPI", "CORE_CPI", "PCE", "CORE_PCE", "PPI", "NFP", "UNRATE", "AVG_HOURLY_EARNINGS", "ICSA", "JOLTS", "GDP", "RETAIL_SALES"):
        assert by_b[ind_id]["value_str"] == by_a[ind_id]["value_str"], f"{ind_id} value mismatch"
        assert by_b[ind_id]["prev_str"] == by_a[ind_id]["prev_str"], f"{ind_id} prev mismatch"
        assert by_b[ind_id]["change_str"] == by_a[ind_id]["change_str"], f"{ind_id} change mismatch"


def test_fedwatch_algorithm_cut_eom_chaining_and_manual_fallback() -> None:
    # 1) +0.93 hikes scenario on Dec 9, 2026 (31 days: 9 before, 22 after; r_pre=3.875)
    # r_post = 3.875 + 0.93*0.25 = 4.1075 -> implied_avg = (9*3.875 + 22*4.1075)/31 = 4.04 -> P = 95.96
    m_dec = {"period": "2026-12", "title": "FOMC 利率決議", "release_et": "2026-12-09T14:00:00"}
    m_jan = {"period": "2027-01", "title": "FOMC 利率決議", "release_et": "2027-01-27T14:00:00"}
    fw_hike = compute_fedwatch_from_prices(
        [m_dec, m_jan],
        current_low=3.75,
        current_high=4.00,
        prices_by_symbol={"ZQZ26.CBT": 95.96, "ZQG27.CBT": 95.755},
        as_of_taipei="2026-11-21 15:00",
    )
    dec_res, jan_res = fw_hike["meetings"]
    assert dec_res["contract_symbol"] == "ZQZ26.CBT"
    assert dec_res["used_next_month_contract"] is False
    assert dec_res["probabilities"]["hike_1"] == 93.0
    assert dec_res["probabilities"]["hold"] == 7.0
    assert "升 1 碼 93%｜維持 7%" in dec_res["summary_text"]

    # Jan 27 is within last 7 days of Jan -> must use Feb contract ZQG27.CBT!
    assert jan_res["contract_symbol"] == "ZQG27.CBT"
    assert jan_res["used_next_month_contract"] is True
    assert jan_res["implied_rate"] == 4.245  # 100 - 95.755
    assert jan_res["probabilities"]["hike_1"] == 52.0
    assert jan_res["probabilities"]["hike_2_plus"] == 48.0

    # 2) Rate cut scenario: expected -0.60 cuts -> cut_1=60%, hold=40%
    # r_post = 3.875 - 0.60*0.25 = 3.725 -> implied_avg = (9*3.875 + 22*3.725)/31 = 3.768548 -> P = 96.231452
    fw_cut = compute_fedwatch_from_prices(
        [m_dec],
        current_low=3.75,
        current_high=4.00,
        prices_by_symbol={"ZQZ26.CBT": 96.2314516},
        as_of_taipei="2026-11-21 15:00",
    )
    assert fw_cut["meetings"][0]["probabilities"]["cut_1"] == 60.0
    assert fw_cut["meetings"][0]["probabilities"]["hold"] == 40.0

    # 3) Sequential chaining across 2 non-EOM meetings (e.g. Dec 9 + Mar 17)
    m_mar = {"period": "2027-03", "title": "FOMC 利率決議", "release_et": "2027-03-17T14:00:00"}
    # Dec r_post = 4.1075; Mar (31 days: 17 before at 4.1075, 14 after at 4.3575) -> avg = (17*4.1075 + 14*4.3575)/31 = 4.220403 -> P = 95.779597
    fw_chain = compute_fedwatch_from_prices(
        [m_dec, m_mar],
        current_low=3.75,
        current_high=4.00,
        prices_by_symbol={"ZQZ26.CBT": 95.96, "ZQH27.CBT": 95.7795968},
        as_of_taipei="2026-11-21 15:00",
    )
    assert abs(fw_chain["meetings"][1]["implied_rate"] - 4.358) <= 0.001

    # 4) Manual fallback (<7d) and expired manual fallback (>7d preserves prev_doc)
    cal = load_macro_calendar()
    cal["fedwatch_manual"] = {
        "as_of_date": "2026-11-20",
        "source": "CME FedWatch 人工抄錄",
        "meetings": [{
            "date": "2026-12-09",
            "implied_rate": 4.10,
            "probabilities": {"cut_2_plus": 0, "cut_1": 0, "hold": 10, "hike_1": 90, "hike_2_plus": 0},
        }],
    }
    now_ok = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
    fw_man, rep_man = build_fedwatch_data(cal, now_ok, {}, yf_fetcher=_make_fake_yf_fetcher(fail_all=True))
    assert rep_man["status"] == "ok"
    assert "手動更新" in fw_man["source_used"]
    assert fw_man["meetings"][0]["probabilities"]["hike_1"] == 90.0

    # Expired manual data (>7 days old) -> preserve prev_doc and flag 資料更新中
    now_exp = datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)
    fw_pres, rep_pres = build_fedwatch_data(cal, now_exp, {"fedwatch": fw_man}, yf_fetcher=_make_fake_yf_fetcher(fail_all=True))
    assert rep_pres["status"] == "failed"
    assert fw_pres["data_status"] == "資料更新中"
    assert fw_pres["updating"] is True
    assert fw_pres["meetings"][0]["probabilities"]["hike_1"] == 90.0


def test_market_snapshot_metrics_verdicts_and_preservation() -> None:
    cal = load_macro_calendar()
    now = datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc)

    def _mock_yf(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
        d0 = date(2025, 11, 20)
        out: list[tuple[str, float]] = []
        for i in range(252):
            d = (d0 + timedelta(days=int(i * 1.44))).isoformat()
            if symbol == "DX-Y.NYB":
                # +2.5% in last 20d -> bear (strong USD)
                val = 100.0 if i < 231 else 100.0 + (i - 230) * 0.12
            elif symbol == "TWD=X":
                # -1.8% in last 20d -> bull (TWD appreciates)
                val = 33.0 if i < 231 else 33.0 - (i - 230) * 0.03
            elif symbol == "^HSI":
                # +4.2% in last 20d -> bull (亞股轉強)
                val = 22000.0 if i < 231 else 22000.0 + (i - 230) * 44.0
            elif symbol == "GC=F":
                val = 3200.0 + i * 1.2
            elif symbol == "CL=F":
                val = 80.0
            else:  # ^TNX
                val = 4.60 if i < 231 else 4.60 + (i - 230) * 0.015  # +0.315pp -> bear
            out.append((d, round(val, 4)))
        return out

    snap, rep = build_market_snapshot(cal, None, None, now, {}, yf_fetcher=_mock_yf)
    assert rep["status"] == "ok"
    assert [it["id"] for it in snap["items"]] == ["DXY", "USDTWD", "HSI", "GOLD", "WTI", "US10Y"]
    by_id = {it["id"]: it for it in snap["items"]}
    assert by_id["DXY"]["verdict"] == "bear"
    assert by_id["USDTWD"]["verdict"] == "bull"
    assert by_id["HSI"]["verdict"] == "bull"
    assert "亞股轉強" in by_id["HSI"]["verdict_tag"]
    assert by_id["US10Y"]["verdict"] == "bear"
    assert len(by_id["DXY"]["closes_90d"]) == 90
    assert by_id["DXY"]["range_1y_pos_pct"] == 100.0

    # Verify HSI bear (< -3%) and neutral (within ±3%) thresholds
    def _mock_hsi_bear(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
        if symbol == "^HSI":
            d0 = date(2025, 11, 20)
            return [((d0 + timedelta(days=int(i * 1.44))).isoformat(), 22000.0 if i < 231 else 22000.0 - (i - 230) * 44.0) for i in range(252)]
        return _mock_yf(symbol, period)

    snap_bear, _ = build_market_snapshot(cal, None, None, now, {}, yf_fetcher=_mock_hsi_bear)
    hsi_bear = next(it for it in snap_bear["items"] if it["id"] == "HSI")
    assert hsi_bear["verdict"] == "bear"
    assert "亞股轉弱" in hsi_bear["verdict_tag"]

    def _mock_hsi_neut(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
        if symbol == "^HSI":
            d0 = date(2025, 11, 20)
            return [((d0 + timedelta(days=int(i * 1.44))).isoformat(), 22000.0 if i < 231 else 22000.0 + (i - 230) * 10.0) for i in range(252)]
        return _mock_yf(symbol, period)

    snap_neut, _ = build_market_snapshot(cal, None, None, now, {}, yf_fetcher=_mock_hsi_neut)
    hsi_neut = next(it for it in snap_neut["items"] if it["id"] == "HSI")
    assert hsi_neut["verdict"] == "neut"
    assert "中性" in hsi_neut["verdict_tag"]

    # Simulate partial failure -> DXY and HSI fail on next run, preserved with 資料更新中
    def _fail_dxy_and_hsi(symbol: str, period: str = "1y") -> list[tuple[str, float]]:
        if symbol in ("DX-Y.NYB", "^HSI"):
            raise TimeoutError("yahoo timeout")
        return _mock_yf(symbol, period)

    snap2, rep2 = build_market_snapshot(cal, None, None, now + timedelta(days=1), {"market_snapshot": snap}, yf_fetcher=_fail_dxy_and_hsi)
    assert rep2["status"] == "partial"
    by_id2 = {it["id"]: it for it in snap2["items"]}
    assert by_id2["DXY"]["data_status"] == "資料更新中"
    assert by_id2["DXY"]["updating"] is True
    assert by_id2["DXY"]["last"] == by_id["DXY"]["last"]
    assert by_id2["HSI"]["data_status"] == "資料更新中"
    assert by_id2["HSI"]["updating"] is True
    assert by_id2["HSI"]["last"] == by_id["HSI"]["last"]
    assert by_id2["USDTWD"]["updating"] is False


def test_jolts_conversion_verdicts_and_blackout_dates() -> None:
    cal = load_macro_calendar()
    # JOLTS verdicts: tight (+0.3M vs prev) -> bear; moderate cool (-0.3M) -> bull; sharp drop (-0.8M) -> 留意景氣放緩
    v_tight, t_tight = _evaluate_verdict("JOLTS", 7.8, None, 7.5, cal_cfg=cal)
    assert v_tight == "bear"
    v_cool, t_cool = _evaluate_verdict("JOLTS", 7.2, None, 7.5, cal_cfg=cal)
    assert v_cool == "bull" and "留意景氣放緩" not in t_cool
    v_sharp, t_sharp = _evaluate_verdict("JOLTS", 6.7, None, 7.5, cal_cfg=cal)
    assert v_sharp == "bull" and "留意景氣放緩" in t_sharp

    # Fed Blackout period check for 3 real FOMC meetings (2nd Saturday before meeting starts)
    assert compute_fomc_blackout_start(date(2026, 12, 9)) == date(2026, 11, 28)
    assert compute_fomc_blackout_start(date(2027, 1, 27)) == date(2027, 1, 16)
    assert compute_fomc_blackout_start(date(2027, 3, 17)) == date(2027, 3, 6)


def test_overview_7_assets_insufficient_data_and_legacy_json_frontend() -> None:
    # Missing data -> 7 assets present with 資料不足，暫不判斷 & gray class 'na'
    ov_empty = _build_overview_summary({}, fedwatch=None, market_snapshot=None)
    assert len(ov_empty["asset_impacts"]) == 7
    assert [x["asset"] for x in ov_empty["asset_impacts"]] == ["美股", "台股", "加密貨幣", "美債", "黃金", "原油", "美元／台幣"]
    for row in ov_empty["asset_impacts"]:
        assert row["cls"] == "na"
        assert row["tag"] == "資料不足"
        assert row["note"] == "資料不足，暫不判斷"

    # Frontend tests via jsc: glossary rules + legacy macro.json compatibility
    js_bin = shutil.which("node") or "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
    glossary_js = (ROOT_DIR / "assets" / "glossary.js").read_text(encoding="utf-8")
    macro_js = (ROOT_DIR / "assets" / "macro.js").read_text(encoding="utf-8")

    js_script = f"""
        const elements = {{}};
        const stubEl = (id) => {{
          if (!elements[id]) {{
            elements[id] = {{
              id, classList: {{ _s: new Set(), add(c){{this._s.add(c);}}, remove(c){{this._s.delete(c);}}, toggle(c,f){{if(f===undefined){{if(this._s.has(c))this._s.delete(c);else this._s.add(c);}}else if(f)this._s.add(c);else this._s.delete(c);}}, contains(c){{return this._s.has(c);}} }},
              addEventListener: () => {{}}, setAttribute: () => {{}}, getAttribute: () => null, removeAttribute: () => {{}},
              focus: () => {{}}, innerHTML: '', textContent: '', value: '', options: [], style: {{}}, querySelectorAll: () => [],
            }};
          }}
          return elements[id];
        }};
        var window = {{ location: {{ hash: '' }}, addEventListener: () => {{}}, SIGNAL_DESK_CONFIG: {{}}, localStorage: {{ _m: {{}}, getItem(k){{return this._m[k]??null;}}, setItem(k,v){{this._m[k]=String(v);}} }} }};
        var document = {{ getElementById: stubEl, querySelectorAll: () => [], querySelector: () => stubEl('_q'), addEventListener: () => {{}}, body: {{ appendChild: () => {{}}, removeChild: () => {{}}, style: {{}} }}, createElement: () => ({{ style: {{}}, setAttribute: () => {{}}, click: () => {{}} }}) }};
        var history = {{ replaceState: () => {{}} }};
        function setInterval() {{}} function setTimeout() {{ return 1; }} function clearTimeout() {{}}
        async function fetch() {{ return {{ ok: false }}; }}
        {glossary_js}
        {macro_js}

        const g = window.SIGNAL_DESK_GLOSSARY;
        if (!g || g.TERMS.length < 40) throw new Error('Expected >= 40 glossary terms, got ' + (g && g.TERMS.length));
        const required = [
          '聯準會', 'FOMC', '鷹派', '鴿派', '升息／降息', '碼（基點）', '點陣圖', '褐皮書', '會議紀要', '靜默期',
          'FedWatch', '聯邦基金利率', 'CPI', '核心 CPI', 'PCE', '核心 PCE', 'PPI', '非農就業', '失業率', '初領失業金',
          'JOLTS', 'ISM 製造業', 'ISM 服務業', '零售銷售', 'GDP', '殖利率', '殖利率曲線倒掛', '美元指數', '台幣升值／貶值',
          '外資買超／賣超', 'ETF 淨流入', '資金費率', '未平倉量（OI）', '爆倉', '穩定幣', 'TVL', '利多／利空',
          '意外值（實際減預期）', '風險偏好（risk-on／risk-off）', '流動性', '恒生指數'
        ];
        const hsiTerm = g.TERMS.find(t => t.term === '恒生指數');
        if (!hsiTerm || hsiTerm.category !== '市場') {{
          throw new Error('Missing or wrong category for 恒生指數 in glossary');
        }}
        const termSet = new Set(g.TERMS.map(t => t.term));
        for (const r of required) {{
          if (!termSet.has(r)) throw new Error('Missing mandatory glossary term: ' + r);
        }}

        // Same term appears twice in one block -> marked once; 5 distinct terms -> capped at 3; original text untouched
        const used = new Set();
        const sampleTitle = '聯準會 FOMC 紀要偏鷹：聯準會重申緊縮，CPI 與 PCE 與 GDP 牽動流動性 123.45%';
        const hits = g.findTermMatchesInText(sampleTitle, used, 3);
        if (hits.length !== 3) throw new Error('Expected max 3 hits per block, got ' + hits.length);
        const fedHits = hits.filter(h => h.id === 'fed');
        if (fedHits.length !== 1) throw new Error('Expected 聯準會 matched only once per block, got ' + fedHits.length);

        // Legacy macro.json without fedwatch / market_snapshot / JOLTS must render without throwing
        const legacyDoc = {{
          version: 1,
          updated_at_taipei: '2026-11-21 15:00',
          overview: {{ stance: '偏鷹', stance_cls: 'bear', stance_desc: '說明。', asset_impacts: [{{ asset: '美股', cls: 'bear', tag: '偏利空', note: '承壓' }}] }},
          indicators: [],
          calendar: {{ upcoming: [], past: [] }},
        }};
        window.__MACRO_DESK_UTILS__.setMacroDataForTest(legacyDoc);
        if (!elements['macro-snapshot-grid'].innerHTML.includes('資料更新中')) {{
          throw new Error('Legacy macro.json should show 資料更新中 for missing market_snapshot');
        }}
        const fwText = elements['macro-fedwatch-summary'].textContent || elements['macro-fedwatch-summary'].innerHTML;
        if (!fwText.includes('資料更新中')) {{
          throw new Error('Legacy macro.json should show 資料更新中 for missing fedwatch');
        }}
        // Legacy 5-item market_snapshot without HSI should still render 6 cards with HSI showing 資料更新中
        const legacySnapDoc = {{
          ...legacyDoc,
          market_snapshot: {{
            hhmm_taipei: '15:00',
            items: [
              {{ id: 'DXY', name_zh: '美元指數', last: 106.5, last_str: '106.50', chg_1d_pct: 0.1, verdict: 'neut', verdict_tag: '🟡 中性' }},
              {{ id: 'USDTWD', name_zh: '美元／台幣', last: 32.5, last_str: '32.50', chg_1d_pct: 0.0, verdict: 'neut', verdict_tag: '🟡 中性' }},
              {{ id: 'GOLD', name_zh: '黃金', last: 2700.0, last_str: '2,700.0', chg_1d_pct: 0.2, verdict: 'neut', verdict_tag: '🟡 中性' }},
              {{ id: 'WTI', name_zh: '原油 (WTI)', last: 70.0, last_str: '70.00', chg_1d_pct: -0.3, verdict: 'neut', verdict_tag: '🟡 中性' }},
              {{ id: 'US10Y', name_zh: '美國 10 年期殖利率', last: 4.4, last_str: '4.40%', chg_1d_pp: 0.01, verdict: 'neut', verdict_tag: '🟡 中性' }},
            ],
          }},
        }};
        window.__MACRO_DESK_UTILS__.setMacroDataForTest(legacySnapDoc);
        const snapGridHtml = elements['macro-snapshot-grid'].innerHTML;
        if (!snapGridHtml.includes('data-snap-id="HSI"') || !snapGridHtml.includes('資料更新中')) {{
          throw new Error('Legacy 5-item snapshot should render HSI fallback with 資料更新中');
        }}
    """
    proc = run_js(js_script, js_bin=js_bin)
    assert proc.returncode == 0, f"New JS checks failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

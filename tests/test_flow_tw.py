"""Tests for Taiwan institutional flow risk alerts (A1/A2), settlement exclusion, staleness, and frontend rendering."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.flow_tw import (
    DISCLAIMER_TEXT,
    compute_rolling_zscore_series,
    evaluate_flow_tw_signals,
    generate_flow_tw_file,
    is_txf_settlement_day,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", content: bytes | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")

    def json(self) -> Any:
        return json.loads(self.text)


def _generate_trading_dates(n: int, end: date = date(2026, 11, 20)) -> list[date]:
    days: list[date] = []
    cur = end
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    days.reverse()
    return days


def test_rolling_zscore_calculation() -> None:
    # Fewer than min_periods -> None
    vals = [float(i) for i in range(1, 35)]
    zs = compute_rolling_zscore_series(vals, window=60, min_periods=30)
    assert all(z is None for z in zs[:29])
    assert zs[29] is not None

    # Exact manual check on 60-element window
    base_60 = [10.0 if i % 2 == 0 else -10.0 for i in range(59)] + [30.0]
    zs_60 = compute_rolling_zscore_series(base_60, window=60, min_periods=30)
    mean_v = sum(base_60) / 60.0
    var_v = sum((x - mean_v) ** 2 for x in base_60) / 59.0
    expected_z = round((30.0 - mean_v) / (var_v ** 0.5), 4)
    assert zs_60[-1] == pytest.approx(expected_z, abs=1e-4)

    # Constant series (std == 0) -> None without division by zero
    const_zs = compute_rolling_zscore_series([5.0] * 40, window=60, min_periods=30)
    assert all(z is None for z in const_zs)


def test_txf_settlement_day_exclusion() -> None:
    # 3rd Wednesdays in late 2026
    assert is_txf_settlement_day("2026-09-16") is True
    assert is_txf_settlement_day("2026-10-21") is True
    assert is_txf_settlement_day("2026-11-18") is True
    assert is_txf_settlement_day("2026-12-16") is True

    # Non-settlement days (1st, 2nd, 4th Wednesdays and non-Wednesdays)
    assert is_txf_settlement_day("2026-11-04") is False
    assert is_txf_settlement_day("2026-11-11") is False
    assert is_txf_settlement_day("2026-11-25") is False
    assert is_txf_settlement_day("2026-11-20") is False

    # Construct 65 trading days ending on 2026-11-18 (settlement day) with huge A2 drop
    dates = _generate_trading_dates(65, end=date(2026, 11, 18))
    a1_rows = [
        {"date": d.isoformat(), "foreign_100m": 10.0 + (i % 5) * 2.0}
        for i, d in enumerate(dates)
    ]
    # Oscillating net_oi changes, then massive -12,000 drop on 2026-11-18
    a2_rows = []
    oi = -50000
    for i, d in enumerate(dates):
        if i < len(dates) - 1:
            oi += 300 if i % 2 == 0 else -250
        else:
            oi -= 12000
        a2_rows.append({"date": d.isoformat(), "net_oi": oi})

    doc_settle = evaluate_flow_tw_signals(
        a1_rows,
        a2_rows,
        now_utc=datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc),
    )
    assert doc_settle["latest_date"] == "2026-11-18"
    assert doc_settle["is_settlement_day"] is True
    assert doc_settle["a2"]["z60"] is not None and doc_settle["a2"]["z60"] < -1.5
    assert doc_settle["a2"]["excluded_by_settlement"] is True
    assert doc_settle["a2"]["triggered"] is False

    # Now shift end date by 1 day to 2026-11-19 (Thursday, non-settlement) with same drop
    dates_non = _generate_trading_dates(65, end=date(2026, 11, 19))
    a1_non = [{"date": d.isoformat(), "foreign_100m": 10.0 + (i % 5) * 2.0} for i, d in enumerate(dates_non)]
    a2_non = [{"date": d.isoformat(), "net_oi": r["net_oi"]} for d, r in zip(dates_non, a2_rows)]
    doc_non = evaluate_flow_tw_signals(
        a1_non,
        a2_non,
        now_utc=datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc),
    )
    assert doc_non["latest_date"] == "2026-11-19"
    assert doc_non["is_settlement_day"] is False
    assert doc_non["a2"]["z60"] < -1.5
    assert doc_non["a2"]["excluded_by_settlement"] is False
    assert doc_non["a2"]["triggered"] is True
    assert "外資期貨空單暴增，隔天下跌風險升高（歷史下跌機率約 68%）" in doc_non["a2"]["message"]


def test_trigger_thresholds_a1_and_a2() -> None:
    dates = _generate_trading_dates(65, end=date(2026, 11, 20))
    # Baseline oscillating series for A1 -> then spike +400 億元 on final day -> z60 > +1.5
    a1_rows = [
        {"date": d.isoformat(), "foreign_100m": (20.0 if i % 2 == 0 else -20.0) if i < 64 else 400.0}
        for i, d in enumerate(dates)
    ]
    a2_rows = [{"date": d.isoformat(), "net_oi": -40000 + (i % 3) * 100} for i, d in enumerate(dates)]

    doc = evaluate_flow_tw_signals(
        a1_rows,
        a2_rows,
        now_utc=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
    )
    assert doc["a1"]["z60"] > 1.5
    assert doc["a1"]["triggered"] is True
    assert "外資異常大買，隔天偏多（歷史上漲機率約 73%，平常約 57%）" in doc["a1"]["message"]
    assert doc["a2"]["triggered"] is False
    assert doc["any_triggered"] is True
    assert doc["disclaimer"] == DISCLAIMER_TEXT
    assert len(doc["history_60d"]) == 60

    # Mild move -> neither triggered
    a1_mild = [
        {"date": d.isoformat(), "foreign_100m": 20.0 if i % 2 == 0 else -20.0}
        for i, d in enumerate(dates)
    ]
    doc_none = evaluate_flow_tw_signals(
        a1_mild,
        a2_rows,
        now_utc=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
    )
    assert doc_none["a1"]["triggered"] is False
    assert doc_none["a2"]["triggered"] is False
    assert doc_none["any_triggered"] is False
    assert "今日無異常" in doc_none["a1"]["message"]
    assert "今日無異常" in doc_none["a2"]["message"]


def test_stale_data_preservation_and_no_workflow_failure(tmp_path: Path) -> None:
    dates = _generate_trading_dates(70, end=date(2026, 11, 20))

    # Build mocked FinMind A1 + TAIFEX A2 CSV + TWSE BFI82U
    fm_a1_data = []
    taifex_lines = ["日期,商品名稱,身分別,多方交易口數,多方契約金額,空方交易口數,空方契約金額,多空交易口數淨額,多空契約金額淨額,多方未平倉口數,多方未平倉契約金額,空方未平倉口數,空方未平倉契約金額,多空未平倉口數淨額,多空未平倉契約金額淨額"]
    for i, d in enumerate(dates):
        net_ntd = (50.0 + (i % 5) * 10.0) * 1e8
        fm_a1_data.append({"date": d.isoformat(), "name": "Foreign_Investor", "buy": net_ntd + 1e10, "sell": 1e10})
        oi = -80000 + (i % 4) * 500
        taifex_lines.append(f"{d.strftime('%Y/%m/%d')},臺股期貨,外資及陸資,0,0,0,0,0,0,15000,0,{15000 - oi},0,{oi},0")

    def _ok_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> _FakeResponse:
        if "api.finmindtrade.com" in url:
            return _FakeResponse(200, json.dumps({"data": fm_a1_data}))
        if "twse.com.tw" in url:
            day_d = str((params or {}).get("dayDate") or "")
            if day_d <= "20261120":
                return _FakeResponse(200, json.dumps({"data": [["外資及陸資(不含外資自營商)", "100", "90", "8,000,000,000"]]}))
            return _FakeResponse(200, json.dumps({"stat": "很抱歉，沒有符合條件的資料!", "data": []}))
        return _FakeResponse(404, "not found")

    def _ok_post(url: str, data: dict[str, Any] | None = None, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(200, content="\n".join(taifex_lines).encode("cp950"))

    now = datetime(2026, 11, 21, 0, 15, tzinfo=timezone.utc)
    doc1 = generate_flow_tw_file(
        tmp_path,
        now_utc=now,
        http_get=_ok_get,
        http_post=_ok_post,
        sleep_sec=0.0,
        use_local_seed=False,
    )
    assert doc1["data_status"] == "ok"
    assert doc1["stale"] is False
    assert doc1["latest_date"] == "2026-11-20"
    assert doc1["a1"]["value"] == 80.0  # TWSE override on latest day
    assert len(doc1["history_60d"]) == 60

    # Next run: ALL external APIs fail -> must NOT raise, must preserve prior values and mark 資料過期
    def _fail_get(*args: Any, **kwargs: Any) -> _FakeResponse:
        raise ConnectionError("network down")

    def _fail_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(503, "unavailable")

    doc2 = generate_flow_tw_file(
        tmp_path,
        now_utc=now + timedelta(days=1),
        http_get=_fail_get,
        http_post=_fail_post,
        sleep_sec=0.0,
        use_local_seed=False,
    )
    assert doc2["data_status"] == "資料過期"
    assert doc2["stale"] is True
    assert doc2["latest_date"] == "2026-11-20"
    assert doc2["a1"]["value"] == 80.0
    assert doc2["a1"]["data_status"] == "資料過期"
    assert doc2["a2"]["data_status"] == "資料過期"
    assert len(doc2["history_60d"]) == 60

    # Calendar staleness check (> 14 days old even if fetch returned old cached dates)
    doc3 = generate_flow_tw_file(
        tmp_path,
        now_utc=now + timedelta(days=20),
        http_get=_ok_get,
        http_post=_ok_post,
        sleep_sec=0.0,
        use_local_seed=False,
    )
    assert doc3["data_status"] == "資料過期"
    assert doc3["stale"] is True

    # Cold start with no prior file and failing network -> safe fallback without crash
    cold_dir = tmp_path / "cold"
    doc_cold = generate_flow_tw_file(
        cold_dir,
        now_utc=now,
        http_get=_fail_get,
        http_post=_fail_post,
        sleep_sec=0.0,
        use_local_seed=False,
    )
    assert (cold_dir / "flow_tw.json").exists()
    assert doc_cold["data_status"] == "資料過期"
    assert doc_cold["stale"] is True
    assert doc_cold["latest_date"] is None


def test_daily_workflow_and_decoupling() -> None:
    daily_yml = (ROOT_DIR / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert "python -m src.flow_tw --out-dir ../data_repo" in daily_yml
    # No new secrets or tokens added
    assert daily_yml.count("secrets.") == 2  # only the two existing GITHUB_TOKEN clones

    # Ensure flow_tw is NOT wired into strategy scoring or simulated account logic
    for rel in (
        "src/jobs/daily.py",
        "src/jobs/live_strategy.py",
        "src/engine/simulator.py",
        "src/risk/manager.py",
    ):
        txt = (ROOT_DIR / rel).read_text(encoding="utf-8")
        assert "flow_tw" not in txt
        assert "A1_foreign_z60" not in txt
        assert "A2_txf_1d_chg" not in txt


def test_frontend_flow_tw_card_glossary_and_research_link() -> None:
    flow_path = ROOT_DIR / "data" / "flow_tw.json"
    assert flow_path.exists(), "Expected committed data/flow_tw.json"
    flow_doc = json.loads(flow_path.read_text(encoding="utf-8"))
    assert flow_doc["latest_date"]
    assert "a1" in flow_doc and "a2" in flow_doc
    assert "is_settlement_day" in flow_doc
    assert len(flow_doc["history_60d"]) == 60

    glossary_js = (ROOT_DIR / "assets" / "glossary.js").read_text(encoding="utf-8")
    flow_tw_js = (ROOT_DIR / "assets" / "flow_tw.js").read_text(encoding="utf-8")
    index_html = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    macro_html = (ROOT_DIR / "macro.html").read_text(encoding="utf-8")

    assert "flow-tw-card-slot" in index_html
    assert "macro-flow-tw-slot" in macro_html
    assert "外資買賣超 z-score" in glossary_js
    assert "台指期淨未平倉" in glossary_js

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    script = f"""
        var window = {{ SIGNAL_DESK_CONFIG: {{}} }};
        {glossary_js}
        {flow_tw_js}

        const g = window.SIGNAL_DESK_GLOSSARY;
        const f = window.SIGNAL_DESK_FLOW_TW;

        // Glossary matches both new terms
        const hits = g.findTermMatchesInText('觀察外資買賣超 z-score與台指期淨未平倉變化', new Set(), 5);
        const ids = hits.map((h) => h.id);
        if (!ids.includes('foreign_zscore') || !ids.includes('txf_net_oi')) {{
          throw new Error('Glossary did not match both new terms: ' + JSON.stringify(ids));
        }}

        // 1) Neither triggered
        const hNormal = f.renderFlowTwCardHtml({json.dumps(flow_doc)}, {{ researchHref: '#/research/flow_signals' }});
        if (!hNormal.includes('今日無異常') || !hNormal.includes('只看隔天、證據偏弱，僅供參考，不是買賣訊號') || !hNormal.includes('#/research/flow_signals')) {{
          throw new Error('Normal card HTML missing required text: ' + hNormal);
        }}

        // 2) A1 triggered
        const dA1 = JSON.parse(JSON.stringify({json.dumps(flow_doc)}));
        dA1.a1.z60 = 1.85;
        dA1.a1.triggered = true;
        const hA1 = f.renderFlowTwCardHtml(dA1, {{ researchHref: '#/research/flow_signals' }});
        if (!hA1.includes('外資異常大買，隔天偏多（歷史上漲機率約 73%，平常約 57%）')) {{
          throw new Error('A1 triggered message missing: ' + hA1);
        }}

        // 3) A2 triggered
        const dA2 = JSON.parse(JSON.stringify({json.dumps(flow_doc)}));
        dA2.a2.z60 = -1.92;
        dA2.a2.triggered = true;
        const hA2 = f.renderFlowTwCardHtml(dA2, {{ researchHref: '#/research/flow_signals' }});
        if (!hA2.includes('外資期貨空單暴增，隔天下跌風險升高（歷史下跌機率約 68%）')) {{
          throw new Error('A2 triggered message missing: ' + hA2);
        }}

        // 4) Stale state
        const dStale = JSON.parse(JSON.stringify({json.dumps(flow_doc)}));
        dStale.stale = true;
        dStale.data_status = '資料過期';
        dStale.stale_reason = '測試抓不到資料';
        const hStale = f.renderFlowTwCardHtml(dStale, {{ researchHref: './index.html#/research/flow_signals' }});
        if (!hStale.includes('資料過期') || !hStale.includes('./index.html#/research/flow_signals')) {{
          throw new Error('Stale card HTML missing marker: ' + hStale);
        }}
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"Flow TW frontend JS check failed:\n{proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)

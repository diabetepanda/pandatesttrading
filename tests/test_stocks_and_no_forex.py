"""Tests for forex removal from UI/instruments output, new TW/US stocks, strategy wiring, and correlation group caps."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config
from src.data.adapters import FallbackDataFetcher
from src.data.models import OHLCVBar
from src.data.quality import is_market_open
from src.jobs.scan import run_scan
from src.risk.manager import RiskManager


EXPECTED_TW_STOCKS = {
    "2330": ("2330.TW", "tw_semis"),
    "2454": ("2454.TW", "tw_semis"),
    "3711": ("3711.TW", "tw_semis"),
    "2317": ("2317.TW", "tw_tech_hw"),
    "2382": ("2382.TW", "tw_tech_hw"),
    "2308": ("2308.TW", "tw_tech_hw"),
    "2881": ("2881.TW", "tw_financials"),
    "2882": ("2882.TW", "tw_financials"),
    "2891": ("2891.TW", "tw_financials"),
    "2603": ("2603.TW", "tw_others"),
    "1301": ("1301.TW", "tw_others"),
    "2412": ("2412.TW", "tw_others"),
}

EXPECTED_US_STOCKS = {
    "TSLA": ("TSLA", "us_megacap"),
    "GOOGL": ("GOOGL", "us_megacap"),
    "AAPL": ("AAPL", "us_megacap"),
    "SNDK": ("SNDK", "memory_semis"),
    "SPCX": ("SPCX", "us_aerospace"),
}


def test_new_stocks_config_and_forex_disabled() -> None:
    cfg = load_config()
    by_sym = {inst["symbol"]: inst for inst in cfg["instruments"]}

    # All 10 forex pairs remain in config with enabled: false (for historical closed trades)
    fx_insts = [inst for inst in cfg["instruments"] if inst["market"] == "forex"]
    assert len(fx_insts) == 10
    assert all(inst.get("enabled") is False for inst in fx_insts)

    # All 12 TW + 5 US stocks are enabled, daily_only, max_leverage 1.0, correct yfinance & group
    for sym, (yf_code, group) in EXPECTED_TW_STOCKS.items():
        assert sym in by_sym
        inst = by_sym[sym]
        assert inst["enabled"] is True
        assert inst["market"] == "stock"
        assert inst["daily_only"] is True
        assert inst["max_leverage"] == 1.0
        assert inst["trading_hours"] == "TW_STOCK"
        assert inst["correlation_group"] == group
        assert inst["sources"]["yfinance"] == yf_code

    for sym, (yf_code, group) in EXPECTED_US_STOCKS.items():
        assert sym in by_sym
        inst = by_sym[sym]
        assert inst["enabled"] is True
        assert inst["market"] == "stock"
        assert inst["daily_only"] is True
        assert inst["max_leverage"] == 1.0
        assert inst["trading_hours"] == "US_EQUITIES"
        assert inst["correlation_group"] == group
        assert inst["sources"]["yfinance"] == yf_code

    # SK Hynix (000660.KS) kept disabled because engine lacks KRW->USD FX conversion
    assert "000660.KS" in by_sym
    sk = by_sym["000660.KS"]
    assert sk["enabled"] is False
    assert sk["sources"]["yfinance"] == "000660.KS"
    assert sk["trading_hours"] == "KR_STOCK"

    # Strategy wiring: L2 includes all new enabled stocks; S3/L1 include "stock"; S2/L3/L4 keep design
    strats = cfg["strategies"]
    l2_syms = set(strats["L2"]["symbols"])
    for sym in set(EXPECTED_TW_STOCKS) | set(EXPECTED_US_STOCKS):
        assert sym in l2_syms
    assert "stock" in strats["S3"]["markets"]
    assert "stock" in strats["L1"]["markets"]
    assert "stock" not in strats["S2"]["markets"]
    assert set(strats["L3"]["markets"]) == {"crypto", "index"}


def test_scan_excludes_forex_from_instruments_json_and_skips_daily_only_stocks(tmp_path: Path) -> None:
    cfg = load_config()
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    eur = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    tw_2330 = next(i for i in cfg["instruments"] if i["symbol"] == "2330")
    us_tsla = next(i for i in cfg["instruments"] if i["symbol"] == "TSLA")

    small_cfg = dict(cfg, instruments=[btc, eur, tw_2330, us_tsla])
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(small_cfg, ensure_ascii=False), encoding="utf-8")

    now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
    daily_bars = [
        OHLCVBar(
            ts_utc=(now - timedelta(days=30 - idx)).strftime("%Y-%m-%dT00:00:00Z"),
            open=100.0 + idx,
            high=102.0 + idx,
            low=99.0 + idx,
            close=101.0 + idx,
            volume=1000.0,
        )
        for idx in range(30)
    ]
    bars_15m = [
        OHLCVBar(
            ts_utc=(now - timedelta(minutes=15 * (10 - idx))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=100.0,
        )
        for idx in range(10)
    ]
    bars_1m = [
        OHLCVBar(
            ts_utc=(now - timedelta(minutes=(10 - idx))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=100.0,
        )
        for idx in range(10)
    ]

    class _DummyAdapter:
        def fetch_bars(self, source_symbol: str, interval: str, limit: int = 300, now_utc: datetime | None = None) -> list[OHLCVBar]:
            if interval == "1d":
                return list(daily_bars)
            if interval == "15m":
                return list(bars_15m)
            return list(bars_1m)

    out_dir = tmp_path / "out"
    run_scan(
        out_dir=out_dir,
        config_path=cfg_path,
        fetcher=FallbackDataFetcher(adapters={"binance_vision": _DummyAdapter(), "yfinance": _DummyAdapter()}),
        now_utc=now,
    )

    inst_doc = json.loads((out_dir / "instruments.json").read_text(encoding="utf-8"))
    syms = [r["symbol"] for r in inst_doc["instruments"]]
    markets = {r["market"] for r in inst_doc["instruments"]}
    assert "EURUSD" not in syms
    assert "forex" not in markets
    assert "2330" in syms and "TSLA" in syms

    row_2330 = next(r for r in inst_doc["instruments"] if r["symbol"] == "2330")
    assert row_2330["intervals"]["1m"]["status"] == "skipped"
    assert row_2330["intervals"]["15m"]["status"] == "skipped"
    assert row_2330["intervals"]["1d"]["status"] == "ok"


def test_correlation_group_limit_and_stock_trading_hours() -> None:
    cfg = load_config()
    rm = RiskManager(cfg)
    tw_2330 = next(i for i in cfg["instruments"] if i["symbol"] == "2330")

    # Max positions for tw_semis is 2 -> 2nd succeeds, 3rd rejected
    res_ok = rm.check_new_signal(
        instrument=tw_2330,
        strategy="L2",
        direction="long",
        entry_price=2000.0,
        stop_loss=1900.0,
        account_state={"subaccount_equity_usd": 20000.0, "subaccount_group_counts": {"tw_semis": 1}},
        control={"paused": False},
        now_utc="2026-11-20T03:00:00Z",
    )
    assert res_ok.approved is True

    res_cap = rm.check_new_signal(
        instrument=tw_2330,
        strategy="L2",
        direction="long",
        entry_price=2000.0,
        stop_loss=1900.0,
        account_state={"subaccount_equity_usd": 20000.0, "subaccount_group_counts": {"tw_semis": 2}},
        control={"paused": False},
        now_utc="2026-11-20T03:00:00Z",
    )
    assert res_cap.approved is False
    assert "相關性群組" in res_cap.reason

    # Market hours: 03:00 UTC (11:00 TPE / 12:00 KST) -> TW & KR open, US closed
    t_asia = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
    assert is_market_open("stock", t_asia, trading_hours="TW_STOCK") is True
    assert is_market_open("stock", t_asia, trading_hours="KR_STOCK") is True
    assert is_market_open("stock", t_asia, trading_hours="US_EQUITIES") is False

    # 1d non-crypto holiday gaps must not count Saturdays/Sundays as missing bars
    from src.data.models import FetchResult
    from src.data.quality import check_continuity

    bars_tw = []
    cur = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    # 30 weekdays skipping 2 public holidays across weekends
    holidays = {"2026-09-25", "2026-09-28", "2026-10-09"}
    for _ in range(45):
        if cur.weekday() < 5 and cur.strftime("%Y-%m-%d") not in holidays:
            bars_tw.append(OHLCVBar(cur.strftime("%Y-%m-%dT00:00:00Z"), 100.0, 102.0, 99.0, 101.0, 100.0))
        cur += timedelta(days=1)
    rep_tw = check_continuity(
        FetchResult(symbol="2330", interval="1d", ok=True, source_used="yfinance", bars=bars_tw),
        market="stock",
        quality_config=cfg["data_quality"],
        now_utc=cur,
        trading_hours="TW_STOCK",
    )
    assert rep_tw.status == "ok"
    assert rep_tw.missing_ratio < 0.10


def test_frontend_no_forex_and_shows_tw_us_stock_categories() -> None:
    root = Path(__file__).resolve().parent.parent
    index_html = (root / "index.html").read_text(encoding="utf-8")
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")

    assert "外匯" not in index_html
    assert "台股" in index_html
    assert "美股／個股" in index_html

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    script = f"""
        const els = {{}};
        const stubEl = (id) => {{
          if (!els[id]) {{
            els[id] = {{
              classList: {{ add: () => {{}}, remove: () => {{}} }},
              addEventListener: () => {{}},
              innerHTML: '',
              textContent: '',
              value: 'all',
              dataset: {{}},
              disabled: false,
            }};
          }}
          return els[id];
        }};
        var navigator = {{}};
        var window = {{ location: {{ hash: '#/data' }}, addEventListener: () => {{}}, SIGNAL_DESK_CONFIG: {{}} }};
        var document = {{
          getElementById: stubEl,
          querySelectorAll: () => [],
          querySelector: () => stubEl('_q'),
          body: {{ appendChild: () => {{}}, removeChild: () => {{}} }},
          createElement: () => ({{ value: '', style: {{}}, setAttribute: () => {{}}, focus: () => {{}}, select: () => {{}} }}),
          execCommand: () => true,
        }};
        function setInterval() {{}}
        function setTimeout() {{ return 1; }}
        function clearTimeout() {{}}
        async function fetch() {{ return {{ ok: false }}; }}
        {app_js}

        const u = window.__SIGNAL_DESK_UTILS__;
        const insts = [
          {{ symbol: 'EURUSD', display_name: 'EUR/USD', market: 'forex', latest_price: 1.08, overall_status: 'ok', intervals: {{}} }},
          {{ symbol: '2330', display_name: '台積電 (2330)', market: 'stock', trading_hours: 'TW_STOCK', correlation_group: 'tw_semis', latest_price: 2100, overall_status: 'ok', intervals: {{}} }},
          {{ symbol: 'TSLA', display_name: '特斯拉 (TSLA)', market: 'stock', trading_hours: 'US_EQUITIES', correlation_group: 'us_megacap', latest_price: 374.4, overall_status: 'ok', intervals: {{}} }},
        ];
        u.setTaDataForTest({{ symbols: {{}} }}, insts);
        u.renderInstruments();
        const html = els['instruments-tbody'].innerHTML;
        if (html.includes('EURUSD') || html.includes('外匯')) {{
          throw new Error('Forex should not render in instruments table');
        }}
        if (!html.includes('台積電 (2330)') || !html.includes('台股') || !html.includes('美股／個股')) {{
          throw new Error('TW/US stock categories or Chinese names missing: ' + html);
        }}
    """
    proc = subprocess.run([js_bin, "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"JS no-forex test failed:\n{proc.stdout}\n{proc.stderr}"

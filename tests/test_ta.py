"""Tests for daily Technical Analysis backend job and expandable frontend panel."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.jobs.daily import run_daily
from src.jobs.scan import run_scan
from src.jobs.ta import (
    TA_DISCLAIMER,
    analyze_symbol_ta,
    backtest_sr_hold_rates,
    classify_trend,
    compute_fibonacci_retracement,
    detect_chart_pattern,
    find_swing_points,
    generate_ta_file,
)


def _make_daily_bars(closes: list[float], spread: float = 2.0, start_year: int = 2022) -> list[OHLCVBar]:
    start = datetime(start_year, 1, 1, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    for i, c in enumerate(closes):
        ts = (start + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z")
        bars.append(
            OHLCVBar(
                ts_utc=ts,
                open=c - 0.3 * spread,
                high=c + spread,
                low=c - spread,
                close=c,
                volume=1000.0,
            )
        )
    return bars


def test_find_swing_points_and_fibonacci() -> None:
    # Build wave with clear swing high and swing low (left_right=5)
    closes = [100.0] * 12 + [102, 105, 110, 115, 120, 116, 112, 108, 104, 100, 95, 90, 94, 98, 102, 106, 110, 112]
    bars = _make_daily_bars(closes, spread=1.0)
    sh, sl = find_swing_points(bars, left_right=5)

    assert any(abs(px - 121.0) < 1e-6 for _, _, px in sh)
    assert any(abs(px - 89.0) < 1e-6 for _, _, px in sl)

    fib = compute_fibonacci_retracement(bars, sh, sl)
    assert fib is not None
    assert fib["direction"] == "down"  # 121 peak came before 89 trough
    assert fib["start_price"] == 121.0
    assert fib["end_price"] == 89.0
    assert abs(fib["levels"]["50.0%"] - 105.0) < 1e-6
    assert abs(fib["levels"]["38.2%"] - (89.0 + 0.382 * 32.0)) < 1e-6
    assert abs(fib["levels"]["61.8%"] - (89.0 + 0.618 * 32.0)) < 1e-6


def test_classify_trend_adx_realistic_and_not_frozen() -> None:
    # Strong uptrend over 250 daily bars should yield 上升趨勢 with realistic ADX14 (< 90)
    closes = [100.0 + i * 0.4 + math.sin(i * 0.4) * 1.5 for i in range(250)]
    bars = _make_daily_bars(closes, spread=2.0)
    trend = classify_trend(bars, closes[-1], market="stocks")
    assert trend["state"] == "上升趨勢"
    assert trend["adx14"] is not None
    assert 20.0 < trend["adx14"] < 85.0
    assert trend["sma20"] > trend["sma50"] > trend["sma200"]

    # Range-bound oscillation should classify as 盤整
    flat_closes = [100.0 + math.sin(i * 0.3) * 2.0 for i in range(250)]
    flat_bars = _make_daily_bars(flat_closes, spread=1.5)
    flat_trend = classify_trend(flat_bars, flat_closes[-1], market="forex")
    assert flat_trend["state"] == "盤整"


def test_analyze_symbol_ta_sr_ordering_volatility_squeeze_pattern_and_insufficient_backtest() -> None:
    # Construct 300 bars transitioning from wide swings into tight compression at end
    closes: list[float] = []
    for i in range(260):
        closes.append(200.0 + 18.0 * math.sin(i * 0.28))
    for i in range(40):
        closes.append(200.0 + 0.3 * math.sin(i * 0.5))
    bars: list[OHLCVBar] = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, c in enumerate(closes):
        sp = 4.0 if i < 260 else 0.15
        bars.append(
            OHLCVBar(
                ts_utc=(start + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z"),
                open=c,
                high=c + sp,
                low=c - sp,
                close=c,
                volume=100.0,
            )
        )

    ta = analyze_symbol_ta("TESTSYM", "Test Symbol", "stocks", bars)
    assert ta is not None
    cur_px = ta["latest_price"]

    # Resistances strictly sorted nearest-to-farthest above current price
    res_prices = [r["price"] for r in ta["resistances"]]
    assert res_prices == sorted(res_prices)
    assert all(p >= cur_px for p in res_prices)
    assert all(r["algo"] for r in ta["resistances"])

    # Supports strictly sorted nearest-to-farthest below current price
    sup_prices = [s["price"] for s in ta["supports"]]
    assert sup_prices == sorted(sup_prices, reverse=True)
    assert all(p < cur_px for p in sup_prices)
    assert all(s["algo"] for s in ta["supports"])

    # Volatility estimate formulas: cur +/- ATR20 * sqrt(days) (5 trading days for TradFi, 7 for crypto)
    atr20 = ta["volatility"]["atr20"]
    assert abs(ta["volatility"]["tomorrow"]["high"] - (cur_px + atr20)) < 1e-6
    assert abs(ta["volatility"]["tomorrow"]["low"] - (cur_px - atr20)) < 1e-6
    assert ta["volatility"]["week"]["days"] == 5
    assert abs(ta["volatility"]["week"]["high"] - (cur_px + atr20 * math.sqrt(5))) < 1e-6

    # Tight final 40 bars should produce squeeze percentile < 20%
    assert ta["squeeze"]["bb_width_pct"] < 20.0
    assert ta["squeeze"]["is_squeezing"] is True
    assert ta["squeeze"]["label"] == "收斂中，可能變盤"

    # Pattern field present
    assert ta["pattern"]["name"] in {"三角收斂", "箱型", "上升通道", "下降通道", "無明顯型態"}

    # Short 60-bar backtest (< 30 samples) must be flagged as 樣本不足
    bt_short = backtest_sr_hold_rates(bars[:60], market="stocks")
    assert bt_short["support_insufficient"] is True
    assert bt_short["resistance_insufficient"] is True
    assert "樣本不足" in bt_short["support_label"]


def test_generate_ta_file_and_jobs_integration(tmp_path: Path) -> None:
    cfg = load_config()
    small_cfg = dict(cfg)
    small_cfg["instruments"] = [dict(cfg["instruments"][0], enabled=True)]
    sym = small_cfg["instruments"][0]["symbol"]

    bars = _make_daily_bars([50000.0 + i * 20.0 + math.sin(i * 0.35) * 400.0 for i in range(280)], spread=250.0)
    out = generate_ta_file(
        out_dir=tmp_path,
        config=small_cfg,
        bars_1d_by_sym={sym: bars},
        latest_prices={sym: bars[-1].close},
        fetch_10y_if_missing=False,
    )
    assert out["disclaimer"] == TA_DISCLAIMER
    assert sym in out["symbols"]
    ta_json = json.loads((tmp_path / "ta.json").read_text(encoding="utf-8"))
    assert sym in ta_json["symbols"]
    assert "summary" in ta_json["symbols"][sym]

    # Verify DummyAdapter wired cleanly in run_scan and run_daily writes ta.json without breaking
    from src.data.adapters import FallbackDataFetcher

    class _DummyAdapter:
        def __init__(self) -> None:
            self._bars: dict[str, list[OHLCVBar]] = {
                "1m": bars[-5:],
                "15m": bars[-5:],
                "1d": bars[-250:],
            }

        def fetch_ohlcv(self, instrument: dict[str, Any], interval: str, limit: int = 300, now_utc: datetime | None = None) -> list[OHLCVBar]:
            return list(self._bars[interval])

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(small_cfg, ensure_ascii=False), encoding="utf-8")
    dummy_fetcher = FallbackDataFetcher(
        adapters={"binance": _DummyAdapter(), "yahoo": _DummyAdapter(), "dukascopy": _DummyAdapter()}
    )

    scan_dir = tmp_path / "scan_out"
    run_scan(
        out_dir=scan_dir,
        config_path=cfg_path,
        fetcher=dummy_fetcher,
        now_utc=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert (scan_dir / "ta.json").exists()

    daily_dir = tmp_path / "daily_out"
    run_daily(
        out_dir=daily_dir,
        config_path=cfg_path,
        fetcher=dummy_fetcher,
        now_utc=datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert (daily_dir / "ta.json").exists()


def test_real_data_ta_json_has_all_symbols_and_btcusdt_constraints() -> None:
    root = Path(__file__).resolve().parent.parent
    ta_path = root / "data" / "ta.json"
    assert ta_path.exists(), "Expected committed data/ta.json"
    doc = json.loads(ta_path.read_text(encoding="utf-8"))
    assert doc["disclaimer"] == TA_DISCLAIMER
    assert doc["symbols_count"] >= 40
    assert "XAUUSD" in doc["symbols"]
    assert "BTCUSDT" in doc["symbols"]

    xau = doc["symbols"]["XAUUSD"]
    assert xau["trend"]["state"] in {"上升趨勢", "下降趨勢", "盤整"}
    assert 5.0 < xau["trend"]["adx14"] < 80.0
    assert xau["backtest"]["support_samples"] >= 30
    assert xau["backtest"]["resistance_samples"] >= 30

    # BTCUSDT nearest resistance distance must be < 3 * ATR20 and max 3 per side
    btc = doc["symbols"]["BTCUSDT"]
    assert 1 <= len(btc["resistances"]) <= 3
    assert 1 <= len(btc["supports"]) <= 3
    btc_atr = btc["volatility"]["atr20"]
    btc_cur = btc["latest_price"]
    assert (btc["resistances"][0]["price"] - btc_cur) < 3.0 * btc_atr
    assert (btc["resistances"][0]["price"] - btc_cur) > 0.3 * btc_atr
    for lvl in btc["resistances"] + btc["supports"]:
        assert lvl["label"]
        assert 1 <= lvl["strength"] <= 3

    # Any symbol with > 2 years daily bars (> 500 bars) must have > 0 backtest samples
    for sym, entry in doc["symbols"].items():
        if entry["bars_count"] >= 500:
            assert entry["backtest"]["support_samples"] > 0, f"{sym} support_samples == 0"
            assert entry["backtest"]["resistance_samples"] > 0, f"{sym} resistance_samples == 0"


def test_btcusdt_2y_backtest_nonzero_and_short_bars_fallback(tmp_path: Path) -> None:
    # 1. Synthetic > 2y crypto (with weekends, 750 days) must yield > 0 samples
    closes = [60000.0 + 4000.0 * math.sin(i * 0.25) for i in range(750)]
    crypto_2y = _make_daily_bars(closes, spread=900.0, start_year=2024)
    bt = backtest_sr_hold_rates(crypto_2y, market="crypto")
    assert bt["support_samples"] > 0
    assert bt["resistance_samples"] > 0

    # 2. Short 60-bar live scan into fresh out_dir must preserve BTCUSDT historical backtest from data/ta.json
    cfg = load_config()
    btc_inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    small_cfg = dict(cfg, instruments=[dict(btc_inst, enabled=True)])
    res = generate_ta_file(
        out_dir=tmp_path,
        config=small_cfg,
        bars_1d_by_sym={"BTCUSDT": crypto_2y[-60:]},
        fetch_10y_if_missing=False,
    )
    btc_bt = res["symbols"]["BTCUSDT"]["backtest"]
    assert btc_bt["support_samples"] > 0
    assert btc_bt["resistance_samples"] > 0
    assert "0 次" not in btc_bt["support_label"]


def test_frontend_ta_expand_collapse_and_formatting() -> None:
    root = Path(__file__).resolve().parent.parent
    index_html = (root / "index.html").read_text(encoding="utf-8")
    style_css = (root / "assets" / "style.css").read_text(encoding="utf-8")
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")

    assert "?v=20261121c" in index_html
    assert "技術分析" in index_html
    assert ".ta-panel" in style_css
    assert ".ta-detail-row" in style_css
    assert ".ta-hero-card" in style_css
    assert ".ta-detail-panel" in style_css
    assert ".ta-detail-grid-2x2" in style_css
    assert ".ta-ruler-card" not in style_css

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    script = f"""
        const tbodyEl = {{
          classList: {{ add: () => {{}}, remove: () => {{}} }},
          addEventListener: () => {{}},
          innerHTML: '',
          textContent: '',
          dataset: {{}},
        }};
        const elements = {{ 'instruments-tbody': tbodyEl }};
        const stubEl = (id) => {{
          if (!elements[id]) {{
            elements[id] = {{
              classList: {{ add: () => {{}}, remove: () => {{}} }},
              addEventListener: () => {{}},
              innerHTML: '',
              textContent: '',
              value: 'all',
              dataset: {{}},
              disabled: false,
            }};
          }}
          return elements[id];
        }};
        var navigator = {{}};
        var window = {{
          location: {{ hash: '#/data' }},
          addEventListener: () => {{}},
          SIGNAL_DESK_CONFIG: {{}},
        }};
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
        const mockInstruments = [
          {{ symbol: 'XAUUSD', display_name: 'Gold', market: 'commodity', latest_price: 3985.5, overall_status: 'healthy', intervals: {{}} }},
          {{ symbol: '2330', display_name: '台積電 (2330)', market: 'stock', trading_hours: 'TW_STOCK', correlation_group: 'tw_semis', latest_price: 2100.0, overall_status: 'healthy', intervals: {{}} }},
          {{ symbol: 'MISSING', display_name: 'No TA', market: 'stock', trading_hours: 'US_EQUITIES', latest_price: 100.0, overall_status: 'healthy', intervals: {{}} }},
        ];
        const mockTa = {{
          disclaimer: '以上為依歷史數據與技術指標計算的參考點位，不是預測，也不是投資建議。',
          symbols: {{
            XAUUSD: {{
              symbol: 'XAUUSD',
              latest_price: 3985.5,
              latest_bar_date: '2026-11-15',
              summary: '下降趨勢，波動正常',
              resistances: [{{ price: 4091.2, label: 'SMA20＋Fibo 50.0%', algo: '擺動高點', strength: 2 }}],
              supports: [{{ price: 3879.3, label: '20日低', algo: '近 20 日最低', strength: 1 }}],
              fibonacci: {{
                direction_label: '下跌波段反彈',
                start_date: '2026-10-01',
                start_price: 4200.0,
                end_date: '2026-11-05',
                end_price: 3800.0,
                levels: {{ '38.2%': 3952.8, '50.0%': 4000.0, '61.8%': 4047.2 }},
              }},
              volatility: {{ atr20: 55.1, tomorrow: {{ low: 3930.4, high: 4040.6 }}, week: {{ low: 3839.7, high: 4131.3 }} }},
              trend: {{ state: '下降趨勢', sma20: 4010.0, sma50: 4050.0, sma200: 3900.0, adx14: 25.5, detail: '空頭排列' }},
              squeeze: {{ bb_width_pct: 15.2, is_squeezing: true, label: '收斂中，可能變盤' }},
              pattern: {{ name: '下降通道', note: '通道下探（僅供參考）' }},
              backtest: {{ years: 10.0, support_label: '70.7%（150 次，10.0 年）', resistance_label: '62.8%（180 次，10.0 年）' }},
            }},
            '2330': {{
              symbol: '2330',
              display_name: '台積電 (2330)',
              latest_price: 2100.0,
              resistance_status: 'ok',
              support_status: 'ok',
              resistances: [{{ price: 2200.0, label: '盤整區上緣（2026/08–10）', algo: '前期盤整區間上緣（2026-08-01~2026-10-15）', strength: 2 }}],
              supports: [{{ price: 2000.0, label: '2026/09 波段低 ＋1 項', algo: '2026-09-10 波段低點', strength: 2 }}],
              trend: {{ state: '盤整', sma20: 2090.0, sma50: 2050.0, sma200: null, adx14: 14.2 }},
              squeeze: {{ bb_width_pct: 48.0, is_squeezing: false, label: '波動正常' }},
              pattern: {{ name: '箱型', note: '區間震盪' }},
              backtest: {{ years: 10.0, support_label: '68.0%（110 次，10.0 年）', resistance_label: '64.0%（105 次，10.0 年）' }},
            }},
            ATHSYM: {{
              symbol: 'ATHSYM',
              display_name: 'All Time High Sym',
              latest_price: 500.0,
              resistance_status: '無明確壓力',
              support_status: 'ok',
              resistances: [{{ price: 540.0, label: 'Fibo 1.272（延伸參考）', algo: 'Fibonacci 1.272 延伸參考', strength: 1, is_extension: true }}],
              supports: [{{ price: 470.0, label: '2026/10 波段高（前壓力）', algo: '突破轉支撐', strength: 2 }}],
              trend: {{ state: '上升趨勢', sma20: 480.0, sma50: 460.0, sma200: 420.0, adx14: 32.0 }},
              squeeze: {{ bb_width_pct: 55.0, is_squeezing: false, label: '波動正常' }},
              pattern: {{ name: '上升通道', note: '上升通道' }},
              backtest: {{ years: 5.0, support_label: '70.0%（60 次，5.0 年）', resistance_label: '65.0%（55 次，5.0 年）' }},
            }},
          }},
        }};

        u.setTaDataForTest(mockTa, mockInstruments);

        // 1. Initially all rows collapsed; table HTML unchanged from normal rows
        u.renderInstruments();
        if (tbodyEl.innerHTML.includes('ta-detail-row')) {{
          throw new Error('Expected no expanded panel by default');
        }}

        // 2. Expand XAUUSD -> left hero + right 2x2 detail panel (no ruler, no collapsed toggle)
        u.toggleTaSymbolForTest('XAUUSD');
        const h1 = tbodyEl.innerHTML;
        const count1 = (h1.match(/ta-detail-row/g) || []).length;
        if (count1 !== 1) throw new Error('Expected 1 expanded TA panel, got ' + count1);
        if (!h1.includes('4,091.20') || !h1.includes('3,879.30') || !h1.includes('SMA20＋Fibo 50.0%')) {{
          throw new Error('Expected comma-formatted S/R and merged label in XAUUSD panel: ' + h1);
        }}
        if (h1.includes('ta-ruler-card') || h1.includes('看詳細數據')) {{
          throw new Error('Old ruler card or hidden details toggle should be removed');
        }}
        if (!h1.includes('詳細數據') || !h1.includes('支撐壓力表') || !h1.includes('均線與趨勢') || !h1.includes('斐波那契與型態')) {{
          throw new Error('Missing 2x2 detail subcards');
        }}
        if (!h1.includes('★★☆') || !h1.includes('25.5（有趨勢）') || h1.includes('（僅供參考）')) {{
          throw new Error('Expected stars, ADX plain label, and stripped duplicate 僅供參考');
        }}
        if (!h1.includes('收斂中，可能變盤') || !h1.includes('以上為依歷史數據與技術指標計算的參考點位，不是預測，也不是投資建議。')) {{
          throw new Error('Missing squeeze warning or disclaimer');
        }}
        if (!h1.includes('歷史驗證（觸及後 5 日）') || !h1.includes('70.7%（150 次，10.0 年）')) {{
          throw new Error('Missing historical validation bar');
        }}
        const idxXau = h1.indexOf('data-symbol="XAUUSD"');
        const idxTw = h1.indexOf('data-symbol="2330"');
        const idxDetail = h1.indexOf('class="ta-detail-row"');
        if (!(idxXau < idxDetail && idxDetail < idxTw)) {{
          throw new Error('Detail row not placed directly beneath XAUUSD');
        }}

        // 3. Click 2330 -> XAUUSD collapses, only 2330 expanded, missing SMA200 shows 資料不足
        u.toggleTaSymbolForTest('2330');
        const h2 = tbodyEl.innerHTML;
        if ((h2.match(/ta-detail-row/g) || []).length !== 1 || !h2.includes('2,200.00') || h2.includes('4,091.20')) {{
          throw new Error('Switching expanded row failed');
        }}
        if (!h2.includes('資料不足') || !h2.includes('14.2（無趨勢）')) {{
          throw new Error('Expected 資料不足 for null SMA200 and 無趨勢 for ADX < 20');
        }}

        // 4. Click 2330 again -> collapses completely
        u.toggleTaSymbolForTest('2330');
        if (tbodyEl.innerHTML.includes('ta-detail-row')) {{
          throw new Error('Re-clicking active row should collapse it');
        }}

        // 5. Missing symbol -> 技術分析資料暫無
        u.toggleTaSymbolForTest('MISSING');
        if (!tbodyEl.innerHTML.includes('技術分析資料暫無')) {{
          throw new Error('Missing TA symbol should render 技術分析資料暫無');
        }}

        // 6. ATH fallback renders 無明確壓力 + 延伸參考 without truncation
        const athHtml = u.renderTaPanelHtml('ATHSYM', null, mockTa);
        if (!athHtml.includes('無明確壓力') || !athHtml.includes('Fibo 1.272（延伸參考）') || athHtml.includes('整數關卡')) {{
          throw new Error('ATH fallback rendering failed: ' + athHtml);
        }}
    """
    proc = subprocess.run([js_bin, "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"JS TA verification failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_no_round_numbers_and_no_ellipsis_truncation_in_ta() -> None:
    """(a) Zero round-number S/R sources anywhere; (c) zero `…` / `...` truncation in labels."""
    root = Path(__file__).resolve().parent.parent
    ta_py = (root / "src" / "jobs" / "ta.py").read_text(encoding="utf-8")
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")
    style_css = (root / "assets" / "style.css").read_text(encoding="utf-8")

    assert "整數關卡" not in ta_py
    assert "整數心理關卡" not in ta_py
    assert "_round_level_step" not in ta_py
    assert "整數關卡" not in app_js

    # CSS labels must wrap cleanly rather than truncating via ellipsis
    src_block = style_css.split(".ta-sr-src {", 1)[1].split("}", 1)[0]
    num_src_block = style_css.split(".ta-num-src {", 1)[1].split("}", 1)[0]
    assert "ellipsis" not in src_block
    assert "ellipsis" not in num_src_block

    doc = json.loads((root / "data" / "ta.json").read_text(encoding="utf-8"))
    for sym, entry in doc["symbols"].items():
        for lvl in entry.get("resistances", []) + entry.get("supports", []):
            lbl = lvl.get("label", "")
            algo = lvl.get("algo", "")
            assert "整數" not in lbl and "心理關卡" not in lbl, f"{sym} kept round label: {lbl}"
            assert "整數" not in algo and "心理關卡" not in algo, f"{sym} kept round algo: {algo}"
            assert "…" not in lbl and "..." not in lbl, f"{sym} truncated label: {lbl}"


def test_60d_low_has_real_support_and_only_ath_atl_use_fibo_extension() -> None:
    """(b) A 60-day low still finds historical/MA support; only true ATH/ATL triggers 無明確支撐/壓力 + Fibo ext."""
    # Construct 360-bar series: base in [60, 85] with multiple swings, rally to 145, pullback to 60-day low at 95
    closes: list[float] = []
    for i in range(220):
        closes.append(72.0 + 10.0 * math.sin(i * 0.22))
    for i in range(80):
        closes.append(75.0 + i * 0.85)  # rally to ~142
    for i in range(60):
        closes.append(142.0 - i * 0.78)  # drop to ~96 (strictly a 60d low, still > earlier 62..82 history!)

    bars_60d_low = _make_daily_bars(closes, spread=2.0, start_year=2024)
    ta_60 = analyze_symbol_ta("LOW60D", "60D Low Test", "stock", bars_60d_low)
    assert ta_60 is not None
    assert ta_60["support_status"] == "ok", f"60d low should find earlier historical/MA support: {ta_60}"
    assert len(ta_60["supports"]) >= 1
    assert all(not s.get("is_extension") for s in ta_60["supports"])
    assert ta_60["supports"][0]["price"] < ta_60["latest_price"]

    # True all-time low breakdown (drops far below every historical bar & MA) -> 無明確支撐 + Fibo extension
    atl_closes = [120.0 + 6.0 * math.sin(i * 0.25) for i in range(250)] + [120.0 - i * 2.2 for i in range(1, 36)]
    bars_atl = _make_daily_bars(atl_closes, spread=2.0, start_year=2024)
    ta_atl = analyze_symbol_ta("ATLSYM", "ATL Test", "stock", bars_atl)
    assert ta_atl is not None
    assert ta_atl["support_status"] == "無明確支撐"
    assert len(ta_atl["supports"]) >= 1
    assert all(s.get("is_extension") is True for s in ta_atl["supports"])
    assert all(s["strength"] == 1 for s in ta_atl["supports"])
    assert all("延伸參考" in s["label"] for s in ta_atl["supports"])

    # True all-time high breakout -> 無明確壓力 + Fibo extension
    ath_closes = [100.0 + 5.0 * math.sin(i * 0.25) for i in range(250)] + [100.0 + i * 2.5 for i in range(1, 36)]
    bars_ath = _make_daily_bars(ath_closes, spread=2.0, start_year=2024)
    ta_ath = analyze_symbol_ta("ATHSYM", "ATH Test", "stock", bars_ath)
    assert ta_ath is not None
    assert ta_ath["resistance_status"] == "無明確壓力"
    assert len(ta_ath["resistances"]) >= 1
    assert all(r.get("is_extension") is True for r in ta_ath["resistances"])
    assert all(r["strength"] == 1 for r in ta_ath["resistances"])
    assert all("延伸參考" in r["label"] for r in ta_ath["resistances"])


def test_short_bars_do_not_overwrite_valid_ta_and_history_archive_works(tmp_path: Path) -> None:
    """(a) 59 bars alone will not overwrite normal TA; (b) long history ensures BTC/ETH supports; (c) short_listing wording."""
    from src.jobs.ta import HISTORY_1D_FILENAME, SHORT_LISTING_NOTE_KEYWORD

    cfg = load_config()
    eth_inst = next(i for i in cfg["instruments"] if i["symbol"] == "ETHUSDT")
    btc_inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    sub_cfg = dict(cfg, instruments=[dict(eth_inst, enabled=True), dict(btc_inst, enabled=True)])

    # Build > 2y oscillating history and populate history_1d.json.gz via update_history_store=True
    eth_long = _make_daily_bars([2000.0 + 350.0 * math.sin(i * 0.12) for i in range(900)], spread=45.0, start_year=2024)
    btc_long = _make_daily_bars([65000.0 + 7000.0 * math.sin(i * 0.12) for i in range(900)], spread=1200.0, start_year=2024)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    res_full = generate_ta_file(
        out_dir=tmp_path,
        config=sub_cfg,
        bars_1d_by_sym={"ETHUSDT": eth_long, "BTCUSDT": btc_long},
        now_utc=now,
        fetch_10y_if_missing=False,
        update_history_store=True,
        use_local_cache=False,
    )
    assert (tmp_path / HISTORY_1D_FILENAME).exists()
    for s in ("ETHUSDT", "BTCUSDT"):
        assert res_full["symbols"][s]["bars_count"] == 900
        assert res_full["symbols"][s]["support_status"] == "ok"
        assert len(res_full["symbols"][s]["supports"]) >= 1
        assert res_full["symbols"][s]["data_status"] == "ok"

    # (a) Remove history_1d.json.gz, pass only 59 declining bars (and inject a legacy round item in prior ta.json to ensure sanitization)
    (tmp_path / HISTORY_1D_FILENAME).unlink()
    ta_doc = json.loads((tmp_path / "ta.json").read_text(encoding="utf-8"))
    ta_doc["symbols"]["ETHUSDT"]["supports"].append({"price": 1500.0, "label": "整數關卡", "algo": "整數心理關卡 1,500", "strength": 1})
    (tmp_path / "ta.json").write_text(json.dumps(ta_doc, ensure_ascii=False), encoding="utf-8")

    bars_59 = _make_daily_bars([1800.0 - i * 4.0 for i in range(59)], spread=15.0, start_year=2026)
    res_short = generate_ta_file(
        out_dir=tmp_path,
        config=sub_cfg,
        bars_1d_by_sym={"ETHUSDT": bars_59, "BTCUSDT": btc_long[-59:]},
        now_utc=now,
        fetch_10y_if_missing=False,
        update_history_store=False,
        use_local_cache=False,
    )
    eth_after = res_short["symbols"]["ETHUSDT"]
    assert eth_after["bars_count"] == 900  # Not overwritten by 59 bars
    assert eth_after["data_status"] == "資料更新中"
    assert eth_after["updating"] is True
    assert len(eth_after["supports"]) >= 1
    assert all("整數" not in s["label"] for s in eth_after["supports"])
    assert SHORT_LISTING_NOTE_KEYWORD not in (eth_after.get("support_note") or "")

    # (c) Without short_listing flag, even cold 59-bar ATL failure must NOT claim 上市或可用歷史較短
    cold_dir = tmp_path / "cold"
    res_cold_fail = generate_ta_file(
        out_dir=cold_dir,
        config=dict(cfg, instruments=[dict(eth_inst, enabled=True)]),
        bars_1d_by_sym={"ETHUSDT": bars_59},
        now_utc=now,
        fetch_10y_if_missing=False,
        update_history_store=False,
        use_local_cache=False,
    )
    eth_cold = res_cold_fail["symbols"]["ETHUSDT"]
    assert eth_cold["data_status"] == "資料更新中"
    assert SHORT_LISTING_NOTE_KEYWORD not in (eth_cold.get("support_note") or "")
    assert SHORT_LISTING_NOTE_KEYWORD not in (eth_cold.get("resistance_note") or "")

    # Real new listing (< 2.2 years) fetched at limit>=1000 cleanly CAN display 上市或可用歷史較短 on an empty side
    new_bars = _make_daily_bars([100.0 - i * 1.2 for i in range(60)], spread=1.5, start_year=2026)
    ta_new_listing = analyze_symbol_ta("NEWIPO", "New IPO", "stock", new_bars, is_short_listing=True)
    assert ta_new_listing is not None
    assert ta_new_listing["support_status"] == "無明確支撐"
    assert SHORT_LISTING_NOTE_KEYWORD in ta_new_listing["support_note"]

    # Also verify real data/ta.json satisfies (b) for BTCUSDT & ETHUSDT
    repo_ta = json.loads((Path(__file__).resolve().parent.parent / "data" / "ta.json").read_text(encoding="utf-8"))
    for sym in ("BTCUSDT", "ETHUSDT"):
        e = repo_ta["symbols"][sym]
        assert e["bars_count"] >= 2000
        assert e["support_status"] == "ok"
        assert len(e["supports"]) >= 1

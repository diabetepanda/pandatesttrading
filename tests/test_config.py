"""Unit tests for configuration and control files."""

from __future__ import annotations

from src.config_loader import load_config, load_control, load_events


def test_config_has_required_sections() -> None:
    cfg = load_config()
    assert cfg["project"]["github_owner"] == "diabetepanda"
    assert cfg["project"]["github_repo"] == "pandatesttrading"
    assert cfg["account"]["initial_capital_usd"] == 100000.0
    assert cfg["account"]["pools"]["short_term"]["capital_usd"] == 20000.0
    assert cfg["account"]["pools"]["long_term"]["capital_usd"] == 80000.0
    assert "strategy_a" not in cfg
    assert "strategy_b" not in cfg
    assert set(cfg["strategies"].keys()) == {"S2", "S3", "L1", "L2", "L3", "L4"}
    total_sub = sum(float(v["capital_usd"]) for v in cfg["strategies"].values())
    assert abs(total_sub - 100000.0) < 0.05
    assert cfg["backtest"]["risk_per_trade_pct"] == 1.0
    assert cfg["backtest"]["max_total_open_risk_pct"] == 10.0
    assert cfg["backtest"]["max_concurrent_positions"] == 10
    assert cfg["backtest"]["max_position_notional_pct"] == 500.0
    assert cfg["backtest"]["max_total_notional_multiple"] == 5.0
    for removed_risk_key in (
        "maintenance_margin_ratio",
        "max_total_stop_risk_usd",
        "max_margin_usage_pct",
        "max_group_stop_risk_usd",
        "max_daily_loss_usd",
        "max_weekly_loss_usd",
        "drawdown_pause_pct",
        "short_term_tp1_rr_min",
        "short_term_tp2_rr_min",
        "long_term_min_rr",
    ):
        assert removed_risk_key not in cfg["risk"]
    assert "signals" not in cfg
    assert "out_sample_ratio" not in cfg["backtest"]
    assert "scan_interval_minutes" not in cfg["schedule"]
    assert "ui_stale_warning_minutes" not in cfg["schedule"]
    enabled = [inst for inst in cfg["instruments"] if inst.get("enabled", True)]
    assert len(enabled) == 59
    assert sum(1 for inst in enabled if inst["market"] == "crypto") == 33
    assert sum(1 for inst in enabled if inst["market"] == "commodity") == 5
    assert sum(1 for inst in enabled if inst["market"] == "forex") == 0
    assert sum(1 for inst in enabled if inst["market"] == "index") == 4
    assert sum(1 for inst in enabled if inst["market"] == "stock") == 17
    index_syms = {inst["symbol"] for inst in enabled if inst["market"] == "index"}
    assert index_syms == {"NAS100", "SP500", "JP225", "TWII"}
    assert all(inst.get("daily_only") is True for inst in enabled if inst["market"] in ("index", "stock"))


def test_instruments_have_valid_fields() -> None:
    cfg = load_config()
    seen_symbols = set()
    for inst in cfg["instruments"]:
        sym = inst["symbol"]
        assert sym not in seen_symbols
        seen_symbols.add(sym)
        assert inst["market"] in {"crypto", "forex", "commodity", "stock", "index"}
        assert inst["correlation_group"] in cfg["correlation_groups"]
        assert inst["max_leverage"] > 0
        assert isinstance(inst["sources"], dict) and len(inst["sources"]) >= 1


def test_control_json_valid() -> None:
    ctrl = load_control()
    assert "paused" in ctrl
    assert "risk_review_done" in ctrl
    assert isinstance(ctrl["paused"], bool)


def test_events_yaml_valid() -> None:
    events = load_events()
    assert isinstance(events, list)
    assert len(events) >= 1
    for ev in events:
        assert "title" in ev
        assert "time_utc" in ev


def test_frontend_assets_and_relative_paths() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    index_html = (root / "index.html").read_text(encoding="utf-8")
    assert "./assets/config.js" in index_html
    assert "./assets/app.js" in index_html
    assert "./assets/style.css" in index_html
    assert 'href="/' not in index_html
    assert 'src="/' not in index_html

    config_js = (root / "assets" / "config.js").read_text(encoding="utf-8")
    assert "LIVE_REFRESH_MS" in config_js
    assert "LIVE_CRYPTO_SYMBOLS" in config_js
    assert "LIVE_METAL_SYMBOLS" in config_js
    assert "LIVE_TRADINGVIEW_SYMBOLS" in config_js
    for code in (
        "NYMEX:CL1!",
        "NYMEX:NG1!",
        "COMEX:GC1!",
        "COMEX:SI1!",
        "COMEX:HG1!",
        "NASDAQ:NDX",
        "SP:SPX",
        "TVC:NI225",
        "TWSE:IX0001",
    ):
        assert code in config_js
    assert "FX:" not in config_js
    assert "外匯" not in index_html
    assert 'data-market="forex"' not in index_html
    assert 'value="forex"' not in index_html
    assert 'data-market="tw_stock"' in index_html
    assert 'data-market="us_stock"' in index_html
    assert "PEPEUSDT" in config_js
    assert "TONUSDT" in config_js

    # Stage 4, 5 & 6 hash routes, disclaimer, backtest results, and fixed CDN check
    for route in ("#/simple", "#/wall", "#/results", "#/stats", "#/account", "#/risk", "#/data", "#/research"):
        assert route in index_html
    nav_block = index_html.split('<nav class="main-nav"', 1)[1].split("</nav>", 1)[0]
    assert nav_block.index("#/simple") < nav_block.index("#/wall")
    assert "#/scaling" not in index_html
    assert "lightweight-charts@4.2.1" in index_html
    assert "backtest-section" in index_html
    assert "歷史不代表未來" in index_html
    assert "尚未驗證" in index_html
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")
    assert "模擬驗證中，非投資建議" in app_js
    assert "computeFloatingPnl" in app_js
    assert "computeCardEstimates" in app_js
    assert "backtest_results.json" in app_js
    assert "renderBacktestResults" in app_js
    for fname in ("performance_stats.json", "risk_status.json", "equity_curve.json"):
        assert fname in app_js
    assert "renderRiskCenter" in app_js
    assert "尚未驗證" in app_js

    cfg = load_config()
    assert cfg["analytics"]["min_verified_trades_stats"] >= 1


def test_frontend_ui_optimizations_and_mobile_css() -> None:
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    index_html = (root / "index.html").read_text(encoding="utf-8")
    style_css = (root / "assets" / "style.css").read_text(encoding="utf-8")
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")

    # Cache-busting version bumped and legacy (A)/(B) labels removed
    assert "?v=20261121c" in index_html
    assert "simple-open-now-grid" in index_html
    assert "simple-copy-all-btn" in index_html
    assert "simple-tp-explainer" in index_html
    assert "怎麼止盈？" in index_html
    assert "simple-signals-grid" in index_html
    assert ".simple-grid" in style_css
    assert "trades.json" in app_js
    assert "watch-scope-tabs" in index_html
    assert "watch_today_daily.json" in app_js
    assert "watch_today_b.json" not in app_js
    assert "liquidity_heatmap.json" not in app_js
    assert "短線池 (A)" not in index_html
    assert "長線池 (B)" not in index_html
    assert "$20,000" in index_html
    assert "$80,000" in index_html
    assert "L4" in index_html
    assert "S1_RETIRED_NOTE" in app_js

    # Mobile responsive card table + horizontal nav scroll rules
    assert "@media (max-width: 640px)" in style_css
    assert ".data-table.mobile-cards" in style_css
    assert "overflow-x: auto" in style_css
    assert "flex-wrap: nowrap" in style_css

    # UI feature hooks & Phase 1 6-strategy results table
    assert "watch-summary-bar" in index_html
    assert "watch-toggle-btn" in index_html
    assert "六個策略回測比較" in index_html
    assert "樣本外交易數" in index_html
    assert "適用標的數" in index_html
    assert "next-scan-est" in index_html
    assert "此時段不計算" in app_js
    assert "ageMin > 40" in app_js
    assert "排程延遲" in app_js
    assert "convertReasonUtcToTaipei" in app_js

    # Run JS helper verification under Node (CI) or JavaScriptCore (macOS)
    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None, "Expected either node or macOS jsc to verify frontend helpers"

    script = f"""
        const stubEl = () => ({{
          classList: {{ add: () => {{}}, remove: () => {{}} }},
          addEventListener: () => {{}},
          innerHTML: '',
          textContent: '',
          value: 'all',
          dataset: {{}},
          disabled: false,
        }});
        var navigator = {{}};
        var window = {{
          location: {{ hash: '#/wall' }},
          addEventListener: () => {{}},
          SIGNAL_DESK_CONFIG: {{}},
        }};
        var document = {{
          getElementById: stubEl,
          querySelectorAll: () => [],
          querySelector: () => stubEl(),
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
        if (u.formatTaipeiTime('2026-11-16T14:51:00Z') !== '11/16 22:51') {{
          throw new Error('formatTaipeiTime failed: ' + u.formatTaipeiTime('2026-11-16T14:51:00Z'));
        }}
        if (u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性') !== '目前 台北 16:00 非低流動性') {{
          throw new Error('UTC->Taipei failed: ' + u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性'));
        }}
        const copyTxt = u.formatSignalCopyText({{
          signal_id: 'SIG-20261116-0009',
          strategy: 'S2',
          symbol: 'BTCUSDT',
          direction: 'long',
          avg_entry_price: 65291,
          entered_at_utc: '2026-11-16T14:51:00Z',
          stop_loss: 64013.62,
          risk_usd: 100,
          notional_usd: 5100,
          subaccount_capital_usd: 10000,
          exit_rule: '止損，或台北時間 07:45 前強制平倉',
        }});
        if (!copyTxt.includes('11/16 22:51') || !copyTxt.includes('出場規則：') || copyTxt.includes('TP1') || copyTxt.includes('槓桿')) {{
          throw new Error('formatSignalCopyText check failed: ' + copyTxt);
        }}
        const pepe = u.formatNumber(0.000004123);
        if (!pepe || !pepe.includes('4123')) {{
          throw new Error('Small price precision failed: ' + pepe);
        }}
        if (u.formatNumber(0.000003882) !== '0.000003882') {{
          throw new Error('PEPE precision failed: ' + u.formatNumber(0.000003882));
        }}
        if (u.formatNumber(2.46312) !== '2.4631') {{
          throw new Error('2.4631 precision failed: ' + u.formatNumber(2.46312));
        }}
        if (u.formatNumber(47.0694) !== '47.069') {{
          throw new Error('47.069 precision failed: ' + u.formatNumber(47.0694));
        }}
        if (u.formatExactPrice(7339.245247) !== '7339.245247') {{
          throw new Error('Exact price rounded unexpectedly: ' + u.formatExactPrice(7339.245247));
        }}

        // Simple Signal exit point rules: long (higher), short (lower), null trailing (SL), L3 suffix
        const exLong = u.computeSimpleExitPoint({{ direction: 'long', current_stop_loss: 95, trailing_exit_price: 98, strategy: 'S2' }});
        if (exLong.price !== 98 || exLong.suffix !== '') throw new Error('exLong higher failed: ' + JSON.stringify(exLong));
        const exLongSl = u.computeSimpleExitPoint({{ direction: 'long', current_stop_loss: 99, trailing_exit_price: 97, strategy: 'L1' }});
        if (exLongSl.price !== 99) throw new Error('exLong SL failed: ' + JSON.stringify(exLongSl));
        const exShort = u.computeSimpleExitPoint({{ direction: 'short', current_stop_loss: 105, trailing_exit_price: 102, strategy: 'S2' }});
        if (exShort.price !== 102 || exShort.suffix !== '') throw new Error('exShort lower failed: ' + JSON.stringify(exShort));
        const exNull = u.computeSimpleExitPoint({{ direction: 'short', current_stop_loss: 105, trailing_exit_price: null, strategy: 'L3' }});
        if (exNull.price !== 105 || exNull.suffix !== '（另外每週檢查排名）') throw new Error('exNull/L3 failed: ' + JSON.stringify(exNull));

        // Simple Signal PnL calculation: long, short, + realized_pnl_usd, null price -> "—"
        const pnlLong = u.computeSimpleTradePnl({{ direction: 'long', avg_entry_price: 100, remaining_qty: 10, realized_pnl_usd: -2.4 }}, 105.5);
        if (pnlLong.text !== '+$53' || pnlLong.cls !== 'chg-up') throw new Error('pnlLong failed: ' + JSON.stringify(pnlLong));
        const pnlShort = u.computeSimpleTradePnl({{ direction: 'short', avg_entry_price: 100, remaining_qty: 10, realized_pnl_usd: -1.0 }}, 103.0);
        if (pnlShort.text !== '-$31' || pnlShort.cls !== 'chg-down') throw new Error('pnlShort failed: ' + JSON.stringify(pnlShort));
        const pnlMissing = u.computeSimpleTradePnl({{ direction: 'long', avg_entry_price: 100, remaining_qty: 10, realized_pnl_usd: 0 }}, null);
        if (pnlMissing.text !== '—') throw new Error('pnlMissing failed: ' + JSON.stringify(pnlMissing));

        // Display names & card builder (sorting, filtering active, reason extraction, position integer formatting)
        if (u.formatSimpleSymbolName('COPPER', 'commodity') !== 'Copper（銅）') throw new Error('COPPER name failed');
        if (u.formatSimpleSymbolName('XAGUSD', 'commodity') !== 'Silver（白銀）') throw new Error('Silver name failed');
        if (u.formatSimpleSymbolName('ICPUSDT', 'crypto') !== 'ICP') throw new Error('ICP name failed');

        const cards = u.buildSimpleSignalCardsData(
          [
            {{ signal_id: 'SIG-1', strategy: 'S2', symbol: 'ICPUSDT', market: 'crypto', direction: 'short', state: 'active', entry_price: 2.4631, avg_entry_price: 2.4631, remaining_qty: 1000, current_stop_loss: 2.6, trailing_exit_price: 2.55, planned_notional_usd: 15000, realized_pnl_usd: -5 }},
            {{ signal_id: 'SIG-2', strategy: 'L3', symbol: 'BTCUSDT', market: 'crypto', direction: 'long', state: 'active', entry_price: 90000, avg_entry_price: 90000, remaining_qty: 0.1, current_stop_loss: 85000, trailing_exit_price: null, planned_notional_usd: 9000, realized_pnl_usd: -2 }},
            {{ signal_id: 'SIG-3', strategy: 'L4', symbol: 'COPPER', market: 'commodity', direction: 'long', state: 'closed', entry_price: 4.5, avg_entry_price: 4.5, remaining_qty: 0, current_stop_loss: 4.3, planned_notional_usd: 5000, realized_pnl_usd: 10 }},
          ],
          {{
            'SIG-1': {{ signal_id: 'SIG-1', verification_status: '觀察中（未通過驗證）', reasons: ['策略狀態：觀察中（未通過驗證）', 'S2 跌破低點'] }},
            'SIG-2': {{ signal_id: 'SIG-2', verification_status: '已驗證', reasons: ['策略狀態：已驗證', 'L3 動能排名前列'] }},
          }},
          {{ strategies: [{{ strategy_id: 'S2', passed: false }}, {{ strategy_id: 'L3', passed: true }}] }},
          (sym) => (sym === 'ICPUSDT' ? 2.4 : null),
          []
        );
        if (cards.length !== 2 || cards[0].signal_id !== 'SIG-2' || cards[1].signal_id !== 'SIG-1') {{
          throw new Error('Sorting/filtering failed: ' + JSON.stringify(cards));
        }}
        if (cards[0].posText !== '本金 $1,800 × 5 倍 = $9,000' || !cards[0].exitStr.includes('（另外每週檢查排名）') || cards[0].pnlInfo.text !== '—') {{
          throw new Error('L3 card check failed: ' + JSON.stringify(cards[0]));
        }}
        if (cards[1].dot !== '🔴' || cards[1].displayName !== 'ICP' || cards[1].reasonLine !== 'S2 跌破低點（S2 短線，觀察中）' || cards[1].exitStr !== '2.55' || cards[1].pnlInfo.text !== '+$58') {{
          throw new Error('ICP card check failed: ' + JSON.stringify(cards[1]));
        }}

        // --- Open-now signals eligibility, sizing %, history back-calc, sorting & copy formats ---
        // 1) Long removal conditions: +0.5R boundary vs exceeded vs stop-hit
        const longOk = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'L1', direction: 'long', avg_entry_price: 100, current_stop_loss: 90 }}, 105);
        if (!longOk.eligible) throw new Error('Long exact +0.5R boundary should remain eligible');
        const longTooFar = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'L1', direction: 'long', avg_entry_price: 100, current_stop_loss: 90 }}, 105.01);
        if (longTooFar.eligible || longTooFar.reason !== 'exceeded_half_r') throw new Error('Long > +0.5R should be removed: ' + JSON.stringify(longTooFar));
        const longStopped = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'L1', direction: 'long', avg_entry_price: 100, current_stop_loss: 90 }}, 90);
        if (longStopped.eligible || longStopped.reason !== 'hit_stop') throw new Error('Long hit stop should be removed');

        // 2) Short removal conditions: -0.5R boundary vs exceeded vs stop-hit, plus missing live price allowed
        const shortOk = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'S2', direction: 'short', avg_entry_price: 100, current_stop_loss: 110 }}, 95);
        if (!shortOk.eligible) throw new Error('Short exact -0.5R boundary should remain eligible');
        const shortTooFar = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'S2', direction: 'short', avg_entry_price: 100, current_stop_loss: 110 }}, 94.99);
        if (shortTooFar.eligible || shortTooFar.reason !== 'exceeded_half_r') throw new Error('Short > 0.5R favorable should be removed');
        const shortStopped = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'S2', direction: 'short', avg_entry_price: 100, current_stop_loss: 110 }}, 110.5);
        if (shortStopped.eligible || shortStopped.reason !== 'hit_stop') throw new Error('Short hit stop should be removed');
        const noLive = u.evaluateOpenNowEligibility({{ state: 'active', strategy: 'S3', direction: 'long', avg_entry_price: 100, current_stop_loss: 96 }}, null);
        if (!noLive.eligible || noLive.hasLivePrice !== false) throw new Error('Missing live price should still be eligible');
        const closedSkip = u.evaluateOpenNowEligibility({{ state: 'closed', strategy: 'L2', direction: 'long', avg_entry_price: 100, current_stop_loss: 96 }}, 100);
        if (closedSkip.eligible) throw new Error('Closed trade must be removed');

        // 3) Position % formula & sizing (leverage, margin %, loss %)
        const sz25 = u.computeOpenNowSizing(100, 96, 10);
        if (sz25.marginPctStr !== '2.5%' || sz25.lossPctStr !== '1%') throw new Error('4% SL sizing failed: ' + JSON.stringify(sz25));
        const sz250 = u.computeOpenNowSizing(100, 99.6, 10);
        if (sz250.marginPctStr !== '25%' || sz250.lossPctStr !== '1%') throw new Error('0.4% SL sizing failed: ' + JSON.stringify(sz250));
        const szCap = u.computeOpenNowSizing(100, 99.9, 10);
        if (szCap.notionalPct !== 500 || szCap.marginPctStr !== '50%' || szCap.lossPctStr !== '0.5%') throw new Error('0.1% SL sizing cap failed: ' + JSON.stringify(szCap));
        if (u.formatPercentDisplay(0.04) !== '<0.1%') throw new Error('<0.1% display failed: ' + u.formatPercentDisplay(0.04));
        if (u.formatPercentDisplay(1.0) !== '1%') throw new Error('1% display failed: ' + u.formatPercentDisplay(1.0));
        if (u.formatPercentDisplay(2.56) !== '2.6%') throw new Error('2.6% display failed: ' + u.formatPercentDisplay(2.56));

        // formatCardPrice & formatCardPricePair tiered decimals:
        // >=1000 -> 2; 100..1000 -> 3; 1..100 -> 5; <1 -> >=5 sig figs
        if (u.formatCardPrice(7339.245247) !== '7339.25') throw new Error('formatCardPrice >=1000 failed: ' + u.formatCardPrice(7339.245247));
        if (u.formatCardPrice(520.1234) !== '520.123') throw new Error('formatCardPrice 100..1000 failed: ' + u.formatCardPrice(520.1234));
        if (u.formatCardPrice(1.321454) !== '1.32145') throw new Error('formatCardPrice 1..100 GBPUSD failed: ' + u.formatCardPrice(1.321454));
        if (u.formatCardPrice(1.430123) !== '1.43012') throw new Error('formatCardPrice 1..100 USDCAD failed: ' + u.formatCardPrice(1.430123));
        if (u.formatCardPrice(0.839612) !== '0.83961') throw new Error('formatCardPrice <1 0.83961 failed: ' + u.formatCardPrice(0.839612));
        if (u.formatCardPrice(0.00000412345) !== '0.0000041235') throw new Error('formatCardPrice PEPE 5 sig figs failed: ' + u.formatCardPrice(0.00000412345));

        // formatCardPricePair collision resolution (same entry/sl expands up to 8 decimals)
        const pairEqualBase = u.formatCardPricePair(1.320001, 1.320004);
        if (pairEqualBase.entryStr !== '1.320001' || pairEqualBase.slStr !== '1.320004') {{
          throw new Error('Collision expansion failed: ' + JSON.stringify(pairEqualBase));
        }}
        const pairHigh = u.formatCardPricePair(1000.001, 1000.004);
        if (pairHigh.entryStr !== '1000.001' || pairHigh.slStr !== '1000.004') {{
          throw new Error('High price collision expansion failed: ' + JSON.stringify(pairHigh));
        }}
        const pairIdentical = u.formatCardPricePair(1.32, 1.32);
        if (pairIdentical.entryStr !== '1.32000' || pairIdentical.slStr !== '1.32000') {{
          throw new Error('Exact equal should not loop forever: ' + JSON.stringify(pairIdentical));
        }}

        // 4) History stats & avgWinR back-calculation formula: (expR + (1 - wr)) / wr
        const mockBt = {{
          strategies: [
            {{ strategy_id: 'L2', name: '唐奇安突破', out_of_sample: {{ trades: 50, win_rate_pct: 37, expectancy_r: 0.443 }} }},
            {{ strategy_id: 'S2', name: '4 小時突破', out_of_sample: {{ trades: 40, win_rate_pct: 35, expectancy_r: -0.05 }} }},
            {{ strategy_id: 'L4', name: '主流週線趨勢', out_of_sample: {{ trades: 0, win_rate_pct: 0, expectancy_r: 0 }} }},
          ],
        }};
        const hsL2 = u.computeOpenNowHistoryStats('L2', mockBt);
        if (!hsL2.available || hsL2.winRateInt !== 37 || hsL2.avgWinStr !== '+2.9%' || hsL2.copyStatsLine !== '策略歷史勝率 37%｜贏時平均 +2.9%｜輸時 −1%') {{
          throw new Error('L2 history stats failed: ' + JSON.stringify(hsL2));
        }}
        const hsS2 = u.computeOpenNowHistoryStats('S2', mockBt);
        if (!hsS2.available || hsS2.expStr !== '-0.05%' || hsS2.expCls !== 'chg-down') {{
          throw new Error('S2 negative exp stats failed: ' + JSON.stringify(hsS2));
        }}
        const hsL4 = u.computeOpenNowHistoryStats('L4', mockBt);
        if (hsL4.available !== false || hsL4.copyStatsLine !== '策略歷史數據暫無') {{
          throw new Error('Zero-trade strategy must show 策略歷史數據暫無: ' + JSON.stringify(hsL4));
        }}

        // 5) Full builder sorting (newest entered first), formatted prices, and copy format
        const openCards = u.buildOpenNowCardsData(
          [
            {{ signal_id: 'SIG-OLD', strategy: 'L4', symbol: 'BTCUSDT', market: 'crypto', direction: 'long', state: 'active', avg_entry_price: 90000.123456, current_stop_loss: 86400.123456, entered_at_utc: '2026-11-16T00:00:00Z' }},
            {{ signal_id: 'SIG-NEW', strategy: 'L2', symbol: 'SP500', market: 'index', direction: 'short', state: 'active', avg_entry_price: 7339.245247, current_stop_loss: 7625.250674, entered_at_utc: '2026-11-18T00:00:00Z' }},
            {{ signal_id: 'SIG-SKIP', strategy: 'S1', symbol: 'ETHUSDT', market: 'crypto', direction: 'long', state: 'active', avg_entry_price: 3000, current_stop_loss: 2900, entered_at_utc: '2026-11-18T02:00:00Z' }},
          ],
          mockBt,
          (sym) => (sym === 'SP500' ? 7300.5 : null),
          []
        );
        if (openCards.length !== 2 || openCards[0].signal_id !== 'SIG-NEW' || openCards[1].signal_id !== 'SIG-OLD') {{
          throw new Error('OpenNow sorting/filtering failed: ' + JSON.stringify(openCards));
        }}
        if (openCards[0].entryStr !== '7339.25' || openCards[0].slStr !== '7625.25' || openCards[0].marginPctStr !== '2.6%' || openCards[0].lossPctStr !== '1%') {{
          throw new Error('Card formatted values failed: ' + JSON.stringify(openCards[0]));
        }}
        if (openCards[1].livePriceText !== '現價暫無') {{
          throw new Error('Missing live price text failed: ' + openCards[1].livePriceText);
        }}
        const singleCopy = openCards[0].copyText;
        const expectedSingle = [
          '【做空】S&P 500（標普 500）（L2 唐奇安突破）',
          '入場：7339.25',
          '止損：7625.25（打到停損，預計虧損約總資產的 1%）',
          '建議：開 10 倍槓桿，用總資產的 2.6% 當保證金',
          '策略歷史勝率 37%｜贏時平均 +2.9%｜輸時 −1%',
          '止盈：移動停利，詳見 https://diabetepanda.github.io/pandatesttrading/',
        ].join('\\n');
        if (singleCopy !== expectedSingle) {{
          throw new Error('Single copy text mismatch:\\n' + singleCopy + '\\nVS\\n' + expectedSingle);
        }}
        const allCopy = u.formatOpenNowAllCopyText(openCards, '2026-11-18T05:30:00Z');
        if (!allCopy.startsWith('📊 Signal Desk 可開單（11/18 13:30 台北時間）\\n\\n') || !allCopy.endsWith('https://diabetepanda.github.io/pandatesttrading/') || !allCopy.includes('策略歷史數據暫無') || !allCopy.includes('開 10 倍槓桿')) {{
          throw new Error('All copy text check failed:\\n' + allCopy);
        }}

        // 6) Simulated account open positions table rendering with active positions
        const mockPositions = [
          {{
            signal_id: 'SIG-20261118-0001',
            symbol: 'BTCUSDT',
            strategy: 'L1',
            direction: 'long',
            state: 'active',
            entered_at_utc: '2026-11-18T10:00:00Z',
            avg_entry_price: 90000,
            remaining_qty: 0.1,
            notional_usd: 9000,
            mark_price: 90500,
            unrealized_pnl_usd: 50,
            current_stop_loss: 86400,
            exit_rule: '移動停損出場',
          }},
          {{
            signal_id: 'SIG-20261118-0002',
            symbol: 'EURUSD',
            strategy: 'S2',
            direction: 'short',
            state: 'active',
            entered_at_utc: '2026-11-18T11:00:00Z',
            avg_entry_price: 1.101156,
            total_qty: 600,
            planned_notional_usd: 660,
            mark_price: 1.1012,
            current_stop_loss: 1.105513,
            exit_rule: '止損出場',
          }},
        ];
        const openPosHtml = u.renderOpenPositionsHtml(mockPositions);
        if (openPosHtml.includes('目前尚無模擬持倉資料') || openPosHtml.includes('目前無持倉')) {{
          throw new Error('Positions table rendered empty state despite active positions: ' + openPosHtml);
        }}
        if (!openPosHtml.includes('SIG-20261118-0001') || !openPosHtml.includes('BTCUSDT') || !openPosHtml.includes('EURUSD')) {{
          throw new Error('Positions table missing expected position details: ' + openPosHtml);
        }}

        let capturedTbodyHtml = '';
        const prevGetElementById = document.getElementById;
        document.getElementById = (id) => {{
          if (id === 'account-positions-tbody') {{
            return {{
              set innerHTML(val) {{ capturedTbodyHtml = val; }},
              get innerHTML() {{ return capturedTbodyHtml; }},
            }};
          }}
          return stubEl();
        }};
        u.renderSimulatedAccount({{ account: {{ equity_usd: 100000 }}, open_positions: mockPositions }});
        document.getElementById = prevGetElementById;
        if (capturedTbodyHtml.includes('目前尚無模擬持倉資料') || !capturedTbodyHtml.includes('SIG-20261118-0001')) {{
          throw new Error('renderSimulatedAccount failed to populate positions: ' + capturedTbodyHtml);
        }}
        """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"JS helper check failed ({js_bin}): {proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)


def test_no_legacy_strategy_ab_references() -> None:
    """Ensure legacy Strategy A/B and Round-1 six-strategy names are completely absent across code, UI, and docs."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    forbidden = [
        "策略 A",
        "策略 B",
        "Strategy A",
        "Strategy B",
        "strategy_a",
        "strategy_b",
        "RSI(2)",
        "雙軌均值回歸",
        "波動突破（Squeeze",
        "區間來回（Range",
        "跨資產動能輪動",
        "雙均線過濾",
    ]
    targets = [
        root / "index.html",
        root / "CHECKLIST.md",
        root / "SPEC.md",
        root / "README.md",
        root / "config" / "config.yaml",
    ]
    for folder in (root / "assets", root / "src", root / ".github" / "workflows"):
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                targets.append(p)

    offenders: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle!r}")
    assert not offenders, "Legacy Strategy A/B or Round-1 strategy references remain:\n" + "\n".join(offenders)


def test_no_policy_shadow_target_stage_residues() -> None:
    """Ensure multi-ledger and stage target scaling residues are completely removed."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    forbidden_patterns = [
        (r"policy", "policy"),
        (r"shadow", "shadow"),
        (r"target_notional", "target_notional"),
        (r"stage_target", "stage_target"),
        (r"\bledger\s+[a-d]\b", "ledger A~D"),
        (r"\bledgers\.[a-e]\b", "ledgers.A~E"),
    ]
    targets = [
        root / "index.html",
        root / "config" / "config.yaml",
    ]
    for folder in (root / "assets", root / "src"):
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                targets.append(p)

    offenders: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for pat, label in forbidden_patterns:
            if re.search(pat, text):
                offenders.append(f"{path.relative_to(root)}: {label!r}")
    assert not offenders, "Legacy residues remain:\n" + "\n".join(offenders)


def test_simulated_account_positions_table_rendering() -> None:
    """Ensure simulated account positions table lists active positions and never shows empty state when positions exist."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")

    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None, "Expected either node or macOS jsc"

    script = f"""
        const stubEl = () => ({{
          classList: {{ add: () => {{}}, remove: () => {{}} }},
          addEventListener: () => {{}},
          innerHTML: '',
          textContent: '',
          value: 'all',
          dataset: {{}},
          disabled: false,
        }});
        var navigator = {{}};
        var window = {{
          location: {{ hash: '#/account' }},
          addEventListener: () => {{}},
          SIGNAL_DESK_CONFIG: {{}},
        }};
        let tbodyHtml = '';
        var document = {{
          getElementById: (id) => {{
            if (id === 'account-positions-tbody') {{
              return {{
                set innerHTML(val) {{ tbodyHtml = val; }},
                get innerHTML() {{ return tbodyHtml; }},
              }};
            }}
            return stubEl();
          }},
          querySelectorAll: () => [],
          querySelector: () => stubEl(),
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
        const mockPositions = [
          {{
            signal_id: 'SIG-POS-001',
            symbol: 'XAUUSD',
            market: 'commodity',
            strategy: 'L1',
            direction: 'long',
            state: 'active',
            entered_at_utc: '2026-11-18T08:00:00Z',
            avg_entry_price: 2650.5,
            total_qty: 2.0,
            notional_usd: 5301,
            mark_price: 2680.0,
            unrealized_pnl_usd: 59.0,
            current_stop_loss: 2620.0,
            exit_rule: '移動停損出場',
          }},
          {{
            signal_id: 'SIG-POS-002',
            symbol: 'NAS100',
            market: 'index',
            strategy: 'L2',
            direction: 'short',
            state: 'active',
            entered_at_utc: '2026-11-18T09:00:00Z',
            avg_entry_price: 21000,
            remaining_qty: 0.5,
            planned_notional_usd: 10500,
            mark_price: 20950,
            current_stop_loss: 21300,
            exit_rule: '跌破均線出場',
          }},
        ];

        // 1. Direct helper rendering
        const html = u.renderOpenPositionsHtml(mockPositions);
        if (html.includes('目前尚無模擬持倉資料') || html.includes('目前無持倉')) {{
          throw new Error('renderOpenPositionsHtml produced empty text with active positions: ' + html);
        }}
        if (!html.includes('SIG-POS-001') || !html.includes('XAUUSD') || !html.includes('SIG-POS-002') || !html.includes('NAS100')) {{
          throw new Error('renderOpenPositionsHtml missing positions: ' + html);
        }}

        // 2. Full renderSimulatedAccount integration with DOM
        u.renderSimulatedAccount({{
          account: {{ equity_usd: 100000, short_term_equity_usd: 20000, long_term_equity_usd: 80000, total_open_notional_usd: 15801 }},
          open_positions: mockPositions,
        }});
        if (tbodyHtml.includes('目前尚無模擬持倉資料') || tbodyHtml.includes('目前無持倉')) {{
          throw new Error('renderSimulatedAccount produced empty text with active positions: ' + tbodyHtml);
        }}
        if (!tbodyHtml.includes('SIG-POS-001') || !tbodyHtml.includes('2,650.5')) {{
          throw new Error('renderSimulatedAccount did not populate tbody properly: ' + tbodyHtml);
        }}
    """

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"JS helper check failed ({js_bin}): {proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)


def test_in_depth_research_json_and_rendering() -> None:
    """Verify in-depth research JSON structure, Article 1/2/3 content, and JS rendering & redirect."""
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from research.build_research_json import build_research_dict

    root = Path(__file__).resolve().parent.parent
    data = build_research_dict()

    # 1. Articles structure validation (Chronological order: short_term -> combo -> exam_sensitivity)
    assert len(data["articles"]) == 3
    art1, art2, art3 = data["articles"][0], data["articles"][1], data["articles"][2]

    # Article 1: Contest short-term (Earliest research)
    assert art1["id"] == "short_term"
    assert art1["title"] == "短線策略能拿去比賽嗎？"
    assert "我們測了 7 種短線策略" in art1["takeaway"]
    assert "沒有一個有穩定優勢" in art1["takeaway"]
    assert len(art1["strategies_intro"]) == 7
    assert [s["id"] for s in art1["strategies_intro"]] == ["A", "B", "C", "D", "E", "F", "G"]
    assert "The5ers MT5" in art1["methodology"]["costs"]
    assert "0.001%" in art1["methodology"]["costs"]
    assert "獲利因子" in art1["methodology"]["definitions"]["pf"]
    assert "期望值" in art1["methodology"]["definitions"]["exp_r"]
    assert len(art1["round2"]["tf_1h"]["strategies"]) == 7
    assert len(art1["round2"]["tf_15m"]["strategies"]) == 7
    assert all(not s["passed"] for s in art1["round2"]["tf_1h"]["strategies"])
    assert all(not s["passed"] for s in art1["round2"]["tf_15m"]["strategies"])
    assert len(art1["round1_summary"]["strategies"]) == 4
    assert "那是運氣，不是策略" in art1["contest_sim"]["quote"]
    assert "L2 是日線長波段策略" in art1["contest_sim"]["l2_note"] or "通常不出手" in art1["contest_sim"]["l2_note"]
    l2_short_row = next(r for r in art1["contest_sim"]["rows"] if r.get("is_baseline"))
    assert l2_short_row["median_return_pct"] == 0.0
    assert l2_short_row["bust_rate_pct"] == 0.0
    assert len(art1["learnings"]) >= 4

    # Article 2: Combo research (Second research)
    assert art2["id"] == "combo"
    assert art2["title"] == "截長補短，能不能把策略組合得更好？"
    assert "把幾個長線策略一起跑能把回撤砍半" in art2["takeaway"]
    assert "考試帳戶仍用原版 L2" in art2["takeaway"]

    sec2 = art2["sections"]
    assert "三段切分" in sec2["anti_self_deception"]["title"]
    assert "61 組" in sec2["anti_self_deception"]["title"] or "61" in str(sec2["anti_self_deception"])
    assert "5a 多策略組合" in sec2["truly_useful"]["title"]
    assert "回撤砍半" in sec2["truly_useful"]["title"]
    assert "18.4%" in str(sec2["truly_useful"]["content"]) or "18%" in str(sec2["truly_useful"]["content"])
    assert "4a 長線策略合議" in sec2["apparent_improvement"]["title"] or "策略合議" in sec2["apparent_improvement"]["title"]
    assert "3d" in str(sec2["apparent_improvement"]["content"])
    assert "S2 正式退休" in sec2["proven_useless"]["title"] or "S2" in str(sec2["proven_useless"]["content"])
    assert "大牛市" in sec2["cautions"]["title"] or "大牛市" in str(sec2["cautions"]["content"])

    # Tables in Article 2
    lockbox_table = art2["tables"]["lockbox_comparison"]
    assert len(lockbox_table) >= 7  # L2, L1 + 5 candidates
    l2_row = next(r for r in lockbox_table if r["id"] == "L2_orig")
    assert l2_row["profit_factor"] >= 3.0
    l1_row = next(r for r in lockbox_table if r["id"] == "L1_orig")
    assert l1_row["profit_factor"] >= 1.5

    kraken_table = art2["tables"]["kraken_scenarios"]
    assert len(kraken_table) >= 8
    k_l2 = next(r for r in kraken_table if r["id"] == "L2_orig")
    assert k_l2["pass_rate_pct"] > 90.0
    assert k_l2["bust_rate_pct"] < 6.0
    k_5a = next(r for r in kraken_table if r["id"] == "5a_multi_combo")
    assert k_5a["pass_rate_pct"] < 25.0
    assert k_5a["bust_rate_pct"] > 50.0

    # Article 3: Exam sensitivity (Latest research)
    assert art3["id"] == "exam_sensitivity"
    assert art3["title"] == "考試回撤規則放寬，結果會不一樣嗎？"
    assert "回撤放寬到 10% 主要是讓爆倉風險幾乎歸零" in art3["takeaway"]
    assert "有 30 天時限的考試幾乎過不了" in art3["takeaway"]
    sec3 = art3["sections"]
    assert "一、我們模擬了什麼？" in sec3["what_we_simulated"]["title"]
    assert "二、主要結果表" in sec3["best_combos"]["title"]
    assert "三、10% 回撤策略矩陣" in sec3["matrix_10pct"]["title"]
    assert "四、三個重點發現" in sec3["three_findings"]["title"]
    assert "五、要打折看的地方" in sec3["caveats"]["title"]
    assert "六、給考生的實戰操作建議" in sec3["advice"]["title"]
    assert len(art3["tables"]["best_combos"]) == 4
    assert len(art3["tables"]["single_slot_matrix"]) == 6
    assert len(art3["tables"]["multi_slot_matrix"]) == 6

    # Files existence (single canonical data/research.json; legacy copies removed)
    data_json_path = root / "data" / "research.json"
    assert data_json_path.exists()
    assert not (root / "assets" / "research.json").exists()
    assert not (root / "data" / "contest_research.json").exists()
    assert not (root / "assets" / "contest_research.json").exists()

    # 2. JS rendering test via Node / JSC
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")
    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    assert js_bin is not None

    script = f"""
        const stubEl = () => ({{
          classList: {{
            _classes: new Set(),
            add: function(c) {{ this._classes.add(c); }},
            remove: function(c) {{ this._classes.delete(c); }},
            contains: function(c) {{ return this._classes.has(c); }},
            toggle: function(c) {{
              if (this._classes.has(c)) {{ this._classes.delete(c); return false; }}
              this._classes.add(c); return true;
            }}
          }},
          addEventListener: () => {{}},
          scrollIntoView: () => {{}},
          innerHTML: '',
          textContent: '',
          value: 'all',
          dataset: {{}},
          disabled: false,
        }});
        var navigator = {{}};
        var window = {{
          location: {{ hash: '#/research' }},
          addEventListener: () => {{}},
          SIGNAL_DESK_CONFIG: {{}},
        }};
        var document = {{
          getElementById: () => stubEl(),
          querySelectorAll: () => [],
          querySelector: () => stubEl(),
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
        const mockData = {json.dumps(data)};
        const contestHtml = u.renderContestResearchHtml(mockData.articles[0]);
        if (!contestHtml.includes('A 跌深反轉') || !contestHtml.includes('G 掃停損反轉')) {{
          throw new Error('renderContestResearchHtml missing strategy intro');
        }}
        if (!contestHtml.includes('The5ers MT5') || !contestHtml.includes('獲利因子 (Profit Factor, PF)')) {{
          throw new Error('renderContestResearchHtml missing methodology');
        }}
        if (!contestHtml.includes('pf-fail')) {{
          throw new Error('renderContestResearchHtml missing pf-fail styling');
        }}
        if (!contestHtml.includes('那是運氣，不是策略')) {{
          throw new Error('renderContestResearchHtml missing contest quote');
        }}
        if (!contestHtml.includes('Signal Desk L2（基準）')) {{
          throw new Error('renderContestResearchHtml missing L2 baseline row');
        }}
        if (!contestHtml.includes('五、我們學到什麼？')) {{
          throw new Error('renderContestResearchHtml missing learnings');
        }}
        if (!contestHtml.includes('report_round2.md')) {{
          throw new Error('renderContestResearchHtml missing github link');
        }}

        const html = u.renderResearchHtml(mockData);

        // Verify TOC
        if (!html.includes('research-toc')) {{
          throw new Error('renderResearchHtml missing research-toc');
        }}
        if (!html.includes('第 1 篇') || !html.includes('第 2 篇') || !html.includes('第 3 篇')) {{
          throw new Error('renderResearchHtml missing TOC article markers');
        }}

        // Verify TOC order: short_term (1) -> combo (2) -> exam_sensitivity (3)
        const tocShort = html.indexOf("scrollToResearchArticle('short_term')");
        const tocCombo = html.indexOf("scrollToResearchArticle('combo')");
        const tocExam = html.indexOf("scrollToResearchArticle('exam_sensitivity')");
        if (tocShort === -1 || tocCombo === -1 || tocExam === -1 || tocShort >= tocCombo || tocCombo >= tocExam) {{
          throw new Error('TOC cards not in chronological order (short_term -> combo -> exam_sensitivity)');
        }}

        // Verify article headers order and numbering
        const idx1 = html.indexOf('第 1 篇：短線策略能拿去比賽嗎？');
        const idx2 = html.indexOf('第 2 篇：截長補短，能不能把策略組合得更好？');
        const idx3 = html.indexOf('第 3 篇：考試回撤規則放寬，結果會不一樣嗎？');
        if (idx1 === -1 || idx2 === -1 || idx3 === -1 || idx1 >= idx2 || idx2 >= idx3) {{
          throw new Error('Article sections not rendered in chronological order (1 -> 2 -> 3)');
        }}

        // Verify default expansion: Article 1 expanded, Article 2 & 3 collapsed
        if (html.includes('class="article-body collapsed" id="body-short_term"') || html.includes('id="body-short_term" class="article-body collapsed"')) {{
          throw new Error('Article 1 should be expanded by default (no collapsed class)');
        }}
        if (!html.includes('class="article-body collapsed" id="body-combo"') && !html.includes('id="body-combo" class="article-body collapsed"')) {{
          throw new Error('Article 2 should be collapsed by default');
        }}
        if (!html.includes('class="article-body collapsed" id="body-exam_sensitivity"') && !html.includes('id="body-exam_sensitivity" class="article-body collapsed"')) {{
          throw new Error('Article 3 should be collapsed by default');
        }}

        // Verify Article 1 content (Contest short-term)
        if (!html.includes('我們測了 7 種短線策略')) {{
          throw new Error('renderResearchHtml missing Article 1 takeaway');
        }}

        // Verify Article 2 content (Combo)
        if (!html.includes('5a 多策略組合')) {{
          throw new Error('renderResearchHtml missing 5a combo');
        }}
        if (!html.includes('S2 正式退休')) {{
          throw new Error('renderResearchHtml missing S2 retirement');
        }}
        if (!html.includes('海妖考試情境模擬')) {{
          throw new Error('renderResearchHtml missing Kraken simulation');
        }}

        // Verify Article 3 content (Exam sensitivity)
        if (!html.includes('回撤放寬到 10% 主要是讓爆倉風險幾乎歸零')) {{
          throw new Error('renderResearchHtml missing Article 3 takeaway');
        }}
        if (!html.includes('各回撤底線下的最佳配置')) {{
          throw new Error('renderResearchHtml missing Article 3 best combos');
        }}
        if (!html.includes('10% 回撤策略矩陣')) {{
          throw new Error('renderResearchHtml missing Article 3 matrix');
        }}
        if (!html.includes('三個重點發現')) {{
          throw new Error('renderResearchHtml missing Article 3 findings');
        }}
        if (!html.includes('exam_sensitivity.md')) {{
          throw new Error('renderResearchHtml missing Article 3 github link');
        }}

        // Verify responsive table wrapper and disclaimers
        if (!html.includes('table-responsive')) {{
          throw new Error('renderResearchHtml missing table-responsive');
        }}
        if (!html.includes('免責聲明') || !html.includes('過去表現不代表未來')) {{
          throw new Error('renderResearchHtml missing disclaimer');
        }}
        if (!html.includes('research/combo/report.md')) {{
          throw new Error('renderResearchHtml missing combo report github link');
        }}

        // 3. Test routing redirect for legacy routes
        window.location.hash = '#/contest-research';
        u.applyRoute();
        if (window.location.hash !== '#/research') {{
          throw new Error('Redirect failed for #/contest-research, got: ' + window.location.hash);
        }}

        window.location.hash = '#/research-short';
        u.applyRoute();
        if (window.location.hash !== '#/research') {{
          throw new Error('Redirect failed for #/research-short, got: ' + window.location.hash);
        }}
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name
    try:
        proc = subprocess.run([js_bin, tmp_js], capture_output=True, text=True)
        assert proc.returncode == 0, f"JS render and route check failed: {proc.stderr or proc.stdout}"
    finally:
        Path(tmp_js).unlink(missing_ok=True)




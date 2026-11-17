"""Unit tests for configuration and control files."""

from __future__ import annotations

from src.config_loader import load_config, load_control, load_events


def test_config_has_required_sections() -> None:
    cfg = load_config()
    assert cfg["project"]["github_owner"] == "diabetepanda"
    assert cfg["project"]["github_repo"] == "pandatesttrading"
    assert cfg["account"]["initial_capital_usd"] == 100000.0
    assert cfg["account"]["pools"]["short_term"]["capital_usd"] == 30000.0
    assert cfg["account"]["pools"]["long_term"]["capital_usd"] == 70000.0
    assert "strategy_a" not in cfg
    assert "strategy_b" not in cfg
    assert set(cfg["strategies"].keys()) == {"S1", "S2", "S3", "L1", "L2", "L3"}
    total_sub = sum(float(v["capital_usd"]) for v in cfg["strategies"].values())
    assert abs(total_sub - 100000.0) < 0.05
    assert cfg["backtest"]["risk_per_trade_pct"] == 1.0
    assert cfg["backtest"]["max_total_open_risk_pct"] == 10.0
    assert cfg["backtest"]["max_concurrent_positions"] == 10
    assert cfg["backtest"]["max_position_notional_pct"] == 500.0
    assert cfg["backtest"]["max_total_notional_multiple"] == 5.0
    for removed_risk_key in (
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
    for removed_sig_key in (
        "tp1_close_fraction",
        "breakeven_trigger_r",
        "trailing_stop_trigger_r",
        "order_expiry_hours",
        "max_hold_bars_15m",
        "max_daily_signals",
    ):
        assert removed_sig_key not in cfg["signals"]
    enabled = [inst for inst in cfg["instruments"] if inst.get("enabled", True)]
    assert len(enabled) == 42
    assert sum(1 for inst in enabled if inst["market"] == "crypto") == 33
    assert sum(1 for inst in enabled if inst["market"] == "commodity") == 5
    assert sum(1 for inst in enabled if inst["market"] == "forex") == 0
    assert sum(1 for inst in enabled if inst["market"] == "index") == 4
    index_syms = {inst["symbol"] for inst in enabled if inst["market"] == "index"}
    assert index_syms == {"NAS100", "SP500", "JP225", "TWII"}
    assert all(inst.get("daily_only") is True for inst in enabled if inst["market"] == "index")


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
        "FX:EURUSD",
        "FX:USDJPY",
        "FX:EURGBP",
        "FX:GBPJPY",
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
    assert "PEPEUSDT" in config_js
    assert "TONUSDT" in config_js

    # Stage 4, 5 & 6 hash routes, disclaimer, backtest results, and fixed CDN check
    for route in ("#/simple", "#/wall", "#/results", "#/stats", "#/account", "#/risk", "#/data"):
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
    assert "?v=20261117b" in index_html
    assert "simple-signals-grid" in index_html
    assert ".simple-grid" in style_css
    assert "trades.json" in app_js
    assert "watch-scope-tabs" in index_html
    assert "watch_today_daily.json" in app_js
    assert "watch_today_b.json" not in app_js
    assert "liquidity_heatmap.json" not in app_js
    assert "短線池 (A)" not in index_html
    assert "長線池 (B)" not in index_html
    assert "$30,000" in index_html
    assert "$70,000" in index_html

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

    # If node is available, verify JS helper behavior directly
    node_bin = shutil.which("node")
    if node_bin:
        script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const stubEl = () => ({{
          classList: {{ add: () => {{}}, remove: () => {{}} }},
          addEventListener: () => {{}},
          innerHTML: '',
          textContent: '',
          value: 'all',
          dataset: {{}},
        }});
        const sandbox = {{
          window: {{
            location: {{ hash: '#/wall' }},
            addEventListener: () => {{}},
            SIGNAL_DESK_CONFIG: {{}},
          }},
          document: {{
            getElementById: stubEl,
            querySelectorAll: () => [],
            querySelector: () => stubEl(),
          }},
          setInterval: () => {{}},
          setTimeout: () => {{}},
          clearTimeout: () => {{}},
          fetch: async () => ({{ ok: false }}),
          console,
          Date,
          Intl,
          Math,
          Number,
          String,
          Array,
          Object,
          Set,
          Map,
          JSON,
        }};
        vm.runInNewContext(fs.readFileSync({repr(str(root / "assets" / "app.js"))}, 'utf8'), sandbox);
        const u = sandbox.window.__SIGNAL_DESK_UTILS__;
        if (u.formatTaipeiTime('2026-11-16T14:51:00Z') !== '11/16 22:51') {{
          throw new Error('formatTaipeiTime failed: ' + u.formatTaipeiTime('2026-11-16T14:51:00Z'));
        }}
        if (u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性') !== '目前 台北 16:00 非低流動性') {{
          throw new Error('UTC->Taipei failed: ' + u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性'));
        }}
        const copyTxt = u.formatSignalCopyText({{
          signal_id: 'SIG-20261116-0009',
          strategy: 'S1',
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
            {{ signal_id: 'SIG-3', strategy: 'S1', symbol: 'COPPER', market: 'commodity', direction: 'long', state: 'closed', entry_price: 4.5, avg_entry_price: 4.5, remaining_qty: 0, current_stop_loss: 4.3, planned_notional_usd: 5000, realized_pnl_usd: 10 }},
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
        """
        proc = subprocess.run([node_bin, "-e", script], capture_output=True, text=True)
        assert proc.returncode == 0, f"Node helper check failed: {proc.stderr or proc.stdout}"


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

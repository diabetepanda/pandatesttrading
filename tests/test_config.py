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
    assert cfg["risk"]["drawdown_warning_pct"] == 8.0
    assert cfg["risk"]["drawdown_pause_pct"] == 12.0
    enabled = [inst for inst in cfg["instruments"] if inst.get("enabled", True)]
    assert len(enabled) == 48
    assert sum(1 for inst in enabled if inst["market"] == "crypto") == 33
    assert sum(1 for inst in enabled if inst["market"] in ("forex", "commodity")) == 15


def test_instruments_have_valid_fields() -> None:
    cfg = load_config()
    seen_symbols = set()
    for inst in cfg["instruments"]:
        sym = inst["symbol"]
        assert sym not in seen_symbols
        seen_symbols.add(sym)
        assert inst["market"] in {"crypto", "forex", "commodity", "stock"}
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
    for code in ("FX:EURUSD", "FX:USDJPY", "FX:EURGBP", "FX:GBPJPY", "NYMEX:CL1!", "NYMEX:NG1!", "COMEX:GC1!", "COMEX:SI1!", "COMEX:HG1!"):
        assert code in config_js
    assert "PEPEUSDT" in config_js
    assert "TONUSDT" in config_js

    # Stage 4, 5 & 6 hash routes, disclaimer, backtest results, shadow comparison, and fixed CDN check
    for route in ("#/wall", "#/results", "#/stats", "#/account", "#/scaling", "#/risk", "#/data"):
        assert route in index_html
    assert "lightweight-charts@4.2.1" in index_html
    assert "backtest-section" in index_html
    assert "歷史不代表未來" in index_html
    assert "尚未驗證" in index_html
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")
    assert "模擬驗證中，非投資建議" in app_js
    assert "computeFloatingPnl" in app_js
    assert "backtest_results.json" in app_js
    assert "renderBacktestResults" in app_js
    for fname in ("shadow_comparison.json", "performance_stats.json", "risk_status.json", "equity_curve.json"):
        assert fname in app_js
    assert "renderScalingComparison" in app_js
    assert "renderRiskCenter" in app_js
    assert "尚未驗證" in app_js

    cfg = load_config()
    assert cfg["analytics"]["min_verified_trades_shadow"] >= 1
    assert cfg["analytics"]["min_verified_trades_stats"] >= 1


def test_frontend_ui_optimizations_and_mobile_css() -> None:
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    index_html = (root / "index.html").read_text(encoding="utf-8")
    style_css = (root / "assets" / "style.css").read_text(encoding="utf-8")
    app_js = (root / "assets" / "app.js").read_text(encoding="utf-8")

    # Cache-busting version bumped
    assert "?v=20261116d" in index_html

    # Mobile responsive card table + horizontal nav scroll rules
    assert "@media (max-width: 640px)" in style_css
    assert ".data-table.mobile-cards" in style_css
    assert "overflow-x: auto" in style_css
    assert "flex-wrap: nowrap" in style_css

    # UI feature hooks
    assert "watch-summary-bar" in index_html
    assert "watch-toggle-btn" in index_html
    assert "bt-near-box" in index_html
    assert "next-scan-est" in index_html
    assert "此時段不計算" in app_js
    assert "唐奇安通道突破" in app_js
    assert "均線交叉" in app_js
    assert "週線趨勢＋ATR" in app_js
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
        if (u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性') !== '目前 台北 16:00 非低流動性') {{
          throw new Error('UTC->Taipei failed: ' + u.convertReasonUtcToTaipei('目前 UTC 08:00 非低流動性'));
        }}
        const pepe = u.formatNumber(0.000004123);
        if (!pepe || !pepe.includes('4123')) {{
          throw new Error('Small price precision failed: ' + pepe);
        }}
        """
        proc = subprocess.run([node_bin, "-e", script], capture_output=True, text=True)
        assert proc.returncode == 0, f"Node helper check failed: {proc.stderr or proc.stdout}"

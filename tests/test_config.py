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
    assert len(cfg["instruments"]) >= 18


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

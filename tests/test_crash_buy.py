from pathlib import Path

from research.crash_buy.run_crash_buy import (
    EXIT_MODES,
    FILTER_MODES,
    REF_MODES,
    RISK_PCT,
    SL_MODES,
    THRESHOLDS_ALL,
    build_symbol_cost_params,
)
from src.config_loader import load_config

CRASH_DIR = Path(__file__).resolve().parent.parent / "research" / "crash_buy"
REPORT_PATH = CRASH_DIR / "report.md"


def test_crash_buy_report_exists():
    assert REPORT_PATH.is_file(), f"Missing {REPORT_PATH}"
    assert REPORT_PATH.stat().st_size > 3_000


def test_crash_buy_grid_and_cost_params():
    assert RISK_PCT == 0.0075
    assert len(THRESHOLDS_ALL) == 5
    assert len(REF_MODES) == 2
    assert len(EXIT_MODES) == 6
    assert len(SL_MODES) == 4
    assert len(FILTER_MODES) == 5

    cfg = load_config()
    crypto_cost = build_symbol_cost_params("BTCUSDT", 60000.0, cfg, {})
    assert crypto_cost["is_crypto"] == 1.0
    assert crypto_cost["fee_rate"] == 0.0005

    tradfi_cost = build_symbol_cost_params("XAUUSD", 2500.0, cfg, {})
    assert tradfi_cost["is_crypto"] == 0.0
    assert tradfi_cost["slippage_bps"] == 0.5


def test_crash_buy_report_sections():
    text = REPORT_PATH.read_text(encoding="utf-8")
    assert "單日大跌做多" in text
    assert "一句話結論" in text
    assert "2020/03" in text
    assert "2022" in text
    assert "與現有策略 (L1 / L2 / L3 / L4 / F4) 之重疊與衝突分析" in text
    assert "樣本不足，不可信" in text

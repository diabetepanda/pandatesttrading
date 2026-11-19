import json
from pathlib import Path


CRASH_DIR = Path(__file__).resolve().parent.parent / "research" / "crash_buy"
RESULTS_PATH = CRASH_DIR / "results.json"
REPORT_PATH = CRASH_DIR / "report.md"


def test_crash_buy_outputs_exist():
    assert RESULTS_PATH.is_file(), f"Missing {RESULTS_PATH}"
    assert REPORT_PATH.is_file(), f"Missing {REPORT_PATH}"
    assert RESULTS_PATH.stat().st_size > 10_000
    assert REPORT_PATH.stat().st_size > 3_000


def test_crash_buy_results_schema_and_grid():
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    assert data["metadata"]["risk_per_trade_pct"] == 0.75

    universes = data["universes"]
    assert set(universes.keys()) == {"all_1h", "tradfi_1h", "crypto_1h", "all_10y_1d"}

    # 2 ref * 4 or 5 thresholds * 6 exits * 4 sl * 5 filters
    assert universes["tradfi_1h"]["total_grid_combos"] == 960
    assert universes["crypto_1h"]["total_grid_combos"] == 1200
    assert universes["all_1h"]["total_grid_combos"] == 1200
    assert universes["all_10y_1d"]["total_grid_combos"] == 1200

    # Verify top IS-selected candidates have overlap and L2 correlation stats
    top_all = universes["all_1h"]["is_selected_top5"][0]
    assert "l2_corr_oos" in top_all
    assert "overlap_full" in top_all
    for sid in ("L1", "L2", "L3", "L4", "F4"):
        assert sid in top_all["overlap_full"]
        ov = top_all["overlap_full"][sid]
        assert abs(ov["long"]["share_pct"] + ov["short"]["share_pct"] + ov["flat"]["share_pct"] - 100.0) < 0.1

    # Verify 10Y crisis drawdowns exist
    top_10y = universes["all_10y_1d"]["is_selected_top5"][0]
    assert top_10y["full"]["dd_2020_03_pct"] is not None
    assert top_10y["full"]["dd_2022_pct"] is not None


def test_crash_buy_per_symbol_insufficient_flag():
    data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    per_sym = data["per_symbol_summary"]
    assert "SP500" in per_sym
    assert "XAUUSD" in per_sym
    assert "BTCUSDT" in per_sym
    # SP500 -5% OOS has < 20 trades -> flagged insufficient
    sp_5 = per_sym["SP500"]["prev_close"]["5%"]
    assert sp_5["oos_trades"] < 20
    assert sp_5["oos_flag"] == "樣本不足，不可信"


def test_crash_buy_report_sections():
    text = REPORT_PATH.read_text(encoding="utf-8")
    assert "單日大跌做多" in text
    assert "一句話結論" in text
    assert "2020/03" in text
    assert "2022" in text
    assert "與現有策略 (L1 / L2 / L3 / L4 / F4) 之重疊與衝突分析" in text
    assert "樣本不足，不可信" in text

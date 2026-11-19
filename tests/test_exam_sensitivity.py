import json
from pathlib import Path
import pytest

RESEARCH_DIR = Path(__file__).resolve().parent.parent / "research" / "combo"
RESULTS_FILE = RESEARCH_DIR / "exam_sensitivity_results.json"
REPORT_FILE = RESEARCH_DIR / "exam_sensitivity.md"


def test_exam_sensitivity_files_exist():
    """Verify that both output files exist and are not empty."""
    assert RESULTS_FILE.is_file(), f"Missing {RESULTS_FILE}"
    assert REPORT_FILE.is_file(), f"Missing {REPORT_FILE}"
    assert RESULTS_FILE.stat().st_size > 1000
    assert REPORT_FILE.stat().st_size > 1000


def test_exam_sensitivity_metadata_structure():
    """Verify metadata in results JSON covers all requested parameter grids."""
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "metadata" in data
    assert "simulations" in data
    meta = data["metadata"]

    # 6 strategies
    assert set(meta["strategies"]) == {
        "L2",
        "L1",
        "4a_daily_consensus",
        "3d_mtf_weekly",
        "5a_multi_combo",
        "2a_chandelier",
    }
    # 4 drawdown floors
    assert set(meta["dd_floors_pct"]) == {3.0, 5.0, 8.0, 10.0}
    # static vs trailing
    assert set(meta["dd_types"]) == {"static", "trailing"}
    # 5 risk levels
    assert set(meta["risk_per_trade_pct"]) == {0.5, 0.75, 1.0, 1.5, 2.0}
    # position limits: 1, 3, unlimited
    assert set(meta["position_limits"]) == {1, 3, "unlimited"}
    # profit targets: 8%, 10%
    assert set(meta["profit_targets_pct"]) == {8.0, 10.0}
    # time limits
    assert set(meta["time_limits"]) == {"none", "60d", "30d"}
    # datasets
    assert meta["cohort_count_full_period"] > 0
    assert meta["cohort_count_pre_lockbox"] > 0

    # Total grid combos: 6 * 2 * 4 * 5 * 3 * 2 = 1,440
    assert len(data["simulations"]) == 1440


def test_exam_sensitivity_baseline_reproduction():
    """Verify that L2 single-slot 3% DD static reproduces high pass rate baseline."""
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    sims = data["simulations"]
    # Look for L2, static, dd 3%, risk 0.75%, pos 1, tgt 8% (or 10%)
    cell_8 = sims.get("L2|static|dd_3|r_0.75|pos_1|tgt_8")
    assert cell_8 is not None

    full_none = cell_8["full_period"]["none"]
    # Pass rate should be > 90%, bust rate < 10%
    assert full_none["pass_rate_pct"] >= 90.0
    assert full_none["bust_rate_pct"] < 10.0
    assert full_none["median_pass_days"] is not None


def test_exam_sensitivity_ten_pct_dd_vs_three_pct():
    """Verify that 10% DD gives higher pass rate and lower bust rate than 3% DD at same risk."""
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    sims = data["simulations"]
    cell_3 = sims["L2|static|dd_3|r_1.0|pos_1|tgt_10"]
    cell_10 = sims["L2|static|dd_10|r_1.0|pos_1|tgt_10"]

    # At 1.0% risk, 10% DD should have strictly lower bust rate than 3% DD
    assert cell_10["full_period"]["none"]["bust_rate_pct"] <= cell_3["full_period"]["none"]["bust_rate_pct"]
    assert cell_10["full_period"]["none"]["pass_rate_pct"] >= cell_3["full_period"]["none"]["pass_rate_pct"]


def test_exam_sensitivity_trailing_vs_static():
    """Verify that Trailing DD is strictly more punitive or equal in bust rate than Static DD."""
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    sims = data["simulations"]
    cell_static = sims["L2|static|dd_5|r_1.0|pos_1|tgt_10"]
    cell_trailing = sims["L2|trailing|dd_5|r_1.0|pos_1|tgt_10"]

    # Trailing DD bust rate should be >= Static DD bust rate
    assert cell_trailing["full_period"]["none"]["bust_rate_pct"] >= cell_static["full_period"]["none"]["bust_rate_pct"]


def test_markdown_report_content():
    """Verify that exam_sensitivity.md contains required sections and Chinese plain language."""
    text = REPORT_FILE.read_text(encoding="utf-8")

    assert "考試情境敏感度" in text
    assert "白話總結論" in text
    assert "10% 靜態回撤底線" in text
    assert "3% 嚴苛回撤底線" in text
    assert "移動回撤" in text
    assert "去鎖定段" in text
    assert "各回撤底線下的最佳組合總覽" in text
    assert "實戰操作建議" in text

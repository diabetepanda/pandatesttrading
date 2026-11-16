"""Unit tests for scaling policies A-E, weighted average cost, and add risk limits."""

from __future__ import annotations

import pytest

from src.config_loader import load_config
from src.scaling.policies import (
    TradeStage,
    compute_weighted_avg_cost,
    get_scaling_policy,
    plan_add_stage,
)


def test_five_scaling_policies_a_b_c_d_e() -> None:
    cfg = load_config()
    max_notional = 2000.0
    r_plan = 250.0
    stop_dist_pct = 0.02  # 2% SL -> risk-budget cap = 250 / 0.02 = 12,500 (> 2000)

    expected_fixed = {
        "A": (1000.0, 600.0, 400.0),
        "B": (660.0, 660.0, 680.0),
        "C": (600.0, 600.0, 800.0),
        "D": (400.0, 600.0, 1000.0),
    }
    for name, exp in expected_fixed.items():
        pol = get_scaling_policy(name, cfg)
        got = pol.compute_all_stage_notionals(
            max_notional_usd=max_notional,
            r_plan_usd=r_plan,
            stop_distance_pct=stop_dist_pct,
        )
        assert got == pytest.approx(exp, rel=1e-4), f"Policy {name} mismatch"

    pol_e = get_scaling_policy("E", cfg)
    normal_e = pol_e.compute_all_stage_notionals(
        max_notional_usd=max_notional,
        r_plan_usd=r_plan,
        stop_distance_pct=stop_dist_pct,
        atr_ratio=1.0,
    )
    high_vol_e = pol_e.compute_all_stage_notionals(
        max_notional_usd=max_notional,
        r_plan_usd=r_plan,
        stop_distance_pct=stop_dist_pct,
        atr_ratio=1.35,
    )
    low_vol_e = pol_e.compute_all_stage_notionals(
        max_notional_usd=max_notional,
        r_plan_usd=r_plan,
        stop_distance_pct=stop_dist_pct,
        atr_ratio=0.70,
    )
    assert sum(high_vol_e) < sum(normal_e)
    assert sum(low_vol_e) > sum(normal_e)
    assert sum(low_vol_e) <= max_notional + 1e-6


def test_weighted_average_cost_long_and_short() -> None:
    stages = [
        TradeStage(stage=1, add_type="initial", fill_price=100.0, qty=2.0, notional_usd=200.0, filled_at_utc="2026-11-16T00:00:00Z"),
        TradeStage(stage=2, add_type="mean_add", fill_price=95.0, qty=2.0, notional_usd=190.0, filled_at_utc="2026-11-16T00:15:00Z"),
        TradeStage(stage=3, add_type="trend_add", fill_price=105.0, qty=1.0, notional_usd=105.0, filled_at_utc="2026-11-16T00:30:00Z"),
    ]
    # (200 + 190 + 105) / 5 = 495 / 5 = 99.0
    assert compute_weighted_avg_cost(stages) == pytest.approx(99.0)


def test_scaling_add_rejected_or_shrunk_when_exceeding_r_plan() -> None:
    # Stage 1 filled at 100.0 with qty 10.0, stop_loss at 90.0 -> initial risk = 10 * (100 - 90) = 100 USD
    existing = [
        TradeStage(stage=1, add_type="initial", fill_price=100.0, qty=10.0, notional_usd=1000.0, filled_at_utc="2026-11-16T00:00:00Z"),
    ]
    # Try mean_add at 96.0 (risk per unit to SL=90 is 6.0). R_plan = 130.0 -> remaining budget = 30.0 -> max add qty = 5.0
    # Request add notional = 960.0 (desired qty = 10.0, risk 60 -> would exceed 130) => shrunk to ~5.0 (minus tiny fees)
    res_shrunk = plan_add_stage(
        existing_stages=existing,
        add_type="mean_add",
        next_price=96.0,
        stop_loss=90.0,
        direction="long",
        target_add_notional=960.0,
        r_plan_usd=130.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        min_qty=0.1,
        in_atr_add_zone=True,
        structure_valid=True,
    )
    assert res_shrunk.allowed is True
    assert res_shrunk.qty == pytest.approx(5.0, rel=1e-6)
    assert res_shrunk.estimated_total_risk_usd <= 130.0 + 1e-6

    # If R_plan is already exhausted (R_plan = 100.0), add must be rejected
    res_rejected = plan_add_stage(
        existing_stages=existing,
        add_type="mean_add",
        next_price=96.0,
        stop_loss=90.0,
        direction="long",
        target_add_notional=960.0,
        r_plan_usd=100.0,
        fee_rate=0.0005,
        slippage_bps=1.0,
        min_qty=0.1,
        in_atr_add_zone=True,
        structure_valid=True,
    )
    assert res_rejected.allowed is False

    # trend_add requires profit >= 1R
    res_trend_blocked = plan_add_stage(
        existing_stages=existing,
        add_type="trend_add",
        next_price=105.0,
        stop_loss=90.0,
        direction="long",
        target_add_notional=500.0,
        r_plan_usd=250.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        min_qty=0.1,
        current_profit_r=0.5,
        trend_min_profit_r=1.0,
        structure_valid=True,
    )
    assert res_trend_blocked.allowed is False

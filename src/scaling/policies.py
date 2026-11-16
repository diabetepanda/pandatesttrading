"""Pluggable scaling policies (A-E), weighted average cost, and add-stage risk planner."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TradeStage:
    """Single executed entry stage of a simulated trade."""

    stage: int
    add_type: str  # "initial", "mean_add", "trend_add"
    fill_price: float
    qty: float
    notional_usd: float
    filled_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AddPlanResult:
    """Result of evaluating a stage 2 or stage 3 add-on entry."""

    allowed: bool
    qty: float
    notional_usd: float
    estimated_total_risk_usd: float
    new_avg_cost: float
    reason: str


def compute_weighted_avg_cost(stages: list[TradeStage]) -> float:
    """Compute quantity-weighted average entry cost across all filled stages."""
    total_qty = sum(s.qty for s in stages)
    if total_qty <= 0:
        return 0.0
    total_cost = sum(s.fill_price * s.qty for s in stages)
    return total_cost / total_qty


class ScalingPolicy(ABC):
    """Unified interface for all five scaling policies (SPEC Section 4.5 & 10)."""

    name: str
    stage_weights: tuple[float, float, float]

    @abstractmethod
    def compute_all_stage_notionals(
        self,
        max_notional_usd: float,
        r_plan_usd: float,
        stop_distance_pct: float,
        atr_ratio: float = 1.0,
        fee_slippage_pct: float = 0.0,
    ) -> tuple[float, float, float]:
        """Return target notional USD for stages 1, 2, and 3."""

    def compute_stage_notional(
        self,
        stage: int,
        max_notional_usd: float,
        r_plan_usd: float,
        stop_distance_pct: float,
        atr_ratio: float = 1.0,
        fee_slippage_pct: float = 0.0,
    ) -> float:
        if stage not in (1, 2, 3):
            raise ValueError(f"階段必須為 1, 2 或 3，收到: {stage}")
        notionals = self.compute_all_stage_notionals(
            max_notional_usd=max_notional_usd,
            r_plan_usd=r_plan_usd,
            stop_distance_pct=stop_distance_pct,
            atr_ratio=atr_ratio,
            fee_slippage_pct=fee_slippage_pct,
        )
        return notionals[stage - 1]


class FixedRatioPolicy(ScalingPolicy):
    """Fixed stage-weight policies A (50/30/20), B (33/33/34), C (30/30/40), D (20/30/50)."""

    def __init__(self, name: str, weights: tuple[float, float, float]) -> None:
        self.name = name
        self.stage_weights = weights

    def compute_all_stage_notionals(
        self,
        max_notional_usd: float,
        r_plan_usd: float,
        stop_distance_pct: float,
        atr_ratio: float = 1.0,
        fee_slippage_pct: float = 0.0,
    ) -> tuple[float, float, float]:
        eff_stop = max(1e-9, stop_distance_pct + fee_slippage_pct)
        risk_cap_notional = r_plan_usd / eff_stop if r_plan_usd > 0 else 0.0
        total_notional = max(0.0, min(max_notional_usd, risk_cap_notional))
        return tuple(round(total_notional * w, 6) for w in self.stage_weights)  # type: ignore[return-value]


class DynamicAtrRiskPolicy(ScalingPolicy):
    """Policy E: ATR volatility + remaining risk budget dynamic scaling."""

    def __init__(
        self,
        weights: tuple[float, float, float],
        atr_high_threshold: float = 1.2,
        atr_low_threshold: float = 0.8,
        high_vol_multiplier: float = 0.75,
        low_vol_multiplier: float = 1.15,
    ) -> None:
        self.name = "E"
        self.stage_weights = weights
        self.atr_high_threshold = atr_high_threshold
        self.atr_low_threshold = atr_low_threshold
        self.high_vol_multiplier = high_vol_multiplier
        self.low_vol_multiplier = low_vol_multiplier

    def _volatility_multiplier(self, atr_ratio: float) -> float:
        if atr_ratio >= self.atr_high_threshold:
            return self.high_vol_multiplier
        if atr_ratio <= self.atr_low_threshold:
            return self.low_vol_multiplier
        return 1.0

    def compute_all_stage_notionals(
        self,
        max_notional_usd: float,
        r_plan_usd: float,
        stop_distance_pct: float,
        atr_ratio: float = 1.0,
        fee_slippage_pct: float = 0.0,
    ) -> tuple[float, float, float]:
        eff_stop = max(1e-9, stop_distance_pct + fee_slippage_pct)
        risk_cap_notional = r_plan_usd / eff_stop if r_plan_usd > 0 else 0.0
        vol_mult = self._volatility_multiplier(atr_ratio)
        # Anchor baseline so normal vol leaves bounded headroom for low_vol_multiplier up to max_notional_usd
        baseline_cap = min(max_notional_usd / max(1.0, self.low_vol_multiplier), risk_cap_notional)
        total_notional = max(0.0, min(max_notional_usd, risk_cap_notional, baseline_cap * vol_mult))
        return tuple(round(total_notional * w, 6) for w in self.stage_weights)  # type: ignore[return-value]


def get_scaling_policy(name: str, config: dict[str, Any]) -> ScalingPolicy:
    """Instantiate a ScalingPolicy from config."""
    scaling_cfg = config.get("scaling", {})
    weights_map = scaling_cfg.get("weights", {})
    default_weights = {
        "A": [0.50, 0.30, 0.20],
        "B": [0.33, 0.33, 0.34],
        "C": [0.30, 0.30, 0.40],
        "D": [0.20, 0.30, 0.50],
        "E": [0.40, 0.35, 0.25],
    }
    raw_w = weights_map.get(name, default_weights.get(name))
    if not raw_w or len(raw_w) != 3:
        raise ValueError(f"未知或無效的補倉政策: {name}")
    weights: tuple[float, float, float] = (float(raw_w[0]), float(raw_w[1]), float(raw_w[2]))

    if name == "E":
        return DynamicAtrRiskPolicy(
            weights=weights,
            atr_high_threshold=float(scaling_cfg.get("policy_e_atr_high_threshold", 1.2)),
            atr_low_threshold=float(scaling_cfg.get("policy_e_atr_low_threshold", 0.8)),
            high_vol_multiplier=float(scaling_cfg.get("policy_e_high_vol_multiplier", 0.75)),
            low_vol_multiplier=float(scaling_cfg.get("policy_e_low_vol_multiplier", 1.15)),
        )
    return FixedRatioPolicy(name=name, weights=weights)


def _stage_risk_to_stop(
    fill_price: float,
    qty: float,
    stop_loss: float,
    direction: str,
    cost_rate: float,
) -> float:
    if direction == "long":
        price_loss = max(0.0, fill_price - stop_loss)
    else:
        price_loss = max(0.0, stop_loss - fill_price)
    cost_loss = fill_price * cost_rate
    return qty * (price_loss + cost_loss)


def plan_add_stage(
    existing_stages: list[TradeStage],
    add_type: str,
    next_price: float,
    stop_loss: float,
    direction: str,
    target_add_notional: float,
    r_plan_usd: float,
    fee_rate: float = 0.0,
    slippage_bps: float = 0.0,
    min_qty: float = 1e-6,
    max_stages: int = 3,
    current_profit_r: float = 0.0,
    trend_min_profit_r: float = 1.0,
    structure_valid: bool = True,
    in_atr_add_zone: bool = True,
) -> AddPlanResult:
    """Validate and size a stage 2 or stage 3 add-on entry within R_plan."""
    current_avg = compute_weighted_avg_cost(existing_stages)

    if len(existing_stages) >= max_stages:
        return AddPlanResult(False, 0.0, 0.0, 0.0, current_avg, f"已達最大進場階段數 ({max_stages})")

    if not structure_valid:
        return AddPlanResult(False, 0.0, 0.0, 0.0, current_avg, "原始交易結構已失效，禁止補倉")

    if add_type == "mean_add":
        if not in_atr_add_zone:
            return AddPlanResult(False, 0.0, 0.0, 0.0, current_avg, "價格未在 ATR 逆勢補倉區間內")
    elif add_type == "trend_add":
        if current_profit_r < trend_min_profit_r:
            return AddPlanResult(
                False, 0.0, 0.0, 0.0, current_avg, f"順勢加倉需獲利達 {trend_min_profit_r}R 以上"
            )
    else:
        return AddPlanResult(False, 0.0, 0.0, 0.0, current_avg, f"未知補倉類型: {add_type}")

    if next_price <= 0 or target_add_notional <= 0:
        return AddPlanResult(False, 0.0, 0.0, 0.0, current_avg, "補倉價格或目標名目無效")

    cost_rate = 2.0 * (fee_rate + slippage_bps / 10000.0)
    existing_risk = sum(
        _stage_risk_to_stop(s.fill_price, s.qty, stop_loss, direction, cost_rate)
        for s in existing_stages
    )
    remaining_risk = r_plan_usd - existing_risk
    if remaining_risk <= 1e-9:
        return AddPlanResult(False, 0.0, 0.0, existing_risk, current_avg, "剩餘風險預算已用盡，禁止補倉")

    unit_risk = _stage_risk_to_stop(next_price, 1.0, stop_loss, direction, cost_rate)
    if unit_risk <= 1e-12:
        # Stop loss is already locked beyond next_price (e.g., risk-free trailing stop on trend_add)
        final_qty = target_add_notional / next_price
    else:
        desired_qty = target_add_notional / next_price
        max_add_qty = remaining_risk / unit_risk
        final_qty = min(desired_qty, max_add_qty)

    if final_qty < min_qty:
        return AddPlanResult(
            False,
            0.0,
            0.0,
            existing_risk,
            current_avg,
            f"依風控縮小補倉後數量 ({final_qty:.6f}) 低於最小單位 ({min_qty})，放棄補倉",
        )

    final_notional = final_qty * next_price
    est_total_risk = existing_risk + final_qty * unit_risk
    new_stages = existing_stages + [
        TradeStage(
            stage=len(existing_stages) + 1,
            add_type=add_type,
            fill_price=next_price,
            qty=final_qty,
            notional_usd=final_notional,
            filled_at_utc="",
        )
    ]
    new_avg = compute_weighted_avg_cost(new_stages)
    return AddPlanResult(
        allowed=True,
        qty=final_qty,
        notional_usd=final_notional,
        estimated_total_risk_usd=est_total_risk,
        new_avg_cost=new_avg,
        reason="通過補倉風控檢核",
    )

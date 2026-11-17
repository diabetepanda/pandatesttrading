"""Strategy-subaccount risk & sizing manager unified with 5-year backtest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategies.six_strategies import SubaccountAllocation, allocate_subaccount_position


@dataclass
class RiskCheckResult:
    """Outcome of a pre-signal sub-account allocation & control audit."""

    approved: bool
    r_plan_usd: float
    notional_usd: float
    qty: float
    risk_status: str  # "normal" or "paused_manual"
    reason: str
    risk_event: dict[str, Any] | None = None


def is_short_term_strategy(strategy: str) -> bool:
    """Classify active short-term strategies (S2-S3) while preserving historical S1 pool attribution."""
    return strategy in ("S2", "S3") or strategy == "S1"


class RiskManager:
    """Delegates all live sizing & limits directly to allocate_subaccount_position."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.risk_cfg = config.get("risk", {})
        self.strategies_cfg = config.get("strategies", {})

    def check_new_signal(
        self,
        instrument: dict[str, Any],
        strategy: str,
        direction: str,
        entry_price: float,
        stop_loss: float | None,
        account_state: dict[str, Any],
        control: dict[str, Any],
        now_utc: str,
    ) -> RiskCheckResult:
        """Validate control pause, duplicate position, and strategy sub-account allocation."""
        if control.get("paused"):
            reason = str(control.get("pause_reason") or "control.json 手動暫停報單")
            return RiskCheckResult(
                approved=False,
                r_plan_usd=0.0,
                notional_usd=0.0,
                qty=0.0,
                risk_status="paused_manual",
                reason=reason,
                risk_event={"ts_utc": now_utc, "event_type": "manual_pause_active", "reason": reason},
            )

        symbol = instrument["symbol"]
        active_pairs = set(account_state.get("active_symbol_strategies", ()))
        if (symbol, strategy) in active_pairs:
            return RiskCheckResult(
                approved=False,
                r_plan_usd=0.0,
                notional_usd=0.0,
                qty=0.0,
                risk_status="normal",
                reason=f"{symbol} 策略 {strategy} 已有進行中持倉，不重複開單",
            )

        strat_cfg = self.strategies_cfg.get(strategy, {})
        default_sub_cap = float(strat_cfg.get("capital_usd", 10000.0 if is_short_term_strategy(strategy) else 20000.0))
        sub_eq = float(account_state.get("subaccount_equity_usd", default_sub_cap))
        vol_mult = float(account_state.get("subaccount_risk_multiplier", 1.0))

        alloc = self.check_subaccount_allocation(
            strategy_id=strategy,
            instrument=instrument,
            entry_price=entry_price,
            stop_loss=stop_loss,
            subaccount_equity_usd=sub_eq,
            open_positions_count=int(account_state.get("subaccount_open_positions", 0)),
            open_total_risk_usd=float(account_state.get("subaccount_open_risk_usd", 0.0)),
            open_total_notional_usd=float(account_state.get("subaccount_open_notional_usd", 0.0)),
            risk_multiplier=vol_mult,
        )
        if not alloc.approved:
            return RiskCheckResult(
                approved=False,
                r_plan_usd=0.0,
                notional_usd=0.0,
                qty=0.0,
                risk_status="normal",
                reason=alloc.reason,
            )

        return RiskCheckResult(
            approved=True,
            r_plan_usd=round(alloc.risk_usd, 6),
            notional_usd=round(alloc.notional_usd, 6),
            qty=alloc.qty,
            risk_status="normal",
            reason="通過策略子帳戶風控與倉位配置",
        )

    def check_subaccount_allocation(
        self,
        strategy_id: str,
        instrument: dict[str, Any],
        entry_price: float,
        stop_loss: float | None,
        subaccount_equity_usd: float,
        open_positions_count: int,
        open_total_risk_usd: float,
        open_total_notional_usd: float,
        risk_multiplier: float = 1.0,
    ) -> SubaccountAllocation:
        """Live sub-account allocation delegating directly to the shared backtest sizing function."""
        fee_rate = float(instrument.get("fee_rate", 0.0005))
        slippage_bps = float(self.risk_cfg.get("default_slippage_bps", 1.0))
        return allocate_subaccount_position(
            strategy_id=strategy_id,
            subaccount_equity_usd=subaccount_equity_usd,
            open_positions_count=open_positions_count,
            open_total_risk_usd=open_total_risk_usd,
            open_total_notional_usd=open_total_notional_usd,
            entry_price=entry_price,
            stop_loss=stop_loss,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            config=self.config,
            risk_multiplier=risk_multiplier,
        )

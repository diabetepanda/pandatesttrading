"""Mandatory risk control engine (SPEC Section 7 & 13.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategies.six_strategies import SubaccountAllocation, allocate_subaccount_position


@dataclass
class RiskCheckResult:
    """Outcome of a pre-signal or pre-add risk check."""

    approved: bool
    r_plan_usd: float
    notional_usd: float
    leverage: float
    initial_margin_usd: float
    maintenance_margin_usd: float
    liquidation_price: float
    stop_to_liq_ratio: float
    risk_status: str  # "normal", "warning", "paused_drawdown", "paused_manual"
    reason: str
    risk_event: dict[str, Any] | None = None


def is_short_term_strategy(strategy: str) -> bool:
    """Classify short-term strategies (S1-S3)."""
    return strategy in ("S1", "S2", "S3")


class RiskManager:
    """Enforces all account, pool, group, margin, drawdown, and liquidation rules."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.risk_cfg = config.get("risk", {})
        self.acct_cfg = config.get("account", {})
        self.pools_cfg = self.acct_cfg.get("pools", {})
        self.groups_cfg = config.get("correlation_groups", {})
        self.signals_cfg = config.get("signals", {})

    def evaluate_drawdown_state(
        self,
        equity_usd: float,
        peak_equity_usd: float,
        control: dict[str, Any],
        already_paused_drawdown: bool = False,
        now_utc: str = "",
    ) -> tuple[str, float, dict[str, Any] | None]:
        """Evaluate manual pause, 8% drawdown warning, and 12% drawdown pause/unlock."""
        if control.get("paused"):
            return (
                "paused_manual",
                0.0,
                {
                    "ts_utc": now_utc,
                    "event_type": "manual_pause_active",
                    "reason": control.get("pause_reason") or "control.json 手動暫停報單",
                },
            )

        peak = max(1e-9, peak_equity_usd)
        dd_pct = max(0.0, (peak - equity_usd) / peak * 100.0)
        warn_pct = float(self.risk_cfg.get("drawdown_warning_pct", 8.0))
        warn_mult = float(self.risk_cfg.get("drawdown_warning_risk_multiplier", 0.5))
        pause_pct = float(self.risk_cfg.get("drawdown_pause_pct", 12.0))

        if already_paused_drawdown or dd_pct >= pause_pct:
            review_done = control.get("risk_review_done") is True
            review_note = str(control.get("risk_review_note", "")).strip()
            if review_done and review_note:
                next_status = "warning" if dd_pct >= warn_pct else "normal"
                next_mult = warn_mult if dd_pct >= warn_pct else 1.0
                return (
                    next_status,
                    next_mult,
                    {
                        "ts_utc": now_utc,
                        "event_type": "risk_review_completed",
                        "drawdown_pct": round(dd_pct, 2),
                        "note": review_note,
                    },
                )
            return (
                "paused_drawdown",
                0.0,
                {
                    "ts_utc": now_utc,
                    "event_type": "drawdown_pause_active",
                    "drawdown_pct": round(dd_pct, 2),
                    "reason": f"帳戶回撤達 {dd_pct:.2f}%（>= {pause_pct:.1f}%），暫停新報單與補倉",
                },
            )

        if dd_pct >= warn_pct:
            return ("warning", warn_mult, None)

        return ("normal", 1.0, None)

    def compute_liquidation_metrics(
        self,
        entry_price: float,
        stop_loss: float,
        direction: str,
        notional_usd: float,
        leverage: float,
    ) -> dict[str, float]:
        """Compute initial/maintenance margin, estimated liquidation price, and SL-to-liq ratio."""
        lev = max(1.0, float(leverage))
        maint_ratio = float(self.risk_cfg.get("maintenance_margin_ratio", 0.50))
        initial_margin = notional_usd / lev
        maint_margin = initial_margin * maint_ratio
        liq_dist_pct = max(1e-6, (1.0 - maint_ratio) / lev)

        if direction == "long":
            liq_price = entry_price * (1.0 - liq_dist_pct)
            sl_before_liq = stop_loss > liq_price
        else:
            liq_price = entry_price * (1.0 + liq_dist_pct)
            sl_before_liq = stop_loss < liq_price

        stop_dist = abs(entry_price - stop_loss)
        liq_dist = abs(entry_price - liq_price)
        stop_to_liq_ratio = (stop_dist / liq_dist) if (liq_dist > 0 and sl_before_liq) else 999.0

        return {
            "leverage": lev,
            "initial_margin_usd": initial_margin,
            "maintenance_margin_usd": maint_margin,
            "liquidation_price": liq_price,
            "stop_to_liq_ratio": stop_to_liq_ratio,
            "sl_before_liq": 1.0 if sl_before_liq else 0.0,
        }

    def check_rr_requirements(
        self,
        strategy: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float | str,
    ) -> tuple[bool, float, str]:
        """Verify minimum initial risk-reward ratio (SPEC Section 9)."""
        risk = (entry_price - stop_loss) if direction == "long" else (stop_loss - entry_price)
        if risk <= 0:
            return False, 0.0, "止損方向錯誤或止損距離為 0"

        rew1 = (tp1 - entry_price) if direction == "long" else (entry_price - tp1)
        rr1 = rew1 / risk

        if is_short_term_strategy(strategy):
            tp1_min = float(self.risk_cfg.get("short_term_tp1_rr_min", 1.5))
            tp2_min = float(self.risk_cfg.get("short_term_tp2_rr_min", 2.0))
            if rr1 < tp1_min - 1e-9:
                return False, rr1, f"短線策略 {strategy} TP1 風報比 {rr1:.2f} 低於下限 {tp1_min}"
            if isinstance(tp2, (int, float)):
                rew2 = (float(tp2) - entry_price) if direction == "long" else (entry_price - float(tp2))
                rr2 = rew2 / risk
                if rr2 < tp2_min - 1e-9:
                    return False, rr2, f"短線策略 {strategy} TP2 風報比 {rr2:.2f} 低於下限 {tp2_min}"
                return True, rr2, "ok"
            return True, rr1, "ok"

        min_rr_b = float(self.risk_cfg.get("long_term_min_rr", 2.0))
        if isinstance(tp2, (int, float)):
            rew2 = (float(tp2) - entry_price) if direction == "long" else (entry_price - float(tp2))
            rr_best = max(rr1, rew2 / risk)
        else:
            rr_best = rr1
        if rr_best < min_rr_b - 1e-9:
            return False, rr_best, f"長線策略 {strategy} 風報比 {rr_best:.2f} 低於下限 {min_rr_b}"
        return True, rr_best, "ok"

    def check_new_signal(
        self,
        instrument: dict[str, Any],
        strategy: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float | str,
        desired_notional_usd: float,
        leverage: float | None,
        account_state: dict[str, Any],
        control: dict[str, Any],
        now_utc: str,
    ) -> RiskCheckResult:
        """Execute full pre-publication risk audit."""
        equity_usd = float(account_state.get("equity_usd", self.acct_cfg.get("initial_capital_usd", 100000.0)))
        peak_equity_usd = float(account_state.get("peak_equity_usd", equity_usd))
        already_paused_dd = bool(account_state.get("drawdown_paused", False))

        risk_status, warning_mult, dd_event = self.evaluate_drawdown_state(
            equity_usd=equity_usd,
            peak_equity_usd=peak_equity_usd,
            control=control,
            already_paused_drawdown=already_paused_dd,
            now_utc=now_utc,
        )
        if risk_status in ("paused_manual", "paused_drawdown"):
            return RiskCheckResult(
                approved=False,
                r_plan_usd=0.0,
                notional_usd=0.0,
                leverage=0.0,
                initial_margin_usd=0.0,
                maintenance_margin_usd=0.0,
                liquidation_price=0.0,
                stop_to_liq_ratio=0.0,
                risk_status=risk_status,
                reason=dd_event["reason"] if dd_event else "風控暫停中",
                risk_event=dd_event,
            )

        # Daily & weekly loss caps
        daily_pnl = float(account_state.get("daily_pnl_usd", 0.0))
        max_daily_loss = float(self.risk_cfg.get("max_daily_loss_usd", 1500.0))
        if daily_pnl <= -max_daily_loss:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"已達單日最大虧損上限 (${max_daily_loss:.0f})",
                {"ts_utc": now_utc, "event_type": "daily_loss_limit_hit", "daily_pnl_usd": daily_pnl},
            )

        weekly_pnl = float(account_state.get("weekly_pnl_usd", 0.0))
        max_weekly_loss = float(self.risk_cfg.get("max_weekly_loss_usd", 4000.0))
        if weekly_pnl <= -max_weekly_loss:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"已達單週最大虧損上限 (${max_weekly_loss:.0f})",
                {"ts_utc": now_utc, "event_type": "weekly_loss_limit_hit", "weekly_pnl_usd": weekly_pnl},
            )

        # Frequency & duplicate checks
        symbol = instrument["symbol"]
        active_pairs = set(account_state.get("active_symbol_strategies", ()))
        if (symbol, strategy) in active_pairs:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"{symbol} 策略 {strategy} 已有進行中或等待入場報單，不重複發布",
            )

        daily_signals = int(account_state.get("daily_signal_count", 0))
        max_daily_signals = int(self.signals_cfg.get("max_daily_signals", 10))
        if daily_signals >= max_daily_signals:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"今日報單數已達上限 ({max_daily_signals})",
            )

        # R:R check
        rr_ok, _, rr_msg = self.check_rr_requirements(strategy, direction, entry_price, stop_loss, tp1, tp2)
        if not rr_ok:
            return RiskCheckResult(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status, rr_msg)

        # Pool constraints
        pool_name = "short_term" if is_short_term_strategy(strategy) else "long_term"
        pool_cfg = self.pools_cfg.get(pool_name, {})
        max_concurrent = int(pool_cfg.get("max_concurrent_trades", 10))
        pool_open_trades = int(account_state.get(f"{pool_name}_open_trades", 0))
        if pool_open_trades >= max_concurrent:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"{pool_name} 資金池同時持倉數已達上限 ({max_concurrent})",
            )

        pool_target_notional = float(pool_cfg.get("target_notional_usd", 1500.0 if pool_name == "short_term" else 4000.0))
        pool_remaining_cap = float(account_state.get(f"{pool_name}_remaining_capital", pool_cfg.get("capital_usd", 20000.0)))
        if pool_remaining_cap <= 0:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"{pool_name} 資金池剩餘額度不足",
            )

        # Risk budget R_plan = min(per_trade_max, account_remaining, group_remaining) * warning_mult
        max_trade_risk = float(self.risk_cfg.get("max_risk_per_trade_usd", {}).get(pool_name, 250.0))
        total_stop_risk = float(account_state.get("total_stop_risk_usd", 0.0))
        max_total_stop_risk = float(self.risk_cfg.get("max_total_stop_risk_usd", 3000.0))
        rem_total_risk = max(0.0, max_total_stop_risk - total_stop_risk)
        if rem_total_risk <= 1e-9:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"全帳戶預估總停損風險已達上限 (${max_total_stop_risk:.0f})",
                {"ts_utc": now_utc, "event_type": "total_stop_risk_limit_hit"},
            )

        group_key = instrument.get("correlation_group", "")
        group_max = float(
            self.groups_cfg.get(group_key, {}).get(
                "max_stop_risk_usd",
                self.risk_cfg.get("max_group_stop_risk_usd", 1000.0),
            )
        )
        group_open_map = account_state.get("group_stop_risk_usd", {})
        group_open_risk = float(group_open_map.get(group_key, 0.0))
        rem_group_risk = max(0.0, group_max - group_open_risk)
        if rem_group_risk <= 1e-9:
            return RiskCheckResult(
                False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_status,
                f"相關性群組 {group_key} 停損風險已達上限 (${group_max:.0f})",
                {"ts_utc": now_utc, "event_type": "group_stop_risk_limit_hit", "group": group_key},
            )

        r_plan = min(max_trade_risk, rem_total_risk, rem_group_risk) * warning_mult

        # Position sizing bounded by R_plan, desired notional, target notional, pool capital, margin
        stop_dist_pct = abs(entry_price - stop_loss) / entry_price
        fee_rate = float(instrument.get("fee_rate", 0.0005))
        slippage_bps = float(self.risk_cfg.get("default_slippage_bps", 1.0))
        cost_pct = 2.0 * (fee_rate + slippage_bps / 10000.0)
        eff_stop_pct = max(1e-9, stop_dist_pct + cost_pct)

        risk_cap_notional = r_plan / eff_stop_pct
        max_lev = float(instrument.get("max_leverage", 1.0))
        eff_leverage = min(max_lev, float(leverage)) if leverage is not None else max_lev
        if eff_leverage <= 0:
            eff_leverage = 1.0

        used_margin = float(account_state.get("used_margin_usd", 0.0))
        max_margin_pct = float(self.risk_cfg.get("max_margin_usage_pct", 30.0))
        max_total_margin = equity_usd * (max_margin_pct / 100.0)
        rem_margin = max_total_margin - used_margin

        notional = min(desired_notional_usd, pool_target_notional, pool_remaining_cap, risk_cap_notional)
        if "subaccount_equity_usd" in account_state:
            vol_mult = float(account_state.get("subaccount_risk_multiplier", 1.0))
            sub_alloc = self.check_subaccount_allocation(
                strategy_id=strategy,
                instrument=instrument,
                entry_price=entry_price,
                stop_loss=stop_loss,
                subaccount_equity_usd=float(account_state["subaccount_equity_usd"]),
                open_positions_count=int(account_state.get("subaccount_open_positions", 0)),
                open_total_risk_usd=float(account_state.get("subaccount_open_risk_usd", 0.0)),
                open_total_notional_usd=float(account_state.get("subaccount_open_notional_usd", 0.0)),
                risk_multiplier=warning_mult * vol_mult,
            )
            if not sub_alloc.approved:
                return RiskCheckResult(False, 0.0, 0.0, eff_leverage, 0.0, 0.0, 0.0, 0.0, risk_status, sub_alloc.reason)
            notional = min(notional, sub_alloc.notional_usd)
            r_plan = min(r_plan, sub_alloc.risk_usd)
        if notional <= 0:
            return RiskCheckResult(False, r_plan, 0.0, eff_leverage, 0.0, 0.0, 0.0, 0.0, risk_status, "計算名目倉位為 0")

        # Liquidation safety check (SPEC 13.2)
        liq = self.compute_liquidation_metrics(entry_price, stop_loss, direction, notional, eff_leverage)
        max_sl_liq_ratio = float(self.risk_cfg.get("stop_to_liquidation_max_ratio", 0.5))
        if liq["sl_before_liq"] < 0.5 or liq["stop_to_liq_ratio"] > max_sl_liq_ratio:
            return RiskCheckResult(
                approved=False,
                r_plan_usd=r_plan,
                notional_usd=0.0,
                leverage=eff_leverage,
                initial_margin_usd=liq["initial_margin_usd"],
                maintenance_margin_usd=liq["maintenance_margin_usd"],
                liquidation_price=liq["liquidation_price"],
                stop_to_liq_ratio=liq["stop_to_liq_ratio"],
                risk_status=risk_status,
                reason=(
                    f"止損距離佔爆倉距離比例 {liq['stop_to_liq_ratio']:.2f} "
                    f"超過安全上限 {max_sl_liq_ratio:.2f}（預估爆倉價 {liq['liquidation_price']:.4f}）"
                ),
            )

        # Margin usage cap check (30%)
        if liq["initial_margin_usd"] > rem_margin + 1e-9:
            return RiskCheckResult(
                approved=False,
                r_plan_usd=r_plan,
                notional_usd=0.0,
                leverage=eff_leverage,
                initial_margin_usd=liq["initial_margin_usd"],
                maintenance_margin_usd=liq["maintenance_margin_usd"],
                liquidation_price=liq["liquidation_price"],
                stop_to_liq_ratio=liq["stop_to_liq_ratio"],
                risk_status=risk_status,
                reason=(
                    f"保證金使用率將超過 {max_margin_pct:.0f}% 上限"
                    f"（需要 ${liq['initial_margin_usd']:.2f}，剩餘可用 ${max(0.0, rem_margin):.2f}）"
                ),
            )

        return RiskCheckResult(
            approved=True,
            r_plan_usd=round(r_plan, 4),
            notional_usd=round(notional, 4),
            leverage=eff_leverage,
            initial_margin_usd=round(liq["initial_margin_usd"], 4),
            maintenance_margin_usd=round(liq["maintenance_margin_usd"], 4),
            liquidation_price=liq["liquidation_price"],
            stop_to_liq_ratio=round(liq["stop_to_liq_ratio"], 4),
            risk_status=risk_status,
            reason="通過風控審核",
            risk_event=dd_event,
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

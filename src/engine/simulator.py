"""Single-ledger simulation and catch-up replay engine with unified sub-account sizing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.engine.analytics import (
    build_performance_stats,
    build_risk_status,
    compute_trade_estimates,
    export_equity_curve,
)
from src.risk.manager import RiskManager, is_short_term_strategy
from src.signals.models import (
    AddLevel,
    InputSnapshot,
    LifecycleState,
    Signal,
    SignalUpdateEvent,
    validate_transition,
)
from src.signals.store import SignalStore
from src.strategies.six_strategies import allocate_subaccount_position


def _parse_utc(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_key(ts_utc: str) -> str:
    return ts_utc[:7] if len(ts_utc) >= 7 else "unknown"


def _date_key(ts_utc: str) -> str:
    return ts_utc[:10] if len(ts_utc) >= 10 else "unknown"


def _week_key(ts_utc: str) -> str:
    dt = _parse_utc(ts_utc)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _bid_price(mid: float, spread_bps: float) -> float:
    return mid * (1.0 - spread_bps / 20000.0)


def _ask_price(mid: float, spread_bps: float) -> float:
    return mid * (1.0 + spread_bps / 20000.0)


@dataclass(frozen=True)
class TradeStage:
    """Recorded execution fill stage for a trade."""

    stage: int
    add_type: str  # "initial"
    fill_price: float
    qty: float
    notional_usd: float
    filled_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketBar:
    """Single closed OHLCV bar fed to the simulation/replay engine."""

    ts_utc: str
    close_ts_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class CandidateSignal:
    """Candidate signal produced by a strategy prior to risk approval."""

    strategy: str  # "S1", "S2", "S3", "L1", "L2", or "L3"
    symbol: str
    direction: str  # "long" or "short"
    entry_low: float
    entry_high: float
    add_levels: tuple[AddLevel, ...]
    stop_loss: float
    tp1: float
    tp2: float | str
    invalidation_price: float
    invalidation_cond: str
    reasons: tuple[str, ...]
    snapshot: InputSnapshot
    atr: float
    atr_ratio: float = 1.0
    leverage: float | None = None
    time_stop_bars: int | None = None
    verification_status: str = ""
    risk_multiplier: float = 1.0


@dataclass
class FillRecord:
    """Simulated execution fill."""

    fill_id: str
    signal_id: str
    symbol: str
    side: str  # "buy" or "sell"
    action: str  # "entry", "tp1", "exit"
    price: float
    qty: float
    notional_usd: float
    fee_usd: float
    realized_pnl_usd: float
    ts_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRecord:
    """Pending or completed limit/stop order."""

    order_id: str
    signal_id: str
    symbol: str
    direction: str
    entry_low: float
    entry_high: float
    valid_until_utc: str
    status: str  # "pending", "filled", "cancelled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeRecord:
    """Simulated trade state in the unified simulation ledger."""

    signal_id: str
    strategy: str
    symbol: str
    market: str
    correlation_group: str
    direction: str
    state: LifecycleState
    entry_low: float
    entry_high: float
    add_levels: tuple[AddLevel, ...]
    initial_stop_loss: float
    current_stop_loss: float
    current_tp1: float
    current_tp2: float | str
    invalidation_price: float
    valid_until_utc: str
    time_stop_bars: int
    r_plan_usd: float
    planned_notional_usd: float
    leverage: float
    assumed_spread_bps: float
    fee_rate: float
    slippage_bps: float
    overnight_cost_daily_bps: float
    stages: list[TradeStage] = field(default_factory=list)
    avg_entry_price: float = 0.0
    remaining_qty: float = 0.0
    initial_filled_qty: float = 0.0
    realized_pnl_usd: float = 0.0
    total_fees_usd: float = 0.0
    total_overnight_usd: float = 0.0
    r_multiple: float = 0.0
    entered_at_utc: str | None = None
    closed_at_utc: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    published_at_utc: str = ""
    margin_leverage: float = 1.0
    migrated_unified_sizing: bool = True

    def current_notional_usd(self) -> float:
        return self.remaining_qty * self.avg_entry_price if self.remaining_qty > 0 else 0.0

    def current_margin_usd(self) -> float:
        lev = max(1.0, float(self.margin_leverage or self.leverage or 1.0))
        return self.current_notional_usd() / lev

    def open_stop_risk_usd(self) -> float:
        if self.state not in (LifecycleState.ACTIVE, LifecycleState.TP1_HIT, LifecycleState.WAITING_ENTRY):
            return 0.0
        if self.state == LifecycleState.WAITING_ENTRY:
            return self.r_plan_usd
        if self.direction == "long":
            dist = max(0.0, self.avg_entry_price - self.current_stop_loss)
        else:
            dist = max(0.0, self.current_stop_loss - self.avg_entry_price)
        return self.remaining_qty * dist

    def unrealized_pnl_usd(self, mark_price: float) -> float:
        if self.remaining_qty <= 0 or self.avg_entry_price <= 0:
            return 0.0
        if self.direction == "long":
            return (mark_price - self.avg_entry_price) * self.remaining_qty
        return (self.avg_entry_price - mark_price) * self.remaining_qty

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "market": self.market,
            "correlation_group": self.correlation_group,
            "direction": self.direction,
            "state": self.state.value,
            "avg_entry_price": round(self.avg_entry_price, 6),
            "remaining_qty": round(self.remaining_qty, 8),
            "current_stop_loss": self.current_stop_loss,
            "current_tp1": self.current_tp1,
            "current_tp2": self.current_tp2,
            "leverage": self.leverage,
            "margin_leverage": self.margin_leverage,
            "r_plan_usd": self.r_plan_usd,
            "planned_notional_usd": round(self.planned_notional_usd, 4),
            "realized_pnl_usd": round(self.realized_pnl_usd, 4),
            "total_fees_usd": round(self.total_fees_usd, 4),
            "total_overnight_usd": round(self.total_overnight_usd, 4),
            "r_multiple": round(self.r_multiple, 4),
            "entered_at_utc": self.entered_at_utc,
            "closed_at_utc": self.closed_at_utc,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "add_levels": [lvl.to_dict() for lvl in self.add_levels],
            "initial_stop_loss": self.initial_stop_loss,
            "invalidation_price": self.invalidation_price,
            "valid_until_utc": self.valid_until_utc,
            "time_stop_bars": self.time_stop_bars,
            "assumed_spread_bps": self.assumed_spread_bps,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "overnight_cost_daily_bps": self.overnight_cost_daily_bps,
            "initial_filled_qty": round(self.initial_filled_qty, 8),
            "stages": [s.to_dict() for s in self.stages],
            "published_at_utc": self.published_at_utc,
            "migrated_unified_sizing": self.migrated_unified_sizing,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeRecord:
        if "planned_notional_usd" in data:
            planned_notional = float(data["planned_notional_usd"])
            migrated_flag = bool(data.get("migrated_unified_sizing", True))
        else:
            # Legacy trade migration: keep already-filled stages intact, mark pending orders for recalculation
            filled_stages = data.get("stages", [])
            has_fills = bool(filled_stages)
            planned_notional = (
                sum(float(s.get("notional_usd", 0.0)) for s in filled_stages)
                if has_fills
                else 0.0
            )
            migrated_flag = has_fills or (data.get("state") in ("closed", "cancelled"))
        lev_val = float(data.get("leverage", 1.0))
        return cls(
            signal_id=data["signal_id"],
            strategy=data["strategy"],
            symbol=data["symbol"],
            market=data["market"],
            correlation_group=data.get("correlation_group", ""),
            direction=data["direction"],
            state=LifecycleState(data["state"]),
            entry_low=float(data["entry_low"]),
            entry_high=float(data["entry_high"]),
            add_levels=tuple(AddLevel(**lvl) for lvl in data.get("add_levels", [])),
            initial_stop_loss=float(data["initial_stop_loss"]),
            current_stop_loss=float(data["current_stop_loss"]),
            current_tp1=float(data["current_tp1"]),
            current_tp2=data["current_tp2"],
            invalidation_price=float(data["invalidation_price"]),
            valid_until_utc=data["valid_until_utc"],
            time_stop_bars=int(data["time_stop_bars"]),
            r_plan_usd=float(data["r_plan_usd"]),
            planned_notional_usd=planned_notional,
            leverage=lev_val,
            margin_leverage=float(data.get("margin_leverage") or max(1.0, lev_val)),
            assumed_spread_bps=float(data.get("assumed_spread_bps", 2.0)),
            fee_rate=float(data.get("fee_rate", 0.0005)),
            slippage_bps=float(data.get("slippage_bps", 1.0)),
            overnight_cost_daily_bps=float(data.get("overnight_cost_daily_bps", 0.0)),
            stages=[TradeStage(**s) for s in data.get("stages", [])],
            avg_entry_price=float(data.get("avg_entry_price", 0.0)),
            remaining_qty=float(data.get("remaining_qty", 0.0)),
            initial_filled_qty=float(data.get("initial_filled_qty", 0.0)),
            realized_pnl_usd=float(data.get("realized_pnl_usd", 0.0)),
            total_fees_usd=float(data.get("total_fees_usd", 0.0)),
            total_overnight_usd=float(data.get("total_overnight_usd", 0.0)),
            r_multiple=float(data.get("r_multiple", 0.0)),
            entered_at_utc=data.get("entered_at_utc"),
            closed_at_utc=data.get("closed_at_utc"),
            exit_price=data.get("exit_price"),
            exit_reason=data.get("exit_reason"),
            published_at_utc=str(data.get("published_at_utc") or ""),
            migrated_unified_sizing=migrated_flag,
        )


class Ledger:
    """Single virtual account ledger with isolated short_term ($30k) and long_term ($70k) pools."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        pools = config.get("account", {}).get("pools", {})
        self.short_term_capital: float = float(pools.get("short_term", {}).get("capital_usd", 30000.0))
        self.long_term_capital: float = float(pools.get("long_term", {}).get("capital_usd", 70000.0))
        self.short_term_realized_pnl: float = 0.0
        self.long_term_realized_pnl: float = 0.0
        self.peak_equity_usd: float = self.short_term_capital + self.long_term_capital
        self.drawdown_paused: bool = False
        self.trades: dict[str, TradeRecord] = {}
        self.orders: dict[str, OrderRecord] = {}
        self.fills: list[FillRecord] = []
        self.daily_realized_pnl: dict[str, float] = {}
        self.weekly_realized_pnl: dict[str, float] = {}
        self.daily_signal_counts: dict[str, int] = {}

    def compute_pool_equity(self, pool_name: str, mark_prices: dict[str, float] | None = None) -> float:
        marks = mark_prices or {}
        base = self.short_term_capital if pool_name == "short_term" else self.long_term_capital
        realized = self.short_term_realized_pnl if pool_name == "short_term" else self.long_term_realized_pnl
        unrealized = 0.0
        for tr in self.trades.values():
            if tr.state not in (LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
                continue
            tr_pool = "short_term" if is_short_term_strategy(tr.strategy) else "long_term"
            if tr_pool == pool_name:
                mp = marks.get(tr.symbol, tr.avg_entry_price)
                unrealized += tr.unrealized_pnl_usd(mp)
        return base + realized + unrealized

    def compute_equity(self, mark_prices: dict[str, float] | None = None) -> float:
        eq = self.compute_pool_equity("short_term", mark_prices) + self.compute_pool_equity("long_term", mark_prices)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        return eq

    def build_account_state(self, now_utc: str, mark_prices: dict[str, float] | None = None) -> dict[str, Any]:
        marks = mark_prices or {}
        equity = self.compute_equity(marks)
        d_key = _date_key(now_utc)
        w_key = _week_key(now_utc)

        unrealized_total = 0.0
        used_margin = 0.0
        total_stop_risk = 0.0
        group_stop_risk: dict[str, float] = {}
        active_pairs: set[tuple[str, str]] = set()
        short_open = 0
        long_open = 0
        short_used_cap = 0.0
        long_used_cap = 0.0

        for tr in self.trades.values():
            if tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
                active_pairs.add((tr.symbol, tr.strategy))
                risk_val = tr.open_stop_risk_usd()
                total_stop_risk += risk_val
                group_stop_risk[tr.correlation_group] = group_stop_risk.get(tr.correlation_group, 0.0) + risk_val
                used_margin += tr.current_margin_usd()
                eff_notional = tr.current_notional_usd() if tr.remaining_qty > 0 else tr.planned_notional_usd
                if is_short_term_strategy(tr.strategy):
                    short_open += 1
                    short_used_cap += eff_notional
                else:
                    long_open += 1
                    long_used_cap += eff_notional
            if tr.state in (LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
                mp = marks.get(tr.symbol, tr.avg_entry_price)
                unrealized_total += tr.unrealized_pnl_usd(mp)

        short_eq = self.compute_pool_equity("short_term", marks)
        long_eq = self.compute_pool_equity("long_term", marks)

        return {
            "equity_usd": equity,
            "peak_equity_usd": self.peak_equity_usd,
            "drawdown_paused": self.drawdown_paused,
            "daily_pnl_usd": self.daily_realized_pnl.get(d_key, 0.0) + unrealized_total,
            "weekly_pnl_usd": self.weekly_realized_pnl.get(w_key, 0.0) + unrealized_total,
            "used_margin_usd": used_margin,
            "total_stop_risk_usd": total_stop_risk,
            "group_stop_risk_usd": group_stop_risk,
            "active_symbol_strategies": active_pairs,
            "daily_signal_count": self.daily_signal_counts.get(d_key, 0),
            "short_term_open_trades": short_open,
            "long_term_open_trades": long_open,
            "short_term_remaining_capital": max(0.0, short_eq - short_used_cap),
            "long_term_remaining_capital": max(0.0, long_eq - long_used_cap),
        }

    def build_subaccount_state(
        self,
        strategy_id: str,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Compute current sub-account equity (short_term $30k / long_term $70k pool) and open risk."""
        marks = mark_prices or {}
        pool_name = "short_term" if is_short_term_strategy(strategy_id) else "long_term"
        sub_eq = self.compute_pool_equity(pool_name, marks)
        open_cnt = 0
        open_risk = 0.0
        open_notional = 0.0
        for tr in self.trades.values():
            tr_pool = "short_term" if is_short_term_strategy(tr.strategy) else "long_term"
            if tr_pool != pool_name:
                continue
            if tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
                open_cnt += 1
                open_risk += tr.open_stop_risk_usd()
                open_notional += tr.current_notional_usd() if tr.remaining_qty > 0 else tr.planned_notional_usd
        return {
            "pool_name": pool_name,
            "subaccount_equity_usd": max(0.0, sub_eq),
            "subaccount_open_positions": open_cnt,
            "subaccount_open_risk_usd": open_risk,
            "subaccount_open_notional_usd": open_notional,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "short_term_realized_pnl": self.short_term_realized_pnl,
            "long_term_realized_pnl": self.long_term_realized_pnl,
            "peak_equity_usd": self.peak_equity_usd,
            "drawdown_paused": self.drawdown_paused,
            "trades": {k: v.to_dict() for k, v in self.trades.items()},
            "orders": {k: v.to_dict() for k, v in self.orders.items()},
            "fills": [f.to_dict() for f in self.fills],
            "daily_realized_pnl": self.daily_realized_pnl,
            "weekly_realized_pnl": self.weekly_realized_pnl,
            "daily_signal_counts": self.daily_signal_counts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: dict[str, Any]) -> Ledger:
        led = cls(config)
        led.short_term_realized_pnl = float(data.get("short_term_realized_pnl", 0.0))
        led.long_term_realized_pnl = float(data.get("long_term_realized_pnl", 0.0))
        led.peak_equity_usd = float(data.get("peak_equity_usd", led.peak_equity_usd))
        led.drawdown_paused = bool(data.get("drawdown_paused", False))
        led.trades = {k: TradeRecord.from_dict(v) for k, v in data.get("trades", {}).items()}
        led.orders = {k: OrderRecord(**v) for k, v in data.get("orders", {}).items()}
        led.fills = [FillRecord(**f) for f in data.get("fills", [])]
        led.daily_realized_pnl = {k: float(v) for k, v in data.get("daily_realized_pnl", {}).items()}
        led.weekly_realized_pnl = {k: float(v) for k, v in data.get("weekly_realized_pnl", {}).items()}
        led.daily_signal_counts = {k: int(v) for k, v in data.get("daily_signal_counts", {}).items()}
        return led


class SimulationEngine:
    """Coordinates signal publication, risk control, bar replay, and the single simulation ledger."""

    def __init__(self, config: dict[str, Any], out_dir: Path | str) -> None:
        self.config = config
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.risk_manager = RiskManager(config)
        self.signal_store = SignalStore(self.out_dir)

        self.instruments: dict[str, dict[str, Any]] = {
            inst["symbol"]: inst for inst in config.get("instruments", [])
        }
        self.ledger: Ledger = Ledger(config)
        self.last_processed_ts: dict[str, str] = {}
        self.latest_marks: dict[str, float] = {}
        self._signal_seq = 0
        self._event_seq = 0
        self._fill_seq = 0

    @property
    def primary_ledger(self) -> Ledger:
        return self.ledger

    def _next_signal_id(self, now_utc: str) -> str:
        self._signal_seq += 1
        day = now_utc[:10].replace("-", "")
        return f"SIG-{day}-{self._signal_seq:04d}"

    def _next_event_id(self, signal_id: str) -> str:
        self._event_seq += 1
        return f"EV-{signal_id}-{self._event_seq:05d}"

    def _next_fill_id(self, signal_id: str) -> str:
        self._fill_seq += 1
        return f"FILL-{signal_id}-{self._fill_seq:05d}"

    def _record_watch(self, symbol: str, strategy: str, ts_utc: str, reason: str) -> None:
        self.signal_store.append_watch_log(
            {"ts_utc": ts_utc, "symbol": symbol, "strategy": strategy, "reason": reason}
        )

    def submit_candidate_signal(
        self,
        candidate: CandidateSignal,
        now_utc: str,
        latest_bar_close_utc: str,
        control: dict[str, Any],
        mark_prices: dict[str, float] | None = None,
    ) -> Signal | None:
        """Validate schedule delay & risk rules; publish immutable Signal with full-entry sizing."""
        inst = self.instruments.get(candidate.symbol)
        if inst is None:
            self._record_watch(candidate.symbol, candidate.strategy, now_utc, f"未知標的 {candidate.symbol}")
            return None

        sched_cfg = self.config.get("schedule", {})
        bar_interval = str(
            candidate.snapshot.kline_summary.get("interval")
            or self.config.get("strategies", {}).get(candidate.strategy, {}).get("interval", "15m")
        )
        if bar_interval == "1d":
            max_delay_min = float(sched_cfg.get("max_daily_candle_delay_minutes", 1500))
        elif bar_interval == "4h":
            max_delay_min = float(sched_cfg.get("max_4h_candle_delay_minutes", 260))
        else:
            max_delay_min = float(sched_cfg.get("max_candle_delay_minutes", 20))
        delay_min = (_parse_utc(now_utc) - _parse_utc(latest_bar_close_utc)).total_seconds() / 60.0
        if delay_min < 0:
            self._record_watch(
                candidate.symbol,
                candidate.strategy,
                now_utc,
                f"K 線尚未收盤（預計收盤 {latest_bar_close_utc} > 目前 {now_utc}）",
            )
            return None
        if delay_min > max_delay_min:
            self._record_watch(
                candidate.symbol,
                candidate.strategy,
                now_utc,
                f"資料過期／排程延遲（最新收盤 K 線距今 {delay_min:.1f} 分鐘 > {max_delay_min:.0f} 分鐘）",
            )
            return None

        marks = mark_prices or self.latest_marks
        entry_mid = (candidate.entry_low + candidate.entry_high) / 2.0
        pool_name = "short_term" if is_short_term_strategy(candidate.strategy) else "long_term"
        pool_cfg = self.config.get("account", {}).get("pools", {}).get(pool_name, {})

        acct = self.ledger.build_account_state(now_utc, marks)
        sub_state = self.ledger.build_subaccount_state(candidate.strategy, marks)
        acct.update(sub_state)
        acct["subaccount_risk_multiplier"] = candidate.risk_multiplier

        risk_res = self.risk_manager.check_new_signal(
            instrument=inst,
            strategy=candidate.strategy,
            direction=candidate.direction,
            entry_price=entry_mid,
            stop_loss=candidate.stop_loss,
            tp1=candidate.tp1,
            tp2=candidate.tp2,
            leverage=candidate.leverage,
            account_state=acct,
            control=control,
            now_utc=now_utc,
        )
        if risk_res.risk_event:
            self.signal_store.append_risk_event(risk_res.risk_event)
            if risk_res.risk_event.get("event_type") == "risk_review_completed":
                self.ledger.drawdown_paused = False

        if not risk_res.approved:
            if risk_res.risk_status == "paused_drawdown":
                self.ledger.drawdown_paused = True
            self._record_watch(candidate.symbol, candidate.strategy, now_utc, risk_res.reason)
            self.signal_store.append_risk_event(
                {
                    "ts_utc": now_utc,
                    "event_type": "signal_rejected",
                    "symbol": candidate.symbol,
                    "strategy": candidate.strategy,
                    "reason": risk_res.reason,
                }
            )
            return None

        sig_id = self._next_signal_id(now_utc)
        snap = InputSnapshot(
            snapshot_id=f"SNAP-{sig_id}",
            signal_id=sig_id,
            captured_at_utc=now_utc,
            symbol=candidate.symbol,
            kline_summary=candidate.snapshot.kline_summary,
            indicators=candidate.snapshot.indicators,
            filter_checks=candidate.snapshot.filter_checks,
        )
        expiry_hours = int(self.config.get("signals", {}).get("order_expiry_hours", 4))
        valid_until = _fmt_utc(_parse_utc(now_utc) + timedelta(hours=expiry_hours))
        time_stop_bars = (
            candidate.time_stop_bars
            if candidate.time_stop_bars is not None
            else int(self.config.get("signals", {}).get("max_hold_bars_15m", 24))
        )

        _, rr_val, _ = self.risk_manager.check_rr_requirements(
            candidate.strategy, candidate.direction, entry_mid, candidate.stop_loss, candidate.tp1, candidate.tp2
        )
        sub_eq = max(1e-9, float(sub_state["subaccount_equity_usd"]))
        risk_pct_pool = round(risk_res.r_plan_usd / sub_eq * 100.0, 2)

        signal = Signal(
            signal_id=sig_id,
            published_at_utc=now_utc,
            strategy=candidate.strategy,
            symbol=candidate.symbol,
            market=inst["market"],
            direction=candidate.direction,
            entry_low=candidate.entry_low,
            entry_high=candidate.entry_high,
            add_levels=(),
            stop_loss=candidate.stop_loss,
            tp1=candidate.tp1,
            tp2=candidate.tp2,
            rr_ratio=round(rr_val, 2),
            risk_pct_pool=risk_pct_pool,
            risk_usd=round(risk_res.r_plan_usd, 2),
            notional_usd=round(risk_res.notional_usd, 2),
            leverage=risk_res.leverage,
            liquidation_price=round(risk_res.liquidation_price, 6),
            valid_until_utc=valid_until,
            invalidation_cond=candidate.invalidation_cond,
            time_stop_bars=time_stop_bars,
            reasons=candidate.reasons,
            snapshot_id=snap.snapshot_id,
            verification_status=candidate.verification_status,
        )
        self.signal_store.publish_signal(signal, snap)

        fee_rate = float(inst.get("fee_rate", 0.0005))
        slippage_bps = float(self.config.get("risk", {}).get("default_slippage_bps", 1.0))

        d_key = _date_key(now_utc)
        self.ledger.daily_signal_counts[d_key] = self.ledger.daily_signal_counts.get(d_key, 0) + 1
        self.ledger.orders[sig_id] = OrderRecord(
            order_id=f"ORD-{sig_id}",
            signal_id=sig_id,
            symbol=candidate.symbol,
            direction=candidate.direction,
            entry_low=candidate.entry_low,
            entry_high=candidate.entry_high,
            valid_until_utc=valid_until,
            status="pending",
        )
        self.ledger.trades[sig_id] = TradeRecord(
            signal_id=sig_id,
            strategy=candidate.strategy,
            symbol=candidate.symbol,
            market=inst["market"],
            correlation_group=inst.get("correlation_group", ""),
            direction=candidate.direction,
            state=LifecycleState.WAITING_ENTRY,
            entry_low=candidate.entry_low,
            entry_high=candidate.entry_high,
            add_levels=(),
            initial_stop_loss=candidate.stop_loss,
            current_stop_loss=candidate.stop_loss,
            current_tp1=candidate.tp1,
            current_tp2=candidate.tp2,
            invalidation_price=candidate.invalidation_price,
            valid_until_utc=valid_until,
            time_stop_bars=time_stop_bars,
            r_plan_usd=risk_res.r_plan_usd,
            planned_notional_usd=risk_res.notional_usd,
            leverage=risk_res.leverage,
            margin_leverage=risk_res.margin_leverage,
            assumed_spread_bps=float(inst.get("assumed_spread_bps", 2.0)),
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            overnight_cost_daily_bps=float(inst.get("overnight_cost_daily_bps", 0.0)),
            published_at_utc=now_utc,
            migrated_unified_sizing=True,
        )

        return signal

    def move_stop_loss(
        self,
        signal_id: str,
        new_sl: float,
        ts_utc: str,
        description: str,
    ) -> bool:
        """Move stop loss ONLY in the favorable direction (SPEC 8.2 & 9)."""
        trade = self.ledger.trades.get(signal_id)
        if trade is None or trade.state not in (LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
            return False

        is_favorable = (
            new_sl > trade.current_stop_loss if trade.direction == "long" else new_sl < trade.current_stop_loss
        )
        if not is_favorable:
            return False

        trade.current_stop_loss = new_sl
        ev = SignalUpdateEvent(
            event_id=self._next_event_id(signal_id),
            signal_id=signal_id,
            ts_utc=ts_utc,
            event_type="stop_moved",
            price=new_sl,
            description=description,
        )
        self.signal_store.append_update(ev)
        return True

    def _emit_event(
        self,
        events_out: list[SignalUpdateEvent],
        signal_id: str,
        ts_utc: str,
        event_type: str,
        price: float,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ev = SignalUpdateEvent(
            event_id=self._next_event_id(signal_id),
            signal_id=signal_id,
            ts_utc=ts_utc,
            event_type=event_type,
            price=price,
            description=description,
            metadata=metadata or {},
        )
        self.signal_store.append_update(ev)
        events_out.append(ev)

    def _close_quantity(
        self,
        trade: TradeRecord,
        qty_to_close: float,
        exec_price: float,
        ts_utc: str,
        action: str,
    ) -> float:
        qty = min(qty_to_close, trade.remaining_qty)
        if qty <= 0:
            return 0.0
        if trade.direction == "long":
            gross_pnl = (exec_price - trade.avg_entry_price) * qty
            side = "sell"
        else:
            gross_pnl = (trade.avg_entry_price - exec_price) * qty
            side = "buy"

        notional = exec_price * qty
        fee_usd = notional * trade.fee_rate
        overnight_usd = 0.0
        if trade.entered_at_utc and trade.overnight_cost_daily_bps > 0:
            days = max(0.0, (_parse_utc(ts_utc) - _parse_utc(trade.entered_at_utc)).total_seconds() / 86400.0)
            overnight_usd = notional * (trade.overnight_cost_daily_bps / 10000.0) * days

        net_pnl = gross_pnl - fee_usd - overnight_usd
        trade.realized_pnl_usd += net_pnl
        trade.total_fees_usd += fee_usd
        trade.total_overnight_usd += overnight_usd
        trade.remaining_qty = max(0.0, trade.remaining_qty - qty)

        if is_short_term_strategy(trade.strategy):
            self.ledger.short_term_realized_pnl += net_pnl
        else:
            self.ledger.long_term_realized_pnl += net_pnl

        d_key = _date_key(ts_utc)
        w_key = _week_key(ts_utc)
        self.ledger.daily_realized_pnl[d_key] = self.ledger.daily_realized_pnl.get(d_key, 0.0) + net_pnl
        self.ledger.weekly_realized_pnl[w_key] = self.ledger.weekly_realized_pnl.get(w_key, 0.0) + net_pnl

        self.ledger.fills.append(
            FillRecord(
                fill_id=self._next_fill_id(trade.signal_id),
                signal_id=trade.signal_id,
                symbol=trade.symbol,
                side=side,
                action=action,
                price=exec_price,
                qty=qty,
                notional_usd=notional,
                fee_usd=fee_usd,
                realized_pnl_usd=net_pnl,
                ts_utc=ts_utc,
            )
        )
        return net_pnl

    def _finalize_close(
        self,
        trade: TradeRecord,
        exec_price: float,
        ts_utc: str,
        exit_reason: str,
        events_out: list[SignalUpdateEvent],
    ) -> None:
        eff_ts = trade.published_at_utc if (trade.published_at_utc and ts_utc < trade.published_at_utc) else ts_utc
        self._close_quantity(trade, trade.remaining_qty, exec_price, eff_ts, action="exit")
        trade.state = validate_transition(trade.state, LifecycleState.CLOSED, exit_reason=exit_reason)
        trade.closed_at_utc = eff_ts
        trade.exit_price = exec_price
        trade.exit_reason = exit_reason
        trade.r_multiple = trade.realized_pnl_usd / max(1e-9, trade.r_plan_usd)
        self._emit_event(
            events_out,
            trade.signal_id,
            eff_ts,
            "closed",
            exec_price,
            f"平倉結束（{exit_reason}），損益 ${trade.realized_pnl_usd:.2f} ({trade.r_multiple:.2f}R)",
            {"exit_reason": exit_reason, "r_multiple": round(trade.r_multiple, 4)},
        )

    def _process_trade_bar(
        self,
        trade: TradeRecord,
        bar: MarketBar,
        atr: float | None,
        atr_ratio: float | None,
        session_end_utc: str | None,
        logic_invalidated: bool,
        events_out: list[SignalUpdateEvent],
    ) -> None:
        if trade.published_at_utc and bar.close_ts_utc <= trade.published_at_utc:
            return

        spread = trade.assumed_spread_bps
        slip = trade.slippage_bps / 10000.0
        bar_close_dt = _parse_utc(bar.close_ts_utc)

        if trade.state == LifecycleState.WAITING_ENTRY:
            if bar_close_dt >= _parse_utc(trade.valid_until_utc):
                trade.state = validate_transition(
                    trade.state, LifecycleState.CANCELLED, exit_reason="expired"
                )
                trade.exit_reason = "expired"
                trade.closed_at_utc = bar.close_ts_utc
                if trade.signal_id in self.ledger.orders:
                    self.ledger.orders[trade.signal_id].status = "cancelled"
                self._emit_event(events_out, trade.signal_id, bar.close_ts_utc, "cancelled", bar.close, "掛單逾時未成交取消")
                return

            inv_hit = logic_invalidated or (
                _bid_price(bar.low, spread) <= trade.invalidation_price
                if trade.direction == "long"
                else _ask_price(bar.high, spread) >= trade.invalidation_price
            )
            if inv_hit:
                trade.state = validate_transition(
                    trade.state, LifecycleState.CANCELLED, exit_reason="pre_entry_invalidated"
                )
                trade.exit_reason = "pre_entry_invalidated"
                trade.closed_at_utc = bar.close_ts_utc
                if trade.signal_id in self.ledger.orders:
                    self.ledger.orders[trade.signal_id].status = "cancelled"
                self._emit_event(events_out, trade.signal_id, bar.close_ts_utc, "cancelled", bar.close, "入場前觸及失效條件取消")
                return

            if trade.direction == "long":
                ask_open = _ask_price(bar.open, spread)
                ask_low = _ask_price(bar.low, spread)
                ask_high = _ask_price(bar.high, spread)
                touched = ask_low <= trade.entry_high and ask_high >= trade.entry_low
                if not touched:
                    return
                fill_price = (
                    ask_open
                    if trade.entry_low <= ask_open <= trade.entry_high
                    else min(trade.entry_high, max(trade.entry_low, ask_low))
                )
                fill_price *= 1.0 + slip
                side = "buy"
            else:
                bid_open = _bid_price(bar.open, spread)
                bid_low = _bid_price(bar.low, spread)
                bid_high = _bid_price(bar.high, spread)
                touched = bid_high >= trade.entry_low and bid_low <= trade.entry_high
                if not touched:
                    return
                fill_price = (
                    bid_open
                    if trade.entry_low <= bid_open <= trade.entry_high
                    else max(trade.entry_low, min(trade.entry_high, bid_high))
                )
                fill_price *= 1.0 - slip
                side = "sell"

            full_notional = trade.planned_notional_usd
            if full_notional <= 0:
                return
            qty = full_notional / fill_price
            fee_usd = full_notional * trade.fee_rate
            trade.total_fees_usd += fee_usd
            trade.realized_pnl_usd -= fee_usd
            if is_short_term_strategy(trade.strategy):
                self.ledger.short_term_realized_pnl -= fee_usd
            else:
                self.ledger.long_term_realized_pnl -= fee_usd

            stage = TradeStage(1, "initial", fill_price, qty, full_notional, bar.ts_utc)
            trade.stages.append(stage)
            trade.avg_entry_price = fill_price
            trade.remaining_qty = qty
            trade.initial_filled_qty = qty
            trade.entered_at_utc = bar.ts_utc
            trade.state = validate_transition(trade.state, LifecycleState.ACTIVE)
            if trade.signal_id in self.ledger.orders:
                self.ledger.orders[trade.signal_id].status = "filled"
            self.ledger.fills.append(
                FillRecord(
                    fill_id=self._next_fill_id(trade.signal_id),
                    signal_id=trade.signal_id,
                    symbol=trade.symbol,
                    side=side,
                    action="entry",
                    price=fill_price,
                    qty=qty,
                    notional_usd=full_notional,
                    fee_usd=fee_usd,
                    realized_pnl_usd=-fee_usd,
                    ts_utc=bar.ts_utc,
                )
            )
            self._emit_event(events_out, trade.signal_id, bar.ts_utc, "entered", fill_price, f"全額進場成交（名目 ${full_notional:.2f}）")
            return

        # 1) Gap open beyond stop loss -> fill at open price
        if trade.direction == "long":
            bid_open = _bid_price(bar.open, spread)
            if bid_open <= trade.current_stop_loss:
                self._finalize_close(trade, bid_open * (1.0 - slip), bar.ts_utc, "stop_loss", events_out)
                return
        else:
            ask_open = _ask_price(bar.open, spread)
            if ask_open >= trade.current_stop_loss:
                self._finalize_close(trade, ask_open * (1.0 + slip), bar.ts_utc, "stop_loss", events_out)
                return

        # 2) Same-bar SL & TP conservative check: SL BEFORE TP
        if trade.direction == "long":
            if _bid_price(bar.low, spread) <= trade.current_stop_loss:
                self._finalize_close(trade, trade.current_stop_loss * (1.0 - slip), bar.close_ts_utc, "stop_loss", events_out)
                return
        else:
            if _ask_price(bar.high, spread) >= trade.current_stop_loss:
                self._finalize_close(trade, trade.current_stop_loss * (1.0 + slip), bar.close_ts_utc, "stop_loss", events_out)
                return

        # 3) Breakout protection
        signals_cfg = self.config.get("signals", {})
        bo_bar_mult = float(signals_cfg.get("breakout_bar_atr_mult", 2.5))
        bo_atr_trig = float(signals_cfg.get("breakout_atr_ratio_trigger", 1.8))
        bar_range = bar.high - bar.low
        breakout_hit = (atr is not None and atr > 0 and bar_range > bo_bar_mult * atr) or (
            atr_ratio is not None and atr_ratio >= bo_atr_trig
        )
        if breakout_hit:
            adverse_px = (
                _bid_price(bar.low, spread) * (1.0 - slip)
                if trade.direction == "long"
                else _ask_price(bar.high, spread) * (1.0 + slip)
            )
            self._finalize_close(trade, adverse_px, bar.close_ts_utc, "breakout_protection", events_out)
            return

        # 4) Logic invalidation
        if logic_invalidated:
            close_px = (
                _bid_price(bar.close, spread) * (1.0 - slip)
                if trade.direction == "long"
                else _ask_price(bar.close, spread) * (1.0 + slip)
            )
            self._finalize_close(trade, close_px, bar.close_ts_utc, "logic_invalidated", events_out)
            return

        # 5) Session-end stop
        if session_end_utc is not None:
            pre_end_min = int(signals_cfg.get("pre_session_end_close_minutes", 30))
            if bar_close_dt >= _parse_utc(session_end_utc) - timedelta(minutes=pre_end_min):
                close_px = (
                    _bid_price(bar.close, spread) * (1.0 - slip)
                    if trade.direction == "long"
                    else _ask_price(bar.close, spread) * (1.0 + slip)
                )
                self._finalize_close(trade, close_px, bar.close_ts_utc, "session_stop", events_out)
                return

        # 6) Time stop
        if trade.entered_at_utc and trade.time_stop_bars > 0:
            held_min = (bar_close_dt - _parse_utc(trade.entered_at_utc)).total_seconds() / 60.0
            if held_min >= trade.time_stop_bars * 15:
                close_px = (
                    _bid_price(bar.close, spread) * (1.0 - slip)
                    if trade.direction == "long"
                    else _ask_price(bar.close, spread) * (1.0 + slip)
                )
                self._finalize_close(trade, close_px, bar.close_ts_utc, "time_stop", events_out)
                return

        # 7) TP1 / TP2
        if trade.state == LifecycleState.ACTIVE:
            tp1_hit = (
                _bid_price(bar.high, spread) >= trade.current_tp1
                if trade.direction == "long"
                else _ask_price(bar.low, spread) <= trade.current_tp1
            )
            if tp1_hit:
                tp1_frac = float(signals_cfg.get("tp1_close_fraction", 0.50))
                close_qty = trade.remaining_qty * tp1_frac
                tp1_px = trade.current_tp1 * (1.0 - slip if trade.direction == "long" else 1.0 + slip)
                pnl = self._close_quantity(trade, close_qty, tp1_px, bar.close_ts_utc, action="tp1")
                trade.state = validate_transition(trade.state, LifecycleState.TP1_HIT)
                self._emit_event(
                    events_out,
                    trade.signal_id,
                    bar.close_ts_utc,
                    "tp1_hit",
                    tp1_px,
                    f"達成 TP1，部分平倉 {tp1_frac*100:.0f}%，實現損益 ${pnl:.2f}",
                )
                self.move_stop_loss(
                    trade.signal_id,
                    trade.avg_entry_price,
                    bar.close_ts_utc,
                    "TP1 達成，剩餘倉位止損移至平均成本保本",
                )

        if trade.state == LifecycleState.TP1_HIT and isinstance(trade.current_tp2, (int, float)):
            tp2_val = float(trade.current_tp2)
            tp2_hit = (
                _bid_price(bar.high, spread) >= tp2_val
                if trade.direction == "long"
                else _ask_price(bar.low, spread) <= tp2_val
            )
            if tp2_hit:
                tp2_px = tp2_val * (1.0 - slip if trade.direction == "long" else 1.0 + slip)
                self._finalize_close(trade, tp2_px, bar.close_ts_utc, "tp2", events_out)
                return

        # 8) Breakeven / trailing stop checks at >= 1R / >= 2R
        unit_risk = max(1e-9, abs(trade.avg_entry_price - trade.initial_stop_loss))
        cur_profit = (
            (bar.close - trade.avg_entry_price)
            if trade.direction == "long"
            else (trade.avg_entry_price - bar.close)
        )
        cur_r = cur_profit / unit_risk
        be_trig = float(signals_cfg.get("breakeven_trigger_r", 1.0))
        tr_trig = float(signals_cfg.get("trailing_stop_trigger_r", 2.0))
        tr_mult = float(signals_cfg.get("trailing_stop_atr_mult", 2.0))

        if cur_r >= tr_trig and atr is not None and atr > 0:
            if trade.direction == "long":
                cand_sl = max(trade.avg_entry_price, bar.close - tr_mult * atr)
            else:
                cand_sl = min(trade.avg_entry_price, bar.close + tr_mult * atr)
            self.move_stop_loss(
                trade.signal_id,
                cand_sl,
                bar.close_ts_utc,
                f"獲利達 {cur_r:.2f}R，更新 ATR 移動止損",
            )
        elif cur_r >= be_trig:
            self.move_stop_loss(
                trade.signal_id,
                trade.avg_entry_price,
                bar.close_ts_utc,
                f"獲利達 {cur_r:.2f}R，止損移至損益兩平",
            )

    def process_bar(
        self,
        symbol: str,
        bar: MarketBar,
        atr: float | None = None,
        atr_ratio: float | None = None,
        session_end_utc: str | None = None,
        logic_invalidated: bool = False,
    ) -> list[SignalUpdateEvent]:
        """Process a single closed bar across the ledger idempotently."""
        prev_ts = self.last_processed_ts.get(symbol)
        if prev_ts is not None and bar.close_ts_utc <= prev_ts:
            return []

        self.latest_marks[symbol] = bar.close
        events_out: list[SignalUpdateEvent] = []

        for trade in list(self.ledger.trades.values()):
            if trade.symbol != symbol:
                continue
            if trade.state in (LifecycleState.CLOSED, LifecycleState.CANCELLED):
                continue
            self._process_trade_bar(
                trade=trade,
                bar=bar,
                atr=atr,
                atr_ratio=atr_ratio,
                session_end_utc=session_end_utc,
                logic_invalidated=logic_invalidated,
                events_out=events_out,
            )

        self.last_processed_ts[symbol] = bar.close_ts_utc
        return events_out

    def replay_bars(
        self,
        symbol: str,
        bars: list[MarketBar],
        atr: float | None = None,
        atr_ratio: float | None = None,
        session_end_utc: str | None = None,
        logic_invalidated: bool = False,
    ) -> list[SignalUpdateEvent]:
        """Chronologically replay bars since last_processed_ts (catch-up replay)."""
        sorted_bars = sorted(bars, key=lambda b: b.close_ts_utc)
        all_events: list[SignalUpdateEvent] = []
        for bar in sorted_bars:
            all_events.extend(
                self.process_bar(
                    symbol=symbol,
                    bar=bar,
                    atr=atr,
                    atr_ratio=atr_ratio,
                    session_end_utc=session_end_utc,
                    logic_invalidated=logic_invalidated,
                )
            )
        return all_events

    def close_trade_by_strategy(
        self,
        strategy_id: str,
        symbol: str,
        close_price: float,
        ts_utc: str,
        exit_reason: str = "logic_invalidated",
    ) -> list[SignalUpdateEvent]:
        """Close or cancel active/pending trades for `(strategy_id, symbol)`."""
        events_out: list[SignalUpdateEvent] = []
        slip_default = float(self.config.get("risk", {}).get("default_slippage_bps", 1.0)) / 10000.0
        for trade in list(self.ledger.trades.values()):
            if trade.strategy != strategy_id or trade.symbol != symbol:
                continue
            eff_ts = trade.published_at_utc if (trade.published_at_utc and ts_utc < trade.published_at_utc) else ts_utc
            if trade.state == LifecycleState.WAITING_ENTRY:
                trade.state = validate_transition(
                    trade.state, LifecycleState.CANCELLED, exit_reason="pre_entry_invalidated"
                )
                trade.exit_reason = "pre_entry_invalidated"
                trade.closed_at_utc = eff_ts
                if trade.signal_id in self.ledger.orders:
                    self.ledger.orders[trade.signal_id].status = "cancelled"
                self._emit_event(
                    events_out, trade.signal_id, eff_ts, "cancelled", close_price, "策略訊號結束，取消未成交掛單"
                )
            elif trade.state in (LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
                spread = trade.assumed_spread_bps
                slip = trade.slippage_bps / 10000.0 if trade.slippage_bps else slip_default
                exec_px = (
                    _bid_price(close_price, spread) * (1.0 - slip)
                    if trade.direction == "long"
                    else _ask_price(close_price, spread) * (1.0 + slip)
                )
                self._finalize_close(trade, exec_px, eff_ts, exit_reason, events_out)
        return events_out

    def save_state(self, snapshot_ts_utc: str | None = None) -> None:
        """Persist single ledger, orders, fills, equity snapshots, and analytics to out_dir."""
        ts = snapshot_ts_utc or max(self.last_processed_ts.values(), default=_fmt_utc(datetime.now(timezone.utc)))
        led = self.ledger

        try:
            for stale in self.out_dir.glob("*_comparison.json"):
                stale.unlink(missing_ok=True)
        except Exception:
            pass

        (self.out_dir / "trades.json").write_text(
            json.dumps([t.to_dict() for t in led.trades.values()], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        all_stages = [
            {"signal_id": t.signal_id, "symbol": t.symbol, "direction": t.direction, **s.to_dict()}
            for t in led.trades.values()
            for s in t.stages
        ]
        (self.out_dir / "trade_stages.json").write_text(
            json.dumps(all_stages, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.out_dir / "orders.json").write_text(
            json.dumps([o.to_dict() for o in led.orders.values()], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        fills_dir = self.out_dir / "fills"
        fills_dir.mkdir(parents=True, exist_ok=True)
        fills_by_month: dict[str, list[dict[str, Any]]] = {}
        for f in led.fills:
            fills_by_month.setdefault(_month_key(f.ts_utc), []).append(f.to_dict())
        for m, rows in fills_by_month.items():
            with (fills_dir / f"{m}.jsonl").open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        eq_dir = self.out_dir / "equity_snapshots"
        eq_dir.mkdir(parents=True, exist_ok=True)
        snap_row = {
            "ts_utc": ts,
            "total_equity_usd": round(led.compute_equity(self.latest_marks), 2),
            "short_term_equity_usd": round(led.compute_pool_equity("short_term", self.latest_marks), 2),
            "long_term_equity_usd": round(led.compute_pool_equity("long_term", self.latest_marks), 2),
        }
        with (eq_dir / f"{_month_key(ts)}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap_row, ensure_ascii=False) + "\n")

        tp1_frac = float(self.config.get("signals", {}).get("tp1_close_fraction", 0.50))
        short_eq = float(led.compute_pool_equity("short_term", self.latest_marks))
        long_eq = float(led.compute_pool_equity("long_term", self.latest_marks))

        signals_index: list[dict[str, Any]] = []
        sig_dir = self.out_dir / "signals"
        if sig_dir.exists():
            for sp in sorted(sig_dir.glob("*.json"), reverse=True):
                try:
                    s_data = json.loads(sp.read_text(encoding="utf-8"))
                    sid = s_data["signal_id"]
                    tr = led.trades.get(sid)
                    s_data["lifecycle_state"] = tr.state.value if tr else "waiting_entry"
                    s_data["stages_filled"] = len(tr.stages) if tr else 0
                    s_data["realized_pnl_usd"] = round(tr.realized_pnl_usd, 2) if tr else 0.0
                    s_data["r_multiple"] = round(tr.r_multiple, 2) if tr else 0.0
                    s_data["exit_reason"] = tr.exit_reason if tr else None
                    s_data["avg_entry_price"] = round(tr.avg_entry_price, 6) if tr else 0.0
                    s_data["remaining_qty"] = round(tr.remaining_qty, 8) if tr else 0.0
                    s_data["current_stop_loss"] = tr.current_stop_loss if tr else s_data["stop_loss"]
                    s_data["current_tp1"] = tr.current_tp1 if tr else s_data["tp1"]
                    s_data["current_tp2"] = tr.current_tp2 if tr else s_data["tp2"]
                    s_data["r_plan_usd"] = tr.r_plan_usd if tr else s_data["risk_usd"]
                    s_data["entered_at_utc"] = tr.entered_at_utc if tr else None
                    s_data["closed_at_utc"] = tr.closed_at_utc if tr else None
                    s_data["exit_price"] = tr.exit_price if tr else None
                    s_data["stages"] = [st.to_dict() for st in tr.stages] if tr else []
                    if tr is not None:
                        sub_eq = short_eq if is_short_term_strategy(tr.strategy) else long_eq
                        est = compute_trade_estimates(tr, sub_eq, tp1_close_fraction=tp1_frac)
                        s_data["effective_notional_usd"] = est["effective_notional_usd"]
                        s_data["actual_leverage"] = est["actual_leverage"]
                        s_data["estimated_stop_loss_usd"] = est["estimated_stop_loss_usd"]
                        s_data["estimated_stop_loss_pct_pool"] = est["estimated_stop_loss_pct_pool"]
                        s_data["estimated_tp1_profit_usd"] = est["estimated_tp1_profit_usd"]
                        s_data["estimated_tp2_profit_usd"] = est["estimated_tp2_profit_usd"]
                    signals_index.append(s_data)
                except Exception:
                    continue
        (self.out_dir / "signals_index.json").write_text(
            json.dumps(signals_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        (self.out_dir / "ledgers_state.json").write_text(
            json.dumps(
                {
                    "sequences": {
                        "signal": self._signal_seq,
                        "event": self._event_seq,
                        "fill": self._fill_seq,
                    },
                    "latest_marks": self.latest_marks,
                    "ledger": led.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        state_path = self.out_dir / "state.json"
        existing_state = {}
        if state_path.exists():
            try:
                existing_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                existing_state = {}
        existing_state["last_processed_ts"] = self.last_processed_ts
        state_path.write_text(json.dumps(existing_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        (self.out_dir / "performance_stats.json").write_text(
            json.dumps(build_performance_stats(self, ts), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.out_dir / "risk_status.json").write_text(
            json.dumps(build_risk_status(self, ts), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.out_dir / "equity_curve.json").write_text(
            json.dumps(export_equity_curve(self.out_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def purge_invalid_signals(self) -> list[str]:
        """Remove corrupted/expired historical signals (e.g. closed_at_utc < published_at_utc or stale S2 FX)."""
        sig_pub_map: dict[str, dict[str, Any]] = {}
        sig_dir = self.out_dir / "signals"
        if sig_dir.exists():
            for sp in sig_dir.glob("*.json"):
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    sid = str(data.get("signal_id", "")).strip()
                    if sid:
                        sig_pub_map[sid] = data
                except Exception:
                    continue

        for sid, tr in self.ledger.trades.items():
            if not tr.published_at_utc and sid in sig_pub_map:
                tr.published_at_utc = str(sig_pub_map[sid].get("published_at_utc") or "")

        corrupted_known_ids = {"SIG-20261116-0001", "SIG-20261116-0002", "SIG-20261116-0003"}
        bad_ids: set[str] = set()

        for sid, sdata in sig_pub_map.items():
            pub_ts = str(sdata.get("published_at_utc") or "")
            if (
                sid in corrupted_known_ids
                and str(sdata.get("strategy", "")) == "S2"
                and str(sdata.get("symbol", "")) in {"EURUSD", "GBPUSD", "USDCHF"}
                and (not pub_ts or pub_ts <= "2026-11-16T12:45:00Z")
            ):
                bad_ids.add(sid)
                continue
            snap_path = self.out_dir / "snapshots" / f"SNAP-{sid}.json"
            if pub_ts and snap_path.exists():
                try:
                    snap = json.loads(snap_path.read_text(encoding="utf-8"))
                    ks = snap.get("kline_summary") or {}
                    kts = str(ks.get("ts_utc") or "")
                    kiv = str(ks.get("interval") or "")
                    if kts and kiv == "4h":
                        close_dt = _parse_utc(kts) + timedelta(hours=4)
                        if (_parse_utc(pub_ts) - close_dt).total_seconds() / 60.0 > 300.0:
                            bad_ids.add(sid)
                except Exception:
                    pass

        for sid, tr in self.ledger.trades.items():
            pub_ts = tr.published_at_utc or str((sig_pub_map.get(sid) or {}).get("published_at_utc") or "")
            if (
                sid in corrupted_known_ids
                and tr.strategy == "S2"
                and tr.symbol in {"EURUSD", "GBPUSD", "USDCHF"}
                and (not pub_ts or pub_ts <= "2026-11-16T12:45:00Z")
            ):
                bad_ids.add(sid)
            if tr.closed_at_utc and pub_ts and tr.closed_at_utc < pub_ts:
                bad_ids.add(sid)

        if not bad_ids:
            return []

        removed_any = False
        for sid in bad_ids:
            tr = self.ledger.trades.pop(sid, None)
            self.ledger.orders.pop(sid, None)
            if tr is not None:
                removed_any = True
                pub_ts = tr.published_at_utc or str((sig_pub_map.get(sid) or {}).get("published_at_utc") or "")
                if pub_ts:
                    d_key = _date_key(pub_ts)
                    if self.ledger.daily_signal_counts.get(d_key, 0) > 0:
                        self.ledger.daily_signal_counts[d_key] -= 1
        if removed_any:
            self.ledger.fills = [f for f in self.ledger.fills if f.signal_id not in bad_ids]
            strat_by_sig = {s: t.strategy for s, t in self.ledger.trades.items()}
            short_pnl = 0.0
            long_pnl = 0.0
            d_pnl: dict[str, float] = {}
            w_pnl: dict[str, float] = {}
            for f in self.ledger.fills:
                strat = strat_by_sig.get(f.signal_id, "S1")
                if is_short_term_strategy(strat):
                    short_pnl += f.realized_pnl_usd
                else:
                    long_pnl += f.realized_pnl_usd
                if f.action != "entry":
                    dk = _date_key(f.ts_utc)
                    wk = _week_key(f.ts_utc)
                    d_pnl[dk] = d_pnl.get(dk, 0.0) + f.realized_pnl_usd
                    w_pnl[wk] = w_pnl.get(wk, 0.0) + f.realized_pnl_usd
            self.ledger.short_term_realized_pnl = short_pnl
            self.ledger.long_term_realized_pnl = long_pnl
            self.ledger.daily_realized_pnl = d_pnl
            self.ledger.weekly_realized_pnl = w_pnl

        for sid in sorted(bad_ids):
            for path in (
                self.out_dir / "signals" / f"{sid}.json",
                self.out_dir / "snapshots" / f"SNAP-{sid}.json",
                self.out_dir / "signal_updates" / f"{sid}.jsonl",
                self.out_dir / "charts" / f"{sid}.json",
            ):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

        return sorted(bad_ids)

    def migrate_pending_orders_to_unified_sizing(self) -> list[str]:
        """Recalculate legacy pending (waiting_entry) orders once using unified full-entry sub-account sizing.

        Already filled/active trades keep their exact original position size, stop-loss, and targets.
        """
        updated_ids: list[str] = []
        for sid, tr in self.ledger.trades.items():
            if tr.state != LifecycleState.WAITING_ENTRY or tr.stages or tr.migrated_unified_sizing:
                continue
            pool_name = "short_term" if is_short_term_strategy(tr.strategy) else "long_term"
            sub_eq = max(1e-6, float(self.ledger.compute_pool_equity(pool_name, self.latest_marks)))
            entry_mid = (tr.entry_low + tr.entry_high) / 2.0
            alloc = allocate_subaccount_position(
                strategy_id=tr.strategy,
                subaccount_equity_usd=sub_eq,
                open_positions_count=0,
                open_total_risk_usd=0.0,
                open_total_notional_usd=0.0,
                entry_price=entry_mid,
                stop_loss=tr.initial_stop_loss,
                fee_rate=tr.fee_rate,
                slippage_bps=tr.slippage_bps,
                config=self.config,
                risk_multiplier=1.0,
            )
            if alloc.approved and alloc.notional_usd > 0:
                actual_lev = round(alloc.notional_usd / sub_eq, 4)
                tr.planned_notional_usd = round(alloc.notional_usd, 4)
                tr.r_plan_usd = round(alloc.risk_usd, 4)
                tr.leverage = actual_lev
                inst = self.instruments.get(tr.symbol, {})
                max_lev = max(1.0, float(inst.get("max_leverage", 5.0)))
                sl_dist_pct = max(0.002, abs(entry_mid - tr.initial_stop_loss) / max(1e-9, entry_mid))
                tr.margin_leverage = min(max_lev, max(1.0, min(5.0, round(0.20 / sl_dist_pct, 2))))
                tr.add_levels = ()
                tr.migrated_unified_sizing = True
                updated_ids.append(sid)

                sig_path = self.out_dir / "signals" / f"{sid}.json"
                if sig_path.exists():
                    try:
                        s_data = json.loads(sig_path.read_text(encoding="utf-8"))
                        s_data["notional_usd"] = round(alloc.notional_usd, 2)
                        s_data["risk_usd"] = round(alloc.risk_usd, 2)
                        s_data["risk_pct_pool"] = round(alloc.risk_usd / sub_eq * 100.0, 2)
                        s_data["leverage"] = actual_lev
                        s_data["add_levels"] = []
                        sig_path.write_text(json.dumps(s_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    except Exception:
                        pass

        return updated_ids

    def load_state(self) -> None:
        """Restore persisted single ledger (migrating legacy E ledger if needed) and replay cursors."""
        try:
            for stale in self.out_dir.glob("*_comparison.json"):
                stale.unlink(missing_ok=True)
        except Exception:
            pass

        state_path = self.out_dir / "state.json"
        if state_path.exists():
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(st.get("last_processed_ts"), dict):
                    self.last_processed_ts = dict(st["last_processed_ts"])
            except Exception:
                pass

        led_path = self.out_dir / "ledgers_state.json"
        if led_path.exists():
            try:
                payload = json.loads(led_path.read_text(encoding="utf-8"))
                seqs = payload.get("sequences", {})
                self._signal_seq = int(seqs.get("signal", 0))
                self._event_seq = int(seqs.get("event", 0))
                self._fill_seq = int(seqs.get("fill", 0))
                if isinstance(payload.get("latest_marks"), dict):
                    self.latest_marks = {k: float(v) for k, v in payload["latest_marks"].items()}
                raw_single = payload.get("ledger")
                if isinstance(raw_single, dict):
                    self.ledger = Ledger.from_dict(raw_single, self.config)
                elif isinstance(payload.get("ledgers"), dict) and payload["ledgers"]:
                    raw_legacy = payload["ledgers"].get("E") or next(iter(payload["ledgers"].values()))
                    self.ledger = Ledger.from_dict(raw_legacy, self.config)
            except Exception:
                pass

        self.purge_invalid_signals()
        self.migrate_pending_orders_to_unified_sizing()

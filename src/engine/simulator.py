"""Single-ledger simulation and catch-up replay engine unified with 5-year backtest."""

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
    EXIT_REASON_LABELS,
    InputSnapshot,
    LifecycleState,
    Signal,
    SignalUpdateEvent,
    round_price,
    validate_transition,
)
from src.signals.store import SignalStore


def _parse_utc(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_key(ts_utc: str) -> str:
    return ts_utc[:7] if len(ts_utc) >= 7 else "unknown"


def _bid_price(mid: float, spread_bps: float) -> float:
    return mid * (1.0 - spread_bps / 20000.0)


def _ask_price(mid: float, spread_bps: float) -> float:
    return mid * (1.0 + spread_bps / 20000.0)


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
    """Candidate signal produced by a strategy prior to risk/allocation check."""

    strategy: str  # "S1", "S2", "S3", "L1", "L2", or "L3"
    symbol: str
    direction: str  # "long" or "short"
    ref_price: float
    stop_loss: float
    reasons: tuple[str, ...]
    snapshot: InputSnapshot
    take_profit: float | None = None
    exit_rule: str = ""
    trailing_exit_price: float | None = None
    verification_status: str = ""
    risk_multiplier: float = 1.0


@dataclass
class FillRecord:
    """Simulated execution fill ("entry" or "exit")."""

    fill_id: str
    signal_id: str
    symbol: str
    side: str  # "buy" or "sell"
    action: str  # "entry" or "exit"
    price: float
    qty: float
    notional_usd: float
    fee_usd: float
    realized_pnl_usd: float
    ts_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeRecord:
    """Simulated trade state in the unified single ledger."""

    signal_id: str
    strategy: str
    symbol: str
    market: str
    correlation_group: str
    direction: str
    state: LifecycleState
    entry_price: float
    initial_stop_loss: float
    current_stop_loss: float
    r_plan_usd: float
    planned_notional_usd: float
    subaccount_capital_usd: float
    assumed_spread_bps: float
    fee_rate: float
    slippage_bps: float
    overnight_cost_daily_bps: float
    take_profit: float | None = None
    exit_rule: str = ""
    trailing_exit_price: float | None = None
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

    def current_notional_usd(self) -> float:
        return self.remaining_qty * self.avg_entry_price if self.remaining_qty > 0 else 0.0

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
            "entry_price": round_price(self.entry_price),
            "avg_entry_price": round_price(self.avg_entry_price),
            "remaining_qty": round(self.remaining_qty, 8),
            "initial_filled_qty": round(self.initial_filled_qty, 8),
            "initial_stop_loss": self.initial_stop_loss,
            "current_stop_loss": self.current_stop_loss,
            "take_profit": self.take_profit,
            "exit_rule": self.exit_rule,
            "trailing_exit_price": self.trailing_exit_price,
            "r_plan_usd": self.r_plan_usd,
            "planned_notional_usd": round(self.planned_notional_usd, 4),
            "subaccount_capital_usd": round(self.subaccount_capital_usd, 4),
            "realized_pnl_usd": round(self.realized_pnl_usd, 4),
            "total_fees_usd": round(self.total_fees_usd, 4),
            "total_overnight_usd": round(self.total_overnight_usd, 4),
            "r_multiple": round(self.r_multiple, 4),
            "entered_at_utc": self.entered_at_utc,
            "closed_at_utc": self.closed_at_utc,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "assumed_spread_bps": self.assumed_spread_bps,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "overnight_cost_daily_bps": self.overnight_cost_daily_bps,
            "published_at_utc": self.published_at_utc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: dict[str, Any] | None = None) -> TradeRecord:
        raw_state = str(data.get("state", "active"))
        if raw_state == "tp1_hit":
            raw_state = "active"
        elif raw_state == "waiting_entry":
            raw_state = "cancelled"
        state = LifecycleState(raw_state)

        strat = str(data["strategy"])
        default_cap = float(
            (config or {}).get("strategies", {}).get(strat, {}).get(
                "capital_usd",
                10000.0 if strat.startswith("S") else 23333.33,
            )
        )
        if "entry_price" in data:
            entry_px = float(data["entry_price"])
        else:
            entry_px = float(data.get("avg_entry_price") or (float(data.get("entry_low", 0.0)) + float(data.get("entry_high", 0.0))) / 2.0)

        planned_notional = float(data.get("planned_notional_usd", 0.0))
        if planned_notional <= 0 and data.get("stages"):
            planned_notional = sum(float(s.get("notional_usd", 0.0)) for s in data["stages"])

        init_sl = float(data["initial_stop_loss"])
        cur_sl = init_sl if state == LifecycleState.ACTIVE else float(data.get("current_stop_loss", init_sl))

        return cls(
            signal_id=data["signal_id"],
            strategy=strat,
            symbol=data["symbol"],
            market=data["market"],
            correlation_group=data.get("correlation_group", ""),
            direction=data["direction"],
            state=state,
            entry_price=entry_px,
            initial_stop_loss=init_sl,
            current_stop_loss=cur_sl,
            take_profit=float(data["take_profit"]) if data.get("take_profit") is not None else None,
            exit_rule=str(data.get("exit_rule") or ""),
            trailing_exit_price=float(data["trailing_exit_price"]) if data.get("trailing_exit_price") is not None else None,
            r_plan_usd=float(data["r_plan_usd"]),
            planned_notional_usd=planned_notional,
            subaccount_capital_usd=float(data.get("subaccount_capital_usd", default_cap)),
            assumed_spread_bps=float(data.get("assumed_spread_bps", 2.0)),
            fee_rate=float(data.get("fee_rate", 0.0005)),
            slippage_bps=float(data.get("slippage_bps", 1.0)),
            overnight_cost_daily_bps=float(data.get("overnight_cost_daily_bps", 0.0)),
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
            exit_reason=data.get("exit_reason") if state != LifecycleState.CANCELLED else (data.get("exit_reason") or "risk_cancelled"),
            published_at_utc=str(data.get("published_at_utc") or ""),
        )


class Ledger:
    """Single virtual account ledger with isolated per-strategy sub-accounts (S1-S3, L1-L3)."""

    STRATEGY_IDS = ("S1", "S2", "S3", "L1", "L2", "L3")

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        strats_cfg = config.get("strategies", {})
        self.strategy_capitals: dict[str, float] = {
            sid: float(strats_cfg.get(sid, {}).get("capital_usd", 10000.0 if sid.startswith("S") else 23333.33))
            for sid in self.STRATEGY_IDS
        }
        self.strategy_realized_pnl: dict[str, float] = {sid: 0.0 for sid in self.STRATEGY_IDS}
        self.peak_equity_usd: float = sum(self.strategy_capitals.values())
        self.trades: dict[str, TradeRecord] = {}
        self.fills: list[FillRecord] = []

    def compute_strategy_realized_equity(self, strategy_id: str) -> float:
        base = self.strategy_capitals.get(strategy_id, 10000.0)
        return max(0.01, base + self.strategy_realized_pnl.get(strategy_id, 0.0))

    def compute_strategy_equity(self, strategy_id: str, mark_prices: dict[str, float] | None = None) -> float:
        marks = mark_prices or {}
        base = self.strategy_capitals.get(strategy_id, 10000.0)
        realized = self.strategy_realized_pnl.get(strategy_id, 0.0)
        unrealized = 0.0
        for tr in self.trades.values():
            if tr.state == LifecycleState.ACTIVE and tr.strategy == strategy_id:
                mp = marks.get(tr.symbol, tr.avg_entry_price)
                # Include already-accrued entry fee on open position for mark-to-market accuracy
                unrealized += tr.unrealized_pnl_usd(mp) + tr.realized_pnl_usd
        return max(0.01, base + realized + unrealized)

    def compute_pool_equity(self, pool_name: str, mark_prices: dict[str, float] | None = None) -> float:
        sids = ("S1", "S2", "S3") if pool_name == "short_term" else ("L1", "L2", "L3")
        return sum(self.compute_strategy_equity(sid, mark_prices) for sid in sids)

    def compute_equity(self, mark_prices: dict[str, float] | None = None) -> float:
        eq = self.compute_pool_equity("short_term", mark_prices) + self.compute_pool_equity("long_term", mark_prices)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        return eq

    def build_account_state(self, now_utc: str, mark_prices: dict[str, float] | None = None) -> dict[str, Any]:
        marks = mark_prices or {}
        equity = self.compute_equity(marks)
        active_pairs: set[tuple[str, str]] = set()
        for tr in self.trades.values():
            if tr.state == LifecycleState.ACTIVE:
                active_pairs.add((tr.symbol, tr.strategy))
        return {
            "equity_usd": equity,
            "peak_equity_usd": self.peak_equity_usd,
            "active_symbol_strategies": active_pairs,
        }

    def build_subaccount_state(self, strategy_id: str) -> dict[str, Any]:
        """Aggregate open risk/notional and realized equity for one strategy sub-account."""
        sub_eq = self.compute_strategy_realized_equity(strategy_id)
        open_cnt = 0
        open_risk = 0.0
        open_notional = 0.0
        for tr in self.trades.values():
            if tr.strategy == strategy_id and tr.state == LifecycleState.ACTIVE:
                open_cnt += 1
                open_risk += tr.r_plan_usd
                open_notional += tr.planned_notional_usd
        return {
            "subaccount_equity_usd": sub_eq,
            "subaccount_open_positions": open_cnt,
            "subaccount_open_risk_usd": open_risk,
            "subaccount_open_notional_usd": open_notional,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_realized_pnl": self.strategy_realized_pnl,
            "peak_equity_usd": self.peak_equity_usd,
            "trades": {k: v.to_dict() for k, v in self.trades.items()},
            "fills": [f.to_dict() for f in self.fills],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: dict[str, Any]) -> Ledger:
        led = cls(config)
        led.peak_equity_usd = float(data.get("peak_equity_usd", led.peak_equity_usd))
        led.trades = {k: TradeRecord.from_dict(v, config) for k, v in data.get("trades", {}).items()}
        led.fills = [FillRecord(**f) for f in data.get("fills", [])]

        if isinstance(data.get("strategy_realized_pnl"), dict):
            for sid in cls.STRATEGY_IDS:
                led.strategy_realized_pnl[sid] = float(data["strategy_realized_pnl"].get(sid, 0.0))
        else:
            # Reconstruct per-strategy closed realized PnL from closed trades' fills
            fills_by_sig: dict[str, float] = {}
            for f in led.fills:
                fills_by_sig[f.signal_id] = fills_by_sig.get(f.signal_id, 0.0) + f.realized_pnl_usd
            for sid, tr in led.trades.items():
                if tr.state == LifecycleState.CLOSED and tr.strategy in led.strategy_realized_pnl:
                    pnl_val = fills_by_sig.get(sid, tr.realized_pnl_usd)
                    led.strategy_realized_pnl[tr.strategy] += pnl_val
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
        entry_bar: MarketBar | None = None,
        mark_prices: dict[str, float] | None = None,
    ) -> Signal | None:
        """Validate freshness & strategy sub-account allocation; publish immutable Signal and fill market open."""
        inst = self.instruments.get(candidate.symbol)
        if inst is None or not inst.get("enabled", True):
            self._record_watch(candidate.symbol, candidate.strategy, now_utc, f"標的未啟用或未知: {candidate.symbol}")
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
        spread_bps = float(inst.get("assumed_spread_bps", 2.0))
        slippage_bps = float(self.config.get("risk", {}).get("default_slippage_bps", 1.0))
        slip = slippage_bps / 10000.0
        fee_rate = float(inst.get("fee_rate", 0.0005))

        open_px = entry_bar.open if entry_bar is not None else candidate.ref_price
        if open_px <= 0:
            self._record_watch(candidate.symbol, candidate.strategy, now_utc, "進場開盤價無效")
            return None
        entry_ts = entry_bar.ts_utc if entry_bar is not None else latest_bar_close_utc
        raw_entry = (
            open_px * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
            if candidate.direction == "long"
            else open_px * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
        )

        acct = self.ledger.build_account_state(now_utc, marks)
        sub_state = self.ledger.build_subaccount_state(candidate.strategy)
        acct.update(sub_state)
        acct["subaccount_risk_multiplier"] = candidate.risk_multiplier

        risk_res = self.risk_manager.check_new_signal(
            instrument=inst,
            strategy=candidate.strategy,
            direction=candidate.direction,
            entry_price=raw_entry,
            stop_loss=candidate.stop_loss,
            account_state=acct,
            control=control,
            now_utc=now_utc,
        )
        if risk_res.risk_event:
            self.signal_store.append_risk_event(risk_res.risk_event)

        if not risk_res.approved:
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
        sub_eq = max(1e-9, float(sub_state["subaccount_equity_usd"]))
        risk_pct_pool = round(risk_res.r_plan_usd / sub_eq * 100.0, 2)

        signal = Signal(
            signal_id=sig_id,
            published_at_utc=now_utc,
            entered_at_utc=entry_ts,
            strategy=candidate.strategy,
            symbol=candidate.symbol,
            market=inst["market"],
            direction=candidate.direction,
            entry_price=round_price(raw_entry),
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            exit_rule=candidate.exit_rule,
            risk_pct_pool=risk_pct_pool,
            risk_usd=round(risk_res.r_plan_usd, 2),
            notional_usd=round(risk_res.notional_usd, 2),
            subaccount_capital_usd=round(sub_eq, 2),
            reasons=candidate.reasons,
            snapshot_id=snap.snapshot_id,
            verification_status=candidate.verification_status,
        )
        self.signal_store.publish_signal(signal, snap)

        qty = risk_res.qty
        entry_fee = (raw_entry * qty) * fee_rate
        side = "buy" if candidate.direction == "long" else "sell"

        tr = TradeRecord(
            signal_id=sig_id,
            strategy=candidate.strategy,
            symbol=candidate.symbol,
            market=inst["market"],
            correlation_group=inst.get("correlation_group", ""),
            direction=candidate.direction,
            state=LifecycleState.ACTIVE,
            entry_price=raw_entry,
            initial_stop_loss=candidate.stop_loss,
            current_stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            exit_rule=candidate.exit_rule,
            trailing_exit_price=candidate.trailing_exit_price,
            r_plan_usd=risk_res.r_plan_usd,
            planned_notional_usd=risk_res.notional_usd,
            subaccount_capital_usd=sub_eq,
            assumed_spread_bps=spread_bps,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            overnight_cost_daily_bps=float(inst.get("overnight_cost_daily_bps", 0.0)),
            avg_entry_price=raw_entry,
            remaining_qty=qty,
            initial_filled_qty=qty,
            realized_pnl_usd=-entry_fee,
            total_fees_usd=entry_fee,
            entered_at_utc=entry_ts,
            published_at_utc=now_utc,
        )
        self.ledger.trades[sig_id] = tr
        self.ledger.fills.append(
            FillRecord(
                fill_id=self._next_fill_id(sig_id),
                signal_id=sig_id,
                symbol=candidate.symbol,
                side=side,
                action="entry",
                price=raw_entry,
                qty=qty,
                notional_usd=risk_res.notional_usd,
                fee_usd=entry_fee,
                realized_pnl_usd=-entry_fee,
                ts_utc=entry_ts,
            )
        )
        events_out: list[SignalUpdateEvent] = []
        self._emit_event(
            events_out,
            sig_id,
            entry_ts,
            "entered",
            raw_entry,
            f"市價進場成交（名目 ${risk_res.notional_usd:,.2f}）",
        )
        return signal

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

        entry_notional = trade.avg_entry_price * qty
        exit_notional = exec_price * qty
        fee_usd = exit_notional * trade.fee_rate
        overnight_usd = 0.0
        if trade.entered_at_utc and trade.overnight_cost_daily_bps > 0:
            days = max(0.0, (_parse_utc(ts_utc) - _parse_utc(trade.entered_at_utc)).total_seconds() / 86400.0)
            overnight_usd = entry_notional * (trade.overnight_cost_daily_bps / 10000.0) * days

        net_exit_pnl = gross_pnl - fee_usd - overnight_usd
        trade.realized_pnl_usd += net_exit_pnl
        trade.total_fees_usd += fee_usd
        trade.total_overnight_usd += overnight_usd
        trade.remaining_qty = max(0.0, trade.remaining_qty - qty)

        # Match screener.py equity accounting: update strategy realized PnL once at close
        strat_eq = self.ledger.compute_strategy_realized_equity(trade.strategy)
        clamped_pnl = max(-(strat_eq - 0.01), trade.realized_pnl_usd)
        self.ledger.strategy_realized_pnl[trade.strategy] = (
            self.ledger.strategy_realized_pnl.get(trade.strategy, 0.0) + clamped_pnl
        )

        self.ledger.fills.append(
            FillRecord(
                fill_id=self._next_fill_id(trade.signal_id),
                signal_id=trade.signal_id,
                symbol=trade.symbol,
                side=side,
                action="exit",
                price=exec_price,
                qty=qty,
                notional_usd=exit_notional,
                fee_usd=fee_usd,
                realized_pnl_usd=net_exit_pnl,
                ts_utc=ts_utc,
            )
        )
        return net_exit_pnl

    def _finalize_close(
        self,
        trade: TradeRecord,
        exec_price: float,
        ts_utc: str,
        exit_reason: str,
        events_out: list[SignalUpdateEvent],
    ) -> None:
        eff_ts = trade.entered_at_utc if (trade.entered_at_utc and ts_utc < trade.entered_at_utc) else ts_utc
        self._close_quantity(trade, trade.remaining_qty, exec_price, eff_ts)
        trade.state = validate_transition(trade.state, LifecycleState.CLOSED, exit_reason=exit_reason)
        trade.closed_at_utc = eff_ts
        trade.exit_price = exec_price
        trade.exit_reason = exit_reason
        trade.r_multiple = trade.realized_pnl_usd / max(1e-9, trade.r_plan_usd)
        reason_zh = EXIT_REASON_LABELS.get(exit_reason, exit_reason)
        self._emit_event(
            events_out,
            trade.signal_id,
            eff_ts,
            "closed",
            exec_price,
            f"平倉結束（{reason_zh}），損益 ${trade.realized_pnl_usd:.2f} ({trade.r_multiple:.2f}R)",
            {"exit_reason": exit_reason, "r_multiple": round(trade.r_multiple, 4)},
        )

    def _process_trade_bar(
        self,
        trade: TradeRecord,
        bar: MarketBar,
        events_out: list[SignalUpdateEvent],
    ) -> None:
        if trade.state != LifecycleState.ACTIVE:
            return
        if trade.entered_at_utc and bar.close_ts_utc <= trade.entered_at_utc:
            return

        spread = trade.assumed_spread_bps
        slip = trade.slippage_bps / 10000.0
        sl_px = trade.current_stop_loss
        tp_px = trade.take_profit

        # Fixed SL and optional fixed TP matching backtest screener.py L418-L443
        if trade.direction == "long":
            open_bid = _bid_price(bar.open, spread)
            if open_bid <= sl_px:
                self._finalize_close(trade, open_bid * (1.0 - slip), bar.ts_utc, "stop_loss", events_out)
                return
            if _bid_price(bar.low, spread) <= sl_px:
                self._finalize_close(trade, sl_px * (1.0 - slip), bar.ts_utc, "stop_loss", events_out)
                return
            if tp_px is not None and _bid_price(bar.high, spread) >= tp_px:
                self._finalize_close(trade, tp_px * (1.0 - slip), bar.ts_utc, "take_profit", events_out)
                return
        else:
            open_ask = _ask_price(bar.open, spread)
            if open_ask >= sl_px:
                self._finalize_close(trade, open_ask * (1.0 + slip), bar.ts_utc, "stop_loss", events_out)
                return
            if _ask_price(bar.high, spread) >= sl_px:
                self._finalize_close(trade, sl_px * (1.0 + slip), bar.ts_utc, "stop_loss", events_out)
                return
            if tp_px is not None and _ask_price(bar.low, spread) <= tp_px:
                self._finalize_close(trade, tp_px * (1.0 + slip), bar.ts_utc, "take_profit", events_out)
                return

    def process_bar(
        self,
        symbol: str,
        bar: MarketBar,
    ) -> list[SignalUpdateEvent]:
        """Process a single closed bar across the ledger idempotently."""
        prev_ts = self.last_processed_ts.get(symbol)
        if prev_ts is not None and bar.close_ts_utc <= prev_ts:
            return []

        self.latest_marks[symbol] = bar.close
        events_out: list[SignalUpdateEvent] = []

        for trade in list(self.ledger.trades.values()):
            if trade.symbol != symbol or trade.state != LifecycleState.ACTIVE:
                continue
            self._process_trade_bar(trade=trade, bar=bar, events_out=events_out)

        self.last_processed_ts[symbol] = bar.close_ts_utc
        return events_out

    def replay_bars(
        self,
        symbol: str,
        bars: list[MarketBar],
    ) -> list[SignalUpdateEvent]:
        """Chronologically replay bars since last_processed_ts (catch-up replay)."""
        sorted_bars = sorted(bars, key=lambda b: b.close_ts_utc)
        all_events: list[SignalUpdateEvent] = []
        for bar in sorted_bars:
            all_events.extend(self.process_bar(symbol=symbol, bar=bar))
        return all_events

    def close_trade_by_strategy(
        self,
        strategy_id: str,
        symbol: str,
        close_price: float,
        ts_utc: str,
        exit_reason: str = "trailing_stop",
    ) -> list[SignalUpdateEvent]:
        """Close active trade on `(strategy_id, symbol)` using backtest open-exit pricing."""
        events_out: list[SignalUpdateEvent] = []
        slip_default = float(self.config.get("risk", {}).get("default_slippage_bps", 1.0)) / 10000.0
        for trade in list(self.ledger.trades.values()):
            if trade.strategy != strategy_id or trade.symbol != symbol or trade.state != LifecycleState.ACTIVE:
                continue
            spread = trade.assumed_spread_bps
            slip = trade.slippage_bps / 10000.0 if trade.slippage_bps else slip_default
            exec_px = (
                _bid_price(close_price, spread) * (1.0 - slip)
                if trade.direction == "long"
                else _ask_price(close_price, spread) * (1.0 + slip)
            )
            self._finalize_close(trade, exec_px, ts_utc, exit_reason, events_out)
        return events_out

    def close_disabled_instrument_trade(
        self,
        trade: TradeRecord,
        close_price: float,
        ts_utc: str,
    ) -> list[SignalUpdateEvent]:
        """Close active trade on a disabled instrument using market price."""
        if trade.state != LifecycleState.ACTIVE:
            return []
        events_out: list[SignalUpdateEvent] = []
        spread = trade.assumed_spread_bps
        slip_default = float(self.config.get("risk", {}).get("default_slippage_bps", 1.0)) / 10000.0
        slip = trade.slippage_bps / 10000.0 if trade.slippage_bps else slip_default
        exec_px = (
            _bid_price(close_price, spread) * (1.0 - slip)
            if trade.direction == "long"
            else _ask_price(close_price, spread) * (1.0 + slip)
        )
        self._finalize_close(trade, exec_px, ts_utc, "instrument_disabled", events_out)
        return events_out

    def save_state(self, snapshot_ts_utc: str | None = None) -> None:
        """Persist single ledger, fills, equity snapshots, and analytics to out_dir."""
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

        fills_dir = self.out_dir / "fills"
        fills_dir.mkdir(parents=True, exist_ok=True)
        fills_by_month: dict[str, list[FillRecord]] = {}
        fills_pnl_by_sig: dict[str, float] = {}
        for f in led.fills:
            fills_by_month.setdefault(_month_key(f.ts_utc), []).append(f)
            fills_pnl_by_sig[f.signal_id] = fills_pnl_by_sig.get(f.signal_id, 0.0) + f.realized_pnl_usd
        for month, flist in fills_by_month.items():
            (fills_dir / f"{month}.jsonl").write_text(
                "\n".join(json.dumps(f.to_dict(), ensure_ascii=False) for f in flist) + "\n",
                encoding="utf-8",
            )

        eq_dir = self.out_dir / "equity_snapshots"
        eq_dir.mkdir(parents=True, exist_ok=True)
        eq_snapshot = {
            "ts_utc": ts,
            "total_equity_usd": round(led.compute_equity(self.latest_marks), 2),
            "short_term_equity_usd": round(led.compute_pool_equity("short_term", self.latest_marks), 2),
            "long_term_equity_usd": round(led.compute_pool_equity("long_term", self.latest_marks), 2),
            "strategy_equities": {
                sid: round(led.compute_strategy_equity(sid, self.latest_marks), 2)
                for sid in Ledger.STRATEGY_IDS
            },
        }
        eq_path = eq_dir / f"{_month_key(ts)}.jsonl"
        with eq_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(eq_snapshot, ensure_ascii=False) + "\n")

        ver_labels: dict[str, str] = {}
        bt_path = self.out_dir / "backtest_results.json"
        if bt_path.exists():
            try:
                bt_doc = json.loads(bt_path.read_text(encoding="utf-8"))
                for row in (bt_doc.get("strategies") or bt_doc.get("results") or []):
                    if isinstance(row, dict) and row.get("strategy_id") and row.get("verification_label"):
                        ver_labels[str(row["strategy_id"])] = str(row["verification_label"])
            except Exception:
                pass

        signals_dir = self.out_dir / "signals"
        signals_index: list[dict[str, Any]] = []
        if signals_dir.exists():
            for p in sorted(signals_dir.glob("*.json"), reverse=True):
                try:
                    s_data = json.loads(p.read_text(encoding="utf-8"))
                    sid = s_data["signal_id"]
                    tr = led.trades.get(sid)
                    strat_key = tr.strategy if tr else str(s_data.get("strategy", ""))
                    if strat_key in ver_labels:
                        s_data["verification_status"] = ver_labels[strat_key]
                    s_data["lifecycle_state"] = tr.state.value if tr else s_data.get("lifecycle_state", "active")
                    s_data["avg_entry_price"] = round_price(tr.avg_entry_price) if tr else 0.0
                    s_data["entry_price"] = round_price(tr.avg_entry_price) if (tr and tr.avg_entry_price > 0) else float(s_data.get("entry_price") or s_data.get("entry_low") or 0.0)
                    s_data["remaining_qty"] = round(tr.remaining_qty, 8) if tr else 0.0
                    s_data["current_stop_loss"] = tr.current_stop_loss if tr else s_data["stop_loss"]
                    s_data["take_profit"] = tr.take_profit if tr else s_data.get("take_profit")
                    s_data["exit_rule"] = (tr.exit_rule if tr and tr.exit_rule else s_data.get("exit_rule", ""))
                    s_data["trailing_exit_price"] = tr.trailing_exit_price if tr else None
                    # Realized PnL from sum of fills when available
                    s_data["realized_pnl_usd"] = round(
                        fills_pnl_by_sig.get(sid, tr.realized_pnl_usd if tr else 0.0), 2
                    )
                    s_data["r_multiple"] = round(tr.r_multiple, 2) if tr else 0.0
                    s_data["r_plan_usd"] = tr.r_plan_usd if tr else s_data.get("risk_usd", 0.0)
                    s_data["exit_reason"] = tr.exit_reason if tr else None
                    s_data["exit_reason_label"] = EXIT_REASON_LABELS.get(tr.exit_reason, tr.exit_reason) if (tr and tr.exit_reason) else None
                    s_data["entered_at_utc"] = tr.entered_at_utc if tr else s_data.get("entered_at_utc")
                    s_data["closed_at_utc"] = tr.closed_at_utc if tr else None
                    s_data["exit_price"] = tr.exit_price if tr else None
                    if tr is not None:
                        sub_eq = led.compute_strategy_realized_equity(tr.strategy)
                        est = compute_trade_estimates(tr, sub_eq)
                        s_data["effective_notional_usd"] = est["effective_notional_usd"]
                        s_data["notional_usd"] = est["effective_notional_usd"]
                        s_data["notional_pct_subaccount"] = est["notional_pct_subaccount"]
                        s_data["subaccount_capital_usd"] = round(sub_eq, 2)
                        s_data["estimated_stop_loss_usd"] = est["estimated_stop_loss_usd"]
                        s_data["risk_usd"] = est["estimated_stop_loss_usd"]
                        s_data["estimated_stop_loss_pct_pool"] = est["estimated_stop_loss_pct_pool"]
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
                    "ledger": self.ledger.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        state_path = self.out_dir / "state.json"
        existing_state: dict[str, Any] = {}
        if state_path.exists():
            try:
                existing_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                existing_state = {}
        existing_state["last_processed_ts"] = self.last_processed_ts
        state_path.write_text(
            json.dumps(existing_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        (self.out_dir / "equity_curve.json").write_text(
            json.dumps(export_equity_curve(self.out_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.out_dir / "performance_stats.json").write_text(
            json.dumps(build_performance_stats(self, ts), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.out_dir / "risk_status.json").write_text(
            json.dumps(build_risk_status(self, ts), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def purge_invalid_signals(self) -> list[str]:
        """Remove known stale-bar corrupted signals and invalid closed_at < published_at records."""
        bad_ids: set[str] = set()
        for sid, tr in list(self.ledger.trades.items()):
            if tr.closed_at_utc and tr.published_at_utc and tr.closed_at_utc < tr.published_at_utc:
                bad_ids.add(sid)
            elif sid in {"SIG-20261116-0001", "SIG-20261116-0002", "SIG-20261116-0003"} and tr.state == LifecycleState.CANCELLED:
                bad_ids.add(sid)

        signals_dir = self.out_dir / "signals"
        if signals_dir.exists():
            for sig_path in signals_dir.glob("*.json"):
                sid = sig_path.stem
                if sid in {"SIG-20261116-0001", "SIG-20261116-0002", "SIG-20261116-0003"}:
                    snap_path = self.out_dir / "snapshots" / f"SNAP-{sid}.json"
                    if snap_path.exists():
                        try:
                            snap = json.loads(snap_path.read_text(encoding="utf-8"))
                            ts_bar = str(snap.get("kline_summary", {}).get("ts_utc", ""))
                            if ts_bar.startswith("2026-11-13"):
                                bad_ids.add(sid)
                        except Exception:
                            bad_ids.add(sid)

        if not bad_ids:
            return []

        for sid in bad_ids:
            self.ledger.trades.pop(sid, None)
        self.ledger.fills = [f for f in self.ledger.fills if f.signal_id not in bad_ids]

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

    def migrate_open_trades_to_backtest_rules(self) -> list[str]:
        """Requirement D: active open trades keep entry price & qty, reset SL to initial_stop_loss, drop TP1/TP2."""
        migrated: list[str] = []
        for sid, tr in self.ledger.trades.items():
            if tr.state != LifecycleState.ACTIVE:
                continue
            changed = False
            if tr.current_stop_loss != tr.initial_stop_loss:
                tr.current_stop_loss = tr.initial_stop_loss
                changed = True
            if tr.take_profit is not None:
                tr.take_profit = None
                changed = True
            if changed:
                migrated.append(sid)
        return migrated

    def load_state(self) -> None:
        """Restore persisted single ledger and replay cursors."""
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
            except Exception:
                pass

        self.purge_invalid_signals()
        self.migrate_open_trades_to_backtest_rules()

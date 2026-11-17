"""Immutable signal models, lifecycle transitions, and copy-text formatter."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

TAIPEI_TZ = timezone(timedelta(hours=8))


class SignalLockedError(ValueError):
    """Raised when attempting to overwrite or mutate a published signal."""


class InvalidStateTransitionError(ValueError):
    """Raised when a signal lifecycle state transition is invalid."""


class LifecycleState(str, Enum):
    """Lifecycle states aligned with market-open backtest execution."""

    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.ACTIVE: {LifecycleState.CLOSED},
    LifecycleState.CLOSED: set(),
    LifecycleState.CANCELLED: set(),
}

VALID_CLOSE_REASONS: set[str] = {
    "stop_loss",
    "trailing_stop",
    "time_stop",
    "session_stop",
    "rebalance_exit",
    "take_profit",
    "logic_invalidated",
    "risk_forced_close",
    "instrument_disabled",
    "trend_exit",
    "strategy_retired",
}

VALID_CANCEL_REASONS: set[str] = {
    "pre_entry_invalidated",
    "risk_cancelled",
}


def validate_transition(
    old_state: LifecycleState | str,
    new_state: LifecycleState | str,
    exit_reason: str | None = None,
) -> LifecycleState:
    """Validate lifecycle state transition and terminal exit reason."""
    old = LifecycleState(old_state)
    new = LifecycleState(new_state)
    if new not in VALID_TRANSITIONS[old]:
        raise InvalidStateTransitionError(f"不允許的狀態轉換: {old.value} -> {new.value}")
    if new == LifecycleState.CLOSED:
        if not exit_reason or exit_reason not in VALID_CLOSE_REASONS:
            raise InvalidStateTransitionError(f"結束狀態必須提供合法出場原因，收到: {exit_reason}")
    if new == LifecycleState.CANCELLED:
        if not exit_reason or exit_reason not in VALID_CANCEL_REASONS:
            raise InvalidStateTransitionError(f"取消狀態必須提供合法取消原因，收到: {exit_reason}")
    return new


@dataclass(frozen=True)
class Signal:
    """Immutable signal record locked upon publication (SPEC Section 3.1)."""

    signal_id: str
    published_at_utc: str
    entered_at_utc: str
    strategy: str  # "S2", "S3", "L1", "L2", "L3", or "L4" (plus historical "S1")
    symbol: str
    market: str
    direction: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit: float | None
    exit_rule: str
    risk_pct_pool: float
    risk_usd: float
    notional_usd: float
    subaccount_capital_usd: float
    reasons: tuple[str, ...]
    snapshot_id: str
    verification_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = list(self.reasons)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        if "entry_price" in data:
            entry_px = float(data["entry_price"])
        else:
            entry_px = (float(data.get("entry_low", 0.0)) + float(data.get("entry_high", 0.0))) / 2.0
        strat = str(data["strategy"])
        default_cap = 10000.0 if strat.startswith("S") else 20000.0
        reasons = tuple(data.get("reasons", []))
        return cls(
            signal_id=data["signal_id"],
            published_at_utc=data["published_at_utc"],
            entered_at_utc=str(data.get("entered_at_utc") or data["published_at_utc"]),
            strategy=strat,
            symbol=data["symbol"],
            market=data["market"],
            direction=data["direction"],
            entry_price=entry_px,
            stop_loss=float(data["stop_loss"]),
            take_profit=float(data["take_profit"]) if data.get("take_profit") is not None else None,
            exit_rule=str(data.get("exit_rule") or data.get("invalidation_cond") or ""),
            risk_pct_pool=float(data.get("risk_pct_pool", 1.0)),
            risk_usd=float(data.get("risk_usd", 0.0)),
            notional_usd=float(data.get("notional_usd", 0.0)),
            subaccount_capital_usd=float(data.get("subaccount_capital_usd", default_cap)),
            reasons=reasons,
            snapshot_id=data["snapshot_id"],
            verification_status=str(data.get("verification_status", "")),
        )


@dataclass(frozen=True)
class InputSnapshot:
    """Frozen input data snapshot captured at signal publication time (SPEC 1.7)."""

    snapshot_id: str
    signal_id: str
    captured_at_utc: str
    symbol: str
    kline_summary: dict[str, Any]
    indicators: dict[str, Any]
    filter_checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SignalUpdateEvent:
    """Append-only signal update event (SPEC Section 3.3)."""

    event_id: str
    signal_id: str
    ts_utc: str
    event_type: str  # entered, closed, cancelled
    price: float
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignalUpdateEvent:
        return cls(
            event_id=data["event_id"],
            signal_id=data["signal_id"],
            ts_utc=data["ts_utc"],
            event_type=data["event_type"],
            price=float(data["price"]),
            description=data["description"],
            metadata=dict(data.get("metadata", {})),
        )


def round_price(val: float, min_decimals: int = 6, min_sig: int = 6) -> float:
    """Round price keeping at least min_decimals and min_sig significant digits."""
    if val == 0 or not math.isfinite(val):
        return val
    abs_v = abs(val)
    if abs_v >= 1.0:
        return round(val, min_decimals)
    leading_zeros = max(0, -math.floor(math.log10(abs_v)) - 1)
    decimals = min(12, max(min_decimals, leading_zeros + min_sig))
    return round(val, decimals)


def _fmt_num(val: float | str) -> str:
    if isinstance(val, str):
        return val
    if not math.isfinite(val):
        return str(val)
    abs_v = abs(val)
    if abs_v == 0:
        return "0"
    if abs_v >= 1000:
        return f"{val:,.2f}"
    if abs_v >= 10:
        s = f"{val:,.3f}"
    elif abs_v >= 1:
        s = f"{val:,.4f}"
    else:
        leading_zeros = max(0, -math.floor(math.log10(abs_v)) - 1)
        decimals = min(12, max(4, leading_zeros + 4))
        s = f"{val:,.{decimals}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fmt_usd_compact(val: float) -> str:
    if abs(val - round(val)) < 1e-4:
        return f"{round(val):,.0f}"
    return f"{val:,.2f}"


def format_taipei_mmdd_hhmm(ts_utc: str) -> str:
    """Convert ISO UTC timestamp to Asia/Taipei MM/DD HH:mm."""
    if not ts_utc:
        return "--"
    try:
        raw = ts_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TAIPEI_TZ).strftime("%m/%d %H:%M")
    except Exception:
        return ts_utc


STRATEGY_LABELS: dict[str, str] = {
    "S1": "S1 開盤區間突破（已停用）",
    "S2": "S2 4 小時突破（短線）",
    "S3": "S3 日線 RSI 回檔（短線）",
    "L1": "L1 波動度調整趨勢跟隨（長線）",
    "L2": "L2 精簡標的趨勢跟隨（長線）",
    "L3": "L3 雙動能（長線）",
    "L4": "L4 主流週線趨勢（長線）",
}

EXIT_REASON_LABELS: dict[str, str] = {
    "stop_loss": "止損",
    "trailing_stop": "移動停利",
    "time_stop": "時間出場",
    "session_stop": "強制平倉",
    "rebalance_exit": "調出",
    "take_profit": "止盈",
    "logic_invalidated": "移動停利",
    "risk_forced_close": "強制平倉",
    "instrument_disabled": "標的停用平倉",
    "trend_exit": "跌破均線出場",
    "strategy_retired": "策略停用平倉",
}


def format_signal_text(signal: Signal, exit_rule_override: str | None = None) -> str:
    """Format signal into plain text matching unified rules & Taipei timestamps."""
    dir_zh = "做多" if signal.direction == "long" else "做空"
    strat_zh = STRATEGY_LABELS.get(signal.strategy, f"策略 {signal.strategy}")
    sub_cap = signal.subaccount_capital_usd or (10000.0 if signal.strategy.startswith("S") else 20000.0)
    notional_pct = (signal.notional_usd / sub_cap * 100.0) if sub_cap > 0 else 0.0
    sl_pct = (signal.risk_usd / sub_cap * 100.0) if sub_cap > 0 else signal.risk_pct_pool
    entered_tp = format_taipei_mmdd_hhmm(signal.entered_at_utc or signal.published_at_utc)
    rule_txt = exit_rule_override or signal.exit_rule or "依策略規則出場"

    lines = [
        f"【Signal Desk 報單】{signal.signal_id}（{strat_zh}）",
        f"標的：{signal.symbol}｜方向：{dir_zh}",
        f"進場：市價 {_fmt_num(signal.entry_price)}（台北時間 {entered_tp} 成交）",
        f"止損：{_fmt_num(signal.stop_loss)}（打到預估虧損 -${_fmt_usd_compact(signal.risk_usd)}，佔 {signal.strategy} 策略資金 {sl_pct:.1f}%）",
        f"出場規則：{rule_txt}",
        f"倉位名目：${_fmt_usd_compact(signal.notional_usd)}（佔 {signal.strategy} 策略資金 {notional_pct:.1f}%）",
        "模擬驗證中，非投資建議",
    ]
    return "\n".join(lines)

"""Immutable signal models, lifecycle transitions, and copy-text formatter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SignalLockedError(ValueError):
    """Raised when attempting to overwrite or mutate a published signal."""


class InvalidStateTransitionError(ValueError):
    """Raised when a signal lifecycle state transition is invalid."""


class LifecycleState(str, Enum):
    """Lifecycle states defined in SPEC Section 3.2."""

    WAITING_ENTRY = "waiting_entry"
    ACTIVE = "active"
    TP1_HIT = "tp1_hit"
    CLOSED = "closed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.WAITING_ENTRY: {LifecycleState.ACTIVE, LifecycleState.CANCELLED},
    LifecycleState.ACTIVE: {LifecycleState.TP1_HIT, LifecycleState.CLOSED},
    LifecycleState.TP1_HIT: {LifecycleState.CLOSED},
    LifecycleState.CLOSED: set(),
    LifecycleState.CANCELLED: set(),
}

VALID_CLOSE_REASONS: set[str] = {
    "tp1",
    "tp2",
    "stop_loss",
    "trailing_stop",
    "time_stop",
    "session_stop",
    "breakout_protection",
    "logic_invalidated",
    "risk_forced_close",
}

VALID_CANCEL_REASONS: set[str] = {
    "expired",
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
class AddLevel:
    """Planned add-on entry level (stage 2 or 3)."""

    stage: int
    add_type: str  # "mean_add" or "trend_add"
    price: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    """Immutable signal record locked upon publication (SPEC Section 3.1)."""

    signal_id: str
    published_at_utc: str
    strategy: str  # "S1", "S2", "S3", "L1", "L2", or "L3"
    symbol: str
    market: str
    direction: str  # "long" or "short"
    entry_low: float
    entry_high: float
    add_levels: tuple[AddLevel, ...]
    stop_loss: float
    tp1: float
    tp2: float | str
    rr_ratio: float
    risk_pct_pool: float
    risk_usd: float
    notional_usd: float
    leverage: float
    liquidation_price: float
    valid_until_utc: str
    invalidation_cond: str
    time_stop_bars: int
    reasons: tuple[str, ...]
    snapshot_id: str
    verification_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["add_levels"] = [lvl.to_dict() for lvl in self.add_levels]
        data["reasons"] = list(self.reasons)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        add_levels = tuple(AddLevel(**lvl) for lvl in data.get("add_levels", []))
        reasons = tuple(data.get("reasons", []))
        return cls(
            signal_id=data["signal_id"],
            published_at_utc=data["published_at_utc"],
            strategy=data["strategy"],
            symbol=data["symbol"],
            market=data["market"],
            direction=data["direction"],
            entry_low=float(data["entry_low"]),
            entry_high=float(data["entry_high"]),
            add_levels=add_levels,
            stop_loss=float(data["stop_loss"]),
            tp1=float(data["tp1"]),
            tp2=data["tp2"],
            rr_ratio=float(data["rr_ratio"]),
            risk_pct_pool=float(data["risk_pct_pool"]),
            risk_usd=float(data["risk_usd"]),
            notional_usd=float(data["notional_usd"]),
            leverage=float(data["leverage"]),
            liquidation_price=float(data["liquidation_price"]),
            valid_until_utc=data["valid_until_utc"],
            invalidation_cond=data["invalidation_cond"],
            time_stop_bars=int(data["time_stop_bars"]),
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
    event_type: str  # entered, add_filled, tp1_hit, stop_moved, trailing_updated, closed, cancelled
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


def _fmt_num(val: float | str) -> str:
    if isinstance(val, str):
        return val
    return f"{val:g}"


STRATEGY_LABELS: dict[str, str] = {
    "S1": "S1 開盤區間突破（短線）",
    "S2": "S2 4 小時突破（短線）",
    "S3": "S3 日線 RSI 回檔（短線）",
    "L1": "L1 波動度調整趨勢跟隨（長線）",
    "L2": "L2 精簡標的趨勢跟隨（長線）",
    "L3": "L3 雙動能（長線）",
}


def format_signal_text(signal: Signal) -> str:
    """Format signal into copyable plain text matching SPEC Section 3.4."""
    dir_zh = "做多" if signal.direction == "long" else "做空"
    strat_zh = STRATEGY_LABELS.get(signal.strategy, f"策略 {signal.strategy}")
    if signal.add_levels:
        add_parts = []
        for lvl in signal.add_levels:
            kind_zh = "逆勢補倉" if lvl.add_type == "mean_add" else "順勢加倉"
            add_parts.append(f"階段{lvl.stage} {kind_zh} {_fmt_num(lvl.price)}")
        add_line = "、".join(add_parts)
    else:
        add_line = "無"

    lines = [
        f"【Signal Desk 報單】{signal.signal_id}（{strat_zh}）",
    ]
    if signal.verification_status:
        lines.append(f"策略狀態：{signal.verification_status}")
    lines.extend(
        [
            f"標的：{signal.symbol}｜方向：{dir_zh}",
            f"入場區間：{_fmt_num(signal.entry_low)} ~ {_fmt_num(signal.entry_high)}",
            f"補倉價位：{add_line}",
            f"止損價：{_fmt_num(signal.stop_loss)}",
            f"止盈 TP1：{_fmt_num(signal.tp1)}｜TP2：{_fmt_num(signal.tp2)}",
            f"建議風險比例：佔資金池 {signal.risk_pct_pool:g}%（預估風險 ${signal.risk_usd:.2f}）",
            f"掛單有效期限：{signal.valid_until_utc}",
            f"失效條件：{signal.invalidation_cond}",
            "模擬驗證中，非投資建議",
        ]
    )
    return "\n".join(lines)

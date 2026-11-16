"""Single-ledger analytics: grouped performance stats, strategy sub-accounts, and equity curves."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.risk.manager import is_short_term_strategy
from src.signals.models import LifecycleState


def _parse_dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def compute_series_metrics(
    trades: list[Any],
    initial_capital: float,
    min_verified_trades: int,
    min_cagr_days: int = 30,
    risk_free_annual: float = 0.0,
    used_margin_usd: float = 0.0,
    current_equity_usd: float | None = None,
) -> dict[str, Any]:
    """Compute portfolio & trade-level statistics with strict minimum sample gating."""
    closed = [t for t in trades if t.state == LifecycleState.CLOSED]
    open_list = [t for t in trades if t.state == LifecycleState.ACTIVE]
    cancelled_count = sum(1 for t in trades if t.state == LifecycleState.CANCELLED)

    closed_sorted = sorted(closed, key=lambda t: t.closed_at_utc or t.entered_at_utc or "")
    n_closed = len(closed_sorted)
    verified = n_closed >= min_verified_trades
    verification_label = "已驗證" if verified else "尚未驗證"

    out: dict[str, Any] = {
        "total_signals": len(trades),
        "closed_trades": n_closed,
        "open_trades": len(open_list),
        "cancelled_orders": cancelled_count,
        "min_trades_required": min_verified_trades,
        "verified": verified,
        "verification_label": verification_label,
        "cagr_note": None,
        "win_rate_pct": None,
        "avg_r": None,
        "expectancy_r": None,
        "profit_factor": None,
        "max_drawdown_pct": None,
        "sharpe": None,
        "recovery_factor": None,
        "max_consecutive_losses": None,
        "capital_efficiency_pct": None,
        "scaled_risk_adjusted_return": None,
        "total_return_pct": None,
        "cagr_pct": None,
        "total_pnl_usd": None,
    }

    if not verified:
        return out

    wins = [t for t in closed_sorted if t.realized_pnl_usd > 0]
    losses = [t for t in closed_sorted if t.realized_pnl_usd <= 0]
    gross_win = sum(t.realized_pnl_usd for t in wins)
    gross_loss = abs(sum(t.realized_pnl_usd for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (99.0 if gross_win > 0 else 0.0)

    equity = initial_capital
    peak = initial_capital
    max_dd_pct = 0.0
    max_dd_usd = 0.0
    returns: list[float] = []
    consec_losses = 0
    max_consec_losses = 0

    for t in closed_sorted:
        prev_eq = max(1e-9, equity)
        returns.append(t.realized_pnl_usd / prev_eq)
        equity += t.realized_pnl_usd
        if equity > peak:
            peak = equity
        dd_usd = peak - equity
        dd_pct = dd_usd / max(1e-9, peak) * 100.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd

        if t.realized_pnl_usd <= 0:
            consec_losses += 1
            if consec_losses > max_consec_losses:
                max_consec_losses = consec_losses
        else:
            consec_losses = 0

    total_pnl = sum(t.realized_pnl_usd for t in closed_sorted)
    total_ret_pct = (total_pnl / max(1e-9, initial_capital)) * 100.0
    avg_r = sum(t.r_multiple for t in closed_sorted) / n_closed

    first_ts = closed_sorted[0].entered_at_utc or closed_sorted[0].closed_at_utc
    last_ts = closed_sorted[-1].closed_at_utc or closed_sorted[-1].entered_at_utc
    cagr_pct: float | None = None
    cagr_note: str | None = None
    span_days = 0.0
    if first_ts and last_ts:
        try:
            span_days = max(0.0, (_parse_dt(last_ts) - _parse_dt(first_ts)).total_seconds() / 86400.0)
        except Exception:
            span_days = 0.0

    if span_days < min_cagr_days:
        cagr_note = "僅供參考"
    else:
        years = span_days / 365.25
        end_ratio = max(1e-6, (initial_capital + total_pnl) / max(1e-9, initial_capital))
        cagr_pct = round((end_ratio ** (1.0 / years) - 1.0) * 100.0, 2)

    sharpe = 0.0
    if len(returns) >= 2:
        mean_ret = sum(returns) / len(returns)
        var = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(var)
        trades_per_year = max(1.0, (n_closed / max(1.0, span_days)) * 365.25) if span_days > 0 else float(n_closed)
        rf_per_trade = risk_free_annual / trades_per_year
        if std_ret > 1e-12:
            sharpe = round(((mean_ret - rf_per_trade) / std_ret) * math.sqrt(trades_per_year), 3)

    recovery_factor = round(total_pnl / max_dd_usd, 3) if max_dd_usd > 1e-9 else (round(total_pnl / 1.0, 3) if total_pnl > 0 else 0.0)

    avg_notional = sum(
        getattr(t, "planned_notional_usd", 0.0) or (t.avg_entry_price * t.initial_filled_qty)
        for t in closed_sorted
    ) / n_closed
    cap_eff_pct = round((avg_notional / max(1e-9, initial_capital)) * 100.0, 2)
    eq_ref = current_equity_usd if current_equity_usd is not None else equity
    used_m_pct = (used_margin_usd / max(1e-9, eq_ref)) * 100.0
    scaled_risk_adj = round(total_ret_pct / max(1.0, max_dd_pct), 3)

    out.update(
        {
            "cagr_note": cagr_note or f"{span_days:.0f} 天統計",
            "win_rate_pct": round(len(wins) / n_closed * 100.0, 2),
            "avg_r": round(avg_r, 4),
            "expectancy_r": round(avg_r, 4),
            "profit_factor": round(pf, 3),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe": sharpe,
            "recovery_factor": recovery_factor,
            "max_consecutive_losses": max_consec_losses,
            "capital_efficiency_pct": cap_eff_pct if cap_eff_pct > 0 else round(used_m_pct, 2),
            "scaled_risk_adjusted_return": scaled_risk_adj,
            "total_return_pct": round(total_ret_pct, 2),
            "cagr_pct": cagr_pct,
            "total_pnl_usd": round(total_pnl, 2),
        }
    )
    return out


def compute_trade_estimates(
    tr: Any,
    subaccount_equity_usd: float,
) -> dict[str, Any]:
    """Compute notional, sub-account share %, and estimated SL loss (no TP1/TP2 or leverage)."""
    eff_entry = tr.avg_entry_price if tr.avg_entry_price > 0 else float(getattr(tr, "entry_price", 0.0) or 0.0)
    eff_notional = tr.current_notional_usd() if tr.remaining_qty > 0 else float(getattr(tr, "planned_notional_usd", 0.0) or 0.0)
    eff_qty = tr.remaining_qty if tr.remaining_qty > 0 else (eff_notional / eff_entry if eff_entry > 0 else 0.0)
    sub_eq = max(1e-9, float(subaccount_equity_usd))

    one_side_cost_pct = max(0.0, tr.fee_rate) + max(0.0, tr.slippage_bps) / 10000.0
    if tr.state == LifecycleState.ACTIVE and tr.remaining_qty > 0 and eff_entry > 0:
        sl_dist = (
            max(0.0, eff_entry - tr.current_stop_loss)
            if tr.direction == "long"
            else max(0.0, tr.current_stop_loss - eff_entry)
        )
        sl_loss_usd = tr.remaining_qty * sl_dist + eff_notional * (2.0 * one_side_cost_pct)
    else:
        sl_dist = abs(eff_entry - tr.current_stop_loss)
        sl_loss_usd = eff_qty * sl_dist + eff_notional * (2.0 * one_side_cost_pct)

    return {
        "effective_notional_usd": round(eff_notional, 2),
        "notional_pct_subaccount": round((eff_notional / sub_eq) * 100.0, 2),
        "estimated_stop_loss_usd": round(sl_loss_usd, 2),
        "estimated_stop_loss_pct_pool": round((sl_loss_usd / sub_eq) * 100.0, 2),
    }


def build_performance_stats(
    engine: Any,
    now_utc: str,
) -> dict[str, Any]:
    """Build grouped performance stats for the single simulation ledger."""
    cfg = engine.config
    analytics_cfg = cfg.get("analytics", {})
    min_verified = int(analytics_cfg.get("min_verified_trades_stats", 50))
    min_cagr_days = int(analytics_cfg.get("min_cagr_days", 30))
    rf_annual = float(analytics_cfg.get("risk_free_rate_annual", 0.0))
    init_cap = float(cfg.get("account", {}).get("initial_capital_usd", 100000.0))

    ledger = engine.ledger
    trades = list(ledger.trades.values())
    acct = ledger.build_account_state(now_utc, engine.latest_marks)

    def _calc_group(sub_trades: list[Any]) -> dict[str, Any]:
        return compute_series_metrics(
            trades=sub_trades,
            initial_capital=init_cap,
            min_verified_trades=min_verified,
            min_cagr_days=min_cagr_days,
            risk_free_annual=rf_annual,
            current_equity_usd=float(acct["equity_usd"]),
        )

    by_strategy = {
        s: _calc_group([t for t in trades if t.strategy == s])
        for s in ("S1", "S2", "S3", "L1", "L2", "L3")
    }
    by_market = {
        m: _calc_group([t for t in trades if t.market == m])
        for m in ("crypto", "forex", "commodity", "index")
    }
    by_direction = {
        d: _calc_group([t for t in trades if t.direction == d])
        for d in ("long", "short")
    }

    return {
        "updated_at_utc": now_utc,
        "min_verified_trades": min_verified,
        "overall": _calc_group(trades),
        "by_strategy": by_strategy,
        "by_market": by_market,
        "by_direction": by_direction,
    }


def build_risk_status(
    engine: Any,
    now_utc: str,
) -> dict[str, Any]:
    """Build per-strategy sub-account Risk Center & open positions payload."""
    cfg = engine.config
    bt_cfg = cfg.get("backtest", {})
    strats_cfg = cfg.get("strategies", {})
    marks = engine.latest_marks
    ledger = engine.ledger

    acct = ledger.build_account_state(now_utc, marks)
    eq = float(acct["equity_usd"])
    peak = float(acct["peak_equity_usd"])
    dd_pct = max(0.0, (peak - eq) / max(1e-9, peak) * 100.0)

    short_eq = float(ledger.compute_pool_equity("short_term", marks))
    long_eq = float(ledger.compute_pool_equity("long_term", marks))

    max_conc = int(bt_cfg.get("max_concurrent_positions", 10))
    max_risk_pct = float(bt_cfg.get("max_total_open_risk_pct", 10.0))
    max_notional_mult = float(bt_cfg.get("max_total_notional_multiple", 5.0))
    risk_per_trade_pct = float(bt_cfg.get("risk_per_trade_pct", 1.0))

    subaccounts_out: list[dict[str, Any]] = []
    strategy_equities: dict[str, float] = {}
    total_open_risk = 0.0
    total_open_notional = 0.0

    for sid in ("S1", "S2", "S3", "L1", "L2", "L3"):
        scfg = strats_cfg.get(sid, {})
        init_cap = float(ledger.strategy_capitals.get(sid, scfg.get("capital_usd", 10000.0)))
        realized_eq = float(ledger.compute_strategy_realized_equity(sid))
        mtm_eq = float(ledger.compute_strategy_equity(sid, marks))
        sub_state = ledger.build_subaccount_state(sid)
        open_cnt = int(sub_state["subaccount_open_positions"])
        open_risk = float(sub_state["subaccount_open_risk_usd"])
        open_notional = float(sub_state["subaccount_open_notional_usd"])
        max_risk_usd = realized_eq * (max_risk_pct / 100.0)
        max_notional_usd = realized_eq * max_notional_mult
        realized_pnl = float(ledger.strategy_realized_pnl.get(sid, 0.0))
        unrealized_pnl = mtm_eq - (init_cap + realized_pnl)

        strategy_equities[sid] = round(mtm_eq, 2)
        total_open_risk += open_risk
        total_open_notional += open_notional

        subaccounts_out.append(
            {
                "strategy_id": sid,
                "name": scfg.get("name", sid),
                "pool": "short_term" if is_short_term_strategy(sid) else "long_term",
                "interval": scfg.get("interval", "1d"),
                "initial_capital_usd": round(init_cap, 2),
                "realized_equity_usd": round(realized_eq, 2),
                "equity_usd": round(mtm_eq, 2),
                "realized_pnl_usd": round(realized_pnl, 2),
                "unrealized_pnl_usd": round(unrealized_pnl, 2),
                "risk_per_trade_pct": risk_per_trade_pct,
                "open_positions_count": open_cnt,
                "max_concurrent_positions": max_conc,
                "open_risk_usd": round(open_risk, 2),
                "max_open_risk_usd": round(max_risk_usd, 2),
                "open_risk_pct": round(open_risk / max(1e-9, realized_eq) * 100.0, 2),
                "max_total_open_risk_pct": max_risk_pct,
                "open_notional_usd": round(open_notional, 2),
                "max_total_notional_usd": round(max_notional_usd, 2),
                "open_notional_pct": round(open_notional / max(1e-9, realized_eq) * 100.0, 2),
                "max_total_notional_multiple": max_notional_mult,
            }
        )

    positions_out: list[dict[str, Any]] = []
    for tr in ledger.trades.values():
        if tr.state != LifecycleState.ACTIVE:
            continue
        sub_eq = float(ledger.compute_strategy_realized_equity(tr.strategy))
        mark_px = float(marks.get(tr.symbol, tr.avg_entry_price or tr.entry_price))
        est = compute_trade_estimates(tr, sub_eq)
        positions_out.append(
            {
                "signal_id": tr.signal_id,
                "symbol": tr.symbol,
                "market": tr.market,
                "strategy": tr.strategy,
                "direction": tr.direction,
                "state": tr.state.value,
                "entered_at_utc": tr.entered_at_utc or tr.published_at_utc,
                "avg_entry_price": round(tr.avg_entry_price, 6),
                "total_qty": round(tr.remaining_qty, 8),
                "notional_usd": est["effective_notional_usd"],
                "notional_pct_subaccount": est["notional_pct_subaccount"],
                "subaccount_capital_usd": round(sub_eq, 2),
                "mark_price": round(mark_px, 6),
                "unrealized_pnl_usd": round(tr.unrealized_pnl_usd(mark_px), 2) if tr.remaining_qty > 0 else 0.0,
                "current_stop_loss": tr.current_stop_loss,
                "take_profit": tr.take_profit,
                "exit_rule": tr.exit_rule,
                "trailing_exit_price": tr.trailing_exit_price,
                "estimated_max_loss_usd": est["estimated_stop_loss_usd"],
                "estimated_max_loss_pct_pool": est["estimated_stop_loss_pct_pool"],
            }
        )

    events_path = engine.out_dir / "risk_events.jsonl"
    recent_events: list[dict[str, Any]] = []
    if events_path.exists():
        lines = events_path.read_text(encoding="utf-8").splitlines()
        for raw in reversed(lines[-50:]):
            raw = raw.strip()
            if not raw:
                continue
            try:
                recent_events.append(json.loads(raw))
            except Exception:
                continue

    return {
        "updated_at_utc": now_utc,
        "account": {
            "equity_usd": round(eq, 2),
            "peak_equity_usd": round(peak, 2),
            "short_term_equity_usd": round(short_eq, 2),
            "long_term_equity_usd": round(long_eq, 2),
            "strategy_equities": strategy_equities,
            "drawdown_pct": round(dd_pct, 2),
            "risk_status": "normal",
            "total_open_risk_usd": round(total_open_risk, 2),
            "total_open_notional_usd": round(total_open_notional, 2),
        },
        "subaccounts": subaccounts_out,
        "open_positions": positions_out,
        "recent_risk_events": recent_events,
    }


def export_equity_curve(out_dir: Path, max_points: int = 2000) -> dict[str, Any]:
    """Aggregate monthly equity_snapshots/*.jsonl into equity_curve.json for web UI."""
    eq_dir = out_dir / "equity_snapshots"
    points: list[dict[str, Any]] = []
    if eq_dir.exists():
        for fp in sorted(eq_dir.glob("*.jsonl")):
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if "total_equity_usd" in row:
                        points.append(
                            {
                                "ts_utc": row.get("ts_utc"),
                                "total_equity_usd": row.get("total_equity_usd"),
                                "short_term_equity_usd": row.get("short_term_equity_usd"),
                                "long_term_equity_usd": row.get("long_term_equity_usd"),
                            }
                        )
                except Exception:
                    continue

    deduped: list[dict[str, Any]] = []
    seen_ts: dict[str, int] = {}
    for pt in points:
        ts = str(pt.get("ts_utc") or "")
        if ts in seen_ts:
            deduped[seen_ts[ts]] = pt
        else:
            seen_ts[ts] = len(deduped)
            deduped.append(pt)

    deduped.sort(key=lambda x: str(x.get("ts_utc") or ""))
    return {
        "points": deduped[-max_points:],
    }

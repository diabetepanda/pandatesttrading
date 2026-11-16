"""Stage 6 analytics: 5 shadow ledger comparison, performance stats, risk center, and equity curves."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.signals.models import LifecycleState

POLICY_LABELS: dict[str, str] = {
    "A": "A: 固定比例 50/30/20",
    "B": "B: 均等比例 33/33/34",
    "C": "C: 金字塔 30/30/40",
    "D": "D: 倒金字塔 20/30/50",
    "E": "E: ATR 動態波動＋風險預算",
}


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
    open_list = [
        t for t in trades if t.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    ]
    cancelled_count = sum(1 for t in trades if t.state == LifecycleState.CANCELLED)

    # Order chronologically by close timestamp
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
        # Performance fields suppressed (None) until min_verified_trades is reached
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

    # Span for CAGR calculation
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

    # Trade-level Sharpe annualized by observed trade frequency (capped reasonably)
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

    # Capital efficiency: return relative to average deployed notional / margin + scaled risk-adjusted return
    avg_notional = sum(sum(s.notional_usd for s in t.stages) for t in closed_sorted) / n_closed
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


def build_shadow_comparison(
    engine: Any,
    now_utc: str,
) -> dict[str, Any]:
    """Build 5 shadow ledger parallel comparison payload (SPEC Section 10)."""
    cfg = engine.config
    analytics_cfg = cfg.get("analytics", {})
    risk_cfg = cfg.get("risk", {})
    min_verified = int(analytics_cfg.get("min_verified_trades_shadow", 30))
    min_cagr_days = int(analytics_cfg.get("min_cagr_days", 30))
    rf_annual = float(analytics_cfg.get("risk_free_rate_annual", 0.0))
    init_cap = float(cfg.get("account", {}).get("initial_capital_usd", 100000.0))
    warn_pct = float(risk_cfg.get("drawdown_warning_pct", 8.0))
    pause_pct = float(risk_cfg.get("drawdown_pause_pct", 12.0))

    policies_out: list[dict[str, Any]] = []
    for pol_name, ledger in engine.ledgers.items():
        acct = ledger.build_account_state(now_utc, engine.latest_marks)
        eq = float(acct["equity_usd"])
        peak = float(acct["peak_equity_usd"])
        dd_now = max(0.0, (peak - eq) / max(1e-9, peak) * 100.0)
        margin_pct = float(acct["used_margin_usd"]) / max(1e-9, eq) * 100.0

        if ledger.drawdown_paused or dd_now >= pause_pct:
            r_status = "暫停 (12%)"
        elif dd_now >= warn_pct:
            r_status = "警戒 (8%)"
        else:
            r_status = "正常"

        metrics = compute_series_metrics(
            trades=list(ledger.trades.values()),
            initial_capital=init_cap,
            min_verified_trades=min_verified,
            min_cagr_days=min_cagr_days,
            risk_free_annual=rf_annual,
            used_margin_usd=float(acct["used_margin_usd"]),
            current_equity_usd=eq,
        )
        metrics.update(
            {
                "policy": pol_name,
                "label": POLICY_LABELS.get(pol_name, f"Policy {pol_name}"),
                "is_primary": pol_name == engine.primary_policy_name,
                "weights": list(cfg.get("scaling", {}).get("weights", {}).get(pol_name, [])),
                "equity_usd": round(eq, 2),
                "short_term_equity_usd": round(ledger.compute_pool_equity("short_term", engine.latest_marks), 2),
                "long_term_equity_usd": round(ledger.compute_pool_equity("long_term", engine.latest_marks), 2),
                "current_drawdown_pct": round(dd_now, 2),
                "used_margin_pct": round(margin_pct, 2),
                "total_stop_risk_usd": round(float(acct["total_stop_risk_usd"]), 2),
                "risk_status": r_status,
            }
        )
        policies_out.append(metrics)

    verified_policies = [p for p in policies_out if p["verified"]]
    best_policy: str | None = None
    if len(verified_policies) == len(policies_out) and verified_policies:
        best_item = max(
            verified_policies,
            key=lambda p: (
                float(p["scaled_risk_adjusted_return"] or -1e9),
                float(p["expectancy_r"] or -1e9),
            ),
        )
        best_policy = best_item["policy"]

    return {
        "updated_at_utc": now_utc,
        "min_verified_trades": min_verified,
        "primary_policy": engine.primary_policy_name,
        "any_verified": len(verified_policies) > 0,
        "all_verified": len(verified_policies) == len(policies_out) and len(policies_out) > 0,
        "best_policy": best_policy,
        "policies": policies_out,
    }


def _classify_add_type(trade: Any) -> str:
    if len(trade.stages) <= 1:
        return "initial_only"
    for st in trade.stages[1:]:
        if st.add_type in ("mean_add", "trend_add"):
            return st.add_type
    return "mean_add"


def build_performance_stats(
    engine: Any,
    now_utc: str,
) -> dict[str, Any]:
    """Build SPEC Section 2.4 grouped performance stats for primary ledger."""
    cfg = engine.config
    analytics_cfg = cfg.get("analytics", {})
    min_verified = int(analytics_cfg.get("min_verified_trades_stats", 50))
    min_cagr_days = int(analytics_cfg.get("min_cagr_days", 30))
    rf_annual = float(analytics_cfg.get("risk_free_rate_annual", 0.0))
    init_cap = float(cfg.get("account", {}).get("initial_capital_usd", 100000.0))

    prim = engine.primary_ledger
    trades = list(prim.trades.values())
    acct = prim.build_account_state(now_utc, engine.latest_marks)

    def _calc_group(sub_trades: list[Any]) -> dict[str, Any]:
        return compute_series_metrics(
            trades=sub_trades,
            initial_capital=init_cap,
            min_verified_trades=min_verified,
            min_cagr_days=min_cagr_days,
            risk_free_annual=rf_annual,
            used_margin_usd=float(acct["used_margin_usd"]),
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
    by_add_type = {
        a: _calc_group([t for t in trades if _classify_add_type(t) == a])
        for a in ("initial_only", "mean_add", "trend_add")
    }

    return {
        "updated_at_utc": now_utc,
        "primary_policy": engine.primary_policy_name,
        "min_verified_trades": min_verified,
        "overall": _calc_group(trades),
        "by_strategy": by_strategy,
        "by_market": by_market,
        "by_direction": by_direction,
        "by_add_type": by_add_type,
    }


def build_risk_status(
    engine: Any,
    now_utc: str,
) -> dict[str, Any]:
    """Build SPEC Section 2.5 & 2.7 Risk Center & Simulated Account positions payload."""
    cfg = engine.config
    risk_cfg = cfg.get("risk", {})
    groups_cfg = cfg.get("correlation_groups", {})
    max_stages = int(cfg.get("scaling", {}).get("max_stages", 3))
    marks = engine.latest_marks

    prim = engine.primary_ledger
    acct = prim.build_account_state(now_utc, marks)
    eq = float(acct["equity_usd"])
    peak = float(acct["peak_equity_usd"])
    dd_pct = max(0.0, (peak - eq) / max(1e-9, peak) * 100.0)
    warn_pct = float(risk_cfg.get("drawdown_warning_pct", 8.0))
    pause_pct = float(risk_cfg.get("drawdown_pause_pct", 12.0))
    max_margin_pct = float(risk_cfg.get("max_margin_usage_pct", 30.0))
    used_margin_pct = float(acct["used_margin_usd"]) / max(1e-9, eq) * 100.0
    max_daily_loss = float(risk_cfg.get("max_daily_loss_usd", 1500.0))
    max_weekly_loss = float(risk_cfg.get("max_weekly_loss_usd", 4000.0))
    max_total_stop = float(risk_cfg.get("max_total_stop_risk_usd", 3000.0))

    if prim.drawdown_paused or dd_pct >= pause_pct:
        risk_state_label = "paused_drawdown"
    elif dd_pct >= warn_pct:
        risk_state_label = "warning"
    else:
        risk_state_label = "normal"

    # Correlation groups utilization
    group_map = acct.get("group_stop_risk_usd", {})
    groups_out: list[dict[str, Any]] = []
    for gid, gcfg in groups_cfg.items():
        lim = float(gcfg.get("max_stop_risk_usd", risk_cfg.get("max_group_stop_risk_usd", 1000.0)))
        used = float(group_map.get(gid, 0.0))
        groups_out.append(
            {
                "group_id": gid,
                "name": gcfg.get("name", gid),
                "used_risk_usd": round(used, 2),
                "limit_usd": lim,
                "usage_pct": round(used / max(1e-9, lim) * 100.0, 2),
            }
        )

    # Open positions (ACTIVE & TP1_HIT & WAITING_ENTRY) for Simulated Account & Risk Center
    positions_out: list[dict[str, Any]] = []
    for tr in prim.trades.values():
        if tr.state not in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT):
            continue
        mark_px = float(marks.get(tr.symbol, tr.avg_entry_price or (tr.entry_low + tr.entry_high) / 2.0))
        liq_metrics = engine.risk_manager.compute_liquidation_metrics(
            entry_price=tr.avg_entry_price if tr.avg_entry_price > 0 else (tr.entry_low + tr.entry_high) / 2.0,
            stop_loss=tr.current_stop_loss,
            direction=tr.direction,
            notional_usd=tr.current_notional_usd(),
            leverage=tr.leverage,
        )
        ref_px = tr.avg_entry_price if tr.avg_entry_price > 0 else mark_px
        sl_dist_pct = abs(ref_px - tr.current_stop_loss) / max(1e-9, ref_px) * 100.0
        liq_dist_pct = abs(ref_px - liq_metrics["liquidation_price"]) / max(1e-9, ref_px) * 100.0
        _, rr_val, _ = engine.risk_manager.check_rr_requirements(
            tr.strategy,
            tr.direction,
            ref_px,
            tr.current_stop_loss,
            tr.current_tp1,
            tr.current_tp2,
        )
        positions_out.append(
            {
                "signal_id": tr.signal_id,
                "symbol": tr.symbol,
                "market": tr.market,
                "strategy": tr.strategy,
                "direction": tr.direction,
                "state": tr.state.value,
                "stages": [s.to_dict() for s in tr.stages],
                "stages_filled": len(tr.stages),
                "remaining_adds": max(0, max_stages - len(tr.stages)),
                "avg_entry_price": round(tr.avg_entry_price, 6),
                "total_qty": round(tr.remaining_qty, 8),
                "notional_usd": round(tr.current_notional_usd(), 2),
                "leverage": tr.leverage,
                "margin_usd": round(tr.current_margin_usd(), 2),
                "mark_price": round(mark_px, 6),
                "unrealized_pnl_usd": round(tr.unrealized_pnl_usd(mark_px), 2) if tr.remaining_qty > 0 else 0.0,
                "current_tp1": tr.current_tp1,
                "current_tp2": tr.current_tp2,
                "current_stop_loss": tr.current_stop_loss,
                "estimated_max_loss_usd": round(tr.open_stop_risk_usd(), 2),
                "rr_ratio": round(rr_val, 2),
                "liquidation_price": round(liq_metrics["liquidation_price"], 6),
                "stop_distance_pct": round(sl_dist_pct, 2),
                "liquidation_distance_pct": round(liq_dist_pct, 2),
                "stop_to_liq_ratio": round(liq_metrics["stop_to_liq_ratio"], 3),
            }
        )

    # Recent risk events
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
        "primary_policy": engine.primary_policy_name,
        "account": {
            "equity_usd": round(eq, 2),
            "peak_equity_usd": round(peak, 2),
            "short_term_equity_usd": round(prim.compute_pool_equity("short_term", marks), 2),
            "long_term_equity_usd": round(prim.compute_pool_equity("long_term", marks), 2),
            "drawdown_pct": round(dd_pct, 2),
            "drawdown_warning_pct": warn_pct,
            "drawdown_pause_pct": pause_pct,
            "drawdown_paused": prim.drawdown_paused,
            "risk_status": risk_state_label,
            "daily_pnl_usd": round(float(acct["daily_pnl_usd"]), 2),
            "max_daily_loss_usd": max_daily_loss,
            "weekly_pnl_usd": round(float(acct["weekly_pnl_usd"]), 2),
            "max_weekly_loss_usd": max_weekly_loss,
            "used_margin_usd": round(float(acct["used_margin_usd"]), 2),
            "used_margin_pct": round(used_margin_pct, 2),
            "max_margin_usage_pct": max_margin_pct,
            "total_stop_risk_usd": round(float(acct["total_stop_risk_usd"]), 2),
            "max_total_stop_risk_usd": max_total_stop,
        },
        "correlation_groups": groups_out,
        "open_positions": positions_out,
        "recent_risk_events": recent_events,
    }


def export_equity_curve(out_dir: Path, primary_policy: str, max_points: int = 2000) -> dict[str, Any]:
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
                    leds = row.get("ledgers") or {}
                    prim = leds.get(primary_policy) or {}
                    points.append(
                        {
                            "ts_utc": row.get("ts_utc"),
                            "total_equity_usd": prim.get("total_equity_usd"),
                            "short_term_equity_usd": prim.get("short_term_equity_usd"),
                            "long_term_equity_usd": prim.get("long_term_equity_usd"),
                            "shadows": {p: v.get("total_equity_usd") for p, v in leds.items()},
                        }
                    )
                except Exception:
                    continue

    # Deduplicate consecutive identical timestamps if re-saved within same second
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
        "primary_policy": primary_policy,
        "points": deduped[-max_points:],
    }

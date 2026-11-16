"""Shared live signal generation and sub-account execution helper (S1-S3, L1-L3)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.backtest.screener import _build_param_grid
from src.data.adapters import INTERVAL_SECONDS
from src.data.models import OHLCVBar
from src.data.quality import _parse_iso_utc
from src.engine.simulator import CandidateSignal, SimulationEngine
from src.indicators.technical import compute_atr, compute_atr_ratio
from src.signals.models import InputSnapshot, LifecycleState
from src.strategies.six_strategies import (
    SignalAction,
    decide_l3_dual_momentum_step,
    evaluate_live_bar,
)


def load_strategy_runtime_configs(
    out_dir: Path,
    config: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], str]]:
    """Load IS-selected params & verification_label per strategy from backtest_results.json."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    strats_cfg = config.get("strategies", {})
    by_id: dict[str, dict[str, Any]] = {}

    bt_path = out_dir / "backtest_results.json"
    if bt_path.exists():
        try:
            payload = json.loads(bt_path.read_text(encoding="utf-8"))
            rows = payload.get("strategies") or payload.get("results") or []
            if isinstance(rows, list):
                for row in rows:
                    sid = row.get("strategy_id")
                    if sid:
                        by_id[str(sid)] = row
        except Exception:
            by_id = {}

    for sid, s_cfg in strats_cfg.items():
        default_params = _build_param_grid(sid, s_cfg)[0]
        row = by_id.get(sid)
        if row and isinstance(row.get("chosen_params"), dict) and row["chosen_params"]:
            params = dict(row["chosen_params"])
        else:
            params = default_params

        if row:
            ver_label = str(
                row.get("verification_label")
                or ("已驗證" if row.get("passed") else ("資料不足，觀察中" if row.get("status") == "insufficient_history" else "觀察中（未通過驗證）"))
            )
        else:
            ver_label = "觀察中（未通過驗證）"
        out[sid] = (params, ver_label)
    return out


def is_instrument_eligible_for_strategy(inst: dict[str, Any], strat_cfg: dict[str, Any]) -> bool:
    if "symbols" in strat_cfg:
        return inst["symbol"] in set(strat_cfg["symbols"])
    return inst.get("market") in set(strat_cfg.get("markets", []))


def check_bar_freshness(
    closed_bars: list[OHLCVBar],
    interval: str,
    now_utc: datetime,
    config: dict[str, Any],
) -> tuple[list[OHLCVBar], bool, str]:
    """Keep strictly closed bars and reject series whose latest closed bar is older than 1 bar + allowed delay."""
    if interval not in INTERVAL_SECONDS:
        return [], False, f"不支援的週期 {interval}"
    interval_sec = INTERVAL_SECONDS[interval]
    now = now_utc.astimezone(timezone.utc)
    valid_bars = [
        b
        for b in closed_bars
        if _parse_iso_utc(b.ts_utc) + timedelta(seconds=interval_sec) <= now
    ]
    if not valid_bars:
        return [], False, "資料過期（無已收盤 K 線）"

    latest = valid_bars[-1]
    latest_close_dt = _parse_iso_utc(latest.ts_utc) + timedelta(seconds=interval_sec)
    age_min = (now - latest_close_dt).total_seconds() / 60.0

    sched_cfg = config.get("schedule", {})
    if interval == "1d":
        max_delay_min = float(sched_cfg.get("max_daily_candle_delay_minutes", 1500))
    elif interval == "4h":
        max_delay_min = float(sched_cfg.get("max_4h_candle_delay_minutes", 260))
    else:
        max_delay_min = float(sched_cfg.get("max_candle_delay_minutes", 20))

    if age_min > max_delay_min:
        return (
            valid_bars,
            False,
            f"資料過期（最新 {interval} 收盤 {latest_close_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}，距今 {age_min:.0f}m > {max_delay_min:.0f}m）",
        )
    return valid_bars, True, "ok"


def _find_open_trade(engine: SimulationEngine, strategy_id: str, symbol: str) -> Any | None:
    for tr in engine.primary_ledger.trades.values():
        if tr.strategy == strategy_id and tr.symbol == symbol and tr.state in (
            LifecycleState.WAITING_ENTRY,
            LifecycleState.ACTIVE,
            LifecycleState.TP1_HIT,
        ):
            return tr
    return None


def _compute_bars_held(tr: Any, latest_bar: OHLCVBar, interval: str) -> int:
    if tr is None or not tr.entered_at_utc:
        return 0
    dur = max(60, INTERVAL_SECONDS.get(interval, 86400))
    elapsed = max(0.0, (_parse_iso_utc(latest_bar.ts_utc) - _parse_iso_utc(tr.entered_at_utc)).total_seconds())
    return max(1, int(elapsed // dur) + 1)


def _submit_action_as_candidate(
    engine: SimulationEngine,
    strategy_id: str,
    inst: dict[str, Any],
    act: SignalAction,
    ver_label: str,
    closed_bars: list[OHLCVBar],
    interval: str,
    now_iso: str,
    control: dict[str, Any],
    params: dict[str, Any],
) -> tuple[bool, str]:
    latest_bar = closed_bars[-1]
    mkt = inst["market"]
    atr_period = int(engine.config.get("backtest", {}).get("atr_period", 14))
    atr_val = compute_atr(closed_bars, period=atr_period, market=mkt, interval=interval) or (latest_bar.close * 0.02)
    atr_ratio = compute_atr_ratio(closed_bars, atr_period=atr_period, baseline_bars=20, market=mkt, interval=interval) or 1.0

    ref_px = latest_bar.close
    if ref_px <= 0:
        return False, "收盤價無效"

    sl_px = float(act.stop_loss) if act.stop_loss is not None else (
        ref_px - 2.0 * atr_val if act.direction == "long" else ref_px + 2.0 * atr_val
    )
    sl_dist = max(ref_px * 0.002, abs(ref_px - sl_px))
    if act.direction == "long":
        sl_px = min(ref_px - ref_px * 0.002, sl_px)
        tp1_px = ref_px + 2.1 * sl_dist
        tp2_val: float | str = (ref_px + 2.6 * sl_dist) if strategy_id.startswith("S") else "策略動態移動止損／反向訊號出場"
    else:
        sl_px = max(ref_px + ref_px * 0.002, sl_px)
        tp1_px = ref_px - 2.1 * sl_dist
        tp2_val = (ref_px - 2.6 * sl_dist) if strategy_id.startswith("S") else "策略動態移動止損／反向訊號出場"

    band = max(ref_px * 0.001, atr_val * 0.10)
    entry_low = max(1e-9, ref_px - band)
    entry_high = ref_px + band
    interval_sec = INTERVAL_SECONDS.get(interval, 86400)
    latest_close_dt = _parse_iso_utc(latest_bar.ts_utc)
    # Bar close timestamp = open + interval duration
    from datetime import timedelta

    latest_close_iso = (latest_close_dt + timedelta(seconds=interval_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")

    strat_cfg = engine.config.get("strategies", {}).get(strategy_id, {})
    sub_state = engine.primary_ledger.build_subaccount_state(strategy_id, engine.latest_marks)
    alloc = engine.risk_manager.check_subaccount_allocation(
        strategy_id=strategy_id,
        instrument=inst,
        entry_price=ref_px,
        stop_loss=sl_px,
        subaccount_equity_usd=float(sub_state["subaccount_equity_usd"]),
        open_positions_count=int(sub_state["subaccount_open_positions"]),
        open_total_risk_usd=float(sub_state["subaccount_open_risk_usd"]),
        open_total_notional_usd=float(sub_state["subaccount_open_notional_usd"]),
        risk_multiplier=act.risk_multiplier,
    )
    if not alloc.approved:
        engine._record_watch(inst["symbol"], strategy_id, now_iso, alloc.reason)
        return False, alloc.reason

    snap = InputSnapshot(
        snapshot_id=f"SNAP-TEMP-{strategy_id}-{inst['symbol']}",
        signal_id="PENDING",
        captured_at_utc=now_iso,
        symbol=inst["symbol"],
        kline_summary={
            "interval": interval,
            "close": latest_bar.close,
            "high": latest_bar.high,
            "low": latest_bar.low,
            "ts_utc": latest_bar.ts_utc,
        },
        indicators={"atr": round(atr_val, 6), "atr_ratio": round(atr_ratio, 3), "params": params},
        filter_checks={"data_ok": True, "subaccount_risk_ok": True},
    )

    safe_lev = min(
        float(inst.get("max_leverage", 5.0)),
        5.0,
        max(1.0, round(0.20 / max(1e-4, sl_dist / ref_px), 2)),
    )
    time_stop_bars = int(params.get("max_hold_bars", 24 if interval == "15m" else 30))
    cand = CandidateSignal(
        strategy=strategy_id,
        symbol=inst["symbol"],
        direction=act.direction,
        entry_low=round(entry_low, 6),
        entry_high=round(entry_high, 6),
        add_levels=(),
        stop_loss=round(sl_px, 6),
        tp1=round(tp1_px, 6),
        tp2=round(tp2_val, 6) if isinstance(tp2_val, (int, float)) else tp2_val,
        invalidation_price=round(sl_px, 6),
        invalidation_cond=f"觸及止損價 {sl_px:g} 或策略反向／時間出場",
        reasons=(f"策略狀態：{ver_label}", act.reason),
        snapshot=snap,
        atr=atr_val,
        atr_ratio=atr_ratio,
        leverage=safe_lev,
        time_stop_bars=time_stop_bars,
        verification_status=ver_label,
        desired_notional_usd=float(strat_cfg.get("capital_usd", 10000.0)),
        subaccount_risk_mode=True,
        risk_multiplier=act.risk_multiplier,
    )
    pub = engine.submit_candidate_signal(
        cand,
        now_utc=now_iso,
        latest_bar_close_utc=latest_close_iso,
        control=control,
    )
    if pub is not None:
        return True, f"已發布 {pub.signal_id}（{strategy_id} / {ver_label}）"
    return False, f"{strategy_id} 未通過風控或排程檢查"


def evaluate_live_single_strategy_step(
    engine: SimulationEngine,
    strategy_id: str,
    inst: dict[str, Any],
    closed_bars: list[OHLCVBar],
    runtime_cfg: tuple[dict[str, Any], str],
    now_iso: str,
    control: dict[str, Any],
    allow_new_entry: bool = True,
) -> tuple[bool, str]:
    """Run live exit/entry check for S1, S2, S3, L1, or L2 on closed_bars."""
    strat_cfg = engine.config.get("strategies", {}).get(strategy_id, {})
    if not is_instrument_eligible_for_strategy(inst, strat_cfg):
        return False, ""
    if not closed_bars:
        return False, f"{strategy_id}: 資料過期（無已收盤 K 線）"

    params, ver_label = runtime_cfg
    interval = str(strat_cfg["interval"])
    now_dt = _parse_iso_utc(now_iso)
    valid_bars, fresh_ok, fresh_msg = check_bar_freshness(closed_bars, interval, now_dt, engine.config)
    if not fresh_ok or not valid_bars:
        return False, f"{strategy_id} ({ver_label}): {fresh_msg}"

    mkt = inst["market"]
    sym = inst["symbol"]
    atr_period = int(engine.config.get("backtest", {}).get("atr_period", 14))
    open_tr = _find_open_trade(engine, strategy_id, sym)
    pos_dir = open_tr.direction if open_tr is not None else ""
    bars_held = _compute_bars_held(open_tr, valid_bars[-1], interval)

    act = evaluate_live_bar(
        strategy_id=strategy_id,
        closed_bars=valid_bars,
        position_dir=pos_dir,
        params=params,
        market=mkt,
        interval=interval,
        allow_short=bool(strat_cfg.get("allow_short", True)),
        atr_period=atr_period,
        bars_held=bars_held,
    )

    if open_tr is not None and act.action == "exit":
        reason_code = "session_stop" if strategy_id == "S1" else "logic_invalidated"
        engine.close_trade_by_strategy(strategy_id, sym, valid_bars[-1].close, now_iso, exit_reason=reason_code)
        return False, f"{strategy_id} ({ver_label}): 平倉（{act.reason}）"

    if not allow_new_entry:
        return False, f"{strategy_id} ({ver_label}): 資料／風控過濾擋下（{act.reason or '未進場'}）"
    if open_tr is not None:
        return False, f"{strategy_id} ({ver_label}): 已有持倉（{open_tr.signal_id}）"

    if act.action in ("enter_long", "enter_short"):
        return _submit_action_as_candidate(
            engine=engine,
            strategy_id=strategy_id,
            inst=inst,
            act=act,
            ver_label=ver_label,
            closed_bars=valid_bars,
            interval=interval,
            now_iso=now_iso,
            control=control,
            params=params,
        )

    return False, f"{strategy_id} ({ver_label}): {act.reason or '未觸及進場條件'}"


def evaluate_live_l3_rebalance(
    engine: SimulationEngine,
    bars_1d_by_sym: dict[str, list[OHLCVBar]],
    instruments_by_sym: dict[str, dict[str, Any]],
    runtime_cfg: tuple[dict[str, Any], str],
    now: datetime,
    now_iso: str,
    control: dict[str, Any],
) -> dict[str, tuple[bool, str]]:
    """Run L3 Dual Momentum live step on closed daily bars across crypto + index."""
    strat_cfg = engine.config.get("strategies", {}).get("L3", {})
    allowed_mkts = set(strat_cfg.get("markets", ["crypto", "index"]))
    params, ver_label = runtime_cfg
    outcomes: dict[str, tuple[bool, str]] = {}

    eligible_bars: dict[str, list[OHLCVBar]] = {}
    for sym, bars in bars_1d_by_sym.items():
        if sym not in instruments_by_sym or instruments_by_sym[sym].get("market") not in allowed_mkts or not bars:
            continue
        valid_bars, fresh_ok, fresh_msg = check_bar_freshness(bars, "1d", now, engine.config)
        if not fresh_ok or not valid_bars:
            outcomes[sym] = (False, f"L3 ({ver_label}): {fresh_msg}")
            continue
        eligible_bars[sym] = valid_bars

    if not eligible_bars:
        return outcomes

    held_syms = {
        tr.symbol
        for tr in engine.primary_ledger.trades.values()
        if tr.strategy == "L3"
        and tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    }
    markets_by_sym = {sym: instruments_by_sym[sym]["market"] for sym in eligible_bars}
    atr_period = int(engine.config.get("backtest", {}).get("atr_period", 14))
    actions = decide_l3_dual_momentum_step(
        bars_up_to_date=eligible_bars,
        markets_by_sym=markets_by_sym,
        held_symbols=held_syms,
        lookback_days=int(params.get("lookback_days", 60)),
        top_k_crypto=int(params.get("top_k_crypto", 2)),
        top_k_index=int(params.get("top_k_index", 1)),
        sl_atr_mult=float(params.get("sl_atr_mult", 2.5)),
        atr_period=atr_period,
        vol_target_pct=float(params.get("vol_target_pct", 0.015)),
    )

    is_rebalance_day = (now.weekday() == 0) or (len(held_syms) == 0)
    for sym in sorted(eligible_bars.keys()):
        if sym not in actions:
            if sym in held_syms:
                outcomes[sym] = (False, f"L3 ({ver_label}): 續抱動能領先標的")
            else:
                outcomes[sym] = (False, f"L3 ({ver_label}): 未入選雙動能前排名")

    for sym, act in sorted(actions.items(), key=lambda kv: (0 if kv[1].action == "exit" else 1, kv[0])):
        bars = eligible_bars[sym]
        if act.action == "exit" and sym in held_syms:
            engine.close_trade_by_strategy("L3", sym, bars[-1].close, now_iso, exit_reason="logic_invalidated")
            outcomes[sym] = (False, f"L3 ({ver_label}): 平倉（{act.reason}）")
        elif act.action == "enter_long" and sym not in held_syms:
            if not is_rebalance_day:
                outcomes[sym] = (False, f"L3 ({ver_label}): 非每週再平衡日（{act.reason}）")
                continue
            ok, msg = _submit_action_as_candidate(
                engine=engine,
                strategy_id="L3",
                inst=instruments_by_sym[sym],
                act=act,
                ver_label=ver_label,
                closed_bars=bars,
                interval="1d",
                now_iso=now_iso,
                control=control,
                params=params,
            )
            outcomes[sym] = (ok, msg if msg else f"L3 ({ver_label}): 已送出進場評估")

    return outcomes

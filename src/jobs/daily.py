"""Daily long-term & daily-swing job: replay open trades and evaluate S3, L1, L2, L3."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config, load_control
from src.data.adapters import FallbackDataFetcher
from src.data.models import OHLCVBar
from src.data.quality import _parse_iso_utc, check_continuity
from src.engine.simulator import MarketBar, SimulationEngine
from src.indicators.technical import compute_atr
from src.jobs.data_sync import append_run_log
from src.jobs.live_strategy import (
    evaluate_live_l3_rebalance,
    evaluate_live_single_strategy_step,
    load_strategy_runtime_configs,
)
from src.signals.models import LifecycleState


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _to_market_bars_15m(bars: list[OHLCVBar], now: datetime) -> list[MarketBar]:
    out: list[MarketBar] = []
    for b in bars:
        open_dt = _parse_iso_utc(b.ts_utc)
        close_dt = open_dt + timedelta(minutes=15)
        if close_dt > now:
            continue
        out.append(
            MarketBar(
                ts_utc=b.ts_utc,
                close_ts_utc=close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
        )
    return out


def run_daily(
    out_dir: Path,
    config_path: Path | None = None,
    control_path: Path | None = None,
    fetcher: FallbackDataFetcher | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Replay open positions & evaluate daily strategies S3, L1, L2, L3 into simulated ledgers."""
    t0 = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    control = load_control(control_path)
    quality_cfg = config.get("data_quality", {})
    timeout_sec = int(quality_cfg.get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    engine = SimulationEngine(config, out_dir=out_dir)
    engine.load_state()
    runtime_cfgs = load_strategy_runtime_configs(out_dir, config)

    instruments = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    inst_by_sym = {i["symbol"]: i for i in instruments}
    bars_1d_ok: dict[str, list[OHLCVBar]] = {}
    watch_items: list[dict[str, Any]] = []

    open_daily_syms = {
        tr.symbol
        for led in engine.ledgers.values()
        for tr in led.trades.values()
        if tr.strategy in ("S3", "L1", "L2", "L3")
        and tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    }

    signals_before = set(engine.primary_ledger.trades.keys())

    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]
        res_1d = data_fetcher.fetch(inst, "1d", limit=320, now_utc=now)
        rep_1d = check_continuity(res_1d, market=mkt, quality_config=quality_cfg, now_utc=now).to_dict()

        if res_1d.ok and res_1d.bars:
            engine.latest_marks[sym] = res_1d.bars[-1].close

        if sym in open_daily_syms and not bool(inst.get("daily_only", False)) and mkt != "index":
            res_15m = data_fetcher.fetch(inst, "15m", limit=500, now_utc=now)
            if res_15m.ok and res_15m.bars:
                atr_d = compute_atr(res_1d.bars, period=14, market=mkt, interval="1d") if res_1d.ok else None
                m_bars = _to_market_bars_15m(res_15m.bars, now)
                engine.replay_bars(sym, m_bars, atr=atr_d)

        st_1d = rep_1d.get("status", "failed")
        if st_1d in ("failed", "unstable") or not (res_1d.ok and res_1d.bars):
            reason = f"日線資料異常（{st_1d}）"
            engine._record_watch(sym, "L1", now_iso, reason)
            watch_items.append({"symbol": sym, "market": mkt, "decision": "watch", "reason": reason})
            continue

        bars_1d_ok[sym] = res_1d.bars
        msgs: list[str] = []
        pub_any = False
        for sid in ("S3", "L1", "L2"):
            if sid in runtime_cfgs:
                ok_s, msg_s = evaluate_live_single_strategy_step(
                    engine=engine,
                    strategy_id=sid,
                    inst=inst,
                    closed_bars=res_1d.bars,
                    runtime_cfg=runtime_cfgs[sid],
                    now_iso=now_iso,
                    control=control,
                    allow_new_entry=True,
                )
                pub_any = pub_any or ok_s
                if msg_s:
                    msgs.append(msg_s)
        if not pub_any:
            reason = "；".join(msgs) if msgs else "未觸及日線進場條件"
            watch_items.append({"symbol": sym, "market": mkt, "decision": "watch", "reason": reason})

    if "L3" in runtime_cfgs and bars_1d_ok:
        l3_outcomes = evaluate_live_l3_rebalance(
            engine=engine,
            bars_1d_by_sym=bars_1d_ok,
            instruments_by_sym=inst_by_sym,
            runtime_cfg=runtime_cfgs["L3"],
            now=now,
            now_iso=now_iso,
            control=control,
        )
        for sym, (ok_l3, msg_l3) in l3_outcomes.items():
            if not ok_l3 and msg_l3:
                watch_items.append({"symbol": sym, "market": inst_by_sym[sym]["market"], "decision": "watch", "reason": msg_l3})

    new_signal_ids = sorted(set(engine.primary_ledger.trades.keys()) - signals_before)
    engine.save_state(now_iso)
    _write_json(out_dir / "watch_today_daily.json", {"updated_at_utc": now_iso, "items": watch_items})
    _write_json(out_dir / "new_signals_to_commit.json", {"signal_ids": new_signal_ids})

    duration_sec = round(time.monotonic() - t0, 2)
    keep_n = int(config.get("schedule", {}).get("keep_run_logs", 100))
    append_run_log(
        out_dir,
        {
            "run_at_utc": now_iso,
            "job": "daily",
            "duration_sec": duration_sec,
            "status": "ok",
            "signals_published": len(new_signal_ids),
            "watch_count": len(watch_items),
        },
        keep_n=keep_n,
    )

    return {
        "updated_at_utc": now_iso,
        "duration_sec": duration_sec,
        "signals_published": new_signal_ids,
        "watch_count": len(watch_items),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Desk daily S3/L1/L2/L3 scan job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    args = parser.parse_args()

    res = run_daily(out_dir=args.out_dir)
    print(f"Daily scan complete: {json.dumps(res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()

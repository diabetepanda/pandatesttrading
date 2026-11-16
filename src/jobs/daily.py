"""Daily Strategy B scan and 15m position replay job (SPEC Section 12 & 13.1)."""

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
from src.signals.models import LifecycleState
from src.strategies.strategy_b import StrategyB


def _read_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


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
    """Run daily Strategy B scan on screening-passed instruments + 15m catch-up replay."""
    t0 = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    control = load_control(control_path)
    quality_cfg = config.get("data_quality", {})
    timeout_sec = int(quality_cfg.get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    bt_data = _read_json(out_dir / "backtest_results.json", {})
    bt_by_sym = {r["symbol"]: r for r in bt_data.get("results", []) if isinstance(r, dict)}

    engine = SimulationEngine(config, out_dir=out_dir)
    engine.load_state()
    strat_b = StrategyB(config)

    instruments = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    signals_before = set(engine.primary_ledger.trades.keys())
    watch_items: list[dict[str, Any]] = []

    open_b_syms = {
        tr.symbol
        for led in engine.ledgers.values()
        for tr in led.trades.values()
        if tr.strategy == "B" and tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    }

    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]
        res_1d = data_fetcher.fetch(inst, "1d", limit=320, now_utc=now)
        rep_1d = check_continuity(res_1d, market=mkt, quality_config=quality_cfg, now_utc=now).to_dict()

        if res_1d.ok and res_1d.bars:
            engine.latest_marks[sym] = res_1d.bars[-1].close

        # Replay 15m bars for open Strategy B trades (SPEC 13.1)
        if sym in open_b_syms:
            res_15m = data_fetcher.fetch(inst, "15m", limit=500, now_utc=now)
            if res_15m.ok and res_15m.bars:
                atr_d = compute_atr(res_1d.bars, period=14, market=mkt, interval="1d") if res_1d.ok else None
                m_bars = _to_market_bars_15m(res_15m.bars, now)
                engine.replay_bars(sym, m_bars, atr=atr_d)

        bt_entry = bt_by_sym.get(sym)
        ev = strat_b.evaluate_instrument(
            instrument=inst,
            bars_1d=res_1d.bars if res_1d.ok else [],
            backtest_entry=bt_entry,
            report_1d=rep_1d,
            now_utc=now,
        )
        if ev.decision == "signal" and ev.candidate is not None:
            latest_ts = ev.candidate.snapshot.kline_summary.get("latest_1d_ts_utc")
            latest_close_dt = _parse_iso_utc(str(latest_ts)) + timedelta(days=1) if latest_ts else now
            pub = engine.submit_candidate_signal(
                ev.candidate,
                now_utc=now_iso,
                latest_bar_close_utc=latest_close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                control=control,
            )
            if pub is None:
                watch_items.append({"symbol": sym, "market": mkt, "decision": "rejected", "reason": "風控或排程延遲擋下"})
        else:
            engine._record_watch(sym, "B", now_iso, ev.watch_reason)
            watch_items.append({"symbol": sym, "market": mkt, "decision": "watch", "reason": ev.watch_reason})

    signals_after = set(engine.primary_ledger.trades.keys())
    new_signal_ids = sorted(signals_after - signals_before)

    engine.save_state(now_iso)
    _write_json(out_dir / "watch_today_b.json", {"updated_at_utc": now_iso, "items": watch_items})
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
    parser = argparse.ArgumentParser(description="Signal Desk daily Strategy B scan job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    args = parser.parse_args()

    res = run_daily(out_dir=args.out_dir)
    print(f"Daily scan complete: {json.dumps(res, ensure_ascii=False)}")


if __name__ == "__main__":
    main()

"""Scheduled scan job (Stage 3: data fetch, quality check, engine replay, and Strategy A)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config, load_control, load_events
from src.data.adapters import (
    FallbackDataFetcher,
    fetch_crypto_derivatives_meta,
    normalize_derivatives_info,
)
from src.data.models import FetchResult, OHLCVBar
from src.data.quality import _parse_iso_utc, check_continuity
from src.data.resample import resample_1m_to_15m
from src.engine.simulator import MarketBar, SimulationEngine
from src.indicators.technical import compute_atr, compute_atr_ratio, resample_15m_to_1h
from src.signals.models import LifecycleState
from src.strategies.strategy_a import StrategyA


def _read_json_if_exists(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _count_runs_last_24h(run_log: list[dict[str, Any]], now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    count = 0
    for row in run_log:
        ts_str = row.get("run_at_utc")
        if not ts_str:
            continue
        try:
            if _parse_iso_utc(ts_str) >= cutoff:
                count += 1
        except Exception:
            continue
    return count


def _to_market_bars_1m(bars: list[OHLCVBar], now: datetime) -> list[MarketBar]:
    out: list[MarketBar] = []
    for b in bars:
        open_dt = _parse_iso_utc(b.ts_utc)
        close_dt = open_dt + timedelta(minutes=1)
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


def run_scan(
    out_dir: Path,
    config_path: Path | None = None,
    control_path: Path | None = None,
    events_path: Path | None = None,
    fetcher: FallbackDataFetcher | None = None,
    derivatives_fetcher: Any | None = None,
    probe_all_adapters: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Execute Stage 3 scan: data quality, catch-up replay, Strategy A scan, and JSON export."""
    t_start = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    control = load_control(control_path)
    events = load_events(events_path)
    quality_cfg = config.get("data_quality", {})
    timeout_sec = int(quality_cfg.get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    instruments_cfg = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]

    instruments_out: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    status_counts = {"ok": 0, "unstable": 0, "stale": 0, "failed": 0}
    source_stats: dict[str, dict[str, int]] = {}

    bars_1m_by_sym: dict[str, list[OHLCVBar]] = {}
    bars_15m_by_sym: dict[str, list[OHLCVBar]] = {}
    reports_by_sym: dict[str, dict[str, dict[str, Any]]] = {}

    for inst in instruments_cfg:
        symbol = inst["symbol"]
        market = inst["market"]

        intervals_reports: dict[str, dict[str, Any]] = {}
        fetch_results: dict[str, FetchResult] = {}
        for iv in ("1m", "15m", "1d"):
            fetch_limit = 1500 if iv == "1m" else (2000 if iv == "15m" else 120)
            res = data_fetcher.fetch(inst, iv, limit=fetch_limit, now_utc=now)
            fetch_results[iv] = res
            eval_res = (
                FetchResult(
                    symbol=res.symbol,
                    interval=res.interval,
                    ok=res.ok,
                    source_used=res.source_used,
                    bars=res.bars[-120:],
                    error=res.error,
                    attempts=res.attempts,
                )
                if iv in ("1m", "15m") and res.ok and len(res.bars) > 120
                else res
            )
            report = check_continuity(eval_res, market=market, quality_config=quality_cfg, now_utc=now)
            intervals_reports[iv] = report.to_dict()

            for att in res.attempts:
                s = source_stats.setdefault(att.adapter, {"ok": 0, "fail": 0})
                if att.ok:
                    s["ok"] += 1
                else:
                    s["fail"] += 1

            if probe_all_adapters:
                all_attempts = data_fetcher.probe_all(inst, iv, limit=60, now_utc=now)
                probe_rows.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "interval": iv,
                        "quality_status": report.status,
                        "missing_ratio": report.missing_ratio,
                        "delay_minutes": report.delay_minutes,
                        "quality_reason": report.reason,
                        "attempts": [a.to_dict() for a in all_attempts],
                    }
                )

        effective_15m_bars = fetch_results["15m"].bars if fetch_results["15m"].ok else []

        # Fallback: if direct 15m is stale/unstable/failed, try resampling closed 1m bars
        res_1m = fetch_results.get("1m")
        rep_15m = intervals_reports["15m"]
        if rep_15m["status"] != "ok" and res_1m is not None and res_1m.ok and res_1m.bars:
            resampled_bars = resample_1m_to_15m(res_1m.bars, now_utc=now)
            if resampled_bars:
                res_resampled = FetchResult(
                    symbol=symbol,
                    interval="15m",
                    ok=True,
                    source_used=f"{res_1m.source_used}+resample_15m",
                    bars=resampled_bars[-120:],
                )
                rep_resampled = check_continuity(
                    res_resampled, market=market, quality_config=quality_cfg, now_utc=now
                )
                rank = {"ok": 3, "stale": 2, "unstable": 1, "failed": 0}
                better_status = rank.get(rep_resampled.status, 0) > rank.get(rep_15m["status"], 0)
                same_status_fresher = (
                    rep_resampled.status == rep_15m["status"]
                    and rep_resampled.delay_minutes is not None
                    and rep_15m["delay_minutes"] is not None
                    and rep_resampled.delay_minutes < rep_15m["delay_minutes"]
                )
                if better_status or same_status_fresher:
                    intervals_reports["15m"] = rep_resampled.to_dict()
                    effective_15m_bars = resampled_bars
                    if probe_all_adapters:
                        for row in reversed(probe_rows):
                            if row["symbol"] == symbol and row["interval"] == "15m":
                                row["quality_status"] = rep_resampled.status
                                row["missing_ratio"] = rep_resampled.missing_ratio
                                row["delay_minutes"] = rep_resampled.delay_minutes
                                row["quality_reason"] = f"已改用 1m 重取樣：{rep_resampled.reason}"
                                break

        bars_1m_by_sym[symbol] = res_1m.bars if (res_1m and res_1m.ok) else []
        bars_15m_by_sym[symbol] = effective_15m_bars
        reports_by_sym[symbol] = intervals_reports

        primary_rep = intervals_reports["15m"]
        latest_price = primary_rep["latest_close"]
        latest_ts = primary_rep["latest_ts_utc"]
        source_used = primary_rep["source_used"]
        if latest_price is None:
            for fallback_iv in ("1m", "1d"):
                if intervals_reports[fallback_iv]["latest_close"] is not None:
                    latest_price = intervals_reports[fallback_iv]["latest_close"]
                    latest_ts = intervals_reports[fallback_iv]["latest_ts_utc"]
                    source_used = intervals_reports[fallback_iv]["source_used"]
                    break

        statuses = {intervals_reports[iv]["status"] for iv in ("1m", "15m", "1d")}
        if all(intervals_reports[iv]["status"] == "failed" for iv in ("1m", "15m", "1d")):
            overall_status = "failed"
        elif intervals_reports["15m"]["status"] in ("failed", "unstable") or intervals_reports["1d"]["status"] == "failed":
            overall_status = "unstable"
        elif "stale" in statuses:
            overall_status = "stale"
        else:
            overall_status = "ok"

        status_counts[overall_status] = status_counts.get(overall_status, 0) + 1
        instruments_out.append(
            {
                "symbol": symbol,
                "display_name": inst.get("display_name", symbol),
                "market": market,
                "correlation_group": inst.get("correlation_group", ""),
                "latest_price": latest_price,
                "latest_ts_utc": latest_ts,
                "source_used": source_used,
                "overall_status": overall_status,
                "intervals": intervals_reports,
            }
        )

    # Restore SimulationEngine state and run catch-up replay + Strategy A
    engine = SimulationEngine(config, out_dir=out_dir)
    engine.load_state()
    for inst_out in instruments_out:
        if inst_out["latest_price"] is not None:
            engine.latest_marks[inst_out["symbol"]] = float(inst_out["latest_price"])

    strategy_a = StrategyA(config, events=events)
    strat_a_cfg = config.get("strategy_a", {})
    bo_mult = float(strat_a_cfg.get("breakout_bar_atr_mult", 2.5))

    # Compute peer metrics per correlation group and 1H ATR / ATR ratio for replay
    peer_metrics: dict[str, dict[str, Any]] = {}
    atr_1h_by_sym: dict[str, float | None] = {}
    atr_ratio_by_sym: dict[str, float | None] = {}
    for inst in instruments_cfg:
        sym = inst["symbol"]
        mkt = inst["market"]
        grp = inst.get("correlation_group", "")
        b15 = bars_15m_by_sym.get(sym, [])
        b1h = resample_15m_to_1h(b15, now_utc=now)
        atr_1h = compute_atr(b1h, period=int(strat_a_cfg.get("atr_period", 14)), market=mkt, interval="1h")
        atr_rat = compute_atr_ratio(
            b1h,
            atr_period=int(strat_a_cfg.get("atr_period", 14)),
            baseline_bars=int(strat_a_cfg.get("atr_baseline_days", 20)) * 24,
            market=mkt,
            interval="1h",
        )
        atr_1h_by_sym[sym] = atr_1h
        atr_ratio_by_sym[sym] = atr_rat

        if b15 and atr_1h and atr_1h > 0:
            last_range = b15[-1].high - b15[-1].low
            if last_range > bo_mult * atr_1h:
                peer_metrics.setdefault(grp, {})["violent_move"] = True

    # Catch-up replay for any symbol with open/pending trades
    symbols_with_open_trades = {
        tr.symbol
        for led in engine.ledgers.values()
        for tr in led.trades.values()
        if tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    }
    for sym in sorted(symbols_with_open_trades):
        m_bars = _to_market_bars_1m(bars_1m_by_sym.get(sym, []), now)
        if m_bars:
            inst_cfg = engine.instruments.get(sym, {})
            mkt = inst_cfg.get("market", "crypto")
            low_hrs, _ = strategy_a.compute_low_liquidity_profile(bars_15m_by_sym.get(sym, []), mkt)
            _, sess_end, _ = strategy_a.is_low_liquidity_window(now, mkt, low_hrs)
            engine.replay_bars(
                sym,
                m_bars,
                atr=atr_1h_by_sym.get(sym),
                atr_ratio=atr_ratio_by_sym.get(sym),
                session_end_utc=sess_end,
            )

    # Evaluate Strategy A on every enabled instrument
    signals_before = set(engine.primary_ledger.trades.keys())
    watch_today_items: list[dict[str, Any]] = []
    heatmap_out: dict[str, dict[str, Any]] = {}

    for inst in instruments_cfg:
        sym = inst["symbol"]
        mkt = inst["market"]
        rep_1m = reports_by_sym[sym]["1m"]
        rep_15m = reports_by_sym[sym]["15m"]
        b15 = bars_15m_by_sym.get(sym, [])

        crypto_meta = None
        deriv_record: dict[str, Any] | None = None
        if mkt == "crypto":
            deriv_fn = derivatives_fetcher or fetch_crypto_derivatives_meta
            crypto_meta = deriv_fn(inst)
            deriv_record = normalize_derivatives_info(crypto_meta)

        ev = strategy_a.evaluate_instrument(
            instrument=inst,
            bars_15m=b15,
            report_1m=rep_1m,
            report_15m=rep_15m,
            now_utc=now,
            peer_metrics=peer_metrics,
            crypto_meta=crypto_meta,
        )
        heatmap_out[sym] = {
            "market": mkt,
            "low_hours": ev.low_liquidity_hours,
            "hourly_profile": ev.hourly_profile,
        }

        if ev.decision == "signal" and ev.candidate is not None:
            latest_ts_15m = ev.candidate.snapshot.kline_summary.get("latest_15m_ts_utc")
            latest_close_dt = _parse_iso_utc(str(latest_ts_15m)) + timedelta(minutes=15) if latest_ts_15m else now
            pub = engine.submit_candidate_signal(
                ev.candidate,
                now_utc=now_iso,
                latest_bar_close_utc=latest_close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                control=control,
            )
            if pub is None:
                item_entry: dict[str, Any] = {
                    "symbol": sym,
                    "market": mkt,
                    "decision": "rejected",
                    "reason": "風控或頻率限制擋下（詳見 watch_log / risk_events）",
                    "indicators": ev.indicators,
                    "data_status_1m": rep_1m["status"],
                    "data_status_15m": rep_15m["status"],
                }
                if deriv_record is not None:
                    item_entry["derivatives"] = deriv_record
                watch_today_items.append(item_entry)
        else:
            engine._record_watch(sym, "A", now_iso, ev.watch_reason)
            item_entry = {
                "symbol": sym,
                "market": mkt,
                "decision": "watch",
                "reason": ev.watch_reason,
                "indicators": ev.indicators,
                "data_status_1m": rep_1m["status"],
                "data_status_15m": rep_15m["status"],
            }
            if deriv_record is not None:
                item_entry["derivatives"] = deriv_record
            watch_today_items.append(item_entry)

    signals_after = set(engine.primary_ledger.trades.keys())
    new_signal_ids = sorted(signals_after - signals_before)

    # Persist engine state & partitioned JSONs
    engine.save_state(now_iso)

    # Export recent 15m bars for open/newly published signals for Single Signal Page chart
    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    for sid, tr in engine.primary_ledger.trades.items():
        if sid in new_signal_ids or tr.state in (
            LifecycleState.WAITING_ENTRY,
            LifecycleState.ACTIVE,
            LifecycleState.TP1_HIT,
        ):
            sym_bars = bars_15m_by_sym.get(tr.symbol, [])[-96:]
            if sym_bars:
                _write_json(
                    charts_dir / f"{sid}.json",
                    {
                        "signal_id": sid,
                        "symbol": tr.symbol,
                        "interval": "15m",
                        "bars": [
                            {
                                "ts_utc": b.ts_utc,
                                "open": b.open,
                                "high": b.high,
                                "low": b.low,
                                "close": b.close,
                                "volume": b.volume,
                            }
                            for b in sym_bars
                        ],
                    },
                )

    duration_sec = round(time.monotonic() - t_start, 2)

    run_log = _read_json_if_exists(out_dir / "run_log.json", [])
    if not isinstance(run_log, list):
        run_log = []
    run_log.insert(
        0,
        {
            "run_at_utc": now_iso,
            "job": "scan",
            "duration_sec": duration_sec,
            "status": "ok" if status_counts["failed"] < len(instruments_out) else "failed",
            "instruments_ok": status_counts["ok"],
            "instruments_total": len(instruments_out),
            "signals_published": len(new_signal_ids),
            "watch_count": len(watch_today_items),
        },
    )
    keep_n = int(config.get("schedule", {}).get("keep_run_logs", 100))
    run_log = run_log[:keep_n]
    runs_24h = _count_runs_last_24h(run_log, now)

    prim = engine.primary_ledger
    total_eq = round(prim.compute_equity(engine.latest_marks), 2)
    short_eq = round(prim.compute_pool_equity("short_term", engine.latest_marks), 2)
    long_eq = round(prim.compute_pool_equity("long_term", engine.latest_marks), 2)

    data_health = {
        "updated_at_utc": now_iso,
        "total_instruments": len(instruments_out),
        "status_counts": status_counts,
        "sources": source_stats,
        "runs_last_24h": runs_24h,
        "total_equity_usd": total_eq,
        "short_term_equity_usd": short_eq,
        "long_term_equity_usd": long_eq,
        "overall": "failed"
        if status_counts["failed"] == len(instruments_out) and len(instruments_out) > 0
        else ("degraded" if (status_counts["failed"] > 0 or status_counts["unstable"] > 0) else "ok"),
    }

    # Merge Stage 3 metadata into state.json while preserving engine's last_processed_ts
    state = _read_json_if_exists(out_dir / "state.json", {})
    state.update(
        {
            "stage": 3,
            "last_run_utc": now_iso,
            "last_run_duration_sec": duration_sec,
            "runs_last_24h": runs_24h,
        }
    )

    _write_json(out_dir / "instruments.json", {"updated_at_utc": now_iso, "instruments": instruments_out})
    _write_json(out_dir / "data_health.json", data_health)
    _write_json(out_dir / "control_status.json", control)
    _write_json(out_dir / "state.json", state)
    _write_json(out_dir / "run_log.json", run_log)
    _write_json(out_dir / "watch_today.json", {"updated_at_utc": now_iso, "items": watch_today_items})
    _write_json(out_dir / "liquidity_heatmap.json", {"updated_at_utc": now_iso, "instruments": heatmap_out})
    _write_json(out_dir / "events_calendar.json", {"updated_at_utc": now_iso, "events": events})
    _write_json(out_dir / "new_signals_to_commit.json", {"signal_ids": new_signal_ids})

    if probe_all_adapters:
        _write_json(
            out_dir / "data_probe_report.json",
            {
                "generated_at_utc": now_iso,
                "results": probe_rows,
            },
        )

    return {
        "updated_at_utc": now_iso,
        "duration_sec": duration_sec,
        "status_counts": status_counts,
        "signals_published": new_signal_ids,
        "watch_count": len(watch_today_items),
        "runs_last_24h": runs_24h,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Desk scheduled scan job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    parser.add_argument("--probe-all", action="store_true", help="Probe every adapter for all intervals")
    args = parser.parse_args()

    summary = run_scan(out_dir=args.out_dir, probe_all_adapters=args.probe_all)
    print(f"Scan complete: {json.dumps(summary, ensure_ascii=False)}")


if __name__ == "__main__":
    main()

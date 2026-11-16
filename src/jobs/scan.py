"""Scheduled scan job (incremental bar cache, parallel fetch, replay, S1-S2 scan, and JSON export)."""

from __future__ import annotations

import argparse
import json
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
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
from src.indicators.technical import compute_atr, compute_atr_ratio, resample_15m_to_1h, resample_1h_to_4h
from src.jobs.live_strategy import (
    evaluate_live_single_strategy_step,
    is_instrument_eligible_for_strategy,
    load_strategy_runtime_configs,
)
from src.signals.models import LifecycleState

LEGACY_OBSOLETE_FILES = ("watch_today_b.json", "liquidity_heatmap.json")


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


def _cleanup_legacy_files(out_dir: Path) -> None:
    for name in LEGACY_OBSOLETE_FILES:
        try:
            (out_dir / name).unlink(missing_ok=True)
        except Exception:
            pass


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


class _ScanBarCache:
    """Disk cache for scan OHLCV bars to turn 15m/1m/1d fetches into lightweight incremental tail updates."""

    MAX_BARS = {"1m": 600, "15m": 2400, "1d": 150}

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, interval: str) -> Path:
        return self.cache_dir / f"{symbol}_{interval}.pkl"

    def load(self, symbol: str, interval: str) -> tuple[str | None, str | None, list[OHLCVBar]]:
        p = self._path(symbol, interval)
        if not p.exists():
            return None, None, []
        try:
            payload = pickle.loads(p.read_bytes())
            return payload.get("fetched_at_utc"), payload.get("source_used"), payload.get("bars", [])
        except Exception:
            return None, None, []

    def merge_and_save(
        self,
        symbol: str,
        interval: str,
        now_iso: str,
        source_used: str | None,
        old_bars: list[OHLCVBar],
        new_bars: list[OHLCVBar],
    ) -> list[OHLCVBar]:
        merged_map: dict[str, OHLCVBar] = {b.ts_utc: b for b in old_bars}
        for b in new_bars:
            merged_map[b.ts_utc] = b
        merged = sorted(merged_map.values(), key=lambda b: b.ts_utc)[-self.MAX_BARS.get(interval, 1000) :]
        try:
            self._path(symbol, interval).write_bytes(
                pickle.dumps({"fetched_at_utc": now_iso, "source_used": source_used, "bars": merged})
            )
        except Exception:
            pass
        return merged


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
    """Execute scheduled scan: incremental fetch, quality check, replay, S1-S2 scan, and JSON export."""
    t_start = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    step_timings_sec: dict[str, float] = {}

    t_step = time.monotonic()
    config = load_config(config_path)
    control = load_control(control_path)
    events = load_events(events_path)
    quality_cfg = config.get("data_quality", {})
    sched_cfg = config.get("schedule", {})
    timeout_sec = min(10, int(quality_cfg.get("request_timeout_sec", 15)))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    _cleanup_legacy_files(out_dir)
    engine = SimulationEngine(config, out_dir=out_dir)
    engine.load_state()
    symbols_with_open_trades = {
        tr.symbol
        for tr in engine.ledger.trades.values()
        if tr.state in (LifecycleState.WAITING_ENTRY, LifecycleState.ACTIVE, LifecycleState.TP1_HIT)
    }
    step_timings_sec["load_and_cleanup_sec"] = round(time.monotonic() - t_step, 3)

    t_step = time.monotonic()
    instruments_cfg = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    use_cache = fetcher is None
    bar_cache = _ScanBarCache(sched_cfg.get("scan_cache_dir", ".cache/scan_bars")) if use_cache else None

    def _process_one_instrument(inst: dict[str, Any]) -> dict[str, Any]:
        symbol = inst["symbol"]
        market = inst["market"]
        is_daily_only = bool(inst.get("daily_only", False)) or market == "index"

        intervals_reports: dict[str, dict[str, Any]] = {}
        fetch_results: dict[str, FetchResult] = {}
        local_probe_rows: list[dict[str, Any]] = []
        local_attempts = []
        ivs_to_fetch = ("1d",) if is_daily_only else ("1m", "15m", "1d")

        for iv in ivs_to_fetch:
            cached_ts_iso, cached_src, cached_bars = (None, None, [])
            if bar_cache is not None:
                cached_ts_iso, cached_src, cached_bars = bar_cache.load(symbol, iv)

            res: FetchResult | None = None
            if iv == "1d" and bar_cache is not None and cached_ts_iso and len(cached_bars) >= 20:
                try:
                    cache_age_sec = (now - _parse_iso_utc(cached_ts_iso)).total_seconds()
                    latest_bar_age_sec = (now - _parse_iso_utc(cached_bars[-1].ts_utc)).total_seconds()
                    if 0 <= cache_age_sec < 6 * 3600 and latest_bar_age_sec < 36 * 3600:
                        res = FetchResult(
                            symbol=symbol,
                            interval="1d",
                            ok=True,
                            source_used=cached_src or "cache",
                            bars=cached_bars,
                        )
                except Exception:
                    res = None

            if res is None:
                if iv == "1m":
                    if symbol in symbols_with_open_trades:
                        fetch_limit = 600
                    elif cached_bars and len(cached_bars) >= 120:
                        fetch_limit = 180
                    else:
                        fetch_limit = 240
                elif iv == "15m":
                    if cached_bars and len(cached_bars) >= 600:
                        fetch_limit = 250
                    else:
                        fetch_limit = 1000
                else:
                    fetch_limit = 60

                res = data_fetcher.fetch(inst, iv, limit=fetch_limit, now_utc=now)
                local_attempts.extend(res.attempts)
                if res.ok and res.bars and bar_cache is not None:
                    merged_bars = bar_cache.merge_and_save(symbol, iv, now_iso, res.source_used, cached_bars, res.bars)
                    res = FetchResult(
                        symbol=res.symbol,
                        interval=res.interval,
                        ok=True,
                        source_used=res.source_used,
                        bars=merged_bars,
                        attempts=res.attempts,
                    )

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

            if probe_all_adapters:
                all_attempts = data_fetcher.probe_all(inst, iv, limit=60, now_utc=now)
                local_probe_rows.append(
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

        if is_daily_only:
            intervals_reports["1m"] = {
                "status": "skipped",
                "reason": "指數僅用於日線與長線策略",
                "latest_close": None,
                "latest_ts_utc": None,
                "source_used": None,
            }
            intervals_reports["15m"] = {
                "status": "skipped",
                "reason": "指數不進 15 分鐘掃描",
                "latest_close": None,
                "latest_ts_utc": None,
                "source_used": None,
            }

        res_15m = fetch_results.get("15m")
        effective_15m_bars = res_15m.bars if (res_15m is not None and res_15m.ok) else []

        res_1m = fetch_results.get("1m")
        rep_15m = intervals_reports["15m"]
        if not is_daily_only and rep_15m["status"] != "ok" and res_1m is not None and res_1m.ok and res_1m.bars:
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
                        for row in reversed(local_probe_rows):
                            if row["symbol"] == symbol and row["interval"] == "15m":
                                row["quality_status"] = rep_resampled.status
                                row["missing_ratio"] = rep_resampled.missing_ratio
                                row["delay_minutes"] = rep_resampled.delay_minutes
                                row["quality_reason"] = f"已改用 1m 重取樣：{rep_resampled.reason}"
                                break

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

        if is_daily_only:
            overall_status = intervals_reports["1d"]["status"]
        else:
            statuses = {intervals_reports[iv]["status"] for iv in ("1m", "15m", "1d")}
            if all(intervals_reports[iv]["status"] == "failed" for iv in ("1m", "15m", "1d")):
                overall_status = "failed"
            elif intervals_reports["15m"]["status"] in ("failed", "unstable") or intervals_reports["1d"]["status"] == "failed":
                overall_status = "unstable"
            elif "stale" in statuses:
                overall_status = "stale"
            else:
                overall_status = "ok"

        return {
            "symbol": symbol,
            "market": market,
            "overall_status": overall_status,
            "inst_out": {
                "symbol": symbol,
                "display_name": inst.get("display_name", symbol),
                "market": market,
                "correlation_group": inst.get("correlation_group", ""),
                "latest_price": latest_price,
                "latest_ts_utc": latest_ts,
                "source_used": source_used,
                "overall_status": overall_status,
                "intervals": intervals_reports,
            },
            "bars_1m": res_1m.bars if (res_1m and res_1m.ok) else [],
            "bars_15m": effective_15m_bars,
            "intervals_reports": intervals_reports,
            "probe_rows": local_probe_rows,
            "attempts": local_attempts,
        }

    max_workers = 12 if fetcher is None else 1
    if max_workers > 1 and len(instruments_cfg) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            fetched_bundles = list(pool.map(_process_one_instrument, instruments_cfg))
    else:
        fetched_bundles = [_process_one_instrument(inst) for inst in instruments_cfg]

    instruments_out: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    status_counts = {"ok": 0, "unstable": 0, "stale": 0, "failed": 0}
    source_stats: dict[str, dict[str, int]] = {}
    bars_1m_by_sym: dict[str, list[OHLCVBar]] = {}
    bars_15m_by_sym: dict[str, list[OHLCVBar]] = {}
    reports_by_sym: dict[str, dict[str, dict[str, Any]]] = {}

    for b in fetched_bundles:
        sym = b["symbol"]
        status_counts[b["overall_status"]] = status_counts.get(b["overall_status"], 0) + 1
        instruments_out.append(b["inst_out"])
        bars_1m_by_sym[sym] = b["bars_1m"]
        bars_15m_by_sym[sym] = b["bars_15m"]
        reports_by_sym[sym] = b["intervals_reports"]
        probe_rows.extend(b["probe_rows"])
        for att in b["attempts"]:
            s = source_stats.setdefault(att.adapter, {"ok": 0, "fail": 0})
            s["ok" if att.ok else "fail"] += 1

    step_timings_sec["fetch_and_quality_sec"] = round(time.monotonic() - t_step, 3)

    t_step = time.monotonic()
    for inst_out in instruments_out:
        if inst_out["latest_price"] is not None:
            engine.latest_marks[inst_out["symbol"]] = float(inst_out["latest_price"])

    atr_period = int(config.get("backtest", {}).get("atr_period", 14))
    bars_1h_by_sym: dict[str, list[OHLCVBar]] = {}
    atr_1h_by_sym: dict[str, float | None] = {}
    atr_ratio_by_sym: dict[str, float | None] = {}
    for inst in instruments_cfg:
        sym = inst["symbol"]
        mkt = inst["market"]
        b15 = bars_15m_by_sym.get(sym, [])
        b1h = resample_15m_to_1h(b15, now_utc=now) if b15 else []
        bars_1h_by_sym[sym] = b1h
        atr_1h = compute_atr(b1h, period=atr_period, market=mkt, interval="1h")
        atr_rat = compute_atr_ratio(b1h, atr_period=atr_period, baseline_bars=20 * 24, market=mkt, interval="1h")
        atr_1h_by_sym[sym] = atr_1h
        atr_ratio_by_sym[sym] = atr_rat

    for sym in sorted(symbols_with_open_trades):
        m_bars = _to_market_bars_1m(bars_1m_by_sym.get(sym, []), now)
        if m_bars:
            engine.replay_bars(
                sym,
                m_bars,
                atr=atr_1h_by_sym.get(sym),
                atr_ratio=atr_ratio_by_sym.get(sym),
            )
    step_timings_sec["indicators_and_replay_sec"] = round(time.monotonic() - t_step, 3)

    t_step = time.monotonic()
    deriv_fn = derivatives_fetcher or fetch_crypto_derivatives_meta
    crypto_intraday = [
        inst
        for inst in instruments_cfg
        if inst["market"] == "crypto" and not bool(inst.get("daily_only", False))
    ]
    deriv_by_sym: dict[str, dict[str, Any]] = {}
    if crypto_intraday:
        if max_workers > 1 and derivatives_fetcher is None and len(crypto_intraday) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                deriv_results = list(pool.map(lambda inst: (inst["symbol"], normalize_derivatives_info(deriv_fn(inst))), crypto_intraday))
            deriv_by_sym = dict(deriv_results)
        else:
            for inst in crypto_intraday:
                deriv_by_sym[inst["symbol"]] = normalize_derivatives_info(deriv_fn(inst))
    step_timings_sec["derivatives_fetch_sec"] = round(time.monotonic() - t_step, 3)

    t_step = time.monotonic()
    runtime_cfgs = load_strategy_runtime_configs(out_dir, config)
    signals_before = set(engine.primary_ledger.trades.keys())
    watch_today_items: list[dict[str, Any]] = []
    s1_cfg = config.get("strategies", {}).get("S1", {})
    s2_cfg = config.get("strategies", {}).get("S2", {})

    for inst in instruments_cfg:
        sym = inst["symbol"]
        mkt = inst["market"]
        if bool(inst.get("daily_only", False)) or mkt == "index":
            continue

        rep_1m = reports_by_sym[sym]["1m"]
        rep_15m = reports_by_sym[sym]["15m"]
        deriv_record = deriv_by_sym.get(sym)

        st_1m = rep_1m.get("status", "failed")
        st_15m = rep_15m.get("status", "failed")
        is_unstable = st_1m in ("unstable", "failed") or st_15m in ("unstable", "failed")
        is_stale = st_1m == "stale" or st_15m == "stale"
        deriv_ok = (deriv_record is None) or (deriv_record.get("status") == "ok")
        allow_entry = (not is_unstable) and (not is_stale) and deriv_ok

        b15 = bars_15m_by_sym.get(sym, [])
        b1h = bars_1h_by_sym.get(sym, [])
        b4h = resample_1h_to_4h(b1h, now_utc=now) if b1h else []

        published_any = False
        strat_judgments: dict[str, dict[str, str]] = {}
        strat_msgs: list[str] = []

        if "S1" in runtime_cfgs and is_instrument_eligible_for_strategy(inst, s1_cfg):
            if b15:
                ok1, msg1 = evaluate_live_single_strategy_step(
                    engine=engine,
                    strategy_id="S1",
                    inst=inst,
                    closed_bars=b15,
                    runtime_cfg=runtime_cfgs["S1"],
                    now_iso=now_iso,
                    control=control,
                    allow_new_entry=allow_entry,
                )
            else:
                ok1, msg1 = False, f"S1 ({runtime_cfgs['S1'][1]}): 資料過期（缺少已收盤 15m K 線）"
            published_any = published_any or ok1
            st_lbl1 = "signal" if ok1 else ("expired" if "資料過期" in msg1 else "watch")
            strat_judgments["S1"] = {"status": st_lbl1, "reason": msg1}
            if msg1:
                strat_msgs.append(msg1)

        if "S2" in runtime_cfgs and is_instrument_eligible_for_strategy(inst, s2_cfg):
            if b4h:
                ok2, msg2 = evaluate_live_single_strategy_step(
                    engine=engine,
                    strategy_id="S2",
                    inst=inst,
                    closed_bars=b4h,
                    runtime_cfg=runtime_cfgs["S2"],
                    now_iso=now_iso,
                    control=control,
                    allow_new_entry=allow_entry,
                )
            else:
                ok2, msg2 = False, f"S2 ({runtime_cfgs['S2'][1]}): 資料過期（缺少已收盤 4h K 線）"
            published_any = published_any or ok2
            st_lbl2 = "signal" if ok2 else ("expired" if "資料過期" in msg2 else "watch")
            strat_judgments["S2"] = {"status": st_lbl2, "reason": msg2}
            if msg2:
                strat_msgs.append(msg2)

        prefix_notes: list[str] = []
        if st_1m in ("unstable", "failed"):
            r1 = rep_1m.get("reason") or "資料不穩"
            prefix_notes.append(f"1m {r1}" if "資料不穩" in r1 or st_1m == "failed" else f"資料不穩（1m {r1}）")
        elif st_15m in ("unstable", "failed"):
            r15 = rep_15m.get("reason") or "資料不穩"
            prefix_notes.append(f"15m {r15}" if "資料不穩" in r15 or st_15m == "failed" else f"資料不穩（15m {r15}）")
        elif is_stale:
            prefix_notes.append(f"資料過期（1m:{st_1m}, 15m:{st_15m}）")
        if deriv_record is not None and deriv_record.get("status") != "ok":
            prefix_notes.append(f"衍生品狀態：{deriv_record.get('status')}（未進場）")

        reason_parts = prefix_notes + strat_msgs
        watch_reason = "；".join(reason_parts) if reason_parts else "未觸及 S1/S2 入場條件"
        decision = "signal" if published_any else ("watch" if "資料過期" not in watch_reason and not is_unstable else "watch")

        if not published_any:
            engine._record_watch(sym, "S1", now_iso, watch_reason)

        item_entry: dict[str, Any] = {
            "symbol": sym,
            "market": mkt,
            "decision": decision,
            "reason": watch_reason,
            "strategies": strat_judgments,
            "indicators": {
                "atr_1h": round(atr_1h_by_sym[sym], 6) if atr_1h_by_sym.get(sym) is not None else None,
                "atr_ratio": round(atr_ratio_by_sym[sym], 3) if atr_ratio_by_sym.get(sym) is not None else None,
            },
            "data_status_1m": st_1m,
            "data_status_15m": st_15m,
        }
        if deriv_record is not None:
            item_entry["derivatives"] = deriv_record
        watch_today_items.append(item_entry)

    signals_after = set(engine.primary_ledger.trades.keys())
    new_signal_ids = sorted(signals_after - signals_before)
    step_timings_sec["strategy_eval_sec"] = round(time.monotonic() - t_step, 3)

    t_step = time.monotonic()
    engine.save_state(now_iso)

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

    watch_count = sum(1 for item in watch_today_items if item["decision"] != "signal")
    prim = engine.primary_ledger
    total_eq = round(prim.compute_equity(engine.latest_marks), 2)
    short_eq = round(prim.compute_pool_equity("short_term", engine.latest_marks), 2)
    long_eq = round(prim.compute_pool_equity("long_term", engine.latest_marks), 2)

    _write_json(out_dir / "instruments.json", {"updated_at_utc": now_iso, "instruments": instruments_out})
    _write_json(out_dir / "control_status.json", control)
    _write_json(out_dir / "watch_today.json", {"updated_at_utc": now_iso, "items": watch_today_items})
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

    step_timings_sec["export_and_save_sec"] = round(time.monotonic() - t_step, 3)
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
            "step_timings_sec": step_timings_sec,
            "status": "ok" if status_counts["failed"] < len(instruments_out) else "failed",
            "instruments_ok": status_counts["ok"],
            "instruments_total": len(instruments_out),
            "signals_published": len(new_signal_ids),
            "watch_count": watch_count,
        },
    )
    keep_n = int(sched_cfg.get("keep_run_logs", 100))
    run_log = run_log[:keep_n]
    runs_24h = _count_runs_last_24h(run_log, now)

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

    state = _read_json_if_exists(out_dir / "state.json", {})
    state.update(
        {
            "stage": 3,
            "last_run_utc": now_iso,
            "last_run_duration_sec": duration_sec,
            "step_timings_sec": step_timings_sec,
            "runs_last_24h": runs_24h,
        }
    )
    _write_json(out_dir / "data_health.json", data_health)
    _write_json(out_dir / "state.json", state)
    _write_json(out_dir / "run_log.json", run_log)

    return {
        "updated_at_utc": now_iso,
        "duration_sec": duration_sec,
        "step_timings_sec": step_timings_sec,
        "status_counts": status_counts,
        "signals_published": new_signal_ids,
        "watch_count": watch_count,
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

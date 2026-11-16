"""Weekly Round-2 6-strategy portfolio backtest & IS/OOS screening job."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backtest.screener import screen_all_six_strategies
from src.config_loader import load_config
from src.data.adapters import (
    BinanceMonthlyArchiveFetcher,
    DukascopyArchiveFetcher,
    FallbackDataFetcher,
)
from src.indicators.technical import resample_15m_to_1h, resample_1h_to_4h
from src.jobs.data_sync import append_run_log


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run_weekly_screening(
    out_dir: Path,
    config_path: Path | None = None,
    fetcher: FallbackDataFetcher | None = None,
    archive_fetcher: BinanceMonthlyArchiveFetcher | None = None,
    dukascopy_fetcher: DukascopyArchiveFetcher | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Fetch 5y+ Binance/Dukascopy (with PAXGUSDT backup) & 10y daily index/FX history and run 6-strategy IS/OOS."""
    t0 = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    timeout_sec = int(config.get("data_quality", {}).get("request_timeout_sec", 15))
    bt_cfg = config.get("backtest", {})
    min_years = float(bt_cfg.get("min_history_years", 5.0))
    bin_cache_dir = bt_cfg.get("archive_cache_dir", ".cache/binance_monthly")
    duka_cache_dir = bt_cfg.get("dukascopy_cache_dir", ".cache/dukascopy")
    gold_backup_sym = str(bt_cfg.get("gold_backup_symbol", "PAXGUSDT"))

    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)
    bin_archive = archive_fetcher or (
        BinanceMonthlyArchiveFetcher(cache_dir=bin_cache_dir, timeout_sec=max(20, timeout_sec))
        if fetcher is None
        else None
    )
    duka_archive = dukascopy_fetcher or (
        DukascopyArchiveFetcher(cache_dir=duka_cache_dir, timeout_sec=min(5, timeout_sec))
        if fetcher is None
        else None
    )

    instruments = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    s1_symbols = set(config.get("strategies", {}).get("S1", {}).get("symbols", []))
    bars_by_interval: dict[str, dict[str, list[Any]]] = {"1d": {}, "15m": {}, "1h": {}, "4h": {}}
    backup_notes: dict[str, str] = {}

    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]

        res_1d = data_fetcher.fetch(inst, "1d", limit=2600, now_utc=now)
        if res_1d.ok and res_1d.bars:
            bars_by_interval["1d"][sym] = res_1d.bars

        # Indices (and daily_only instruments) only enter daily long-term strategies
        if inst.get("daily_only") or mkt == "index":
            continue

        if mkt == "crypto":
            bin_code = inst.get("sources", {}).get("binance_vision")
            if bin_archive is not None and bin_code:
                bars_15m: list[Any] = []
                if sym in s1_symbols or not s1_symbols:
                    bars_15m = bin_archive.fetch_history(
                        bin_code,
                        "15m",
                        years=min_years + 0.15,
                        now_utc=now,
                    )
                    if bars_15m:
                        bars_by_interval["15m"][sym] = bars_15m
                bars_1h = bin_archive.fetch_history(
                    bin_code,
                    "1h",
                    years=min_years + 0.15,
                    now_utc=now,
                )
                if not bars_1h and bars_15m:
                    bars_1h = resample_15m_to_1h(bars_15m, now_utc=now)
                if bars_1h:
                    bars_by_interval["1h"][sym] = bars_1h
                    bars_4h = resample_1h_to_4h(bars_1h, now_utc=now)
                    if bars_4h:
                        bars_by_interval["4h"][sym] = bars_4h
            else:
                res_15m = data_fetcher.fetch(inst, "15m", limit=2400, now_utc=now)
                if res_15m.ok and res_15m.bars:
                    bars_by_interval["15m"][sym] = res_15m.bars
                    bars_1h = resample_15m_to_1h(res_15m.bars, now_utc=now)
                    if bars_1h:
                        bars_by_interval["1h"][sym] = bars_1h
                        bars_4h = resample_1h_to_4h(bars_1h, now_utc=now)
                        if bars_4h:
                            bars_by_interval["4h"][sym] = bars_4h
            continue

        # FX and commodities: try Dukascopy 5y free archives; never mix in 1-month Yahoo intraday data
        if duka_archive is not None:
            duka_1h = duka_archive.fetch_history(sym, "1h", years=min_years + 0.15, now_utc=now)
            if duka_1h:
                bars_by_interval["1h"][sym] = duka_1h
                duka_4h = resample_1h_to_4h(duka_1h, now_utc=now)
                if duka_4h:
                    bars_by_interval["4h"][sym] = duka_4h
            if sym in s1_symbols:
                duka_15m = duka_archive.fetch_history(sym, "15m", years=min_years + 0.15, now_utc=now)
                if duka_15m:
                    bars_by_interval["15m"][sym] = duka_15m

        # Gold backup: if XAUUSD lacks 5y Dukascopy data, use Binance PAXGUSDT 5y+ history
        if sym == "XAUUSD" and bin_archive is not None and not bars_by_interval["15m"].get("XAUUSD"):
            paxg_15m = bin_archive.fetch_history(
                gold_backup_sym,
                "15m",
                years=min_years + 0.15,
                now_utc=now,
            )
            paxg_1h = bin_archive.fetch_history(
                gold_backup_sym,
                "1h",
                years=min_years + 0.15,
                now_utc=now,
            )
            if not paxg_1h and paxg_15m:
                paxg_1h = resample_15m_to_1h(paxg_15m, now_utc=now)
            if paxg_15m:
                bars_by_interval["15m"]["XAUUSD"] = paxg_15m
            if paxg_1h:
                bars_by_interval["1h"]["XAUUSD"] = paxg_1h
                paxg_4h = resample_1h_to_4h(paxg_1h, now_utc=now)
                if paxg_4h:
                    bars_by_interval["4h"]["XAUUSD"] = paxg_4h
            if paxg_15m or paxg_1h:
                backup_notes["XAUUSD"] = (
                    f"黃金 (XAUUSD) Dukascopy 無法連線，改用 Binance {gold_backup_sym} 5 年歷史資料備援"
                )

    strategy_results = screen_all_six_strategies(bars_by_interval, config, backup_notes=backup_notes)
    passed_count = sum(1 for r in strategy_results if r["passed"])
    insufficient_count = sum(1 for r in strategy_results if r["status"] == "insufficient_history")
    failed_count = len(strategy_results) - passed_count

    payload = {
        "generated_at_utc": now_iso,
        "duration_sec": round(time.monotonic() - t0, 2),
        "summary": {
            "total_strategies": len(strategy_results),
            "passed": passed_count,
            "failed": failed_count,
            "insufficient_history": insufficient_count,
        },
        "strategies": strategy_results,
        "results": strategy_results,
    }

    _write_json(out_dir / "backtest_results.json", payload)
    _write_json(out_dir / "backtests" / "latest.json", payload)
    keep_n = int(config.get("schedule", {}).get("keep_run_logs", 100))
    append_run_log(
        out_dir,
        {
            "run_at_utc": now_iso,
            "job": "weekly",
            "duration_sec": payload["duration_sec"],
            "status": "ok",
            "passed": passed_count,
            "total": len(strategy_results),
        },
        keep_n=keep_n,
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Desk weekly Round-2 6-strategy backtest job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    args = parser.parse_args()

    res = run_weekly_screening(out_dir=args.out_dir)
    print(f"Weekly 6-strategy backtest complete: {json.dumps(res['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()

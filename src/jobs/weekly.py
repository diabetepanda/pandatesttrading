"""Weekly 6-strategy portfolio backtest & IS/OOS screening job."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backtest.screener import screen_all_six_strategies
from src.config_loader import load_config
from src.data.adapters import FallbackDataFetcher
from src.indicators.technical import resample_15m_to_1h
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
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Fetch daily & intraday bars and run portfolio-level 6-strategy IS/OOS screening."""
    t0 = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    timeout_sec = int(config.get("data_quality", {}).get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    instruments = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    bars_by_interval: dict[str, dict[str, list[Any]]] = {"1d": {}, "15m": {}, "1h": {}}

    for inst in instruments:
        sym = inst["symbol"]
        res_1d = data_fetcher.fetch(inst, "1d", limit=2600, now_utc=now)
        if res_1d.ok and res_1d.bars:
            bars_by_interval["1d"][sym] = res_1d.bars

        res_15m = data_fetcher.fetch(inst, "15m", limit=2400, now_utc=now)
        if res_15m.ok and res_15m.bars:
            bars_by_interval["15m"][sym] = res_15m.bars
            bars_1h = resample_15m_to_1h(res_15m.bars, now_utc=now)
            if bars_1h:
                bars_by_interval["1h"][sym] = bars_1h

    strategy_results = screen_all_six_strategies(bars_by_interval, config)
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
    parser = argparse.ArgumentParser(description="Signal Desk weekly 6-strategy screening job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    args = parser.parse_args()

    res = run_weekly_screening(out_dir=args.out_dir)
    print(f"Weekly screening complete: {json.dumps(res['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()

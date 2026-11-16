"""Weekly Strategy B historical backtest & OOS screening job (SPEC Section 12.1)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backtest.screener import screen_instrument
from src.config_loader import load_config
from src.data.adapters import FallbackDataFetcher


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
    """Fetch 5+ year daily history and screen every enabled instrument."""
    t0 = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    timeout_sec = int(config.get("data_quality", {}).get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    instruments = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]
    results: list[dict[str, Any]] = []

    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]
        res = data_fetcher.fetch(inst, "1d", limit=2600, now_utc=now)
        if not res.ok or not res.bars:
            results.append(
                {
                    "symbol": sym,
                    "market": mkt,
                    "status": "data_failed",
                    "history_years": 0.0,
                    "bar_count": 0,
                    "passed": False,
                    "chosen_strategy": None,
                    "chosen_params": None,
                    "in_sample": None,
                    "out_of_sample": None,
                    "walk_forward": None,
                    "source_used": None,
                    "reason": f"日線歷史資料取得失敗：{res.error}",
                }
            )
            continue

        screen_res = screen_instrument(inst, res.bars, config)
        screen_res["source_used"] = res.source_used
        results.append(screen_res)

    passed_count = sum(1 for r in results if r["passed"])
    insufficient_count = sum(1 for r in results if r["status"] == "insufficient_history")
    failed_count = len(results) - passed_count

    payload = {
        "generated_at_utc": now_iso,
        "duration_sec": round(time.monotonic() - t0, 2),
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": failed_count,
            "insufficient_history": insufficient_count,
        },
        "results": results,
    }

    _write_json(out_dir / "backtest_results.json", payload)
    _write_json(out_dir / "backtests" / "latest.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Desk weekly Strategy B screening job")
    parser.add_argument("--out-dir", type=Path, default=Path("_data_out"))
    args = parser.parse_args()

    res = run_weekly_screening(out_dir=args.out_dir)
    print(f"Weekly screening complete: {json.dumps(res['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()

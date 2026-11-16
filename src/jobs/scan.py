"""Scheduled scan job (Stage 1: market data fetch, quality check, and probe report)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config, load_control
from src.data.adapters import FallbackDataFetcher
from src.data.quality import check_continuity


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def run_scan(
    out_dir: Path,
    config_path: Path | None = None,
    control_path: Path | None = None,
    fetcher: FallbackDataFetcher | None = None,
    probe_all_adapters: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Execute Stage 1 scan: fetch market data, run quality checks, write data branch JSONs."""
    t_start = time.monotonic()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    config = load_config(config_path)
    control = load_control(control_path)
    quality_cfg = config.get("data_quality", {})
    timeout_sec = int(quality_cfg.get("request_timeout_sec", 15))
    data_fetcher = fetcher or FallbackDataFetcher(timeout_sec=timeout_sec)

    instruments_cfg = [inst for inst in config.get("instruments", []) if inst.get("enabled", True)]

    instruments_out: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    status_counts = {"ok": 0, "unstable": 0, "stale": 0, "failed": 0}
    source_stats: dict[str, dict[str, int]] = {}

    for inst in instruments_cfg:
        symbol = inst["symbol"]
        market = inst["market"]

        intervals_reports: dict[str, dict[str, Any]] = {}
        for iv in ("1m", "15m", "1d"):
            res = data_fetcher.fetch(inst, iv, limit=120, now_utc=now)
            report = check_continuity(res, market=market, quality_config=quality_cfg, now_utc=now)
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
                        "attempts": [a.to_dict() for a in all_attempts],
                    }
                )

        # Primary price comes from 15m, falling back to 1m or 1d if 15m failed
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

        # Overall status prioritizes 15m & 1d availability
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

    duration_sec = round(time.monotonic() - t_start, 2)

    data_health = {
        "updated_at_utc": now_iso,
        "total_instruments": len(instruments_out),
        "status_counts": status_counts,
        "sources": source_stats,
        "overall": "failed"
        if status_counts["failed"] == len(instruments_out) and len(instruments_out) > 0
        else ("degraded" if (status_counts["failed"] > 0 or status_counts["unstable"] > 0) else "ok"),
    }

    state = _read_json_if_exists(out_dir / "state.json", {})
    state.update(
        {
            "stage": 1,
            "last_processed_ts": now_iso,
            "last_run_utc": now_iso,
            "last_run_duration_sec": duration_sec,
        }
    )

    run_log = _read_json_if_exists(out_dir / "run_log.json", [])
    if not isinstance(run_log, list):
        run_log = []
    run_log.insert(
        0,
        {
            "run_at_utc": now_iso,
            "job": "scan",
            "duration_sec": duration_sec,
            "status": "ok" if data_health["overall"] != "failed" else "failed",
            "instruments_ok": status_counts["ok"],
            "instruments_total": len(instruments_out),
        },
    )
    keep_n = int(config.get("schedule", {}).get("keep_run_logs", 100))
    run_log = run_log[:keep_n]

    _write_json(
        out_dir / "instruments.json",
        {"updated_at_utc": now_iso, "instruments": instruments_out},
    )
    _write_json(out_dir / "data_health.json", data_health)
    _write_json(out_dir / "control_status.json", control)
    _write_json(out_dir / "state.json", state)
    _write_json(out_dir / "run_log.json", run_log)

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

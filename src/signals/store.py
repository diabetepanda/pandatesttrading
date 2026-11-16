"""Write-once signal storage and monthly-partitioned append-only event logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.signals.models import (
    InputSnapshot,
    Signal,
    SignalLockedError,
    SignalUpdateEvent,
)


def _month_key(ts_utc: str) -> str:
    """Extract YYYY-MM from an ISO UTC timestamp."""
    return ts_utc[:7] if len(ts_utc) >= 7 else "unknown"


class SignalStore:
    """Persists locked signal files and append-only JSONL logs under base_dir."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def publish_signal(self, signal: Signal, snapshot: InputSnapshot) -> Path:
        """Write an immutable signal JSON and its input snapshot once."""
        sig_dir = self.base_dir / "signals"
        snap_dir = self.base_dir / "snapshots"
        sig_dir.mkdir(parents=True, exist_ok=True)
        snap_dir.mkdir(parents=True, exist_ok=True)

        sig_path = sig_dir / f"{signal.signal_id}.json"
        if sig_path.exists():
            raise SignalLockedError(f"報單 {signal.signal_id} 已發布並鎖定，不可覆寫或修改")

        snap_path = snap_dir / f"{snapshot.snapshot_id}.json"
        if snap_path.exists():
            raise SignalLockedError(f"輸入快照 {snapshot.snapshot_id} 已存在，不可覆寫")

        with sig_path.open("w", encoding="utf-8") as f:
            json.dump(signal.to_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")

        with snap_path.open("w", encoding="utf-8") as f:
            json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")

        return sig_path

    def load_signal(self, signal_id: str) -> Signal:
        sig_path = self.base_dir / "signals" / f"{signal_id}.json"
        with sig_path.open("r", encoding="utf-8") as f:
            return Signal.from_dict(json.load(f))

    def append_update(self, event: SignalUpdateEvent) -> Path:
        """Append a SignalUpdateEvent to the monthly JSONL file without modifying prior lines."""
        upd_dir = self.base_dir / "signal_updates"
        upd_dir.mkdir(parents=True, exist_ok=True)
        path = upd_dir / f"{_month_key(event.ts_utc)}.jsonl"

        existing_ids: set[str] = set()
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing_ids.add(json.loads(line)["event_id"])
                    except Exception:
                        continue

        if event.event_id not in existing_ids:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False))
                f.write("\n")

        return path

    def load_updates(self, signal_id: str | None = None) -> list[SignalUpdateEvent]:
        upd_dir = self.base_dir / "signal_updates"
        if not upd_dir.exists():
            return []
        events: list[SignalUpdateEvent] = []
        for path in sorted(upd_dir.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    ev = SignalUpdateEvent.from_dict(json.loads(line))
                    if signal_id is None or ev.signal_id == signal_id:
                        events.append(ev)
        return events

    def append_watch_log(self, entry: dict[str, Any]) -> Path:
        """Append a watch-log (rejected/skipped candidate) entry to monthly JSONL."""
        watch_dir = self.base_dir / "watch_log"
        watch_dir.mkdir(parents=True, exist_ok=True)
        ts = str(entry.get("ts_utc", ""))
        path = watch_dir / f"{_month_key(ts)}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")
        return path

    def append_risk_event(self, entry: dict[str, Any]) -> Path:
        """Append a risk event entry to monthly JSONL."""
        risk_dir = self.base_dir / "risk_events"
        risk_dir.mkdir(parents=True, exist_ok=True)
        ts = str(entry.get("ts_utc", ""))
        path = risk_dir / f"{_month_key(ts)}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")
        return path

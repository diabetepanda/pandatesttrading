"""Utilities for merging overlapping data-branch files and resolving git rebase conflicts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


OBSOLETE_DATA_BRANCH_FILES = (
    "orders.json",
    "trade_stages.json",
    "backtests/latest.json",
)


def cleanup_obsolete_data_files(out_dir: Path) -> list[str]:
    """Remove retired data-branch files on scan/daily/weekly runs while preserving probe reports."""
    removed: list[str] = []
    for rel in OBSOLETE_DATA_BRANCH_FILES:
        target = out_dir / rel
        try:
            if target.exists():
                target.unlink()
                removed.append(rel)
        except Exception:
            pass
    return removed


def merge_run_logs(remote_items: list[dict[str, Any]], local_items: list[dict[str, Any]], keep_n: int = 200) -> list[dict[str, Any]]:
    """Merge two run_log.json arrays without dropping entries from either workflow."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in remote_items + local_items:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("run_at_utc", "")), str(row.get("job", "")))
        merged[key] = row
    ordered = sorted(merged.values(), key=lambda r: str(r.get("run_at_utc", "")), reverse=True)
    return ordered[:keep_n]


def append_run_log(out_dir: Path, entry: dict[str, Any], keep_n: int = 200) -> list[dict[str, Any]]:
    """Append a job run entry to run_log.json safely, preserving entries from other workflows."""
    log_path = out_dir / "run_log.json"
    existing: list[dict[str, Any]] = []
    if log_path.exists():
        try:
            raw = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        except Exception:
            existing = []
    merged = merge_run_logs(existing, [entry], keep_n=keep_n)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def merge_jsonl_lines(remote_text: str, local_text: str) -> str:
    """Merge two JSONL files by deduplicating lines while preserving chronological order."""
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for raw_line in remote_text.splitlines() + local_text.splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        ts_key = ""
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                ts_key = str(
                    obj.get("ts_utc")
                    or obj.get("event_ts_utc")
                    or obj.get("run_at_utc")
                    or obj.get("published_at_utc")
                    or ""
                )
        except Exception:
            pass
        rows.append((ts_key, line))

    if all(k for k, _ in rows):
        rows.sort(key=lambda pair: pair[0])
    return "".join(f"{line}\n" for _, line in rows)


def merge_state_json(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Merge state.json, keeping the latest last_processed_ts per symbol."""
    merged = dict(remote)
    for k, v in local.items():
        if k == "last_processed_ts":
            continue
        if k in ("last_run_utc",) and k in remote:
            merged[k] = max(str(remote[k]), str(v))
        else:
            merged[k] = v

    r_ts = remote.get("last_processed_ts") if isinstance(remote.get("last_processed_ts"), dict) else {}
    l_ts = local.get("last_processed_ts") if isinstance(local.get("last_processed_ts"), dict) else {}
    comb_ts = dict(r_ts)
    for sym, ts in l_ts.items():
        prev = comb_ts.get(sym)
        if prev is None or str(ts) > str(prev):
            comb_ts[sym] = ts
    merged["last_processed_ts"] = comb_ts
    return merged


def _merge_list_by_id(
    remote_list: list[Any],
    local_list: list[Any],
    id_keys: tuple[str, ...],
) -> list[Any]:
    by_key: dict[str, Any] = {}
    extras: list[Any] = []
    for item in remote_list + local_list:
        if isinstance(item, dict):
            k_parts = [str(item.get(k)) for k in id_keys if k in item]
            if len(k_parts) == len(id_keys):
                by_key["|".join(k_parts)] = item
                continue
        if item not in extras:
            extras.append(item)
    return list(by_key.values()) + extras


def _merge_single_ledger(rl: dict[str, Any], ll: dict[str, Any]) -> dict[str, Any]:
    comb_trades = dict(rl.get("trades") or {})
    comb_trades.update(ll.get("trades") or {})
    comb_orders = dict(rl.get("orders") or {})
    comb_orders.update(ll.get("orders") or {})
    fills_by_id: dict[str, Any] = {}
    for f in (rl.get("fills") or []) + (ll.get("fills") or []):
        fid = str(f.get("fill_id", "")) if isinstance(f, dict) else str(f)
        fills_by_id[fid] = f
    comb_re = _merge_list_by_id(rl.get("risk_events") or [], ll.get("risk_events") or [], ("ts_utc", "event_type"))
    out = dict(rl)
    out.update(ll)
    out["trades"] = comb_trades
    out["orders"] = comb_orders
    out["fills"] = list(fills_by_id.values())
    out["risk_events"] = comb_re
    return out


def _merge_ledgers_state(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    merged = dict(remote)
    r_seq = remote.get("sequences") or {}
    l_seq = local.get("sequences") or {}
    merged["sequences"] = {
        k: max(int(r_seq.get(k, 0)), int(l_seq.get(k, 0)))
        for k in set(r_seq.keys()) | set(l_seq.keys())
    }
    r_marks = remote.get("latest_marks") or {}
    l_marks = local.get("latest_marks") or {}
    merged["latest_marks"] = {**r_marks, **l_marks}

    r_led = remote.get("ledger") or (remote.get("ledgers") or {}).get("E")
    l_led = local.get("ledger") or (local.get("ledgers") or {}).get("E")
    merged.pop("ledgers", None)
    if r_led and l_led:
        merged["ledger"] = _merge_single_ledger(dict(r_led), dict(l_led))
    elif l_led:
        merged["ledger"] = dict(l_led)
    elif r_led:
        merged["ledger"] = dict(r_led)
    return merged


def merge_conflicted_content(rel_path: str, remote_text: str, local_text: str) -> str:
    """Merge remote (:2:) and local (:3:) versions of a data-branch file during rebase."""
    norm = rel_path.replace("\\", "/")
    if norm.endswith(".jsonl"):
        return merge_jsonl_lines(remote_text, local_text)

    if norm.endswith(".json"):
        try:
            remote_obj = json.loads(remote_text)
            local_obj = json.loads(local_text)
        except Exception:
            return local_text

        if norm == "run_log.json" and isinstance(remote_obj, list) and isinstance(local_obj, list):
            return json.dumps(merge_run_logs(remote_obj, local_obj), ensure_ascii=False, indent=2) + "\n"

        if norm == "state.json" and isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            return json.dumps(merge_state_json(remote_obj, local_obj), ensure_ascii=False, indent=2) + "\n"

        if norm == "ledgers_state.json" and isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            return json.dumps(_merge_ledgers_state(remote_obj, local_obj), ensure_ascii=False, indent=2) + "\n"

        if norm == "signals_index.json" and isinstance(remote_obj, list) and isinstance(local_obj, list):
            merged_idx = _merge_list_by_id(remote_obj, local_obj, ("signal_id",))
            merged_idx.sort(key=lambda x: str(x.get("signal_id", "")) if isinstance(x, dict) else "", reverse=True)
            return json.dumps(merged_idx, ensure_ascii=False, indent=2) + "\n"

        if norm == "trades.json" and isinstance(remote_obj, list) and isinstance(local_obj, list):
            return json.dumps(_merge_list_by_id(remote_obj, local_obj, ("signal_id",)), ensure_ascii=False, indent=2) + "\n"

        if norm == "equity_curve.json" and isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            merged_pts = _merge_list_by_id(
                remote_obj.get("points") or [],
                local_obj.get("points") or [],
                ("ts_utc",),
            )
            merged_pts.sort(key=lambda x: str(x.get("ts_utc", "")) if isinstance(x, dict) else "")
            return json.dumps({**remote_obj, **local_obj, "points": merged_pts[-2000:]}, ensure_ascii=False, indent=2) + "\n"

        if norm == "news_index.json" and isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            merged_issues = _merge_list_by_id(
                remote_obj.get("issues") or [],
                local_obj.get("issues") or [],
                ("issue",),
            )
            merged_issues.sort(
                key=lambda x: (str(x.get("date", "")), 1 if x.get("half") == "PM" else 0) if isinstance(x, dict) else ("", 0),
                reverse=True,
            )
            merged_months = _merge_list_by_id(
                remote_obj.get("months") or [],
                local_obj.get("months") or [],
                ("month",),
            )
            merged_months.sort(key=lambda x: str(x.get("month", "")) if isinstance(x, dict) else "", reverse=True)
            return json.dumps(
                {**remote_obj, **local_obj, "months": merged_months, "issues": merged_issues},
                ensure_ascii=False,
                indent=2,
            ) + "\n"

        if norm == "news_reactions.json" and isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            r_react = remote_obj.get("reactions") if isinstance(remote_obj.get("reactions"), dict) else {}
            l_react = local_obj.get("reactions") if isinstance(local_obj.get("reactions"), dict) else {}
            merged_react = {**r_react, **l_react}
            return json.dumps(
                {**remote_obj, **local_obj, "count": len(merged_react), "reactions": merged_react},
                ensure_ascii=False,
                indent=2,
            ) + "\n"

        if isinstance(remote_obj, dict) and isinstance(local_obj, dict):
            merged_dict = {**remote_obj, **local_obj}
            return json.dumps(merged_dict, ensure_ascii=False, indent=2) + "\n"

    return local_text


def _git_show_stage(repo_dir: Path, stage: int, rel_path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f":{stage}:{rel_path}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def resolve_rebase_conflicts(repo_dir: Path) -> list[str]:
    """Resolve unmerged files during git pull --rebase by merging overlapping JSON/JSONL content."""
    unmerged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    resolved: list[str] = []
    for rel_path in [p.strip() for p in unmerged if p.strip()]:
        # Note: during `git pull --rebase`, stage 2 is HEAD (remote branch), stage 3 is our rebased commit.
        remote_text = _git_show_stage(repo_dir, 2, rel_path)
        local_text = _git_show_stage(repo_dir, 3, rel_path)
        target = repo_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if remote_text is not None and local_text is not None:
            merged_text = merge_conflicted_content(rel_path, remote_text, local_text)
            target.write_text(merged_text, encoding="utf-8")
        elif local_text is not None:
            target.write_text(local_text, encoding="utf-8")
        elif remote_text is not None:
            target.write_text(remote_text, encoding="utf-8")

        subprocess.run(["git", "add", rel_path], cwd=str(repo_dir), check=True)
        resolved.append(rel_path)

    return resolved


def pull_rebase_with_merge(repo_dir: Path, branch: str = "data") -> None:
    """Run git pull --rebase and automatically resolve overlapping data conflicts."""
    proc = subprocess.run(
        ["git", "pull", "--rebase", "origin", branch],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return

    env = dict(os.environ, GIT_EDITOR="true")
    # Resolve all commits in the rebase queue if multiple commits conflict
    for _ in range(20):
        resolve_rebase_conflicts(repo_dir)
        cont = subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=str(repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        if cont.returncode == 0:
            return
        # Check whether another commit hit a conflict
        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not unmerged:
            raise RuntimeError(f"git rebase --continue failed: {cont.stderr or cont.stdout}")

    raise RuntimeError("Exceeded max rebase continuation iterations")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve data-branch conflicts and push with retry")
    parser.add_argument("--repo-dir", type=Path, default=Path("."))
    parser.add_argument("--branch", default="data")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    last_err = ""
    for attempt in range(1, args.max_retries + 1):
        push = subprocess.run(
            ["git", "push", "origin", args.branch],
            cwd=str(args.repo_dir),
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            print(f"Push to {args.branch} succeeded on attempt {attempt}.")
            return
        last_err = (push.stderr or push.stdout).strip()
        print(f"Push attempt {attempt}/{args.max_retries} failed; rebasing and merging: {last_err}")
        if attempt < args.max_retries:
            pull_rebase_with_merge(args.repo_dir, branch=args.branch)

    raise SystemExit(f"Failed to push to {args.branch} after {args.max_retries} attempts: {last_err}")


if __name__ == "__main__":
    main()

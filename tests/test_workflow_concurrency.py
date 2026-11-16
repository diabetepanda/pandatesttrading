"""Tests for distinct workflow concurrency groups and rebase/merge push retry."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from src.jobs.data_sync import (
    merge_conflicted_content,
    merge_jsonl_lines,
    merge_run_logs,
    merge_state_json,
    pull_rebase_with_merge,
)

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_data_writer_workflows_have_distinct_concurrency_groups() -> None:
    targets = ["scan.yml", "daily.yml", "weekly.yml"]
    groups: list[str] = []
    for name in targets:
        wf = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
        conc = wf.get("concurrency") or {}
        assert "group" in conc and conc["group"], f"{name} missing concurrency.group"
        assert conc.get("cancel-in-progress") is False, f"{name} must set cancel-in-progress: false"
        groups.append(str(conc["group"]))

        raw = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "data_sync.py" in raw
        assert "--max-retries 3" in raw

    assert len(set(groups)) == len(targets), f"Duplicate concurrency groups: {groups}"
    assert "data-branch-writer" not in groups


def test_merge_overlapping_run_log_jsonl_and_state() -> None:
    remote_log = [
        {"run_at_utc": "2026-11-16T07:00:00Z", "job": "daily", "status": "ok"},
        {"run_at_utc": "2026-11-16T06:52:00Z", "job": "scan", "status": "ok"},
    ]
    local_log = [
        {"run_at_utc": "2026-11-16T07:07:00Z", "job": "scan", "status": "ok"},
        {"run_at_utc": "2026-11-16T06:52:00Z", "job": "scan", "status": "ok"},
    ]
    merged_log = json.loads(
        merge_conflicted_content(
            "run_log.json",
            json.dumps(remote_log),
            json.dumps(local_log),
        )
    )
    keys = {(r["run_at_utc"], r["job"]) for r in merged_log}
    assert ("2026-11-16T07:00:00Z", "daily") in keys
    assert ("2026-11-16T07:07:00Z", "scan") in keys
    assert len(merged_log) == 3

    r_jsonl = '{"ts_utc": "2026-11-16T07:00:00Z", "msg": "daily"}\n'
    l_jsonl = '{"ts_utc": "2026-11-16T07:07:00Z", "msg": "scan"}\n'
    merged_jsonl = merge_jsonl_lines(r_jsonl, l_jsonl)
    assert "daily" in merged_jsonl and "scan" in merged_jsonl

    r_state = {"last_processed_ts": {"BTCUSDT": "2026-11-16T07:05:00Z", "EURUSD": "2026-11-16T07:00:00Z"}}
    l_state = {"last_processed_ts": {"BTCUSDT": "2026-11-16T07:00:00Z", "ETHUSDT": "2026-11-16T07:07:00Z"}}
    m_state = merge_state_json(r_state, l_state)
    assert m_state["last_processed_ts"]["BTCUSDT"] == "2026-11-16T07:05:00Z"
    assert m_state["last_processed_ts"]["EURUSD"] == "2026-11-16T07:00:00Z"
    assert m_state["last_processed_ts"]["ETHUSDT"] == "2026-11-16T07:07:00Z"


def test_git_pull_rebase_resolves_run_log_conflict(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "data", str(bare)], check=True, capture_output=True)

    c1 = tmp_path / "c1"
    subprocess.run(["git", "clone", str(bare), str(c1)], check=True, capture_output=True)
    for d in (c1,):
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(d), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(d), check=True)
    (c1 / "run_log.json").write_text(
        json.dumps([{"run_at_utc": "2026-11-16T06:00:00Z", "job": "scan"}], indent=2) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "run_log.json"], cwd=str(c1), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(c1), check=True)
    subprocess.run(["git", "push", "origin", "data"], cwd=str(c1), check=True, capture_output=True)

    c2 = tmp_path / "c2"
    subprocess.run(["git", "clone", "--branch", "data", str(bare), str(c2)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test2"], cwd=str(c2), check=True)
    subprocess.run(["git", "config", "user.email", "test2@example.com"], cwd=str(c2), check=True)

    # c1 writes daily entry and pushes first
    (c1 / "run_log.json").write_text(
        json.dumps(
            [
                {"run_at_utc": "2026-11-16T07:00:00Z", "job": "daily"},
                {"run_at_utc": "2026-11-16T06:00:00Z", "job": "scan"},
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "run_log.json"], cwd=str(c1), check=True)
    subprocess.run(["git", "commit", "-m", "daily"], cwd=str(c1), check=True)
    subprocess.run(["git", "push", "origin", "data"], cwd=str(c1), check=True, capture_output=True)

    # c2 writes scan entry concurrently and tries to push
    (c2 / "run_log.json").write_text(
        json.dumps(
            [
                {"run_at_utc": "2026-11-16T07:07:00Z", "job": "scan"},
                {"run_at_utc": "2026-11-16T06:00:00Z", "job": "scan"},
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "run_log.json"], cwd=str(c2), check=True)
    subprocess.run(["git", "commit", "-m", "scan"], cwd=str(c2), check=True)

    # Pull-rebase + merge preserves all 3 run_log entries
    pull_rebase_with_merge(c2, branch="data")
    subprocess.run(["git", "push", "origin", "data"], cwd=str(c2), check=True, capture_output=True)

    final_items = json.loads((c2 / "run_log.json").read_text(encoding="utf-8"))
    jobs = {(r["run_at_utc"], r["job"]) for r in final_items}
    assert ("2026-11-16T07:00:00Z", "daily") in jobs
    assert ("2026-11-16T07:07:00Z", "scan") in jobs
    assert ("2026-11-16T06:00:00Z", "scan") in jobs

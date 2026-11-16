"""Configuration and control file loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"
DEFAULT_CONTROL_PATH = ROOT_DIR / "control" / "control.json"
DEFAULT_EVENTS_PATH = ROOT_DIR / "events.yaml"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load the main YAML configuration file."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config format in {target}")
    return data


def load_control(path: Path | str | None = None) -> dict[str, Any]:
    """Load control/control.json."""
    target = Path(path) if path else DEFAULT_CONTROL_PATH
    with target.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid control.json format in {target}")
    return data


def load_events(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load manual economic calendar events from events.yaml."""
    target = Path(path) if path else DEFAULT_EVENTS_PATH
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    events = data.get("events", [])
    return events if isinstance(events, list) else []

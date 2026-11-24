"""Shared test helpers for Signal Desk test suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def get_js_binary() -> str:
    """Locate node (on Linux / CI) or JavaScriptCore jsc (on macOS)."""
    js_bin = shutil.which("node")
    if not js_bin:
        jsc_path = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if jsc_path.exists():
            js_bin = str(jsc_path)
    if not js_bin:
        raise RuntimeError("Expected either node or macOS jsc to verify frontend helpers")
    return js_bin


def run_js(script: str, js_bin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Safely execute a JavaScript script using Node or JavaScriptCore without exceeding argument list limits.

    Writes the script to a temporary file before execution.
    """
    binary = js_bin or get_js_binary()
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(script)
        tmp_js = tf.name

    try:
        return subprocess.run([binary, tmp_js], capture_output=True, text=True, check=False)
    finally:
        try:
            os.unlink(tmp_js)
        except OSError:
            pass

"""Cross-process JSON flock used by daemon state and semantic cache."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from src.graph.json_flock import cross_process_json_flock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl flock is not available on Windows",
)


def test_cross_process_json_flock_serializes_subprocesses(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"n": 0}', encoding="utf-8")
    script = f"""
import json
import sys
import time
from pathlib import Path

from src.graph.json_flock import cross_process_json_flock

target = Path({str(target)!r})
with cross_process_json_flock(target):
    data = json.loads(target.read_text(encoding="utf-8"))
    time.sleep(0.05)
    data["n"] = data.get("n", 0) + 1
    target.write_text(json.dumps(data), encoding="utf-8")
"""
    repo_root = Path(__file__).resolve().parents[1]
    procs = [subprocess.Popen([sys.executable, "-c", script], cwd=str(repo_root)) for _ in range(2)]
    for proc in procs:
        proc.wait(timeout=10)
        assert proc.returncode == 0
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result["n"] == 2


def test_in_process_json_flock_context_manager(tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    target.write_text("{}", encoding="utf-8")
    with cross_process_json_flock(target):
        target.write_text('{"ok": true}', encoding="utf-8")
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

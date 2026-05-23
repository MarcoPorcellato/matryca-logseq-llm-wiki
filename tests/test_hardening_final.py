"""Tests for per-page write locks and payload bounds."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from src.agent.graph_dispatch import dispatch_mutate
from src.agent.mcp_tool_guard import format_tool_error, guard_mcp_tool
from src.agent.quality_gate import (
    MAX_OUTLINE_NODES,
    advanced_query_bounds_violations,
    markdown_append_bounds_violations,
    outline_bounds_violations,
)
from src.graph import page_write_lock as page_write_lock_mod
from src.graph.markdown_blocks import atomic_write_bytes
from src.graph.page_write_lock import (
    clear_page_write_locks,
    cross_process_lock_available,
    page_rmw_lock,
)


def test_outline_bounds_rejects_too_many_nodes() -> None:
    outline: dict[str, object] = {"text": "root", "children": []}
    child: dict[str, object] = {"text": "leaf", "children": []}
    outline["children"] = [dict(child) for _ in range(MAX_OUTLINE_NODES)]
    issues = outline_bounds_violations(outline)
    assert issues and "node count" in issues[0]


def test_advanced_query_bounds_rejects_oversized_body() -> None:
    huge = "x" * 40_000
    issues = advanced_query_bounds_violations(huge)
    assert issues and "max bytes" in issues[0]


def test_markdown_append_bounds_reject_oversized_body() -> None:
    huge = "y" * 300_000
    issues = markdown_append_bounds_violations(huge)
    assert issues and "markdown_body" in issues[0]


def test_concurrent_page_rmw_lock_serializes_writes(tmp_path: Path) -> None:
    clear_page_write_locks()
    target = tmp_path / "pages" / "Shared.md"
    target.parent.mkdir(parents=True)
    target.write_text("start\n", encoding="utf-8")
    order: list[str] = []

    def writer(tag: str, delay: float) -> None:
        with page_rmw_lock(target):
            order.append(f"{tag}-enter")
            time.sleep(delay)
            text = target.read_text(encoding="utf-8")
            target.write_text(text + f"{tag}\n", encoding="utf-8")
            order.append(f"{tag}-exit")

    t1 = threading.Thread(target=writer, args=("A", 0.04))
    t2 = threading.Thread(target=writer, args=("B", 0.01))
    t1.start()
    time.sleep(0.005)
    t2.start()
    t1.join()
    t2.join()

    assert order.index("A-enter") < order.index("A-exit")
    assert order.index("B-enter") < order.index("B-exit")
    assert order.index("A-exit") < order.index("B-enter") or order.index("B-exit") < order.index(
        "A-enter"
    )
    body = target.read_text(encoding="utf-8")
    assert "A" in body and "B" in body


@pytest.mark.skipif(not cross_process_lock_available(), reason="fcntl flock unavailable")
def test_flock_oserror_falls_back_to_thread_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_page_write_locks()
    target = tmp_path / "CloudSync.md"
    target.write_text("start\n", encoding="utf-8")

    def _reject_flock(fd: int, op: int) -> None:
        _ = (fd, op)
        msg = "flock not supported on this filesystem"
        raise OSError(95, msg)

    monkeypatch.setenv("MATRYCA_ALLOW_FLOCK_DEGRADATION", "true")
    monkeypatch.setattr(page_write_lock_mod._fcntl, "flock", _reject_flock)
    with page_rmw_lock(target):
        target.write_text("updated\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "updated\n"


def test_atomic_write_respects_existing_page_lock(tmp_path: Path) -> None:
    clear_page_write_locks()
    target = tmp_path / "Locked.md"
    target.write_text("old", encoding="utf-8")
    with page_rmw_lock(target):
        atomic_write_bytes(target, b"new", graph_root=tmp_path)
    assert target.read_bytes() == b"new"


@pytest.mark.skipif(not cross_process_lock_available(), reason="fcntl flock unavailable")
def test_cross_process_page_rmw_lock_serializes_subprocesses(tmp_path: Path) -> None:
    clear_page_write_locks()
    target = tmp_path / "pages" / "Shared.md"
    target.parent.mkdir(parents=True)
    target.write_text("start\n", encoding="utf-8")
    script = f"""
import sys
import time
from pathlib import Path
from src.graph.page_write_lock import page_rmw_lock

target = Path({str(target)!r})
tag = sys.argv[1]
delay = float(sys.argv[2])
with page_rmw_lock(target):
    time.sleep(delay)
    text = target.read_text(encoding="utf-8")
    target.write_text(text + tag + "\\n", encoding="utf-8")
"""
    proc_a = subprocess.Popen(
        [sys.executable, "-c", script, "A", "0.15"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    time.sleep(0.02)
    proc_b = subprocess.Popen(
        [sys.executable, "-c", script, "B", "0.01"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc_a.wait(timeout=30) == 0
    assert proc_b.wait(timeout=30) == 0
    body = target.read_text(encoding="utf-8")
    assert "A" in body and "B" in body
    assert body.index("A\n") < body.index("B\n") or body.index("B\n") < body.index("A\n")


@pytest.mark.asyncio
async def test_dispatch_mutate_unknown_alias_returns_ok_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))
    out = await dispatch_mutate("write_outline", "[42]", '{"text":"x","children":[]}')
    assert out.get("ok") is False
    assert "[42]" in str(out.get("error"))


@pytest.mark.asyncio
async def test_guard_mcp_tool_returns_clean_dict_error() -> None:
    @guard_mcp_tool
    async def boom() -> dict[str, object]:
        msg = "domain failure"
        raise RuntimeError(msg)

    out = await boom()
    assert out["ok"] is False
    assert out["code"] == "tool_error"
    assert "domain failure" in str(out["error"])


@pytest.mark.asyncio
async def test_guard_mcp_tool_returns_clean_text_error() -> None:
    @guard_mcp_tool
    async def boom() -> str:
        msg = "read failed"
        raise OSError(msg)

    out = await boom()
    assert isinstance(out, str)
    assert out.startswith("Tool failed:")
    assert "read failed" in out


def test_format_tool_error_text_and_dict() -> None:
    text_err = format_tool_error(ValueError("bad"), as_text=True)
    assert isinstance(text_err, str)
    assert text_err.startswith("Tool failed:")
    dict_err = format_tool_error(ValueError("bad"), as_text=False)
    assert isinstance(dict_err, dict)
    assert dict_err["ok"] is False

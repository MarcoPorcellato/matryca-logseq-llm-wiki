"""Tests for Logseq UUID helpers and block-ref pre-flight guard."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from src.graph.global_fence_scanner import compute_page_protected_line_indices
from src.graph.logseq_uuid import (
    assert_valid_block_refs_in_markdown,
    find_malformed_block_refs,
    is_logseq_block_uuid,
    is_malformed_block_ref_error,
)
from src.graph.markdown_blocks import atomic_write_bytes


def test_is_logseq_block_uuid_accepts_v4_and_v5() -> None:
    v4 = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    v5 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "block"))
    assert is_logseq_block_uuid(v4)
    assert is_logseq_block_uuid(v5)


def test_is_logseq_block_uuid_rejects_v1() -> None:
    u1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    assert not is_logseq_block_uuid(u1)


def test_find_malformed_block_refs_short_uuid() -> None:
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"  # 35 hex chars in last group
    assert find_malformed_block_refs(f"- link (({bad}))") == [bad]


def test_find_malformed_block_refs_ignores_double_parens_in_code_fence() -> None:
    md = "\n".join(
        [
            "- visible note",
            "```python",
            "if ((x > 0)):",
            "    pass",
            "```",
        ],
    )
    protected = compute_page_protected_line_indices(md)
    assert find_malformed_block_refs(md, protected_lines=protected) == []


def test_assert_valid_block_refs_allows_code_fence_double_parens(tmp_path: Path) -> None:
    md = "\n".join(
        [
            "- ok",
            "```",
            "if ((x > 0)):",
            "```",
        ],
    )
    assert_valid_block_refs_in_markdown(md)
    path = tmp_path / "pages" / "code.md"
    path.parent.mkdir(parents=True)
    atomic_write_bytes(path, (md + "\n").encode(), graph_root=tmp_path)
    assert path.read_text(encoding="utf-8") == md + "\n"


def test_assert_valid_block_refs_raises_on_typo() -> None:
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    with pytest.raises(ValueError, match="Malformed UUID"):
        assert_valid_block_refs_in_markdown(f"- link (({bad}))")


def test_atomic_write_bytes_rejects_malformed_block_ref(tmp_path: Path) -> None:
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    path = tmp_path / "pages" / "x.md"
    path.parent.mkdir(parents=True)
    with pytest.raises(ValueError, match="Malformed UUID"):
        atomic_write_bytes(path, f"- ref (({bad}))\n".encode(), graph_root=tmp_path)


def test_atomic_write_bytes_can_bypass_block_ref_validation(tmp_path: Path) -> None:
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    path = tmp_path / "pages" / "x.md"
    path.parent.mkdir(parents=True)
    payload = f"- ref (({bad}))\n".encode()
    atomic_write_bytes(path, payload, graph_root=tmp_path, validate_block_refs=False)
    assert path.read_text(encoding="utf-8") == payload.decode()


def test_atomic_write_bytes_skips_block_ref_validation_for_non_markdown(tmp_path: Path) -> None:
    """JSON/PID/log payloads must not trigger ((uuid)) pre-flight even with default validation."""
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    payload = json.dumps(
        {"error": f"Malformed UUID in block ref (({bad}))"},
        indent=2,
    ).encode()
    json_path = tmp_path / ".matryca_daemon_state.json"
    pid_path = tmp_path / ".matryca_plumber_daemon.pid"
    log_path = tmp_path / "matryca_plumber_ops.log"
    atomic_write_bytes(json_path, payload, graph_root=tmp_path)
    atomic_write_bytes(pid_path, b"12345\n", graph_root=tmp_path)
    atomic_write_bytes(log_path, payload, graph_root=tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["error"].startswith("Malformed UUID")
    assert pid_path.read_text(encoding="utf-8") == "12345\n"
    assert log_path.read_text(encoding="utf-8") == payload.decode()


def test_is_malformed_block_ref_error_detects_preflight_value_error() -> None:
    bad = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    try:
        assert_valid_block_refs_in_markdown(f"- link (({bad}))")
    except ValueError as exc:
        assert is_malformed_block_ref_error(exc)
    else:
        pytest.fail("expected ValueError")
    assert not is_malformed_block_ref_error(ValueError("other"))

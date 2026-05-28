"""Tests for per-page KV-cache prompt sessions."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.agent.page_prompt_session import (
    FrozenPromptPrefix,
    PagePromptSession,
    PrefixDriftError,
    build_page_prompt_session,
)
from src.agent.plumber_config import PlumberLintConfig
from src.agent.semantic_lint_prompts import build_semantic_lint_system_prompt


def test_page_prompt_session_stable_block_shared_across_tasks(tmp_path: Path) -> None:
    graph_root = tmp_path
    content = "- [[Link]] note\n"
    session = build_page_prompt_session(
        graph_root,
        "Demo",
        content,
        config=PlumberLintConfig(mapreduce_trigger_chars=10_000),
        stable_system=build_semantic_lint_system_prompt(),
    )
    task_a = session.build_task_prompt("Task A")
    task_b = session.build_task_prompt("Task B")
    assert session.stable_page_block in task_a
    assert session.stable_page_block in task_b
    assert task_a.index(session.stable_page_block) < task_a.index("Task A")
    assert task_b.index(session.stable_page_block) < task_b.index("Task B")
    assert task_a.rsplit("Task A", 1)[0] == task_b.rsplit("Task B", 1)[0]


def test_page_prompt_session_prefix_hash_stable(tmp_path: Path) -> None:
    page = tmp_path / "pages"
    page.mkdir(parents=True)
    path = page / "Demo.md"
    path.write_text("- note\n", encoding="utf-8")
    session = build_page_prompt_session(
        tmp_path,
        "Demo",
        path.read_text(encoding="utf-8"),
        config=PlumberLintConfig(),
        stable_system="sys",
        page_path=path,
    )
    assert isinstance(session, PagePromptSession)
    assert len(session.page_fingerprint) == 16
    assert len(session.prefix_sha256) == 64
    session.frozen.verify_unchanged()


def test_frozen_prefix_drift_detected() -> None:
    frozen = FrozenPromptPrefix.build(system_text="sys", stable_user_prefix="Page content:\nbody")
    object.__setattr__(frozen, "stable_user_prefix", "Page content:\nchanged")
    with pytest.raises(PrefixDriftError):
        frozen.verify_unchanged()

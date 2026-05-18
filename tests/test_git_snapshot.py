"""Tests for optional git snapshot helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from src.agent.git_snapshot import snapshot_git_working_tree


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not installed",
)
def test_snapshot_commit_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_GIT_SNAPSHOT_ON_WRITE", "true")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Matryca Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@matryca.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Matryca Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@matryca.local")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "a.md").write_text("hello", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)

    (tmp_path / "pages" / "a.md").write_text("hello world", encoding="utf-8")

    r1 = snapshot_git_working_tree(tmp_path, message="snap1")
    assert r1.get("committed") is True

    r2 = snapshot_git_working_tree(tmp_path, message="snap2")
    assert r2.get("reason") == "clean working tree; nothing to commit"


def test_snapshot_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MATRYCA_GIT_SNAPSHOT_ON_WRITE", raising=False)
    r = snapshot_git_working_tree(tmp_path)
    assert r.get("skipped") is True
    assert "MATRYCA_GIT_SNAPSHOT_ON_WRITE" in str(r.get("reason", ""))

"""Tests for atomic write hygiene and dangling temp sweeper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from src.graph.markdown_blocks import atomic_write_bytes, sweep_dangling_atomic_tmp_files


def test_atomic_write_unlinks_temp_when_replace_fails(tmp_path: Path) -> None:
    target = tmp_path / "page.md"
    temps_before = list(tmp_path.glob(".*.tmp"))

    def boom(src: Path, dst: Path) -> None:
        msg = "simulated replace failure"
        raise OSError(msg)

    with (
        patch("src.graph.markdown_blocks.os.replace", side_effect=boom),
        pytest.raises(OSError, match="simulated replace failure"),
    ):
        atomic_write_bytes(target, b"payload")

    assert not target.exists()
    assert list(tmp_path.glob(".page.md.*.tmp")) == []
    assert temps_before == []


def test_sweep_dangling_atomic_tmp_files_removes_orphans(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    journals = tmp_path / "journals"
    pages.mkdir()
    journals.mkdir()
    orphan_page = pages / ".Note.md.deadbeef.tmp"
    orphan_journal = journals / ".2026_05_19.md.cafebabe.tmp"
    keep_page = pages / "Note.md"
    orphan_page.write_bytes(b"stale")
    orphan_journal.write_bytes(b"stale")
    keep_page.write_text("live", encoding="utf-8")

    removed = sweep_dangling_atomic_tmp_files(tmp_path)
    assert removed == 2
    assert not orphan_page.exists()
    assert not orphan_journal.exists()
    assert keep_page.read_text(encoding="utf-8") == "live"


def test_sweep_ignores_unrelated_hidden_files(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    unrelated = pages / ".gitkeep"
    unrelated.write_text("", encoding="utf-8")
    assert sweep_dangling_atomic_tmp_files(tmp_path) == 0
    assert unrelated.is_file()

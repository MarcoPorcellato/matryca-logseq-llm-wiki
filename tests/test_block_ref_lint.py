"""Tests for on-disk ``((uuid))`` block reference lint."""

from __future__ import annotations

from pathlib import Path

from src.graph.block_ref_lint import (
    collect_block_ref_targets,
    collect_id_declarations,
    lint_block_refs_in_graph,
)


def test_collect_id_declarations_finds_property_lines() -> None:
    text = """
- Root
  id:: F47AC10B-58CC-4372-A567-0E02B2C3D479
  - child
"""
    ids = collect_id_declarations(text)
    assert ids == {"f47ac10b-58cc-4372-a567-0e02b2c3d479"}


def test_lint_resolves_cross_page_ref(tmp_path: Path) -> None:
    """A ref defined in one page resolves when used in another."""
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    known = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    (pages / "a.md").write_text(
        f"- Block A\n  id:: {known}\n",
        encoding="utf-8",
    )
    (pages / "b.md").write_text(
        f"- Ref\n  - See (({known}))\n",
        encoding="utf-8",
    )
    result = lint_block_refs_in_graph(tmp_path)
    assert result.pages_scanned == 2
    assert not result.broken


def test_lint_flags_unresolved_ref(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    missing = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    (pages / "x.md").write_text(
        f"- Broken\n  - (({missing}))\n",
        encoding="utf-8",
    )
    result = lint_block_refs_in_graph(tmp_path)
    assert len(result.broken) == 1
    assert result.broken[0].reason == "unresolved"


def test_collect_block_ref_targets_flags_non_v4_uuid() -> None:
    """UUID v1 shape inside (()) is matched but marked non-v4."""
    u1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    targets = collect_block_ref_targets(f"x (({u1})) y")
    assert len(targets) == 1
    assert targets[0][0] == u1.lower()
    assert targets[0][1] is False


def test_lint_missing_pages_directory(tmp_path: Path) -> None:
    """Graph root without ``pages/`` yields a single diagnostic."""
    result = lint_block_refs_in_graph(tmp_path)
    assert result.pages_scanned == 0
    assert len(result.broken) == 1
    assert result.broken[0].reason == "missing_pages_directory"

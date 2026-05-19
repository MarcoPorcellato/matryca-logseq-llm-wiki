"""Tests for on-disk ``((uuid))`` block reference lint."""

from __future__ import annotations

import uuid
from pathlib import Path

from src.graph.block_ref_lint import lint_block_refs_in_graph


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


def test_lint_resolves_uuid_v5_id_and_ref(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    known = str(uuid.uuid5(uuid.NAMESPACE_DNS, "ephemeral-block"))
    (pages / "a.md").write_text(
        f"- Block A\n  id:: {known}\n",
        encoding="utf-8",
    )
    (pages / "b.md").write_text(
        f"- Ref\n  - See (({known}))\n",
        encoding="utf-8",
    )
    result = lint_block_refs_in_graph(tmp_path)
    assert not result.broken


def test_lint_flags_invalid_uuid_version(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    u1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    (pages / "x.md").write_text(
        f"- Broken\n  - (({u1}))\n",
        encoding="utf-8",
    )
    result = lint_block_refs_in_graph(tmp_path)
    assert len(result.broken) == 1
    assert result.broken[0].reason in {"invalid_uuid", "unresolved"}


def test_lint_missing_pages_directory(tmp_path: Path) -> None:
    """Graph root without ``pages/`` yields a single diagnostic."""
    result = lint_block_refs_in_graph(tmp_path)
    assert result.pages_scanned == 0
    assert len(result.broken) == 1
    assert result.broken[0].reason == "missing_pages_directory"

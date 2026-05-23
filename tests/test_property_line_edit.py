"""Tests for surgical property-line edits."""

from __future__ import annotations

from pathlib import Path

from src.graph.markdown_blocks import (
    block_property_insert_index,
    find_id_line_index,
    strip_line_endings,
)
from src.graph.property_line_edit import edit_block_property_lines

_UUID = "11111111-1111-4111-8111-111111111111"


def _sample_page(uuid_line: str = _UUID) -> str:
    return "\n".join(
        [
            "- Root block",
            f"  id:: {uuid_line}",
            "  type:: alpha",
            "  domain:: tech",
            "- Sibling",
            "",
        ]
    )


def _multiline_page() -> str:
    return "\n".join(
        [
            "- First line of block",
            "  second continuation line",
            f"  id:: {_UUID}",
            "  type:: alpha",
            "  domain:: tech",
            "  - nested child",
            "- Sibling",
            "",
        ]
    )


def test_property_line_dry_run(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Demo.md").write_text(_sample_page(), encoding="utf-8")

    out = edit_block_property_lines(
        tmp_path,
        "Demo",
        _UUID,
        "alpha",
        "beta",
        dry_run=True,
        use_regex=False,
        replace_all=False,
    )
    assert out.ok and out.code == "dry_run_ok"
    assert out.match_count == 1
    assert pages.joinpath("Demo.md").read_text(encoding="utf-8").find("type:: alpha") >= 0


def test_property_line_apply_and_backup(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Demo.md").write_text(_sample_page(), encoding="utf-8")

    out = edit_block_property_lines(
        tmp_path,
        "Demo",
        _UUID,
        "alpha",
        "beta",
        dry_run=False,
        use_regex=False,
        replace_all=False,
    )
    assert out.ok and out.code == "applied"
    text = (pages / "Demo.md").read_text(encoding="utf-8")
    assert "type:: beta" in text
    assert (pages / "Demo.md.bak").is_file()


def test_regex_capture_replacement(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Demo.md").write_text(_sample_page(), encoding="utf-8")

    out = edit_block_property_lines(
        tmp_path,
        "Demo",
        _UUID,
        r"type:: (\w+)",
        r"type:: \g<1>-x",
        dry_run=True,
        use_regex=True,
        replace_all=False,
    )
    assert out.ok
    assert "alpha-x" in "".join(out.previews)


def test_append_page_alias_to_existing_line(tmp_path: Path) -> None:
    from src.graph.property_line_edit import append_page_alias_line

    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Topic.md").write_text(
        "alias:: [[One]], Two\n",
        encoding="utf-8",
    )
    out = append_page_alias_line(tmp_path, "Topic", "Three", dry_run=False)
    assert out.ok and out.added
    text = (pages / "Topic.md").read_text(encoding="utf-8")
    assert "Three" in text
    dup = append_page_alias_line(tmp_path, "Topic", "three", dry_run=True)
    assert dup.code == "noop_duplicate"


def test_append_page_alias_creates_line(tmp_path: Path) -> None:
    from src.graph.property_line_edit import append_page_alias_line

    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Empty.md").write_text("- root\n", encoding="utf-8")
    out = append_page_alias_line(tmp_path, "Empty", "NewAlias", dry_run=False)
    assert out.ok and out.added
    lines = (pages / "Empty.md").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("alias::")
    assert not lines[0].lstrip().startswith("- ")
    assert lines[1] == ""
    assert lines[2].startswith("- ")


def test_crlf_id_line_resolution(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    crlf = _sample_page().replace("\n", "\r\n")
    (pages / "Demo.md").write_bytes(crlf.encode("utf-8"))

    out = edit_block_property_lines(
        tmp_path,
        "Demo",
        _UUID,
        "alpha",
        "beta",
        dry_run=False,
    )
    assert out.ok and out.code == "applied"
    written = (pages / "Demo.md").read_bytes()
    assert b"\r\n" not in written
    assert b"type:: beta" in written


def test_multiline_block_property_edit(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Demo.md").write_text(_multiline_page(), encoding="utf-8")

    out = edit_block_property_lines(
        tmp_path,
        "Demo",
        _UUID,
        "alpha",
        "beta",
        dry_run=False,
    )
    assert out.ok and out.code == "applied"
    lines = (pages / "Demo.md").read_text(encoding="utf-8").splitlines()
    cont_idx = next(i for i, ln in enumerate(lines) if "second continuation" in ln)
    type_idx = next(i for i, ln in enumerate(lines) if "type:: beta" in ln)
    child_idx = next(i for i, ln in enumerate(lines) if ln.strip() == "- nested child")
    assert cont_idx < type_idx < child_idx


def test_crlf_append_page_alias_existing_line(tmp_path: Path) -> None:
    from src.graph.property_line_edit import append_page_alias_line

    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Topic.md").write_bytes(b"alias:: One\r\n")
    out = append_page_alias_line(tmp_path, "Topic", "Two", dry_run=False)
    assert out.ok and out.added
    assert "\r" not in (pages / "Topic.md").read_text(encoding="utf-8")


def test_find_id_line_index_strips_cr_before_match() -> None:
    lines = [f"  id:: {_UUID}\r"]
    assert find_id_line_index(lines, _UUID) == 0
    assert strip_line_endings(lines[0]) == f"  id:: {_UUID}"


def test_block_property_insert_index_after_continuations() -> None:
    stripped = [
        "- Parent text",
        "  continuation line",
        f"  id:: {_UUID}",
        "  type:: alpha",
        "  - child block",
        "- sibling",
    ]
    insert_at = block_property_insert_index(stripped, bullet_idx=0, block_end=5)
    assert insert_at == 4
    assert stripped[insert_at - 1] == "  type:: alpha"

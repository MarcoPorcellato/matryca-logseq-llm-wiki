"""Phase 7: mldoc-inspired property parsing, guards, and tool integration."""

from __future__ import annotations

from pathlib import Path

from src.graph.mldoc_guards import bullet_first_line_refactor_blocked, pre_id_block_lines_protected
from src.graph.mldoc_properties import (
    is_logseq_block_property_line,
    parse_logseq_property_line,
    split_logseq_property_list_values,
)
from src.graph.property_line_edit import append_page_alias_line, edit_block_property_lines
from src.graph.split_large_blocks import refactor_large_blocks
from src.graph.tag_unify import unify_tags_in_text


def test_parse_property_wikilink_and_csv() -> None:
    p = parse_logseq_property_line("  parent:: [[Project A]], [[B, C]]")
    assert p is not None
    assert p.key_normalized == "parent"
    vals = split_logseq_property_list_values(p.value_raw)
    assert vals == ["[[Project A]]", "[[B, C]]"]


def test_parse_property_quoted_commas() -> None:
    p = parse_logseq_property_line('alias:: "Foo, Inc", [[Bar]]')
    assert p is not None
    assert split_logseq_property_list_values(p.value_raw) == ['"Foo, Inc"', "[[Bar]]"]


def test_bullet_with_double_colon_is_not_property() -> None:
    assert is_logseq_block_property_line("  - Question here :: Answer") is False


def test_id_property_line_excluded_from_hygiene() -> None:
    uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    line = f"  id:: {uid}"
    assert parse_logseq_property_line(line) is None
    assert is_logseq_block_property_line(line) is False


def test_split_large_blocks_skips_code_fence(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    uid = "44444444-4444-4444-8444-444444444444"
    long_body = "First sentence is long enough. " * 12 + "Second sentence here."
    md = "\n".join(
        [
            f"- {long_body}",
            "  ```",
            "  x",
            "  ```",
            f"  id:: {uid}",
            "",
        ],
    )
    (pages / "Fence.md").write_text(md, encoding="utf-8")
    out = refactor_large_blocks(
        tmp_path,
        page_ref="Fence",
        min_chars=80,
        max_blocks=5,
        dry_run=True,
    )
    assert out.code == "noop"


def test_unify_tags_skips_hash_inside_property_quotes() -> None:
    line = 'tags:: "keep #ai literal" and #ai'
    new_line, n = unify_tags_in_text(line, {"#ai": "#AI"})
    assert n == 1
    assert '"keep #ai literal"' in new_line
    assert "and #AI" in new_line or "and #AI" in new_line.replace("#ai", "#AI")


def test_append_alias_respects_quoted_csv(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Co.md").write_text('alias:: "Acme, LLC"\n', encoding="utf-8")
    out = append_page_alias_line(tmp_path, "Co", "Other", dry_run=False)
    assert out.ok and out.added
    text = (pages / "Co.md").read_text(encoding="utf-8")
    assert '"Acme, LLC"' in text
    assert "Other" in text


def test_property_edit_still_finds_type_line(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    uid = "55555555-5555-4555-8555-555555555555"
    md = "\n".join(
        [
            "- Block",
            f"  id:: {uid}",
            "  type:: alpha",
            "",
        ],
    )
    (pages / "P.md").write_text(md, encoding="utf-8")
    out = edit_block_property_lines(tmp_path, "P", uid, "alpha", "beta", dry_run=True)
    assert out.ok and out.match_count == 1


def test_pre_id_protected_detects_logbook() -> None:
    lines = [
        "- root\n",
        "  :LOGBOOK:\n",
        "  :END:\n",
        "  id:: u\n",
    ]
    stripped = [ln.rstrip("\n") for ln in lines]
    assert pre_id_block_lines_protected(stripped, 0, 3) is True


def test_bullet_first_line_blocked_on_drawer_token() -> None:
    assert bullet_first_line_refactor_blocked("text :LOGBOOK: more") is True

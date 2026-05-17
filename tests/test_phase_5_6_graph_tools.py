"""Tests for Phase 5/6 graph gardener + synthesis helpers."""

from __future__ import annotations

from pathlib import Path

from src.graph.flashcards import append_logseq_flashcards_under_block
from src.graph.markdown_blocks import locate_block_by_uuid
from src.graph.moc_page import build_moc_markdown, collect_pages_for_namespace
from src.graph.reparent_blocks import refactor_logseq_blocks
from src.graph.split_large_blocks import refactor_large_blocks
from src.graph.tag_unify import lint_unify_logseq_tags, unify_tags_in_text
from src.graph.unlinked_mentions import resolve_unlinked_mentions


def test_unify_tags_in_text_replaces_variant() -> None:
    text = "Tags #AI and #ai end."
    new_t, n = unify_tags_in_text(text, {"#ai": "#AI"})
    assert n == 1
    assert "#AI" in new_t
    assert "#ai" not in new_t


def test_append_logseq_flashcards_dry_run(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    uid = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"
    body = "\n".join(
        [
            "- Source block",
            f"  id:: {uid}",
            "  - Capital of France :: Paris",
            "  - Two plus two :: Four",
            "",
        ],
    )
    (pages / "Dense.md").write_text(body, encoding="utf-8")
    out = append_logseq_flashcards_under_block(
        tmp_path,
        "Dense",
        uid,
        max_cards=10,
        dry_run=True,
    )
    assert out.ok and out.dry_run
    assert len(out.cards_preview) == 2


def test_refactor_logseq_blocks_dry_run(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    u1 = "11111111-1111-4111-8111-111111111111"
    u2 = "22222222-2222-4222-8222-222222222222"
    md = "\n".join(
        [
            "- A",
            f"  id:: {u1}",
            "- B",
            f"  id:: {u2}",
            "",
        ],
    )
    (pages / "Flat.md").write_text(md, encoding="utf-8")
    res = refactor_logseq_blocks(
        tmp_path,
        "Flat",
        [{"category": "Group1", "block_uuids": [u1, u2]}],
        dry_run=True,
    )
    assert res.ok and res.code == "dry_run_ok"


def test_resolve_unlinked_mentions_finds_plain_title(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Alpha Topic.md").write_text("- x\n", encoding="utf-8")
    (pages / "Other.md").write_text("See Alpha Topic today.\n", encoding="utf-8")
    out = resolve_unlinked_mentions(tmp_path, max_hits_per_file=20, max_titles=50)
    assert out["ok"] is True
    hits = out["hits"]
    assert isinstance(hits, list)
    assert any(h.get("title") == "Alpha Topic" for h in hits)


def test_build_moc_markdown_groups(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Proj___a.md").write_text("- x\n", encoding="utf-8")
    (pages / "Proj___b.md").write_text("- y\n", encoding="utf-8")
    rows = collect_pages_for_namespace(tmp_path, "Proj")
    assert len(rows) == 2
    md = build_moc_markdown(tmp_path, "Proj")
    assert "[[Proj/a]]" in md or "Proj/a" in md


def test_refactor_large_blocks_splits_long_bullet(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    uid = "33333333-3333-4333-8333-333333333333"
    long_body = "First sentence is long enough. " * 12 + "Second sentence here. Third ends now."
    md = "\n".join([f"- {long_body}", f"  id:: {uid}", ""])
    (pages / "Long.md").write_text(md, encoding="utf-8")
    out = refactor_large_blocks(
        tmp_path,
        page_ref="Long",
        min_chars=80,
        max_blocks=5,
        dry_run=False,
    )
    assert out.ok and out.code == "applied"
    text = (pages / "Long.md").read_text(encoding="utf-8")
    assert uid in text
    assert text.count("id::") >= 2


def test_lint_unify_tags_clusters(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "T.md").write_text("#AI #ai #AI\n", encoding="utf-8")
    rep = lint_unify_logseq_tags(tmp_path, dry_run=True)
    assert rep.ok
    assert rep.total_replacements >= 1 or len(rep.clusters) >= 1


def test_locate_block_by_uuid_returns_span() -> None:
    lines = [
        "- root\n",
        "  id:: bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb\n",
        "- sib\n",
    ]
    stripped = [ln.rstrip("\n") for ln in lines]
    loc = locate_block_by_uuid(stripped, "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb")
    assert loc is not None
    b, i, e = loc
    assert e == 2

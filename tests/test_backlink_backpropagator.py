"""Tests for backlink backpropagation idempotency."""

from __future__ import annotations

from pathlib import Path

from src.agent.plumber_modules.backlink_backpropagator import (
    BacklinkCorrection,
    run_backlink_backpropagator,
)
from src.graph.page_write_lock import clear_page_write_locks


def _write_page(graph_root: Path, title: str, body: str) -> Path:
    pages = graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    path = pages / f"{title}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_backprop_skips_duplicate_block_on_reprocess(tmp_path: Path) -> None:
    clear_page_write_locks()
    graph_root = tmp_path
    source = _write_page(graph_root, "Source", "- links to target\n")
    target = _write_page(graph_root, "Target", "- target page\n")
    correction = BacklinkCorrection(
        block_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        original_text="links to Redis",
        corrected_text="links to [[Target]]",
        lint_type="auto_wikilink",
        reason="Added wikilink for Redis concept",
    )
    details = ["auto_wikilink:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:seed"]

    first = run_backlink_backpropagator(
        graph_root,
        source,
        "Source",
        [correction],
        details,
    )
    assert first.pages_modified == ["Target"]
    after_first = target.read_text(encoding="utf-8")
    assert after_first.count("### Matryca Backlink Context") == 1
    assert "source-block-uuid:: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in after_first

    second = run_backlink_backpropagator(
        graph_root,
        source,
        "Source",
        [correction],
        details,
    )
    assert second.pages_modified == []
    assert target.read_text(encoding="utf-8") == after_first


def test_backprop_updates_summary_when_reason_changes(tmp_path: Path) -> None:
    clear_page_write_locks()
    graph_root = tmp_path
    source = _write_page(graph_root, "Source", "- note\n")
    target = _write_page(graph_root, "Target", "- target\n")
    uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    details = [f"auto_wikilink:{uuid}:seed"]

    run_backlink_backpropagator(
        graph_root,
        source,
        "Source",
        [
            BacklinkCorrection(
                block_uuid=uuid,
                original_text="alpha",
                corrected_text="[[Target]] alpha",
                lint_type="auto_wikilink",
                reason="First reason",
            ),
        ],
        details,
    )
    run_backlink_backpropagator(
        graph_root,
        source,
        "Source",
        [
            BacklinkCorrection(
                block_uuid=uuid,
                original_text="alpha",
                corrected_text="[[Target]] alpha",
                lint_type="auto_wikilink",
                reason="Updated reason",
            ),
        ],
        details,
    )
    text = target.read_text(encoding="utf-8")
    assert text.count("### Matryca Backlink Context") == 1
    assert "context-summary:: Updated reason" in text
    assert "First reason" not in text

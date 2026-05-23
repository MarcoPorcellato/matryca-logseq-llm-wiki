"""Tests for live graph telemetry exposed to the Plumber UI."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.graph.graph_analytics import compute_graph_analytics


def test_compute_graph_analytics_counts_topology(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Alpha.md").write_text("- see [[Beta]]\n", encoding="utf-8")
    (pages / "Beta.md").write_text("alias:: B, Beta Prime\n", encoding="utf-8")
    (pages / "PlumberSpawn.md").write_text(
        "created-by:: plumber\n- seeded concept\n",
        encoding="utf-8",
    )

    journals = tmp_path / "journals"
    journals.mkdir()
    (journals / "2026_05_23.md").write_text("- daily\n", encoding="utf-8")

    cache_dir = tmp_path / ".matryca_semantic_cache"
    cache_dir.mkdir()
    envelope = {
        "created_at": time.time(),
        "ttl_seconds": 86_400,
        "payload": {"ok": True},
    }
    (cache_dir / "abc.json").write_text(json.dumps(envelope) + (" " * 600_000), encoding="utf-8")

    metrics = compute_graph_analytics(tmp_path, ai_links_injected=0, ai_blocks_healed=0)

    assert metrics.total_pages == 3
    assert metrics.ai_pages == 1
    assert metrics.human_pages == 2
    assert metrics.total_links == 1
    assert metrics.human_links == 1
    assert metrics.ai_links == 0
    assert metrics.ai_blocks_healed == 0
    assert metrics.total_journals == 1
    assert metrics.alias_count == 2
    assert metrics.semantic_links == 1
    assert metrics.semantic_cache_mb > 0.0
    assert metrics.context_acceleration == round(100 / 3, 1)
    assert metrics.status == "online"


def test_compute_graph_analytics_subtracts_agent_ledger(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Human.md").write_text("- links [[Other]] and [[Third]]\n", encoding="utf-8")
    (pages / "Other.md").write_text("- note\n", encoding="utf-8")
    (pages / "Third.md").write_text("created-by:: plumber\n- agent page\n", encoding="utf-8")

    metrics = compute_graph_analytics(tmp_path, ai_links_injected=1, ai_blocks_healed=4)

    assert metrics.total_pages == 3
    assert metrics.ai_pages == 1
    assert metrics.human_pages == 2
    assert metrics.total_links == 2
    assert metrics.human_links == 1
    assert metrics.ai_links == 1
    assert metrics.ai_blocks_healed == 4


def test_compute_graph_analytics_counts_versioned_ai_pages(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Legacy.md").write_text("created-by:: plumber\n- legacy\n", encoding="utf-8")
    (pages / "Versioned.md").write_text(
        "made-by:: matryca plumber v1.5.0\n- versioned\n",
        encoding="utf-8",
    )
    (pages / "Human.md").write_text("- human\n", encoding="utf-8")

    metrics = compute_graph_analytics(tmp_path)

    assert metrics.total_pages == 3
    assert metrics.ai_pages == 2
    assert metrics.human_pages == 1


def test_compute_graph_analytics_reflects_deleted_ai_page(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    ai_page = pages / "AgentPage.md"
    ai_page.write_text("created-by:: plumber\n- concept\n", encoding="utf-8")
    (pages / "Human.md").write_text("- note\n", encoding="utf-8")

    before = compute_graph_analytics(tmp_path)
    assert before.ai_pages == 1
    assert before.human_pages == 1

    ai_page.unlink()
    after = compute_graph_analytics(tmp_path)
    assert after.total_pages == 1
    assert after.ai_pages == 0
    assert after.human_pages == 1

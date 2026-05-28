"""Tests for RAM budget hooks and Phase 1 teardown."""

from __future__ import annotations

from pathlib import Path

from src.agent.memory_budget import release_phase1_memory, snapshot
from src.graph.generational_cache import get_cached_bm25_corpus, release_bm25_corpus
from src.graph.master_catalog import load_master_catalog, unload_master_catalog


def test_release_phase1_memory_clears_bm25_cache(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "A.md").write_text("- alpha beta gamma\n", encoding="utf-8")
    corpus = get_cached_bm25_corpus(tmp_path)
    assert corpus.n_docs >= 1
    release_phase1_memory(tmp_path)
    release_bm25_corpus(tmp_path)
    from src.graph.generational_cache import _bm25_cache

    assert str(tmp_path.resolve()) not in _bm25_cache


def test_unload_master_catalog(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "A.md").write_text("- x\n", encoding="utf-8")
    load_master_catalog(tmp_path)
    assert unload_master_catalog(tmp_path) is True
    assert unload_master_catalog(tmp_path) is False


def test_memory_snapshot_returns_positive_rss() -> None:
    snap = snapshot()
    assert snap.rss_bytes > 0
    assert snap.budget_mb >= 512

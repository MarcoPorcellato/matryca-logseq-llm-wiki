"""Semantic inference cache with cross-process flock."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from src.agent.plumber_modules import semantic_cache_router as router
from src.agent.plumber_modules.semantic_cache_router import (
    cache_get,
    cache_put,
    clear_semantic_cache,
    semantic_cache_key,
)


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    root = tmp_path / "graph"
    (root / "pages").mkdir(parents=True)
    return root


def test_semantic_cache_round_trip(graph_root: Path) -> None:
    clear_semantic_cache(graph_root)
    page = graph_root / "pages" / "Note.md"
    page.write_text("- note\n", encoding="utf-8")
    key = semantic_cache_key(page, "semantic_index")
    cache_put(graph_root, "index", key, {"summary": "cached"})
    hit = cache_get(graph_root, "index", key)
    assert hit == {"summary": "cached"}


def test_semantic_cache_miss_after_ttl_expired(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_semantic_cache(graph_root)
    page = graph_root / "pages" / "TTL.md"
    page.write_text("- ttl\n", encoding="utf-8")
    monkeypatch.setenv("MATRYCA_LINT_SEMANTIC_CACHE_TTL", "1")
    key = semantic_cache_key(page, "semantic_index")
    cache_put(graph_root, "index", key, {"summary": "old"}, ttl_seconds=1)
    time.sleep(1.1)
    assert cache_get(graph_root, "index", key) is None


def test_semantic_cache_memory_lru_eviction(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_semantic_cache(graph_root)
    monkeypatch.setattr(router, "_memory_max_entries", lambda: 2)
    page = graph_root / "pages" / "A.md"
    page.write_text("- a\n", encoding="utf-8")
    for index in range(3):
        key = f"op{index}:{page.name}:1"
        cache_put(graph_root, "index", key, {"n": index})
    with router._lock:
        assert len(router._memory) <= 2

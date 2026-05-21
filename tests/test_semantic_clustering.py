"""Structural tests for deterministic semantic clustering."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.graph.master_catalog import clear_master_catalog_cache
from src.graph.semantic_clustering import (
    DEFAULT_MAX_CLUSTER_SIZE,
    LOUVAIN_MAX_ITERATIONS,
    MIN_CLUSTER_SIZE,
    _louvain_communities,
    _tokenize,
    compute_semantic_clusters,
    format_cluster_neighborhood,
    load_or_compute_semantic_clusters,
    load_semantic_clusters,
    save_semantic_clusters,
    semantic_clusters_path,
)


def _mock_catalog(page_count: int) -> dict[str, object]:
    """Build a synthetic catalog with topical neighborhoods."""
    topics = (
        "machine learning",
        "distributed systems",
        "graph databases",
        "note taking",
        "project management",
        "security engineering",
        "frontend react",
        "backend python",
        "devops kubernetes",
        "data pipelines",
    )
    pages: dict[str, dict[str, object]] = {}
    for index in range(page_count):
        topic = topics[index % len(topics)]
        variant = index // len(topics)
        title = f"{topic.replace(' ', '-').title()} Page {variant:03d}"
        pages[title] = {
            "summary": f"A focused note about {topic} pattern {variant % 7}.",
            "domain": "risorsa",
            "tags": [topic.split()[0], topic.split()[-1], f"batch-{variant % 5}"],
            "last_mtime": 1_700_000_000 + index,
            "orphan": False,
        }
    return {
        "version": 1,
        "updated_at": "2026-05-21T12:00:00+00:00",
        "pages": pages,
    }


def test_compute_semantic_clusters_partitions_catalog() -> None:
    catalog = _mock_catalog(120)
    clusters = compute_semantic_clusters(catalog, max_cluster_size=35)
    titles = [title for page_titles in clusters.values() for title in page_titles]
    assert len(titles) == 120
    assert len(set(titles)) == 120
    assert all(5 <= len(page_titles) <= 35 for page_titles in clusters.values())


def test_compute_semantic_clusters_scales_to_three_thousand_pages() -> None:
    catalog = _mock_catalog(3000)
    started = time.perf_counter()
    clusters = compute_semantic_clusters(catalog, max_cluster_size=DEFAULT_MAX_CLUSTER_SIZE)
    elapsed = time.perf_counter() - started

    titles = [title for page_titles in clusters.values() for title in page_titles]
    sizes = [len(page_titles) for page_titles in clusters.values()]

    assert len(titles) == 3000
    assert len(set(titles)) == 3000
    assert all(MIN_CLUSTER_SIZE <= size <= DEFAULT_MAX_CLUSTER_SIZE for size in sizes)
    assert elapsed < 1.0, f"clustering took {elapsed:.3f}s, expected sub-second"


def test_save_and_load_semantic_clusters(tmp_path: Path) -> None:
    clear_master_catalog_cache(tmp_path)
    clusters = {
        "cluster_001": ["Alpha", "Beta"],
        "cluster_002": ["Gamma", "Delta", "Epsilon"],
    }
    save_semantic_clusters(
        tmp_path,
        clusters,
        catalog_updated_at="2026-05-21T12:00:00+00:00",
    )
    path = semantic_clusters_path(tmp_path)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["clusters"]["cluster_001"] == ["Alpha", "Beta"]
    loaded = load_semantic_clusters(tmp_path)
    assert loaded == clusters


def test_load_or_compute_semantic_clusters_uses_cache(tmp_path: Path) -> None:
    clear_master_catalog_cache(tmp_path)
    catalog = _mock_catalog(40)
    cache_dir = tmp_path / ".matryca_semantic_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "master_catalog.json").write_text(
        json.dumps(catalog, indent=2),
        encoding="utf-8",
    )

    first = load_or_compute_semantic_clusters(tmp_path, max_cluster_size=35)
    second = load_or_compute_semantic_clusters(tmp_path, max_cluster_size=35)
    assert first == second
    assert semantic_clusters_path(tmp_path).is_file()


def test_load_or_compute_force_recompute(tmp_path: Path) -> None:
    clear_master_catalog_cache(tmp_path)
    catalog = _mock_catalog(25)
    pages = catalog["pages"]
    assert isinstance(pages, dict)
    save_semantic_clusters(
        tmp_path,
        {"cluster_001": list(pages.keys())[:25]},
        catalog_updated_at="stale",
    )
    recomputed = load_or_compute_semantic_clusters(
        tmp_path,
        catalog_data=catalog,
        force_recompute=True,
    )
    assert len(recomputed) >= 1
    assert sum(len(titles) for titles in recomputed.values()) == 25


def test_empty_catalog_returns_empty_clusters() -> None:
    assert compute_semantic_clusters({"pages": {}}) == {}


def test_tokenize_filters_structural_stopwords() -> None:
    tokens = _tokenize("Questa pagina Logseq descrive Redis caching")
    assert "questa" not in tokens
    assert "pagina" not in tokens
    assert "logseq" not in tokens
    assert "redis" in tokens
    assert "caching" in tokens


def test_louvain_communities_respects_iteration_ceiling() -> None:
    adjacency = {
        "a": {"b": 1.0, "c": 1.0},
        "b": {"a": 1.0, "c": 1.0},
        "c": {"a": 1.0, "b": 1.0},
    }
    assignments = _louvain_communities(adjacency, max_iterations=LOUVAIN_MAX_ITERATIONS)
    assert set(assignments) == {"a", "b", "c"}


def test_format_cluster_neighborhood_marks_hub_anchor() -> None:
    catalog: dict[str, object] = {
        "pages": {
            "Alpha": {
                "summary": "Core redis architecture overview",
                "tags": ["redis", "core"],
            },
            "Beta": {
                "summary": "Redis cache eviction policy",
                "tags": ["redis", "cache"],
            },
            "Gamma": {
                "summary": "Unrelated kubernetes scheduling note",
                "tags": ["kubernetes", "ops"],
            },
        },
    }
    rendered = format_cluster_neighborhood(catalog, ["Alpha", "Beta", "Gamma"])
    assert "[CLUSTER FOCUS ANCHOR NODE]" in rendered
    assert rendered.count("[CLUSTER FOCUS ANCHOR NODE]") == 1

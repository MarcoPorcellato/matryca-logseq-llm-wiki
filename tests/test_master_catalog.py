"""Tests for master catalog, bootstrap harvest, and graph insights engine."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from src.agent.plumber_llm import BootstrapSummaryResult, GraphInsightsLLMResult
from src.graph.bootstrap_harvest import (
    harvest_page_into_catalog,
    run_bootstrap_harvest,
    run_incremental_catalog_refresh,
)
from src.graph.insights_engine import (
    compute_topology_metrics,
    format_graph_insights_markdown,
    run_graph_insights_engine,
)
from src.graph.master_catalog import (
    SEMANTIC_INDEX_HEADER,
    CatalogEntry,
    MasterCatalog,
    build_master_index_markdown,
    clear_master_catalog_cache,
    extract_catalog_fields_from_content,
    is_bootstrap_catalog_complete,
    load_master_catalog,
    master_index_page_path,
)
from src.graph.page_write_lock import clear_page_write_locks

BLOCK_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class StubHarvestLLM:
    """Deterministic bootstrap harvest LLM stub."""

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> BootstrapSummaryResult:
        _ = (page_path, graph_root, content)
        return BootstrapSummaryResult(
            summary=f"Harvested summary for {page_title}",
            suggested_tags=["harvest", "test"],
            domain="risorsa",
        )


class StubInsightsLLM:
    """Deterministic insights LLM stub."""

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult:
        _ = (metrics_json, graph_root)
        return GraphInsightsLLMResult(
            ontology_report="The graph reveals a compact test cluster around harvest tags.",
            cleanup_suggestions=[
                "Consider aliasing Alpha and Beta due to semantic overlap.",
            ],
        )


def _write_page(graph_root: Path, title: str, body: str) -> Path:
    pages = graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    safe = title.replace("/", "___")
    path = pages / f"{safe}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _indexed_body(
    *,
    summary: str = "Existing summary.",
    tags: str = "#alpha #beta",
    domain: str = "risorsa",
) -> str:
    return (
        f"- type:: {domain}\n"
        f"- First bullet\n  id:: {BLOCK_UUID}\n"
        f"\n{SEMANTIC_INDEX_HEADER}\n"
        f"- indexed-at:: 2026-01-01 00:00 UTC\n"
        f"- summary:: {summary}\n"
        f"- suggested-tags:: {tags}\n"
    )


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    clear_page_write_locks()
    clear_master_catalog_cache()
    return tmp_path


def test_catalog_entry_roundtrip() -> None:
    entry = CatalogEntry(
        summary="One sentence.",
        domain="area",
        tags=["foo", "bar"],
        last_mtime=123,
        orphan=True,
    )
    payload = entry.to_json()
    restored = CatalogEntry.from_json(payload)
    assert restored.summary == "One sentence."
    assert restored.domain == "area"
    assert restored.tags == ["foo", "bar"]
    assert restored.last_mtime == 123
    assert restored.orphan is True


def test_extract_catalog_fields_from_semantic_index() -> None:
    content = _indexed_body(summary="Parsed summary.", tags="#one #two", domain="progetto")
    extracted = extract_catalog_fields_from_content(content)
    assert extracted is not None
    assert extracted.summary == "Parsed summary."
    assert extracted.tags == ["one", "two"]
    assert extracted.domain == "progetto"


def test_load_master_catalog_self_heals_corrupt_json(graph_root: Path) -> None:
    clear_master_catalog_cache(graph_root)
    path = MasterCatalog.catalog_path(graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    catalog = load_master_catalog(graph_root, force_reload=True)
    assert catalog.pages == {}


def test_load_and_save_master_catalog(graph_root: Path) -> None:
    catalog = load_master_catalog(graph_root)
    catalog.upsert(
        "Demo",
        CatalogEntry(
            summary="Demo page",
            domain="mappa",
            tags=["demo"],
            last_mtime=100,
            orphan=False,
        ),
    )
    catalog.save()

    path = MasterCatalog.catalog_path(graph_root)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pages"]["Demo"]["summary"] == "Demo page"

    reloaded = load_master_catalog(graph_root, force_reload=True)
    assert reloaded.pages["Demo"].domain == "mappa"


def test_needs_refresh_detects_mtime_drift(graph_root: Path) -> None:
    path = _write_page(graph_root, "Fresh", _indexed_body())
    catalog = load_master_catalog(graph_root)
    catalog.upsert(
        "Fresh",
        CatalogEntry(summary="Old", domain="", tags=[], last_mtime=1, orphan=False),
    )
    assert catalog.needs_refresh("Fresh", path.stat().st_mtime_ns) is True


def test_build_master_index_groups_by_domain_with_collapsed(graph_root: Path) -> None:
    catalog = MasterCatalog(graph_root=graph_root)
    catalog.upsert(
        "Area/Topic",
        CatalogEntry(summary="Operational note.", domain="area", tags=[], last_mtime=1),
    )
    catalog.upsert(
        "Vision",
        CatalogEntry(summary="North star.", domain="mappa", tags=[], last_mtime=1),
    )
    md = build_master_index_markdown(catalog)
    assert "collapsed:: true" in md
    assert "[[Vision]] — North star." in md
    assert "[[Area/Topic]] — Operational note." in md
    assert "Mappa — strategic vision" in md


def test_bootstrap_harvest_regex_path_without_llm(graph_root: Path) -> None:
    _write_page(graph_root, "Indexed", _indexed_body(summary="Already indexed."))
    metrics = run_bootstrap_harvest(
        graph_root,
        llm=None,
        incremental=False,
        phase1_strict=True,
    )
    assert metrics.regex_harvested == 1
    assert metrics.llm_harvested == 0
    assert metrics.index_rebuilt is True
    assert metrics.files_created == 0

    catalog = load_master_catalog(graph_root, force_reload=True)
    assert catalog.pages["Indexed"].summary == "Already indexed."

    master_index = master_index_page_path(graph_root)
    assert master_index.is_file()
    assert "[[Indexed]]" in master_index.read_text(encoding="utf-8")


def test_is_bootstrap_catalog_complete_requires_master_index_and_rows(
    graph_root: Path,
) -> None:
    assert is_bootstrap_catalog_complete(graph_root) is False
    _write_page(graph_root, "Ready", _indexed_body(summary="Ready summary."))
    run_bootstrap_harvest(graph_root, llm=None, incremental=False, phase1_strict=True)
    assert is_bootstrap_catalog_complete(graph_root) is True


def test_bootstrap_harvest_llm_path_writes_index_and_catalog(graph_root: Path) -> None:
    _write_page(graph_root, "Needs Index", "- type:: risorsa\n- Body content\n")
    metrics = run_bootstrap_harvest(
        graph_root,
        llm=StubHarvestLLM(),
        incremental=False,
        phase1_strict=True,
    )
    assert metrics.llm_harvested == 1
    assert metrics.files_created == 0

    page_path = graph_root / "pages" / "Needs Index.md"
    text = page_path.read_text(encoding="utf-8")
    assert SEMANTIC_INDEX_HEADER in text
    assert "Harvested summary for Needs Index" in text

    catalog = load_master_catalog(graph_root, force_reload=True)
    assert catalog.pages["Needs Index"].summary.startswith("Harvested summary")


def test_incremental_refresh_only_touches_stale_pages(graph_root: Path) -> None:
    indexed = _write_page(graph_root, "Stable", _indexed_body(summary="Stable summary."))
    run_bootstrap_harvest(graph_root, llm=None, incremental=False)

    time.sleep(0.02)
    _write_page(graph_root, "Changed", _indexed_body(summary="Changed summary."))

    metrics = run_incremental_catalog_refresh(graph_root, llm=None)
    assert metrics.scanned >= 1
    catalog = load_master_catalog(graph_root, force_reload=True)
    assert catalog.pages["Changed"].summary == "Changed summary."
    assert catalog.pages["Stable"].summary == "Stable summary."
    assert catalog.pages["Stable"].last_mtime == int(indexed.stat().st_mtime)


def test_compute_topology_metrics_orphans_and_clusters(graph_root: Path) -> None:
    _write_page(
        graph_root,
        "Hub",
        "- Links to [[Orphan A]] and [[Alpha]]\n",
    )
    _write_page(
        graph_root,
        "Alpha",
        _indexed_body(summary="Alpha page.", tags="#cluster #shared", domain="risorsa"),
    )
    _write_page(
        graph_root,
        "Beta",
        _indexed_body(summary="Beta page.", tags="#cluster #shared #extra", domain="risorsa"),
    )
    _write_page(
        graph_root,
        "Orphan A",
        _indexed_body(summary="Lonely page.", tags="#cluster #shared", domain="archivio"),
    )

    catalog = load_master_catalog(graph_root)
    for title in ("Hub", "Alpha", "Beta", "Orphan A"):
        catalog.upsert(
            title,
            CatalogEntry(summary=title, domain="risorsa", tags=["cluster"], last_mtime=1),
        )

    metrics = compute_topology_metrics(graph_root, catalog)
    assert metrics.page_count == 4
    assert "Hub" in metrics.orphan_pages
    assert "Beta" in metrics.orphan_pages
    assert "Orphan A" not in metrics.orphan_pages
    assert metrics.tag_clusters


def test_run_graph_insights_engine_writes_dashboard(graph_root: Path) -> None:
    _write_page(graph_root, "Demo", _indexed_body(summary="Demo summary."))
    run_bootstrap_harvest(graph_root, llm=None, incremental=False)

    result = run_graph_insights_engine(graph_root, llm=StubInsightsLLM())
    assert result.output_path.is_file()
    text = result.output_path.read_text(encoding="utf-8")
    assert "## 🧠 Implicit Ontology Report" in text
    assert "## 🧹 Structural Cleanup Suggestions" in text
    assert "#todo [[Matryca Cleanup Opportunity]]" in text
    assert result.llm_used is True


def test_format_graph_insights_fallback_markdown() -> None:
    from src.graph.insights_engine import TopologyMetrics, _fallback_insights

    metrics = TopologyMetrics(page_count=3, orphan_pages=["Lonely"], catalog_coverage=66.7)
    llm_result = _fallback_insights(metrics)
    md = format_graph_insights_markdown(metrics, llm_result)
    assert "Topology Snapshot" in md
    assert "Lonely" in md or "orphan" in md.lower()


def test_harvest_page_into_catalog_skips_empty_body(graph_root: Path) -> None:
    path = _write_page(graph_root, "Empty", "   \n")
    catalog = load_master_catalog(graph_root)
    status, changed = harvest_page_into_catalog(graph_root, catalog, path, llm=None)
    assert status == "skipped_empty"
    assert changed is False

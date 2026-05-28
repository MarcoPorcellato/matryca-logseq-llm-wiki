"""Tests for hierarchical MapReduce outliner summarization."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from src.agent.plumber_config import PlumberLintConfig
from src.agent.plumber_llm import BootstrapSummaryResult
from src.graph.bootstrap_harvest import harvest_page_into_catalog
from src.graph.hierarchical_summarization import (
    chunk_outliner_content,
    mapreduce_harvest_page_summary,
)
from src.graph.master_catalog import load_master_catalog


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    root = tmp_path / "graph"
    (root / "pages").mkdir(parents=True)
    return root


def test_chunk_outliner_content_preserves_root_subtrees() -> None:
    content = "- Root A\n  - child A1\n  - child A2\n- Root B\n  - child B1\n"
    chunks = chunk_outliner_content(content, max_chunk_chars=40)
    assert len(chunks) >= 2
    assert "child A1" in chunks[0]
    assert "child A2" in chunks[0]
    assert "Root B" in chunks[-1]
    assert "child B1" in chunks[-1]
    assert "child A1" not in chunks[-1]


def test_chunk_outliner_content_keeps_oversized_tree_atomic() -> None:
    tree = "- Root\n" + "  - child line\n" * 500
    chunks = chunk_outliner_content(tree, max_chunk_chars=100)
    assert len(chunks) == 1
    assert chunks[0] == tree


def test_mapreduce_harvest_uses_single_pass_below_trigger() -> None:
    calls: list[str] = []

    class SinglePassLLM:
        def harvest_page_summary(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            task_instruction: str | None = None,
        ) -> BootstrapSummaryResult:
            _ = (page_path, graph_root, task_instruction)
            calls.append(content)
            return BootstrapSummaryResult(summary=f"Summary for {page_title}")

    content = "- Small page\n  - one child\n"
    config = PlumberLintConfig(mapreduce_trigger_chars=10_000)
    result = mapreduce_harvest_page_summary(
        SinglePassLLM(),
        page_title="Small",
        content=content,
        config=config,
    )
    assert result.summary == "Summary for Small"
    assert calls == [content]


def test_mapreduce_harvest_splits_and_reduces_above_trigger() -> None:
    calls: list[tuple[str, str | None]] = []

    class MapReduceLLM:
        def harvest_page_summary(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            task_instruction: str | None = None,
        ) -> BootstrapSummaryResult:
            _ = (page_path, graph_root)
            calls.append((content, task_instruction))
            if task_instruction and "MapReduce consolidation task" in task_instruction:
                return BootstrapSummaryResult(
                    summary="Unified giant page summary.",
                    suggested_tags=["activity", "fbu"],
                    domain="progetto",
                )
            return BootstrapSummaryResult(
                summary=f"Partial for chunk {len(calls)}",
                suggested_tags=[f"tag{len(calls)}"],
                domain="",
            )

    root_a = "- Root A\n" + "  - child\n" * 200
    root_b = "- Root B\n" + "  - child\n" * 200
    content = root_a + root_b
    config = PlumberLintConfig(mapreduce_trigger_chars=500, mapreduce_chunk_chars=400)
    result = mapreduce_harvest_page_summary(
        MapReduceLLM(),
        page_title="Giant Log",
        content=content,
        config=config,
    )
    assert result.summary == "Unified giant page summary."
    assert result.suggested_tags == ["activity", "fbu"]
    assert len(calls) >= 3
    assert calls[-1][1] is not None
    assert "MapReduce consolidation task" in calls[-1][1]


def test_mapreduce_thermal_pause_after_each_harvest_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_THERMAL_DELAY_BOOTSTRAP", "1.5")
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    calls: list[str] = []

    class MapReduceLLM:
        def harvest_page_summary(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            task_instruction: str | None = None,
        ) -> BootstrapSummaryResult:
            _ = (page_title, page_path, graph_root)
            calls.append(content[:40])
            if task_instruction and "MapReduce consolidation task" in task_instruction:
                return BootstrapSummaryResult(summary="Unified summary.")
            return BootstrapSummaryResult(summary="Partial.")

    root_a = "- Root A\n" + "  - child\n" * 200
    root_b = "- Root B\n" + "  - child\n" * 200
    content = root_a + root_b
    config = PlumberLintConfig(
        mapreduce_trigger_chars=500,
        mapreduce_chunk_chars=400,
        thermal_delay_bootstrap=1.5,
    )
    mapreduce_harvest_page_summary(
        MapReduceLLM(),
        page_title="Giant",
        content=content,
        config=config,
    )
    assert len(calls) >= 3
    assert len(sleeps) == len(calls)
    assert all(delay == 1.5 for delay in sleeps)


def test_harvest_page_into_catalog_mapreduce_integration(graph_root: Path) -> None:
    calls: list[int] = []

    class TrackingLLM:
        def harvest_page_summary(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            task_instruction: str | None = None,
        ) -> BootstrapSummaryResult:
            _ = (page_title, page_path, graph_root)
            calls.append(len(content))
            if task_instruction and "MapReduce consolidation task" in task_instruction:
                return BootstrapSummaryResult(
                    summary="Consolidated activity log.",
                    suggested_tags=["fbu"],
                )
            return BootstrapSummaryResult(summary="Chunk summary.", suggested_tags=["chunk"])

    pages = graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    body = (
        "- Activity root A\n"
        + "  - event line with details\n" * 200
        + "- Activity root B\n"
        + "  - event line with details\n" * 200
    )
    path = pages / "Giant.md"
    path.write_text(body, encoding="utf-8")

    catalog = load_master_catalog(graph_root)
    config = PlumberLintConfig(mapreduce_trigger_chars=2_000, mapreduce_chunk_chars=800)
    status, changed, llm_called = harvest_page_into_catalog(
        graph_root,
        catalog,
        path,
        llm=TrackingLLM(),
        config=config,
    )
    assert status == "llm"
    assert changed is True
    assert llm_called is True
    assert len(calls) >= 3
    entry = catalog.get("Giant")
    assert entry is not None
    assert entry.summary == "Consolidated activity log."

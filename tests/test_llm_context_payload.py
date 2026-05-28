"""Tests for LLM context payload reduction and cache-aligned prompt layout."""

from __future__ import annotations

from pathlib import Path

from src.agent.llm_context_payload import (
    extract_semantic_skeleton,
    prepare_llm_context_payload,
)
from src.agent.plumber_config import PlumberLintConfig
from src.agent.plumber_modules.marpa_framework import build_marpa_classify_user_prompt
from src.agent.prompt_layout import build_cache_aligned_prompt
from src.graph.master_catalog import CatalogEntry, MasterCatalog


def test_build_cache_aligned_prompt_puts_content_first() -> None:
    prompt = build_cache_aligned_prompt(
        content="Heavy page body with [[Links]]",
        task_instruction="Task: classify this page.\nPage title: Demo",
    )
    content_pos = prompt.index("Heavy page body")
    task_pos = prompt.index("Task: classify")
    assert content_pos < task_pos
    assert prompt.startswith("Page content:\n")


def test_harvest_prompt_layout_content_before_task() -> None:
    from src.agent.prompt_layout import build_cache_aligned_prompt

    prompt = build_cache_aligned_prompt(
        content="Body text for bootstrap",
        task_instruction="Task: extract summary.\nPage title: Demo",
    )
    assert prompt.index("Body text") < prompt.index("Task: extract")


def test_build_marpa_classify_user_prompt_is_cache_aligned() -> None:
    prompt = build_marpa_classify_user_prompt(
        "Demo",
        "- [[Alpha]] note\n",
        namespace_hint="area",
    )
    assert prompt.startswith("Page content:\n")
    assert "Task: classify the page content above" in prompt
    assert prompt.index("Page content:") < prompt.index("Task:")


def test_extract_semantic_skeleton_strips_prose() -> None:
    raw = (
        "- This is conversational prose without structure.\n"
        "- See [[Project X]] for #planning details.\n"
        "status:: active\n"
        "- Another plain sentence.\n"
    )
    skeleton = extract_semantic_skeleton(raw)
    assert "[[Project X]]" in skeleton
    assert "#planning" in skeleton
    assert "status:: active" in skeleton
    assert "conversational prose" not in skeleton
    assert "plain sentence" not in skeleton


def test_prepare_llm_context_payload_uses_raw_for_small_pages(tmp_path: Path) -> None:
    graph_root = tmp_path
    content = "- small page\n"
    payload, source = prepare_llm_context_payload(
        graph_root,
        "Small",
        content,
        config=PlumberLintConfig(mapreduce_trigger_chars=1000),
    )
    assert payload == content
    assert source == "raw"


def test_prepare_llm_context_payload_uses_phase1_summary(tmp_path: Path) -> None:
    graph_root = tmp_path
    catalog = MasterCatalog(graph_root=graph_root)
    catalog.upsert(
        "Giant",
        CatalogEntry(summary="Dense hierarchical abstract from Phase 1.", domain="risorsa"),
    )
    catalog.save()

    giant = "x" * 5000
    payload, source = prepare_llm_context_payload(
        graph_root,
        "Giant",
        giant,
        config=PlumberLintConfig(mapreduce_trigger_chars=1000),
    )
    assert source == "summary"
    assert "Dense hierarchical abstract" in payload
    assert "domain:: risorsa" in payload


def test_prepare_llm_context_payload_falls_back_to_skeleton(tmp_path: Path) -> None:
    graph_root = tmp_path
    giant = (
        "- " + ("word " * 200) + "\n- linked to [[Target]] with #tag\n" + ("- filler line\n" * 300)
    )
    payload, source = prepare_llm_context_payload(
        graph_root,
        "NoSummary",
        giant,
        config=PlumberLintConfig(mapreduce_trigger_chars=1000),
    )
    assert source == "skeleton"
    assert "[[Target]]" in payload
    assert len(payload) < len(giant)

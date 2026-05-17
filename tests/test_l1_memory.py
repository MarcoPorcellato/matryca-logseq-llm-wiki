"""Tests for L1 memory path resolution and bounded reads."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.agent.l1_memory import (
    collect_l1_markdown_paths,
    read_l1_memory_async,
    read_l1_memory_text,
)
from src.config import MatrycaWikiConfig


def test_collect_l1_single_file(tmp_path: Path) -> None:
    """``MATRYCA_L1_PATH`` pointing at a file returns that file only."""
    f = tmp_path / "rules.md"
    f.write_text("x", encoding="utf-8")
    paths = collect_l1_markdown_paths(matryca_l1_path=str(f), logseq_graph_path="")
    assert paths == [f]


def test_collect_l1_directory_sorted(tmp_path: Path) -> None:
    """Directory mode collects ``*.md`` sorted by name."""
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("n", encoding="utf-8")
    paths = collect_l1_markdown_paths(matryca_l1_path=str(tmp_path), logseq_graph_path="")
    assert [p.name for p in paths] == ["a.md", "b.md"]


def test_collect_l1_fallback_next_to_graph(tmp_path: Path) -> None:
    """When ``MATRYCA_L1_PATH`` is unset, use ``<graph>/../matryca-l1/*.md``."""
    graph = tmp_path / "vault"
    l1 = tmp_path / "matryca-l1"
    graph.mkdir()
    l1.mkdir()
    (l1 / "z.md").write_text("z", encoding="utf-8")
    paths = collect_l1_markdown_paths(matryca_l1_path="", logseq_graph_path=str(graph))
    assert paths == [l1 / "z.md"]


def test_read_l1_memory_text_truncates_per_file(tmp_path: Path) -> None:
    """Per-file cap truncates content and still returns labels."""
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 70_000)
    labels, body = read_l1_memory_text([big], max_bytes_per_file=10_000, max_bytes_total=200_000)
    assert labels == ["big.md"]
    assert "truncated" in body.lower() or len(body) < 70_000


@pytest.mark.asyncio
async def test_read_l1_memory_async_uses_yaml_memory_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MATRYCA_L1_PATH", raising=False)
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    note = tmp_path / "from-yaml.md"
    note.write_text("yaml l1", encoding="utf-8")
    cfg = MatrycaWikiConfig(memory_path=str(note))
    labels, body = await read_l1_memory_async(cfg)
    assert labels == ["from-yaml.md"]
    assert "yaml l1" in body


@pytest.mark.asyncio
async def test_read_l1_memory_async_uses_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Async reader honors ``MATRYCA_L1_PATH`` via the process environment."""
    monkeypatch.delenv("MATRYCA_L1_PATH", raising=False)
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    note = tmp_path / "note.md"
    note.write_text("session rule", encoding="utf-8")
    monkeypatch.setenv("MATRYCA_L1_PATH", str(note))
    labels, body = await read_l1_memory_async()
    assert labels == ["note.md"]
    assert "session rule" in body

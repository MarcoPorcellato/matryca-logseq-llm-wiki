"""Tests for ``matryca-wiki.yml`` loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.config import MatrycaWikiConfig, load_matryca_wiki_config


def test_load_matryca_wiki_config_from_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "custom.yml"
    cfg_path.write_text(
        "namespaces: [A, B]\nmemory_path: /tmp/x\nwiki_file_prefix: Wiki___\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    monkeypatch.setenv("MATRYCA_WIKI_CONFIG", str(cfg_path))
    cfg = load_matryca_wiki_config()
    assert cfg.namespaces == ["A", "B"]
    assert cfg.memory_path == "/tmp/x"
    assert cfg.wiki_file_prefix == "Wiki___"


def test_load_matryca_wiki_config_beside_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g"
    graph.mkdir()
    (graph / "matryca-wiki.yml").write_text(
        "namespaces: [Wiki]\nmax_depth: 2\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MATRYCA_WIKI_CONFIG", raising=False)
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(graph))
    cfg = load_matryca_wiki_config()
    assert cfg.namespaces == ["Wiki"]
    assert cfg.max_depth == 2


def test_matryca_wiki_config_defaults() -> None:
    cfg = MatrycaWikiConfig()
    assert cfg.namespaces == []
    assert cfg.max_depth == 3

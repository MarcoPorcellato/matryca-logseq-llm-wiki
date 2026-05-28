"""Slow memory soak tests (nightly / ``pytest -m slow``)."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest
from src.agent.memory_budget import snapshot
from src.graph.bootstrap_harvest import run_bootstrap_harvest


@pytest.mark.slow
def test_bootstrap_200_pages_memory_bounded(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    for i in range(200):
        (pages / f"Page{i:04d}.md").write_text(f"- item {i} #tag\n", encoding="utf-8")
    tracemalloc.start()
    before = snapshot()
    metrics = run_bootstrap_harvest(tmp_path, llm=None, incremental=False, rebuild_index=False)
    _peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    after = snapshot()
    assert metrics.scanned == 200
    assert after.rss_mb <= before.rss_mb + 250

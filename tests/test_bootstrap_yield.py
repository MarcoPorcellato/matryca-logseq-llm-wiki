"""Tests for cooperative yield during bootstrap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.graph.bootstrap_harvest import run_bootstrap_harvest
from src.graph.master_catalog import load_master_catalog


def test_bootstrap_harvest_calls_yield_host(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    for i in range(30):
        (pages / f"Page{i}.md").write_text(f"- note {i}\n", encoding="utf-8")
    with patch("src.graph.bootstrap_harvest.yield_host") as mock_yield:
        metrics = run_bootstrap_harvest(tmp_path, llm=None, incremental=False, rebuild_index=False)
    assert metrics.scanned == 30
    assert mock_yield.call_count >= 1
    load_master_catalog(tmp_path, force_reload=True)

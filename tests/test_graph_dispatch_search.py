"""Integration tests for ``dispatch_search`` routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.agent.graph_dispatch import dispatch_search


@pytest.mark.asyncio
async def test_dispatch_search_resolve_entity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Acme Corp.md").write_text("alias:: ACME, Acme Corporation\n", encoding="utf-8")
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))

    out = await dispatch_search("resolve_entity", "ACME")
    assert isinstance(out, dict)
    assert out.get("matched") is True
    assert out.get("canonical_page_title") == "Acme Corp"

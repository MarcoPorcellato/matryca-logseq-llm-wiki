"""Tests for Logseq advanced-query fence assembly."""

from __future__ import annotations

import pytest
from src.graph.advanced_query_block import (
    resolve_advanced_query_preset,
    validate_advanced_query_edn,
    wrap_logseq_advanced_query,
)


def test_wrap_logseq_advanced_query_minimal() -> None:
    inner = '{:title [:h2 "X"] :query [:find ?a :where [?a :block/name _]]}'
    out = wrap_logseq_advanced_query(inner)
    assert out.startswith("#+BEGIN_QUERY\n")
    assert out.endswith("#+END_QUERY\n") or out.endswith("#+END_QUERY")


def test_validate_rejects_unbalanced() -> None:
    inner = "{:query [:find ?a "
    with pytest.raises(ValueError, match="bracket"):
        validate_advanced_query_edn(inner)


def test_validate_requires_query_key() -> None:
    with pytest.raises(ValueError, match=":query"):
        validate_advanced_query_edn('{:title "only"}')


def test_preset_open_markers_round_trip() -> None:
    inner = resolve_advanced_query_preset("open_markers")
    md = wrap_logseq_advanced_query(inner)
    assert "TODO" in md
    assert ":query" in md


def test_preset_pages_tagged_requires_tag() -> None:
    with pytest.raises(ValueError, match="tag"):
        resolve_advanced_query_preset("pages_tagged")

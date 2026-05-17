"""Tests for L1/L2 routing hint footer."""

from __future__ import annotations

from src.agent.routing_hint import append_read_page_routing_hint


def test_routing_hint_skips_error_messages() -> None:
    body = "LOGSEQ_GRAPH_PATH is not set in the environment"
    assert append_read_page_routing_hint(body) == body


def test_routing_hint_appends_for_normal_page() -> None:
    body = "# Spatial view\n\nSome content about deployment."
    out = append_read_page_routing_hint(body)
    assert "matryca_routing" in out
    assert "L1_candidate" in out or "L2_default" in out

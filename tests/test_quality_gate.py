"""Tests for outline security gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.agent.graph_dispatch import _headless_write_outline
from src.agent.quality_gate import advanced_query_security_violations, outline_security_violations


def test_outline_security_flags_token_property() -> None:
    outline = {
        "text": "x",
        "properties": {"token::": "abc"},
        "children": [],
    }
    assert outline_security_violations(outline)


def test_clean_outline_has_no_violations() -> None:
    outline = {"text": "Safe note", "properties": {"tags::": "[[x]]"}, "children": []}
    assert not outline_security_violations(outline)


def test_advanced_query_security_flags_sk_pattern() -> None:
    bad = '{:query [:find ?a :where [?a :block/content "sk-12345678901234567890123456789012"]]}'
    assert advanced_query_security_violations(bad)


def test_headless_write_entity_includes_alias_routing_hint(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    parent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    (pages / "Demo.md").write_text(
        f"- Root\n  id:: {parent_id}\n",
        encoding="utf-8",
    )
    outline = {
        "text": "Entity root",
        "page_type": "entity",
        "entity_type": "tool",
        "children": [],
    }
    out = _headless_write_outline(str(tmp_path), parent_id, outline)
    assert "resolve_entity" in out["routing_hint"]


def test_headless_write_rejects_outline_with_secret(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    parent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    (pages / "Demo.md").write_text(
        f"- Root\n  id:: {parent_id}\n",
        encoding="utf-8",
    )
    bad = {"text": "leak", "properties": {"password::": "x"}, "children": []}
    with pytest.raises(ValueError, match="credential-like"):
        _headless_write_outline(str(tmp_path), parent_id, bad)

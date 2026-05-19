"""Tests for MCP server models and outline validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from src.agent.mcp_server import (
    MatrycaMCPServer,
    OutlineNode,
    _format_regex_search_markdown,
    _parse_optional_json_query,
    _read_block_ast_markdown,
    register_mcp_tools,
)
from src.bridge.logseq_client import LogseqClient


def test_mcp_registers_five_mega_tools() -> None:
    """Consolidated MCP surface exposes exactly five tool names."""
    app = FastMCP("matryca-test")
    register_mcp_tools(app)
    names = sorted(app._tool_manager._tools.keys())  # noqa: SLF001
    assert names == [
        "mutate_graph",
        "read_graph_data",
        "refactor_blocks",
        "run_linter",
        "search_graph",
    ]


def test_parse_optional_json_query_accepts_plain_or_json() -> None:
    assert _parse_optional_json_query("") == {}
    assert _parse_optional_json_query("alpha") == {}
    assert _parse_optional_json_query('{"days": 3}') == {"days": 3}


def test_read_block_ast_markdown_returns_subtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    block_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (pages / "Demo.md").write_text(
        f"- Parent\n  id:: {block_id}\n  - Child bullet\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))
    md = _read_block_ast_markdown(str(tmp_path), f"Demo|{block_id}")
    assert "Child bullet" in md
    assert block_id in md


def test_format_regex_search_markdown_finds_line(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Hit.md").write_text("- TODO fix parser\n", encoding="utf-8")
    report = _format_regex_search_markdown(str(tmp_path), r"TODO", limit=10)
    assert "Hit.md" in report
    assert "TODO" in report


def test_outline_node_validates_nested_hierarchy() -> None:
    """``OutlineNode`` should accept nested JSON and preserve parent/child structure."""
    payload: dict[str, object] = {
        "text": "Root thesis",
        "properties": {"tags::": "[[AI]]"},
        "children": [
            {
                "text": "Supporting claim",
                "children": [{"text": "Evidence leaf", "properties": {}, "children": []}],
            },
        ],
    }
    node = OutlineNode.model_validate(payload)
    assert node.text == "Root thesis"
    assert len(node.children) == 1
    assert node.children[0].text == "Supporting claim"
    assert node.children[0].children[0].text == "Evidence leaf"


def test_outline_knowledge_requires_domain_when_typed() -> None:
    """Knowledge ``page_type`` without ``domain`` must fail validation."""
    with pytest.raises(ValidationError):
        OutlineNode.model_validate({"text": "Note", "page_type": "knowledge"})


def test_outline_entity_merges_entity_type_into_properties() -> None:
    """Entity nodes merge ``entity-type::`` for Logseq."""
    node = OutlineNode.model_validate(
        {
            "text": "Tool X",
            "page_type": "entity",
            "entity_type": "tool",
        },
    )
    assert node.properties.get("type::") == "entity"
    assert node.properties.get("entity-type::") == "tool"


def test_outline_child_without_schema_fields() -> None:
    """Nested bullets may omit schema fields when the parent carries classification."""
    node = OutlineNode.model_validate(
        {
            "text": "Root",
            "page_type": "knowledge",
            "domain": "tech",
            "children": [{"text": "Evidence", "properties": {"source::": "[[Paper]]"}}],
        },
    )
    assert node.children[0].properties.get("source::") == "[[Paper]]"


@pytest.mark.asyncio
async def test_write_logseq_outline_chains_parent_uuids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each child append must use the UUID returned from its parent's ``append_block``."""
    client = LogseqClient(api_url="http://127.0.0.1:9", token="test-token")
    calls: list[tuple[str, str]] = []

    async def fake_append(
        parent_uuid: str,
        content: str,
        properties: dict[str, str],
    ) -> str:
        calls.append((parent_uuid, content))
        return f"uuid-{len(calls)}"

    monkeypatch.setattr(client, "append_block", fake_append)

    server = MatrycaMCPServer(client=client)
    outline: dict[str, Any] = {
        "text": "Root",
        "children": [{"text": "Child", "children": [{"text": "Grandchild"}]}],
    }
    uuids = await server.write_logseq_outline(outline, parent_block_uuid="page-root")

    assert uuids["uuids"] == ["uuid-1", "uuid-2", "uuid-3"]
    assert "routing_hint" in uuids
    assert "L2" in uuids["routing_hint"]
    assert uuids.get("outline_block_count") == 3
    assert "git_snapshot" in uuids
    assert calls[0] == ("page-root", "Root")
    assert calls[1] == ("uuid-1", "Child")
    assert calls[2] == ("uuid-2", "Grandchild")

"""Tests for MCP server models and outline validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from src.agent.mcp_server import MatrycaMCPServer, OutlineNode
from src.bridge.logseq_client import LogseqClient


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
    assert calls[0] == ("page-root", "Root")
    assert calls[1] == ("uuid-1", "Child")
    assert calls[2] == ("uuid-2", "Grandchild")

"""Tests for outline security gate."""

from __future__ import annotations

import pytest
from src.agent.quality_gate import outline_security_violations


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


@pytest.mark.asyncio
async def test_write_rejects_outline_with_secret() -> None:
    from src.agent.mcp_server import MatrycaMCPServer
    from src.bridge.logseq_client import LogseqClient

    client = LogseqClient(api_url="http://127.0.0.1:9", token="t")
    server = MatrycaMCPServer(client=client)
    bad = {"text": "leak", "properties": {"password::": "x"}, "children": []}
    with pytest.raises(ValueError, match="credential-like"):
        await server.write_logseq_outline(bad, parent_block_uuid="root")

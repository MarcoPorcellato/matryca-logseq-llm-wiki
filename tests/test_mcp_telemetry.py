"""Tests for MCP client-visible telemetry bridge."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from src.agent import mcp_telemetry
from src.agent.mcp_telemetry import (
    install_loguru_mcp_bridge,
    mcp_tool_info,
    mcp_tool_session,
)


@pytest.mark.asyncio
async def test_mcp_tool_info_forwards_to_context() -> None:
    ctx = AsyncMock()
    await mcp_tool_info(ctx, "hello from matryca")
    ctx.info.assert_awaited_once_with("hello from matryca")


@pytest.mark.asyncio
async def test_mcp_tool_session_binds_context_for_loguru_bridge() -> None:
    install_loguru_mcp_bridge()
    ctx = AsyncMock()
    async with mcp_tool_session(ctx):
        assert mcp_telemetry._mcp_ctx.get() is ctx
        logger.info("bridged message")
        await asyncio.sleep(0)
    assert mcp_telemetry._mcp_ctx.get() is None
    ctx.info.assert_awaited()
    assert "bridged message" in ctx.info.await_args.args[0]


def test_install_loguru_mcp_bridge_is_idempotent() -> None:
    install_loguru_mcp_bridge()
    install_loguru_mcp_bridge()
    # No assertion beyond "does not raise"; sink registration is guarded internally.

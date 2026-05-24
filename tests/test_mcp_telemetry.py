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
    sanitize_log_message,
)


async def _await_mcp_log_bridge(ctx: AsyncMock, *, timeout: float = 2.0) -> None:
    """Wait until the loguru→MCP ``create_task`` bridge has invoked ``ctx.info``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while ctx.info.await_count == 0:
        if loop.time() >= deadline:
            pytest.fail("timed out waiting for MCP log bridge to forward loguru record")
        await asyncio.sleep(0.01)


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
        await _await_mcp_log_bridge(ctx)
    assert mcp_telemetry._mcp_ctx.get() is None
    ctx.info.assert_awaited()
    assert "bridged message" in ctx.info.await_args.args[0]


def test_sanitize_log_message_masks_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATRYCA_DEBUG", raising=False)
    raw = "block aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    censored = sanitize_log_message(raw)
    assert censored == "block [CENSORED_UUID]"


def test_sanitize_log_message_masks_content_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATRYCA_DEBUG", raising=False)
    censored = sanitize_log_message("payload: secret outline")
    assert censored == "payload: [REDACTED_CONTENT]"
    assert sanitize_log_message("query= hidden terms") == "query= [REDACTED_CONTENT]"


def test_sanitize_log_message_preserves_raw_when_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_DEBUG", "true")
    raw = "payload: keep-me aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert sanitize_log_message(raw) == raw


@pytest.mark.asyncio
async def test_mcp_tool_session_censors_bridged_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATRYCA_DEBUG", raising=False)
    install_loguru_mcp_bridge()
    ctx = AsyncMock()
    uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    async with mcp_tool_session(ctx):
        logger.info("payload: top secret {}", uuid)
        await _await_mcp_log_bridge(ctx)
    forwarded = ctx.info.await_args.args[0]
    assert forwarded == "payload: [REDACTED_CONTENT]"
    assert uuid not in forwarded
    assert "top secret" not in forwarded


def test_install_loguru_mcp_bridge_is_idempotent() -> None:
    install_loguru_mcp_bridge()
    install_loguru_mcp_bridge()
    # No assertion beyond "does not raise"; sink registration is guarded internally.

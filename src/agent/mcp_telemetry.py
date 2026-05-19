"""Bridge loguru logs to FastMCP client-visible notifications during tool calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger

_mcp_ctx: ContextVar[Any | None] = ContextVar("matryca_mcp_context", default=None)
_sink_id: int | None = None


async def mcp_tool_info(ctx: Any, message: str) -> None:
    """Send a user-facing info log to the active MCP client session."""
    await ctx.info(message)


@asynccontextmanager
async def mcp_tool_session(ctx: Any) -> AsyncIterator[Any]:
    """Bind ``ctx`` for the loguru→MCP bridge for the duration of a tool call."""
    token = _mcp_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _mcp_ctx.reset(token)


async def run_in_thread_with_mcp_context[R](
    fn: Callable[..., R],
    /,
    *args: object,
    **kwargs: object,
) -> R:
    """Offload ``fn`` to a worker thread while preserving the MCP context variable."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _loguru_mcp_sink(message: Any) -> None:
    """Forward INFO+ loguru records to ``Context.info`` when a tool session is active."""
    record = message.record
    if record["level"].no < 20:
        return
    ctx = _mcp_ctx.get()
    if ctx is None:
        return
    text = str(record["message"])
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(ctx.info(text), name="matryca-mcp-log-bridge")


def install_loguru_mcp_bridge() -> None:
    """Register the loguru sink once (idempotent)."""
    global _sink_id
    if _sink_id is not None:
        return
    _sink_id = logger.add(_loguru_mcp_sink, level="INFO")


__all__ = [
    "install_loguru_mcp_bridge",
    "mcp_tool_info",
    "mcp_tool_session",
    "run_in_thread_with_mcp_context",
]

"""Bridge loguru logs to FastMCP client-visible notifications during tool calls."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from loguru import logger

_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
)
_CONTENT_MARKER_PATTERN = re.compile(
    r"(?i)(\b(?:payload|content|snippet|query)\s*[:=]\s*)(.+)",
)

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


def _matryca_debug_enabled() -> bool:
    return os.environ.get("MATRYCA_DEBUG", "").strip().lower() == "true"


def sanitize_log_message(text: str) -> str:
    """Mask UUIDs and sensitive payload markers unless ``MATRYCA_DEBUG=true``."""
    if _matryca_debug_enabled():
        return text
    censored = _UUID_PATTERN.sub("[CENSORED_UUID]", text)
    return _CONTENT_MARKER_PATTERN.sub(r"\1[REDACTED_CONTENT]", censored)


def _log_bridge_task_done(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("MCP log bridge task failed during shutdown: {}", exc)


def _loguru_mcp_sink(message: Any) -> None:
    """Forward INFO+ loguru records to ``Context.info`` when a tool session is active."""
    record = message.record
    if record["level"].no < 20:
        return
    ctx = _mcp_ctx.get()
    if ctx is None:
        return
    text = sanitize_log_message(str(record["message"]))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(ctx.info(text), name="matryca-mcp-log-bridge")
    task.add_done_callback(_log_bridge_task_done)


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
    "sanitize_log_message",
]

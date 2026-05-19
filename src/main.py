"""Application entrypoint: load config and run the headless MCP server (stdio)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from loguru import logger
from mcp.server.fastmcp import FastMCP

from .agent.mcp_server import AppContext, register_mcp_tools
from .agent.mcp_telemetry import install_loguru_mcp_bridge
from .config import load_matryca_wiki_config
from .graph.markdown_blocks import sweep_dangling_atomic_tmp_files
from .graph.page_write_lock import clear_page_write_locks

install_loguru_mcp_bridge()


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Prepare wiki config and graph hygiene for the stdio session lifetime.

    Args:
        _server: FastMCP instance (unused; reserved for future hooks).

    Yields:
        Application context holding :class:`MatrycaWikiConfig`.
    """
    load_dotenv()
    wiki_config = load_matryca_wiki_config()
    graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if graph_path:
        swept = await asyncio.to_thread(sweep_dangling_atomic_tmp_files, graph_path)
        if swept:
            logger.bind(graph=graph_path, removed=swept).info(
                "Swept dangling atomic-write temp files at startup",
            )
    logger.bind(namespaces=len(wiki_config.namespaces)).info(
        "Matryca MCP lifespan started (headless stdio)",
    )
    try:
        yield AppContext(wiki_config=wiki_config)
    finally:
        clear_page_write_locks()
        logger.info("Matryca MCP lifespan stopped")


mcp = FastMCP("matryca-logseq-llm-wiki", lifespan=app_lifespan)
register_mcp_tools(mcp)


async def _run_stdio_server() -> None:
    """Run the FastMCP stdio transport on the current event loop."""
    await mcp.run_stdio_async()


def main() -> None:
    """CLI entrypoint for ``python -m src.main``."""
    asyncio.run(_run_stdio_server())


if __name__ == "__main__":
    main()

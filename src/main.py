"""Application entrypoint: load config, wire Logseq, and run the MCP server (stdio)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from loguru import logger
from mcp.server.fastmcp import FastMCP

from .agent.mcp_server import AppContext, MatrycaMCPServer, register_mcp_tools
from .bridge.logseq_client import LogseqClient
from .config import load_matryca_wiki_config


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Create the Logseq client and MCP bridge for the stdio session lifetime.

    Args:
        _server: FastMCP instance (unused; reserved for future hooks).

    Yields:
        Application context holding the live :class:`MatrycaMCPServer`.

    Raises:
        ValueError: If ``LOGSEQ_API_TOKEN`` is unset (required for authenticated calls).
    """
    load_dotenv()
    api_url = os.environ.get("LOGSEQ_API_URL", "http://localhost:12315").rstrip("/")
    token = os.environ.get("LOGSEQ_API_TOKEN", "").strip()
    if not token:
        msg = (
            "LOGSEQ_API_TOKEN is not set. Add it to your environment or `.env` file "
            "before starting the MCP server."
        )
        raise ValueError(msg)

    client = LogseqClient(api_url=api_url, token=token)
    bridge = MatrycaMCPServer(client=client)
    wiki_config = load_matryca_wiki_config()
    logger.bind(api_url=api_url, namespaces=len(wiki_config.namespaces)).info(
        "Matryca MCP lifespan started (stdio)",
    )
    try:
        yield AppContext(bridge=bridge, wiki_config=wiki_config)
    finally:
        await client.aclose()
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

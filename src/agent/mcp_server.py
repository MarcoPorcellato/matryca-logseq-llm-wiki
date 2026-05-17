"""Agent-facing MCP server scaffolding (tools bridge to Logseq)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Literal, Self, cast

from loguru import logger
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel, Field, field_validator, model_validator

from ..bridge.logseq_client import LogseqClient
from ..config import MatrycaWikiConfig
from ..graph.block_ref_lint import lint_block_refs_in_graph
from ..graph.dashboard import build_dashboard_markdown
from ..graph.hubs import build_namespace_index_markdown
from ..graph.wiki_lint import format_wiki_lint_report, lint_wiki_prefixed_pages
from ..rag.local_query import format_keyword_query_markdown
from ..rag.matryca_hooks import get_page_spatial_context
from .l1_memory import read_l1_memory_async
from .quality_gate import outline_security_violations
from .routing_hint import append_read_page_routing_hint, routing_hint_for_write_outline


@dataclass(frozen=True, slots=True)
class AppContext:
    """Dependencies available for the MCP server lifetime."""

    bridge: MatrycaMCPServer
    wiki_config: MatrycaWikiConfig


PageType = Literal["entity", "project", "knowledge", "hub", "feedback"]
Domain = Literal["tech", "business", "content", "ops"]
EntityType = Literal["person", "client", "tool", "service", "technology"]


class OutlineNode(BaseModel):
    """Hierarchical outline node as accepted by agent tools (JSON-serializable)."""

    text: str = Field(..., description="Block text (Logseq outliner / Markdown body).")
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Optional Logseq-style properties (string keys/values).",
    )
    children: list[OutlineNode] = Field(default_factory=list)
    page_type: PageType | None = Field(
        default=None,
        description="Optional; merged into Logseq ``type::`` on this block when set.",
    )
    domain: Domain | None = Field(
        default=None,
        description="Optional; merged into ``domain::`` (required for knowledge nodes).",
    )
    entity_type: EntityType | None = Field(
        default=None,
        description="Optional; merged into ``entity-type::`` when ``page_type`` is entity.",
    )

    @field_validator("children", mode="before")
    @classmethod
    def _empty_children(cls, value: Any) -> list[Any]:  # noqa: ANN401
        """Treat ``null`` / missing children as an empty list."""
        if value is None:
            return []
        return cast(list[Any], value)

    @model_validator(mode="after")
    def _merge_schema_fields_into_properties(self) -> Self:
        """Mirror llm-wiki schema helpers into Logseq property lines."""
        explicit_schema = (
            self.page_type is not None or self.domain is not None or self.entity_type is not None
        )
        if not explicit_schema:
            return self

        merged = dict(self.properties)
        if self.page_type is not None:
            merged.setdefault("type::", self.page_type)
        if self.domain is not None:
            merged.setdefault("domain::", self.domain)
        if self.entity_type is not None:
            merged.setdefault("entity-type::", self.entity_type)

        ptype = merged.get("type::")
        dom = merged.get("domain::")
        ent = merged.get("entity-type::")
        if ptype == "entity" and not ent:
            msg = "entity blocks require `entity_type` or `properties['entity-type::']`"
            raise ValueError(msg)
        if ptype == "knowledge" and not dom:
            msg = "knowledge blocks require `domain` or `properties['domain::']`"
            raise ValueError(msg)

        if merged == self.properties:
            return self
        return self.model_copy(update={"properties": merged})


class MatrycaMCPServer:
    """MCP-oriented bridge: validates tool payloads and drives :class:`LogseqClient`."""

    def __init__(self, client: LogseqClient | None = None) -> None:
        """Store the Logseq client used for async block creation.

        Args:
            client: Live Logseq API client; required for :meth:`write_logseq_outline`.
        """
        self._client = client

    async def write_logseq_outline(
        self,
        outline: dict[str, Any],
        *,
        parent_block_uuid: str,
    ) -> dict[str, Any]:
        """Create blocks depth-first, awaiting each parent UUID before writing children.

        Args:
            outline: Nested mapping matching :class:`OutlineNode`
                (``text`` / ``properties`` / ``children``).
            parent_block_uuid: Existing Logseq block UUID to attach the root node under.

        Returns:
            Mapping with ``uuids`` (DFS-ordered new block ids) and a machine-readable
            ``routing_hint`` comment for L1/L2 traceability.

        Raises:
            ValueError: If no :class:`LogseqClient` was configured, outline fails
                validation, or credential-like content is detected.
        """
        client = self._client
        if client is None:
            msg = "write_logseq_outline requires a configured LogseqClient"
            raise ValueError(msg)

        sec = outline_security_violations(outline)
        if sec:
            raise ValueError("; ".join(sec))

        root = OutlineNode.model_validate(outline)
        created_ids: list[str] = []

        async def walk(node: OutlineNode, parent_uuid: str) -> None:
            new_uuid = await client.append_block(
                parent_uuid,
                node.text,
                dict(node.properties),
            )
            created_ids.append(new_uuid)
            for child in node.children:
                await walk(child, new_uuid)

        await walk(root, parent_block_uuid)
        logger.bind(
            blocks=len(created_ids),
            root_parent=parent_block_uuid,
        ).info("Applied Logseq outline with parent-chained UUIDs")
        return {
            "uuids": created_ids,
            "routing_hint": routing_hint_for_write_outline(),
        }


def register_mcp_tools(mcp: FastMCP) -> None:
    """Register read/write tools on the FastMCP application (stdio / hosted runtimes).

    Args:
        mcp: The application instance created in :mod:`src.main`.
    """

    @mcp.tool()
    async def read_l1_memory(ctx: Context[ServerSession, AppContext]) -> str:
        """Load **L1** fast-context Markdown (session rules, identity, gotchas).

        Reads small ``*.md`` files from ``MATRYCA_L1_PATH`` (file or directory), or—if
        unset—from ``memory_path`` in ``matryca-wiki.yml`` (``MATRYCA_WIKI_CONFIG`` or
        ``$LOGSEQ_GRAPH_PATH/matryca-wiki.yml``), else
        ``<parent of LOGSEQ_GRAPH_PATH>/matryca-l1/*.md``.
        Total size is capped so the whole vault is never loaded.

        **When to use:** At the start of substantive work, or when the user asks for
        house style, credentials *pointers* (not values), deploy gotchas, or routing
        rules that must apply before querying the graph (L2).

        **Returns:** Markdown with a file list and each file's contents, or a short
        message when no L1 sources are configured or found.
        """
        wiki_config = ctx.request_context.lifespan_context.wiki_config
        labels, body = await read_l1_memory_async(wiki_config)
        if not labels:
            return (
                "No L1 memory loaded. Set **MATRYCA_L1_PATH**, or **memory_path** in "
                "**matryca-wiki.yml**, or create **matryca-l1/*.md** next to your graph. "
                "See `SYSTEM_PROMPT.md` for L1 vs L2 routing."
            )
        logger.bind(files=len(labels)).info("read_l1_memory loaded L1 context")
        return body

    @mcp.tool()
    async def lint_logseq_block_refs() -> str:
        """Scan ``pages/**/*.md`` for ``((uuid))`` refs without a graph-wide ``id::`` target.

        Uses a two-pass text scan (regex for ``id::`` lines and block refs). Does **not**
        replace the spatial parser; it catches broken transclusions before edit sessions.

        **Requires:** ``LOGSEQ_GRAPH_PATH`` pointing at the Logseq graph root (folder with
        ``pages/``).

        Returns:
            Markdown report listing unresolved or non-v4 references.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("lint_logseq_block_refs called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot lint block references on disk. "
                "Set it to your graph root, then retry."
            )

        result = await asyncio.to_thread(lint_block_refs_in_graph, graph_path)
        logger.bind(
            pages=result.pages_scanned,
            issues=len(result.broken),
        ).info("lint_logseq_block_refs completed")
        return result.format_report()

    @mcp.tool()
    async def lint_matryca_wiki_pages(ctx: Context[ServerSession, AppContext]) -> str:
        """Lint prefixed wiki pages (``wiki_file_prefix`` from ``matryca-wiki.yml``).

        Checks under ``LOGSEQ_GRAPH_PATH/pages/*.md`` for: missing ``type::``, stale
        ``knowledge`` + ``confidence:: high`` + old ``updated::``, credential-like
        property lines, long base64-like tokens, and missing ``[[wikilinks]]``.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("lint_matryca_wiki_pages called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot lint wiki pages on disk. "
                "Set it to your graph root, then retry."
            )
        wiki_config = ctx.request_context.lifespan_context.wiki_config

        def _run() -> str:
            findings = lint_wiki_prefixed_pages(graph_path, wiki_config)
            return format_wiki_lint_report(findings, prefix=wiki_config.wiki_file_prefix)

        report = await asyncio.to_thread(_run)
        logger.bind(graph=graph_path).info("lint_matryca_wiki_pages completed")
        return report

    @mcp.tool()
    async def render_logseq_dashboard(ctx: Context[ServerSession, AppContext]) -> str:
        """Build a **[[Matryca Dashboard]]**-style outline: page counts, ``id::`` tally, ref health.

        Scans ``LOGSEQ_GRAPH_PATH/pages/**/*.md`` (no SQLite). Uses the same block-ref
        heuristics as :func:`lint_logseq_block_refs`. Returns Markdown you can paste into
        a Logseq page or split under a parent block.

        **Requires:** ``LOGSEQ_GRAPH_PATH`` set to the graph root.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("render_logseq_dashboard called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot render a dashboard. "
                "Set it to your graph root, then retry."
            )
        wiki_config = ctx.request_context.lifespan_context.wiki_config
        markdown = await asyncio.to_thread(build_dashboard_markdown, graph_path, wiki_config)
        logger.bind(graph=graph_path).info("render_logseq_dashboard completed")
        return markdown

    @mcp.tool()
    async def list_logseq_namespace_index(ctx: Context[ServerSession, AppContext]) -> str:
        """Group ``pages/*.md`` by first ``___`` segment for hub-style navigation."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("list_logseq_namespace_index called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot list namespaces. "
                "Set it to your graph root, then retry."
            )
        wiki_config = ctx.request_context.lifespan_context.wiki_config
        return await asyncio.to_thread(
            build_namespace_index_markdown,
            graph_path,
            wiki_config,
        )

    @mcp.tool()
    async def query_logseq_pages_local(keyword: str, limit: int = 15) -> str:
        """Rank ``pages/**/*.md`` by case-insensitive substring hits (no vector DB)."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("query_logseq_pages_local called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot query pages on disk. "
                "Set it to your graph root, then retry."
            )
        return await asyncio.to_thread(
            format_keyword_query_markdown,
            graph_path,
            keyword,
            limit=limit,
        )

    @mcp.tool()
    async def write_logseq_outline(
        outline: dict[str, Any],
        parent_block_uuid: str,
        ctx: Context[ServerSession, AppContext],
    ) -> dict[str, Any]:
        """Write nested outline bullets into Logseq under an existing parent block (API).

        **When to use:** The user or plan asks you to *create* or *append* structured
        bullets under a block that **already exists** in the graph and you know its
        **UUID** (e.g. from Logseq, prior tool output, or ``id::`` in file context).
        Sends each node depth-first via Logseq's HTTP JSON-RPC API so children attach
        to the freshly returned parent UUIDs (avoids unresolved-parent races).

        **When not to use:** You only need to *read* a page, fix typos in a whole file,
        or you do not have a real parent block UUID—prefer :func:`read_logseq_page` or
        a human/editor workflow instead.

        Args:
            outline: JSON tree shaped like ``OutlineNode`` (``text``, optional
                ``properties``, ``page_type`` / ``domain`` / ``entity_type``, nested
                ``children``).
            parent_block_uuid: Target parent block's UUID in Logseq.

        Returns:
            ``uuids`` (DFS-ordered list of new block UUID strings) plus ``routing_hint``.
        """
        bridge = ctx.request_context.lifespan_context.bridge
        return await bridge.write_logseq_outline(
            outline,
            parent_block_uuid=parent_block_uuid,
        )

    @mcp.tool()
    async def read_logseq_page(page_name: str) -> str:
        """Read a Logseq **page** from the on-disk Markdown graph (spatial / eyes).

        **When to use:** You need **ground truth** for what is already on a page—block
        hierarchy, ``id::`` lines, properties, links, or evidence—before editing,
        merging, or answering from the user's vault. Uses ``LOGSEQ_GRAPH_PATH`` and the
        external ``logseq-matryca-parser`` (no Logseq HTTP call).

        **When not to use:** You need to **insert** bullets under a known block UUID;
        use :func:`write_logseq_outline` and the Logseq API instead.

        Args:
            page_name: Page title as in Logseq (e.g. ``My Topic``), not a file path.

        Returns:
            Markdown summary of the parsed spatial tree, or a short human-readable
            message if the graph path is missing, the page file is absent, or the
            parser is not installed.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("read_logseq_page called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set in the environment; cannot read pages "
                "from disk. Set it to your Logseq graph root (the folder that contains "
                "`pages/`), then retry."
            )

        try:
            markdown = await get_page_spatial_context(page_name, graph_path)
        except FileNotFoundError as exc:
            logger.bind(page=page_name, graph=graph_path).info("read_logseq_page miss: {}", exc)
            return "Page not found, you can create it."
        except ImportError as exc:
            logger.error("read_logseq_page failed (parser missing): {}", exc)
            return (
                f"Spatial parser is not available (install `logseq-matryca-parser`). Detail: {exc}"
            )
        except OSError as exc:
            logger.bind(page=page_name).exception("read_logseq_page OS error")
            return f"Could not read the page file from disk: {exc}"

        logger.bind(page=page_name).debug("read_logseq_page returned spatial markdown")
        return append_read_page_routing_hint(markdown)


__all__ = [
    "AppContext",
    "Domain",
    "EntityType",
    "MatrycaMCPServer",
    "OutlineNode",
    "PageType",
    "register_mcp_tools",
]

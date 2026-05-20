"""Agent-facing MCP server scaffolding (headless graph tools)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Self, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import MatrycaWikiConfig
from .graph_dispatch import (
    dispatch_lint,
    dispatch_mutate,
    dispatch_read,
    dispatch_refactor,
    dispatch_search,
)
from .graph_tool_helpers import (
    MutateGraphAction,
    ReadGraphTarget,
    RefactorBlocksAction,
    RunLinterName,
    SearchGraphMethod,
)
from .mcp_telemetry import mcp_tool_info, mcp_tool_session
from .mcp_tool_guard import guard_mcp_tool
from .quality_gate import (
    outline_bounds_violations,
    outline_security_violations,
)


@dataclass(frozen=True, slots=True)
class AppContext:
    """Dependencies available for the MCP server lifetime."""

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


def outline_block_count(outline: dict[str, Any]) -> int:
    """Count nodes in a nested outline dict (including the root)."""
    n = 1
    raw = outline.get("children")
    children = raw if isinstance(raw, list) else []
    for ch in children:
        if isinstance(ch, dict):
            n += outline_block_count(cast(dict[str, Any], ch))
    return n


def _validate_outline_for_write(outline: dict[str, Any]) -> OutlineNode:
    """Run bounds, security scan, and Pydantic validation (CPU-heavy; call via ``to_thread``)."""
    bounds = outline_bounds_violations(outline)
    if bounds:
        raise ValueError("; ".join(bounds))
    sec = outline_security_violations(outline)
    if sec:
        raise ValueError("; ".join(sec))
    return OutlineNode.model_validate(outline)


def register_mcp_tools(mcp: FastMCP) -> None:
    """Register five consolidated MCP mega-tools on the FastMCP application.

    Tools: ``read_graph_data``, ``search_graph``, ``mutate_graph``, ``refactor_blocks``,
    ``run_linter`` — each routes by a ``typing.Literal`` discriminator to existing graph/RAG
    helpers (see module-level docstrings on each handler).

    Args:
        mcp: The application instance created in :mod:`src.main`.
    """

    def safe_tool(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """Register an MCP tool wrapped with :func:`guard_mcp_tool`."""

        def decorator(fn: Any) -> Any:
            return mcp.tool(*args, **kwargs)(guard_mcp_tool(fn))

        return decorator

    @safe_tool()
    async def read_graph_data(
        ctx: Context[ServerSession, AppContext],
        target_type: ReadGraphTarget,
        query: str = "",
    ) -> str:
        """Unified read plane: pages, L1 memory, block excerpts, structural hops, dashboards.

        Pick ``target_type`` first, then set ``query`` exactly as below (ignored where noted).

        **``target_type=page``** — ``query`` = Logseq **page title** (e.g. ``My Project``),
        not a file path.
        Returns spatial-parser Markdown: block tree, ``synthetic_id``, ``source_uuid``, ``uuid``.
        Use before edits; pair with ``mutate_graph`` only when you have a parent block UUID.

        **``target_type=memory``** — ``query`` ignored. Loads L1 fast-context Markdown
        (``MATRYCA_L1_PATH``, ``memory_path`` in ``matryca-wiki.yml``, or ``matryca-l1/*.md``).

        **``target_type=block_ast``** — ``query`` = ``Page Title|block-uuid`` (pipe-separated).
        Raw on-disk bullet subtree for that ``id::`` block (headless; no Logseq HTTP API).

        **``target_type=structural_hops``** — ``query`` = comma-separated seed page titles,
        or JSON ``{"seeds":"A, B", "max_depth": 3, "max_per_level": 40}``. BFS over wikilinks,
        shared tags, and light ``type::`` / ``domain::`` rings (no vectors).

        **``target_type=dashboard``** — ``query`` ignored. Matryca dashboard Markdown:
        page counts, ``id::`` tally, block-ref health under ``pages/``.

        **``target_type=xray_page``** — ``query`` = Logseq **page title**. Ultra-dense
        ``[n]`` outline (X-Ray mode); persists alias→UUID map to ``.matryca_xray_state.json`` at
        the graph root. Use ``[n]`` in ``target`` / ``target_uuid`` on later mutations.

        **Requires:** ``LOGSEQ_GRAPH_PATH`` for every target except ``memory`` (still recommended).
        """
        wiki_config = ctx.request_context.lifespan_context.wiki_config
        if target_type == "dashboard":
            await mcp_tool_info(
                ctx,
                "Rendering Matryca dashboard: scanning pages/ and block-reference health…",
            )
            async with mcp_tool_session(ctx):
                dashboard_md: str = await dispatch_read(wiki_config, target_type, query)
            await mcp_tool_info(ctx, "Dashboard render complete.")
            return dashboard_md
        return await dispatch_read(wiki_config, target_type, query)

    @safe_tool()
    async def search_graph(
        ctx: Context[ServerSession, AppContext],
        method: SearchGraphMethod,
        query: str = "",
    ) -> str | dict[str, Any]:
        """Lexical and structural discovery on the on-disk graph (no vector DB).

        **``method=bm25``** — ``query`` = natural-language keywords (e.g. ``redis cache``), or JSON
        ``{"keyword":"...", "limit":15}``. Ranks ``pages/**/*.md`` by Okapi BM25.

        **``method=regex``** — ``query`` = Python regex pattern (line scan in ``pages/``), or JSON
        ``{"pattern":"TODO|LATER", "limit":50}``.

        **``method=unlinked_mentions``** — ``query`` empty or JSON
        ``{"max_hits_per_file":80, "max_titles":500}``. Plain-text mentions of existing titles.

        **``method=journal_tasks``** — ``query`` = days to scan (default ``7``), or JSON
        ``{"days":14}``. Open ``TODO`` / ``LATER`` / ``WAITING`` in ``journals/`` plus review MD.

        **``method=resolve_entity``** — ``query`` = page title or ``alias::`` name. Resolves
        collisions, lists existing aliases, and reports whether a new entity page is safe to create.
        """
        if method == "bm25":
            await mcp_tool_info(
                ctx,
                "Building in-memory BM25 index over pages/ "
                "(first run or cache miss may take a moment)…",
            )
            async with mcp_tool_session(ctx):
                bm25_md = await dispatch_search(method, query)
            await mcp_tool_info(ctx, "Local page query complete.")
            return bm25_md
        return await dispatch_search(method, query)

    @safe_tool()
    async def mutate_graph(
        ctx: Context[ServerSession, AppContext],
        action: MutateGraphAction,
        target: str,
        payload: str,
    ) -> dict[str, Any]:
        """Create or patch durable graph content via headless on-disk writes.

        **``action=write_outline``** — ``target`` = parent **block UUID** (or X-Ray ``[n]`` alias).
        ``payload`` = JSON outline tree (``text``, optional ``properties`` / schema fields,
        nested ``children``) matching ``OutlineNode``.

        **``action=edit_property``** — ``target`` = ``Page Title|block-uuid``.
        ``payload`` = JSON: ``search``, ``replacement``, optional ``dry_run`` (default true),
        ``use_regex``, ``replace_all``, ``case_sensitive``. Surgical ``key::`` line edits only.

        **``action=append_journal``** — ``target`` ignored (use ``""``).
        ``payload`` = Markdown to append to today's ``journals/YYYY_MM_DD.md``, or JSON
        ``{"markdown_body":"...", "dry_run":true}``.

        **``action=inject_query``** — ``target`` = parent block UUID (or ``[n]`` alias).
        ``payload`` = JSON with inner EDN in ``query_edn`` and/or ``query_preset``
        (``open_markers``, ``pages_tagged``) plus optional ``tag``, ``dry_run`` (default true).
        """
        _ = ctx
        return await dispatch_mutate(action, target, payload)

    @safe_tool()
    async def refactor_blocks(
        ctx: Context[ServerSession, AppContext],
        action: RefactorBlocksAction,
        target_uuid: str,
        payload: str = "",
    ) -> dict[str, Any]:
        """AST-heavy block restructuring on disk (indent-only; preserves ``id::`` where possible).

        **``action=split_large``** — ``target_uuid`` = page title (optional; empty = all pages).
        ``payload`` = optional JSON ``{"min_chars":400, "max_blocks":25, "dry_run":true}``.

        **``action=reparent``** — ``target_uuid`` = page title. ``payload`` = JSON array of groups
        (same shape as legacy ``refactor_logseq_blocks`` ``groups`` argument).

        **``action=generate_flashcards``** — ``target_uuid`` = ``Page Title|source-block-uuid``.
        ``payload`` = optional JSON ``{"max_cards":30, "dry_run":true}``.
        """
        _ = ctx
        return await dispatch_refactor(action, target_uuid, payload)

    @safe_tool()
    async def run_linter(
        ctx: Context[ServerSession, AppContext],
        linter_name: RunLinterName,
    ) -> str | dict[str, Any]:
        """Vault hygiene scans (read-only or dry-run by default).

        **``linter_name=unify_tags``** — Preview-only tag clustering (``dry_run=true``).
        Cluster ``#tag`` spellings vault-wide; apply only after explicit operator consent.

        **``linter_name=block_refs``** — Markdown report: ``((uuid))`` vs graph-wide node index.

        **``linter_name=full_wiki_scan``** — Lint wiki-prefixed pages per ``matryca-wiki.yml``
        (``type::``, stale knowledge, credentials, wikilinks).
        """
        wiki_config = ctx.request_context.lifespan_context.wiki_config
        if linter_name == "full_wiki_scan":
            await mcp_tool_info(
                ctx,
                "Scanning wiki-prefixed pages under pages/ for lint findings…",
            )
            async with mcp_tool_session(ctx):
                wiki_report = await dispatch_lint(wiki_config, linter_name)
            await mcp_tool_info(ctx, "Wiki lint scan complete.")
            return wiki_report
        return await dispatch_lint(wiki_config, linter_name)


__all__ = [
    "AppContext",
    "Domain",
    "EntityType",
    "OutlineNode",
    "PageType",
    "MutateGraphAction",
    "ReadGraphTarget",
    "RefactorBlocksAction",
    "RunLinterName",
    "SearchGraphMethod",
    "outline_block_count",
    "register_mcp_tools",
]

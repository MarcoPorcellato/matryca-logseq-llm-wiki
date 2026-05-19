"""Agent-facing MCP server scaffolding (tools bridge to Logseq)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from loguru import logger
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel, Field, field_validator, model_validator

from ..bridge.logseq_client import LogseqClient
from ..config import MatrycaWikiConfig
from ..graph.advanced_query_block import (
    resolve_advanced_query_preset,
    wrap_logseq_advanced_query,
)
from ..graph.block_ref_lint import lint_block_refs_in_graph
from ..graph.dashboard import build_dashboard_markdown
from ..graph.flashcards import append_logseq_flashcards_under_block
from ..graph.journal_task_scan import (
    append_journal_markdown_section,
    format_journal_task_review_markdown,
    scan_journal_tasks,
)
from ..graph.link_tag_hop import format_hop_report_markdown
from ..graph.markdown_blocks import locate_block_by_uuid
from ..graph.path_sandbox import graph_safe_page_path
from ..graph.property_line_edit import edit_block_property_lines
from ..graph.reparent_blocks import refactor_logseq_blocks as run_reparent_logseq_blocks
from ..graph.split_large_blocks import refactor_large_blocks as run_refactor_large_blocks
from ..graph.tag_unify import lint_unify_logseq_tags as core_lint_unify_logseq_tags
from ..graph.unlinked_mentions import resolve_unlinked_mentions as scan_unlinked_mentions
from ..graph.wiki_lint import format_wiki_lint_report, lint_wiki_prefixed_pages
from ..rag.local_query import format_keyword_query_markdown
from ..rag.matryca_hooks import get_page_spatial_context
from .git_snapshot import snapshot_git_working_tree
from .l1_memory import read_l1_memory_async
from .mcp_telemetry import mcp_tool_info, mcp_tool_session, run_in_thread_with_mcp_context
from .mcp_tool_guard import guard_mcp_tool
from .quality_gate import (
    advanced_query_security_violations,
    markdown_append_bounds_violations,
    outline_bounds_violations,
    outline_security_violations,
)
from .routing_hint import (
    append_read_page_routing_hint,
    routing_hint_for_entity_alias_preflight,
    routing_hint_for_write_outline,
)


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

        root = await asyncio.to_thread(_validate_outline_for_write, outline)

        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        git_snap: dict[str, object] = {
            "enabled": False,
            "skipped": True,
            "reason": "LOGSEQ_GRAPH_PATH unset",
            "committed": False,
        }
        if graph_path:
            git_snap = await asyncio.to_thread(
                snapshot_git_working_tree,
                graph_path,
                message="matryca: AI pre-edit snapshot",
            )

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
        join_hint = routing_hint_for_write_outline()
        if root.properties.get("type::") == "entity":
            join_hint = f"{join_hint}\n{routing_hint_for_entity_alias_preflight()}"
        return {
            "uuids": created_ids,
            "routing_hint": join_hint,
            "outline_block_count": outline_block_count(outline),
            "git_snapshot": git_snap,
        }

    async def inject_logseq_advanced_query_block(
        self,
        *,
        parent_block_uuid: str,
        query_edn: str,
    ) -> dict[str, Any]:
        """Append one advanced-query fence block under ``parent_block_uuid`` via Logseq API."""
        client = self._client
        if client is None:
            msg = "inject_logseq_advanced_query_block requires a configured LogseqClient"
            raise ValueError(msg)
        sec = advanced_query_security_violations(query_edn)
        if sec:
            raise ValueError("; ".join(sec))
        content = wrap_logseq_advanced_query(query_edn)
        new_uuid = await client.append_block(parent_block_uuid, content, {})
        return {
            "uuid": new_uuid,
            "markdown": content,
            "routing_hint": routing_hint_for_write_outline(),
        }


ReadGraphTarget = Literal["page", "memory", "block_ast", "structural_hops", "dashboard"]
SearchGraphMethod = Literal["bm25", "regex", "unlinked_mentions", "journal_tasks"]
MutateGraphAction = Literal["write_outline", "edit_property", "append_journal", "inject_query"]
RefactorBlocksAction = Literal["split_large", "reparent", "generate_flashcards"]
RunLinterName = Literal["unify_tags", "block_refs", "full_wiki_scan"]


def _graph_path_from_env() -> str:
    return os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()


def _graph_missing_text() -> str:
    return (
        "LOGSEQ_GRAPH_PATH is not set; cannot access the graph on disk. "
        "Set it to your Logseq graph root (the folder that contains `pages/`), then retry."
    )


def _graph_missing_dict() -> dict[str, Any]:
    return {"ok": False, "code": "graph_missing", "hint": _graph_missing_text()}


def _parse_json_object(payload: str, *, field_name: str = "payload") -> dict[str, Any]:
    raw = payload.strip()
    if not raw:
        msg = f"`{field_name}` must be a non-empty JSON object"
        raise ValueError(msg)
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = f"`{field_name}` must decode to a JSON object"
        raise TypeError(msg)
    return cast(dict[str, Any], data)


def _parse_optional_json_query(query: str) -> dict[str, Any]:
    raw = query.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            msg = "`query` JSON must be an object when it starts with `{`"
            raise TypeError(msg)
        return cast(dict[str, Any], data)
    return {}


def _read_block_ast_markdown(graph_path: str, query: str) -> str:
    """Return the on-disk Markdown subtree for one block (page title + ``id::`` UUID)."""
    parts = [p.strip() for p in query.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        msg = (
            "For `target_type=block_ast`, set `query` to `Page Title|block-uuid` "
            "(Logseq page name, pipe, 36-char block UUID from `id::`)."
        )
        raise ValueError(msg)
    page_ref, block_uuid = parts[0], parts[1]
    path = graph_safe_page_path(graph_path, page_ref)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    stripped = [ln.rstrip("\n") for ln in lines]
    loc = locate_block_by_uuid(stripped, block_uuid)
    if loc is None:
        return (
            f"Block `{block_uuid}` not found on page `{page_ref}`. "
            "Confirm the UUID matches an `id::` line on that page."
        )
    b_idx, _id_idx, end = loc
    excerpt = "".join(lines[b_idx:end])
    return (
        f"# Block AST excerpt\n\n"
        f"- **Page:** [[{page_ref}]]\n"
        f"- **Block UUID:** `{block_uuid}`\n\n"
        f"```markdown\n{excerpt.rstrip()}\n```\n"
    )


def _format_regex_search_markdown(graph_path: str, pattern: str, *, limit: int = 50) -> str:
    """Vault-wide ``pages/**/*.md`` line scan (MCP orchestration; not the spatial parser)."""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        msg = f"Invalid regex in `query`: {exc}"
        raise ValueError(msg) from exc

    root = Path(graph_path).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return f"{_graph_missing_text()}\n\n`pages/` directory is missing."

    hits: list[tuple[str, int, str]] = []
    for path in sorted(pages.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(body.splitlines(), start=1):
            if compiled.search(line):
                hits.append((rel, line_no, line.strip()[:240]))
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    lines = [
        "# Regex search (pages/)",
        "",
        f"- **Graph:** `{root}`",
        f"- **Pattern:** `{pattern}`",
        f"- **Hits (cap {limit}):** {len(hits)}",
        "",
    ]
    if not hits:
        lines.append("_No matches in `pages/**/*.md`._")
        return "\n".join(lines) + "\n"
    for rel, line_no, preview in hits:
        lines.append(f"- `{rel}`:{line_no} — {preview}")
    lines.append("")
    return "\n".join(lines) + "\n"


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
        Raw on-disk bullet subtree for that ``id::`` block (no Logseq HTTP API).

        **``target_type=structural_hops``** — ``query`` = comma-separated seed page titles,
        or JSON ``{"seeds":"A, B", "max_depth": 3, "max_per_level": 40}``. BFS over wikilinks,
        shared tags, and light ``type::`` / ``domain::`` rings (no vectors).

        **``target_type=dashboard``** — ``query`` ignored. Matryca dashboard Markdown:
        page counts, ``id::`` tally, block-ref health under ``pages/``.

        **Requires:** ``LOGSEQ_GRAPH_PATH`` for every target except ``memory`` (still recommended).
        """
        if target_type == "memory":
            wiki_config = ctx.request_context.lifespan_context.wiki_config
            labels, body = await read_l1_memory_async(wiki_config)
            if not labels:
                return (
                    "No L1 memory loaded. Set **MATRYCA_L1_PATH**, or **memory_path** in "
                    "**matryca-wiki.yml**, or create **matryca-l1/*.md** next to your graph. "
                    "See `SYSTEM_PROMPT.md` for L1 vs L2 routing."
                )
            logger.bind(files=len(labels)).info("read_graph_data(memory) loaded L1 context")
            return body

        graph_path = _graph_path_from_env()
        if not graph_path:
            logger.warning("read_graph_data(%s) but LOGSEQ_GRAPH_PATH unset", target_type)
            return _graph_missing_text()

        if target_type == "page":
            page_name = query.strip()
            if not page_name:
                return "For `target_type=page`, set `query` to the Logseq page title."
            try:
                markdown = await get_page_spatial_context(page_name, graph_path)
            except FileNotFoundError as exc:
                logger.bind(page=page_name, graph=graph_path).info(
                    "read_graph_data page miss: {}",
                    exc,
                )
                return "Page not found, you can create it."
            except ImportError as exc:
                logger.error("read_graph_data parser missing: {}", exc)
                return (
                    "Spatial parser is not available (install `logseq-matryca-parser`). "
                    f"Detail: {exc}"
                )
            except OSError as exc:
                logger.bind(page=page_name).exception("read_graph_data OS error")
                return f"Could not read the page file from disk: {exc}"
            return append_read_page_routing_hint(markdown)

        if target_type == "block_ast":
            block_query = query.strip()
            if not block_query:
                return "For `target_type=block_ast`, set `query` to `Page Title|block-uuid`."
            return await asyncio.to_thread(_read_block_ast_markdown, graph_path, block_query)

        if target_type == "structural_hops":
            wiki = ctx.request_context.lifespan_context.wiki_config
            hop_opts = _parse_optional_json_query(query)
            seeds_raw = str(hop_opts.get("seeds", query)).strip()
            seed_list = [s.strip() for s in seeds_raw.split(",") if s.strip()]
            if not seed_list:
                return (
                    "For `target_type=structural_hops`, provide seed page titles in `query` "
                    "(comma-separated) or JSON with `seeds`."
                )
            depth = wiki.max_depth
            if hop_opts.get("max_depth") is not None:
                depth = max(1, min(int(hop_opts["max_depth"]), 10))
            per = wiki.structural_hop_max_per_level
            if hop_opts.get("max_per_level") is not None:
                per = max(1, min(int(hop_opts["max_per_level"]), 500))

            def _hops() -> str:
                return format_hop_report_markdown(
                    graph_path,
                    seed_list,
                    max_depth=depth,
                    max_per_level=per,
                )

            return await asyncio.to_thread(_hops)

        wiki_config = ctx.request_context.lifespan_context.wiki_config
        await mcp_tool_info(
            ctx,
            "Rendering Matryca dashboard: scanning pages/ and block-reference health…",
        )

        def _dash() -> str:
            return build_dashboard_markdown(graph_path, wiki_config)

        async with mcp_tool_session(ctx):
            dashboard_md: str = await run_in_thread_with_mcp_context(_dash)
        await mcp_tool_info(ctx, "Dashboard render complete.")
        logger.bind(graph=graph_path).info("read_graph_data(dashboard) completed")
        return dashboard_md

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
        """
        graph_path = _graph_path_from_env()
        if not graph_path:
            if method == "journal_tasks":
                return {
                    "ok": False,
                    "error": _graph_missing_text(),
                    "items": [],
                    "task_review_markdown": "",
                }
            return _graph_missing_text()

        if method == "bm25":
            bm_opts = _parse_optional_json_query(query)
            keyword = str(bm_opts.get("keyword", query)).strip()
            if not keyword:
                return "For `method=bm25`, set `query` to search keywords or JSON with `keyword`."
            limit = max(1, min(int(bm_opts.get("limit", 15)), 100))
            await mcp_tool_info(
                ctx,
                "Building in-memory BM25 index over pages/ "
                "(first run or cache miss may take a moment)…",
            )
            async with mcp_tool_session(ctx):
                bm25_md: str = await run_in_thread_with_mcp_context(
                    format_keyword_query_markdown,
                    graph_path,
                    keyword,
                    limit=limit,
                    mode="bm25",
                )
            await mcp_tool_info(ctx, "Local page query complete.")
            return bm25_md

        if method == "regex":
            rx_opts = _parse_optional_json_query(query)
            pattern = str(rx_opts.get("pattern", query)).strip()
            if not pattern:
                return "For `method=regex`, set `query` to a regex pattern or JSON with `pattern`."
            rx_limit = max(1, min(int(rx_opts.get("limit", 50)), 200))
            return await asyncio.to_thread(
                _format_regex_search_markdown,
                graph_path,
                pattern,
                limit=rx_limit,
            )

        if method == "unlinked_mentions":
            um_opts = _parse_optional_json_query(query)
            max_hits = max(1, min(int(um_opts.get("max_hits_per_file", 80)), 500))
            max_titles = max(1, min(int(um_opts.get("max_titles", 500)), 2000))

            def _unlinked() -> dict[str, Any]:
                return scan_unlinked_mentions(
                    graph_path,
                    max_hits_per_file=max_hits,
                    max_titles=max_titles,
                )

            return await asyncio.to_thread(_unlinked)

        j_opts = _parse_optional_json_query(query)
        days_raw = j_opts.get("days", query.strip() or 7)
        days = max(1, min(int(days_raw), 90))

        def _journal() -> dict[str, Any]:
            report = scan_journal_tasks(graph_path, days=days)
            md = format_journal_task_review_markdown(report)
            rows = [
                {
                    "source_iso_date": it.source_iso_date,
                    "source_relpath": it.source_relpath,
                    "marker": it.marker,
                    "headline": it.headline,
                    "scheduled": it.scheduled,
                    "deadline": it.deadline,
                    "block_text": it.block_text,
                }
                for it in report.items
            ]
            return {
                "ok": True,
                "days_scanned": report.days_scanned,
                "files_scanned": report.files_scanned,
                "open_item_count": len(report.items),
                "notes": report.notes,
                "items": rows,
                "task_review_markdown": md,
            }

        return await asyncio.to_thread(_journal)

    @safe_tool()
    async def mutate_graph(
        ctx: Context[ServerSession, AppContext],
        action: MutateGraphAction,
        target: str,
        payload: str,
    ) -> dict[str, Any]:
        """Create or patch durable graph content (Logseq API or on-disk).

        **``action=write_outline``** — ``target`` = parent **block UUID** in Logseq.
        ``payload`` = JSON outline tree (``text``, optional ``properties`` / schema fields,
        nested ``children``) matching ``OutlineNode``.

        **``action=edit_property``** — ``target`` = ``Page Title|block-uuid``.
        ``payload`` = JSON: ``search``, ``replacement``, optional ``dry_run`` (default true),
        ``use_regex``, ``replace_all``, ``case_sensitive``. Surgical ``key::`` line edits only.

        **``action=append_journal``** — ``target`` ignored (use ``""``).
        ``payload`` = Markdown to append to today's ``journals/YYYY_MM_DD.md``, or JSON
        ``{"markdown_body":"...", "dry_run":true}``.

        **``action=inject_query``** — ``target`` = parent block UUID.
        ``payload`` = JSON with inner EDN in ``query_edn`` and/or ``query_preset``
        (``open_markers``, ``pages_tagged``) plus optional ``tag``, ``dry_run`` (default true).
        """
        bridge = ctx.request_context.lifespan_context.bridge
        graph_path = _graph_path_from_env()

        if action == "write_outline":
            parent_uuid = target.strip()
            if not parent_uuid:
                return {"ok": False, "error": "`target` must be the parent block UUID."}
            outline = _parse_json_object(payload, field_name="payload")
            return await bridge.write_logseq_outline(
                outline,
                parent_block_uuid=parent_uuid,
            )

        if action == "edit_property":
            if not graph_path:
                return {
                    **_graph_missing_dict(),
                    "dry_run": True,
                    "match_count": 0,
                    "previews": [],
                    "previous_size_bytes": 0,
                    "current_size_bytes": 0,
                    "lines_changed": 0,
                }
            target_parts = [p.strip() for p in target.split("|", 1)]
            if len(target_parts) != 2 or not target_parts[0] or not target_parts[1]:
                return {
                    "ok": False,
                    "error": "For edit_property, `target` must be `Page Title|block-uuid`.",
                }
            page_ref, block_uuid = target_parts[0], target_parts[1]
            prop_opts = _parse_json_object(payload, field_name="payload")
            search = str(prop_opts.get("search", ""))
            replacement = str(prop_opts.get("replacement", ""))
            if not search:
                return {"ok": False, "error": "payload must include non-empty `search`."}

            def _edit() -> dict[str, object]:
                return edit_block_property_lines(
                    graph_path,
                    page_ref,
                    block_uuid,
                    search,
                    replacement,
                    dry_run=bool(prop_opts.get("dry_run", True)),
                    use_regex=bool(prop_opts.get("use_regex", False)),
                    replace_all=bool(prop_opts.get("replace_all", False)),
                    case_sensitive=bool(prop_opts.get("case_sensitive", True)),
                ).as_dict()

            return cast(dict[str, Any], await asyncio.to_thread(_edit))

        if action == "append_journal":
            if not graph_path:
                return _graph_missing_dict()
            body = payload
            dry_run = True
            if payload.strip().startswith("{"):
                journal_opts = _parse_json_object(payload, field_name="payload")
                body = str(journal_opts.get("markdown_body", ""))
                dry_run = bool(journal_opts.get("dry_run", True))
            bounds = markdown_append_bounds_violations(body)
            if bounds:
                return {
                    "ok": False,
                    "code": "payload_too_large",
                    "error": "; ".join(bounds),
                }
            return await asyncio.to_thread(
                append_journal_markdown_section,
                graph_path,
                body,
                dry_run=dry_run,
            )

        parent_block = target.strip()
        if not parent_block:
            return {
                "ok": False,
                "error": "For inject_query, `target` must be the parent block UUID.",
            }
        inject_opts = _parse_json_object(payload, field_name="payload")
        query_preset = inject_opts.get("query_preset")
        tag = inject_opts.get("tag")
        query_edn = str(inject_opts.get("query_edn", ""))
        dry_run = bool(inject_opts.get("dry_run", True))

        inner: str
        if query_preset and str(query_preset).strip():
            try:
                inner = resolve_advanced_query_preset(str(query_preset).strip(), tag=tag)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        elif query_edn.strip():
            inner = query_edn.strip()
        else:
            return {
                "ok": False,
                "error": "payload must include `query_preset` or non-empty `query_edn`.",
            }

        sec = advanced_query_security_violations(inner)
        if sec:
            return {"ok": False, "error": "; ".join(sec)}

        try:
            markdown = wrap_logseq_advanced_query(inner)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "markdown": markdown,
                "uuid": None,
                "routing_hint": routing_hint_for_write_outline(),
            }

        try:
            out = await bridge.inject_logseq_advanced_query_block(
                parent_block_uuid=parent_block,
                query_edn=inner,
            )
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

        return {"ok": True, "dry_run": False, **out}

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
        graph_path = _graph_path_from_env()
        if not graph_path:
            return _graph_missing_dict()

        refactor_opts = _parse_optional_json_query(payload)
        dry_run = bool(refactor_opts.get("dry_run", True))

        if action == "split_large":
            page_ref = target_uuid.strip() or None
            min_chars = max(50, int(refactor_opts.get("min_chars", 400)))
            max_blocks = max(1, min(int(refactor_opts.get("max_blocks", 25)), 100))
            git_snap: dict[str, object] = {"skipped": True, "reason": "dry_run"}
            if not dry_run:
                git_snap = await asyncio.to_thread(
                    snapshot_git_working_tree,
                    graph_path,
                    message="matryca: pre refactor_blocks split_large",
                )

            def _split() -> dict[str, Any]:
                return run_refactor_large_blocks(
                    graph_path,
                    page_ref=page_ref,
                    min_chars=min_chars,
                    max_blocks=max_blocks,
                    dry_run=dry_run,
                ).as_dict()

            split_out = await asyncio.to_thread(_split)
            split_out["git_snapshot"] = git_snap
            return split_out

        if action == "reparent":
            reparent_page = target_uuid.strip()
            if not reparent_page:
                return {"ok": False, "error": "For reparent, `target_uuid` must be the page title."}
            groups_raw = refactor_opts.get("groups")
            if groups_raw is None and payload.strip().startswith("["):
                groups_raw = json.loads(payload)
            if not isinstance(groups_raw, list):
                return {
                    "ok": False,
                    "error": "For reparent, `payload` must be a JSON array of reparent groups.",
                }
            groups = cast(list[dict[str, Any]], groups_raw)
            reparent_git: dict[str, object] = {"skipped": True, "reason": "dry_run"}
            if not dry_run:
                reparent_git = await asyncio.to_thread(
                    snapshot_git_working_tree,
                    graph_path,
                    message="matryca: pre refactor_blocks reparent",
                )

            def _reparent() -> dict[str, Any]:
                return run_reparent_logseq_blocks(
                    graph_path,
                    reparent_page,
                    groups,
                    dry_run=dry_run,
                ).as_dict()

            reparent_out = await asyncio.to_thread(_reparent)
            reparent_out["git_snapshot"] = reparent_git
            return reparent_out

        flash_parts = [p.strip() for p in target_uuid.split("|", 1)]
        if len(flash_parts) != 2 or not flash_parts[0] or not flash_parts[1]:
            return {
                "ok": False,
                "error": (
                    "For generate_flashcards, `target_uuid` must be `Page Title|source-block-uuid`."
                ),
            }
        page_ref, source_uuid = flash_parts[0], flash_parts[1]
        max_cards = max(1, min(int(refactor_opts.get("max_cards", 30)), 200))

        def _flash() -> dict[str, Any]:
            return append_logseq_flashcards_under_block(
                graph_path,
                page_ref,
                source_uuid,
                max_cards=max_cards,
                dry_run=dry_run,
            ).as_dict()

        return await asyncio.to_thread(_flash)

    @safe_tool()
    async def run_linter(
        ctx: Context[ServerSession, AppContext],
        linter_name: RunLinterName,
    ) -> str | dict[str, Any]:
        """Vault hygiene scans (read-only or dry-run by default).

        **``linter_name=unify_tags``** — Preview-only tag clustering (``dry_run=true``).
        Cluster ``#tag`` spellings vault-wide; apply only after explicit operator consent.

        **``linter_name=block_refs``** — Markdown report: ``((uuid))`` vs graph-wide ``id::``.

        **``linter_name=full_wiki_scan``** — Lint wiki-prefixed pages per ``matryca-wiki.yml``
        (``type::``, stale knowledge, credentials, wikilinks).
        """
        graph_path = _graph_path_from_env()
        if not graph_path:
            if linter_name == "unify_tags":
                return _graph_missing_dict()
            return _graph_missing_text()

        if linter_name == "unify_tags":

            def _tags() -> dict[str, Any]:
                raw = core_lint_unify_logseq_tags(graph_path, dry_run=True).as_dict()
                return cast(dict[str, Any], raw)

            return await asyncio.to_thread(_tags)

        if linter_name == "block_refs":

            def _refs() -> str:
                result = lint_block_refs_in_graph(graph_path)
                logger.bind(
                    pages=result.pages_scanned,
                    issues=len(result.broken),
                ).info("run_linter(block_refs) completed")
                return result.format_report()

            return await asyncio.to_thread(_refs)

        wiki_config = ctx.request_context.lifespan_context.wiki_config
        await mcp_tool_info(ctx, "Scanning wiki-prefixed pages under pages/ for lint findings…")

        def _wiki() -> str:
            findings = lint_wiki_prefixed_pages(graph_path, wiki_config)
            return format_wiki_lint_report(findings, prefix=wiki_config.wiki_file_prefix)

        async with mcp_tool_session(ctx):
            wiki_report: str = await run_in_thread_with_mcp_context(_wiki)
        await mcp_tool_info(ctx, "Wiki lint scan complete.")
        logger.bind(graph=graph_path).info("run_linter(full_wiki_scan) completed")
        return wiki_report


__all__ = [
    "AppContext",
    "Domain",
    "EntityType",
    "MatrycaMCPServer",
    "MutateGraphAction",
    "OutlineNode",
    "PageType",
    "ReadGraphTarget",
    "RefactorBlocksAction",
    "RunLinterName",
    "SearchGraphMethod",
    "outline_block_count",
    "register_mcp_tools",
]

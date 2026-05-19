"""Agent-facing MCP server scaffolding (tools bridge to Logseq)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Self, cast

from loguru import logger
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel, Field, field_validator, model_validator

from ..bridge.logseq_client import LogseqClient
from ..config import MatrycaWikiConfig
from ..graph import templates as graph_templates
from ..graph.advanced_query_block import (
    resolve_advanced_query_preset,
    wrap_logseq_advanced_query,
)
from ..graph.block_ref_lint import lint_block_refs_in_graph
from ..graph.dashboard import build_dashboard_markdown
from ..graph.flashcards import append_logseq_flashcards_under_block
from ..graph.generational_cache import cached_build_alias_index
from ..graph.hubs import build_namespace_index_markdown
from ..graph.journal_task_scan import (
    append_journal_markdown_section,
    format_journal_task_review_markdown,
    scan_journal_tasks,
)
from ..graph.link_tag_hop import format_hop_report_markdown, format_hub_orphan_markdown
from ..graph.moc_page import build_moc_markdown, write_moc_page
from ..graph.property_line_edit import (
    append_page_alias_line,
    edit_block_property_lines,
)
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


def register_mcp_tools(mcp: FastMCP) -> None:
    """Register read/write tools on the FastMCP application (stdio / hosted runtimes).

    Args:
        mcp: The application instance created in :mod:`src.main`.
    """

    def safe_tool(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """Register an MCP tool wrapped with :func:`guard_mcp_tool`."""

        def decorator(fn: Any) -> Any:
            return mcp.tool(*args, **kwargs)(guard_mcp_tool(fn))

        return decorator

    @safe_tool()
    async def traverse_logseq_structural_hops(
        ctx: Context[ServerSession, AppContext],
        seeds: str,
        max_depth: int | None = None,
        max_per_level: int | None = None,
    ) -> str:
        """BFS over wikilinks, shared tags, and light ``type::`` / ``domain::`` rings (no vectors).

        **Seeds:** comma-separated page titles or stems (e.g. ``My Page, Other``).

        **Inspired by:** ``obsidian-graph`` connection BFS — structural only.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return "LOGSEQ_GRAPH_PATH is not set; cannot traverse the graph on disk."
        wiki = ctx.request_context.lifespan_context.wiki_config
        depth = wiki.max_depth if max_depth is None else max(1, min(max_depth, 10))
        per = (
            wiki.structural_hop_max_per_level
            if max_per_level is None
            else max(1, min(max_per_level, 500))
        )
        seed_list = [s.strip() for s in seeds.split(",") if s.strip()]

        def _run() -> str:
            return format_hop_report_markdown(
                graph_path,
                seed_list,
                max_depth=depth,
                max_per_level=per,
            )

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def report_structural_hubs_orphans(ctx: Context[ServerSession, AppContext]) -> str:
        """List high-degree hub pages and low-degree orphans (structural graph only)."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return "LOGSEQ_GRAPH_PATH is not set; cannot analyze the graph on disk."
        return await asyncio.to_thread(format_hub_orphan_markdown, graph_path)

    @safe_tool()
    async def patch_logseq_block_property_lines(
        ctx: Context[ServerSession, AppContext],
        page_ref: str,
        block_uuid: str,
        search: str,
        replacement: str,
        dry_run: bool = True,
        use_regex: bool = False,
        replace_all: bool = False,
        case_sensitive: bool = True,
    ) -> dict[str, object]:
        """Surgical edits on ``key::`` lines for one block (anchored at ``id::``).

        **Inspired by:** cyanheads ``obsidian_replace_in_note`` (regex + ``$1`` / ``$&``).

        Use ``dry_run=true`` first; apply with ``dry_run=false`` (writes ``.bak``).
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {
                "ok": False,
                "code": "graph_missing",
                "hint": "LOGSEQ_GRAPH_PATH is not set.",
                "dry_run": dry_run,
                "match_count": 0,
                "previews": [],
                "previous_size_bytes": 0,
                "current_size_bytes": 0,
                "lines_changed": 0,
            }

        def _run() -> dict[str, object]:
            return edit_block_property_lines(
                graph_path,
                page_ref,
                block_uuid,
                search,
                replacement,
                dry_run=dry_run,
                use_regex=use_regex,
                replace_all=replace_all,
                case_sensitive=case_sensitive,
            ).as_dict()

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def inject_logseq_advanced_query(
        ctx: Context[ServerSession, AppContext],
        parent_block_uuid: str,
        query_edn: str = "",
        dry_run: bool = True,
        query_preset: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        """Append a live Logseq **Advanced Query** block (``#+BEGIN_QUERY``) under a parent UUID.

        Supply either ``query_preset`` (``open_markers`` or ``pages_tagged`` + ``tag``) **or**
        raw inner **EDN** in ``query_edn`` (must include a ``:query`` clause). Prefer presets
        for dashboards that refresh inside Logseq instead of static bullet dumps.

        **Requires:** configured Logseq HTTP API client and ``dry_run=false`` to write.
        """
        bridge = ctx.request_context.lifespan_context.bridge

        inner: str
        if query_preset and query_preset.strip():
            try:
                inner = resolve_advanced_query_preset(query_preset.strip(), tag=tag)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        elif query_edn.strip():
            inner = query_edn.strip()
        else:
            return {
                "ok": False,
                "error": "Provide `query_preset` or a non-empty `query_edn` (inner EDN map).",
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
                parent_block_uuid=parent_block_uuid,
                query_edn=inner,
            )
        except (ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

        return {"ok": True, "dry_run": False, **out}

    @safe_tool()
    async def analyze_journal_tasks(
        ctx: Context[ServerSession, AppContext],
        days: int = 7,
    ) -> dict[str, Any]:
        """Scan ``journals/`` for the last ``days`` for ``TODO`` / ``LATER`` / ``WAITING`` bullets.

        Parses ``SCHEDULED:`` / ``DEADLINE:`` markers inside each task cluster. Returns JSON
        rows plus a ready-to-paste **Task review** Markdown section (disk scan only).

        To **append** the review into today's journal file, call
        :func:`append_logseq_journal_markdown` with ``markdown_body`` set to the returned
        ``task_review_markdown`` (or a shortened variant you author).
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {
                "ok": False,
                "error": "LOGSEQ_GRAPH_PATH is not set.",
                "items": [],
                "task_review_markdown": "",
            }

        def _run() -> dict[str, Any]:
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

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def append_logseq_journal_markdown(
        ctx: Context[ServerSession, AppContext],
        markdown_body: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Append Markdown to today's ``journals/YYYY_MM_DD.md`` (atomic write, ``.bak``).

        Typical use: paste the ``task_review_markdown`` from :func:`analyze_journal_tasks`.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {
                "ok": False,
                "code": "graph_missing",
                "hint": "LOGSEQ_GRAPH_PATH is not set.",
            }
        bounds = markdown_append_bounds_violations(markdown_body)
        if bounds:
            return {
                "ok": False,
                "code": "payload_too_large",
                "error": "; ".join(bounds),
            }
        return await asyncio.to_thread(
            append_journal_markdown_section,
            graph_path,
            markdown_body,
            dry_run=dry_run,
        )

    @safe_tool()
    async def resolve_logseq_entity(
        ctx: Context[ServerSession, AppContext],
        candidates: str,
    ) -> dict[str, Any]:
        """Resolve page titles / aliases before creating entities (``alias::`` graph scan).

        ``candidates`` is a comma-separated list. Matching is normalization-only
        (casefold, whitespace); no fuzzy edit distance.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "error": "LOGSEQ_GRAPH_PATH is not set.", "results": []}

        def _run() -> dict[str, Any]:
            index = cached_build_alias_index(graph_path)
            parts = [p.strip() for p in candidates.split(",") if p.strip()]
            results = [index.resolve(p).as_dict() for p in parts]
            return {
                "ok": True,
                "index_collision_notes": index.collision_notes[:25],
                "results": results,
            }

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def append_logseq_page_alias(
        ctx: Context[ServerSession, AppContext],
        page_ref: str,
        alias: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Append to the first ``alias::`` line on a page (or create one); idempotent."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {
                "ok": False,
                "code": "graph_missing",
                "hint": "LOGSEQ_GRAPH_PATH is not set.",
            }

        def _run() -> dict[str, Any]:
            return append_page_alias_line(graph_path, page_ref, alias, dry_run=dry_run).as_dict()

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def generate_logseq_flashcards(
        ctx: Context[ServerSession, AppContext],
        page_ref: str,
        source_block_uuid: str,
        max_cards: int = 30,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Extract ``question :: answer`` pairs in a block subtree and append ``#card`` children.

        **Inspired by:** ``st3v3nmw/obsidian-spaced-repetition`` (SRS ``::`` pairs)
        and Logseq ``#card``.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "code": "graph_missing", "hint": "LOGSEQ_GRAPH_PATH is not set."}

        def _run() -> dict[str, Any]:
            return append_logseq_flashcards_under_block(
                graph_path,
                page_ref,
                source_block_uuid,
                max_cards=max_cards,
                dry_run=dry_run,
            ).as_dict()

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def lint_unify_logseq_tags(
        ctx: Context[ServerSession, AppContext],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Cluster ``#tag`` spellings (case / variants), pick the most frequent, unify across graph.

        **Inspired by:** ``obsidian-linter`` and tags-manager workflows.
        Skips URLs, wikilinks, inline code.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "code": "graph_missing", "hint": "LOGSEQ_GRAPH_PATH is not set."}

        def _run() -> dict[str, Any]:
            raw = core_lint_unify_logseq_tags(graph_path, dry_run=dry_run).as_dict()
            return cast(dict[str, Any], raw)

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def refactor_logseq_blocks(
        ctx: Context[ServerSession, AppContext],
        page_ref: str,
        groups: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Group flat sibling blocks under new category headings (indent-only; preserves ``id::``).

        **Inspired by:** ``vslinko/obsidian-outliner`` reparenting.
        Triggers a git snapshot when applying.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "code": "graph_missing", "hint": "LOGSEQ_GRAPH_PATH is not set."}

        git_snap: dict[str, object] = {"skipped": True, "reason": "dry_run"}
        if not dry_run:
            git_snap = await asyncio.to_thread(
                snapshot_git_working_tree,
                graph_path,
                message="matryca: pre refactor_logseq_blocks",
            )

        def _run() -> dict[str, Any]:
            return run_reparent_logseq_blocks(
                graph_path,
                page_ref,
                groups,
                dry_run=dry_run,
            ).as_dict()

        out = await asyncio.to_thread(_run)
        out["git_snapshot"] = git_snap
        return out

    @safe_tool()
    async def resolve_unlinked_mentions(
        ctx: Context[ServerSession, AppContext],
        max_hits_per_file: int = 80,
        max_titles: int = 500,
    ) -> dict[str, Any]:
        """List plain-text mentions of existing page titles (candidates for ``[[wikilinks]]``).

        **Inspired by:** ``logseq-plugin-unlinked-references`` (guards for URLs / links / code).
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "error": "LOGSEQ_GRAPH_PATH is not set.", "hits": []}

        def _run() -> dict[str, Any]:
            return scan_unlinked_mentions(
                graph_path,
                max_hits_per_file=max_hits_per_file,
                max_titles=max_titles,
            )

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def generate_moc_page(
        ctx: Context[ServerSession, AppContext],
        namespace: str,
        output_page_title: str | None = None,
        write_to_disk: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Build a hierarchical MOC (Map of Content) Markdown index for a namespace stem.

        **Inspired by:** ``zoottel/obsidian-zoottelkeeper`` index pages. Optional atomic page write.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "error": "LOGSEQ_GRAPH_PATH is not set."}

        def _run() -> dict[str, Any]:
            if write_to_disk:
                snap: dict[str, object] = {"skipped": True, "reason": "dry_run"}
                if not dry_run:
                    snap = snapshot_git_working_tree(
                        graph_path,
                        message="matryca: pre generate_moc_page",
                    )
                out = write_moc_page(
                    graph_path,
                    namespace,
                    output_page_title=output_page_title,
                    dry_run=dry_run,
                )
                merged = dict(out)
                merged["git_snapshot"] = snap
                return merged
            md = build_moc_markdown(graph_path, namespace)
            return {
                "ok": True,
                "write_to_disk": False,
                "markdown": md,
                "hint": "Pass write_to_disk=true and dry_run=false to save under pages/.",
            }

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def refactor_large_blocks(
        ctx: Context[ServerSession, AppContext],
        page_ref: str | None = None,
        min_chars: int = 400,
        max_blocks: int = 25,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Split very long bullets into parent + children; parent keeps the original ``id::``.

        **Inspired by:** ``FeralFlora/obsidian-text-segmenter``. Git snapshot when applying.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {"ok": False, "code": "graph_missing", "hint": "LOGSEQ_GRAPH_PATH is not set."}

        git_snap: dict[str, object] = {"skipped": True, "reason": "dry_run"}
        if not dry_run:
            git_snap = await asyncio.to_thread(
                snapshot_git_working_tree,
                graph_path,
                message="matryca: pre refactor_large_blocks",
            )

        def _run() -> dict[str, Any]:
            return run_refactor_large_blocks(
                graph_path,
                page_ref=page_ref,
                min_chars=min_chars,
                max_blocks=max_blocks,
                dry_run=dry_run,
            ).as_dict()

        out = await asyncio.to_thread(_run)
        out["git_snapshot"] = git_snap
        return out

    @safe_tool()
    async def snapshot_logseq_graph_git(
        message: str = "matryca: AI manual snapshot",
    ) -> dict[str, object]:
        """Run the same opt-in ``git add -A`` + ``git commit`` snapshot as writes (manual).

        Requires ``MATRYCA_GIT_SNAPSHOT_ON_WRITE=true`` and a git repo at ``LOGSEQ_GRAPH_PATH``.
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return {
                "enabled": False,
                "skipped": True,
                "reason": "LOGSEQ_GRAPH_PATH unset",
                "committed": False,
            }
        return await asyncio.to_thread(snapshot_git_working_tree, graph_path, message=message)

    @safe_tool()
    async def list_logseq_templates(ctx: Context[ServerSession, AppContext]) -> str:
        """List ``*.md`` in the graph ``templates/`` folder (Templater-style discovery)."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return "LOGSEQ_GRAPH_PATH is not set."
        wiki = ctx.request_context.lifespan_context.wiki_config

        def _run() -> str:
            names = graph_templates.list_logseq_templates(graph_path, subdir=wiki.templates_subdir)
            if not names:
                return f"_No templates under `{wiki.templates_subdir}/`._"
            lines = ["# Logseq templates", "", f"- **Directory:** `{wiki.templates_subdir}/`", ""]
            for n in names:
                lines.append(f"- `{n}`")
            lines.append("")
            return "\n".join(lines)

        return await asyncio.to_thread(_run)

    @safe_tool()
    async def read_logseq_template(
        ctx: Context[ServerSession, AppContext],
        template_name: str,
    ) -> str:
        """Read one template file as Markdown (for mirroring properties / bullets)."""
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            return "LOGSEQ_GRAPH_PATH is not set."
        wiki = ctx.request_context.lifespan_context.wiki_config

        def _run() -> str:
            try:
                rel, body = graph_templates.read_logseq_template(
                    graph_path,
                    template_name,
                    subdir=wiki.templates_subdir,
                )
            except FileNotFoundError:
                return f"Template not found under `{wiki.templates_subdir}/`: `{template_name}`"
            except ValueError as exc:
                return f"Invalid template name: {exc}"
            return f"# Template `{rel}`\n\n```markdown\n{body.rstrip()}\n```\n"

        return await asyncio.to_thread(_run)

    @safe_tool()
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

    @safe_tool()
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

    @safe_tool()
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

        await mcp_tool_info(ctx, "Scanning wiki-prefixed pages under pages/ for lint findings…")

        def _run() -> str:
            findings = lint_wiki_prefixed_pages(graph_path, wiki_config)
            return format_wiki_lint_report(findings, prefix=wiki_config.wiki_file_prefix)

        async with mcp_tool_session(ctx):
            report: str = await run_in_thread_with_mcp_context(_run)
        await mcp_tool_info(ctx, "Wiki lint scan complete.")
        logger.bind(graph=graph_path).info("lint_matryca_wiki_pages completed")
        return report

    @safe_tool()
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
        await mcp_tool_info(
            ctx,
            "Rendering Matryca dashboard: scanning pages/ and block-reference health…",
        )
        async with mcp_tool_session(ctx):
            markdown: str = await run_in_thread_with_mcp_context(
                build_dashboard_markdown,
                graph_path,
                wiki_config,
            )
        await mcp_tool_info(ctx, "Dashboard render complete.")
        logger.bind(graph=graph_path).info("render_logseq_dashboard completed")
        return markdown

    @safe_tool()
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

    @safe_tool()
    async def query_logseq_pages_local(
        ctx: Context[ServerSession, AppContext],
        keyword: str,
        limit: int = 15,
        mode: str = "bm25",
    ) -> str:
        """Rank ``pages/**/*.md`` by BM25 (default) or legacy substring counts (no vector DB).

        **Modes:** ``bm25`` (Okapi BM25 over token bags) or ``substring`` (hit counts).
        """
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if not graph_path:
            logger.warning("query_logseq_pages_local called but LOGSEQ_GRAPH_PATH is unset")
            return (
                "LOGSEQ_GRAPH_PATH is not set; cannot query pages on disk. "
                "Set it to your graph root, then retry."
            )
        query_mode = mode.strip().lower()
        if query_mode in ("bm25", "tfidf", "relevance"):
            await mcp_tool_info(
                ctx,
                "Building in-memory BM25 index over pages/ "
                "(first run or cache miss may take a moment)…",
            )
        else:
            await mcp_tool_info(ctx, f"Scanning pages/ for substring matches ({query_mode})…")
        async with mcp_tool_session(ctx):
            result: str = await run_in_thread_with_mcp_context(
                format_keyword_query_markdown,
                graph_path,
                keyword,
                limit=limit,
                mode=mode,
            )
        await mcp_tool_info(ctx, "Local page query complete.")
        return result

    @safe_tool()
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

    @safe_tool()
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
            Markdown summary of the parsed spatial tree (includes per-block
            ``synthetic_id``, ``source_uuid``, and ``uuid`` from the parser AST),
            or a short human-readable message if the graph path is missing, the page
            is absent, or the parser is not installed.
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
    "outline_block_count",
    "register_mcp_tools",
]

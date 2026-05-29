"""Shared async dispatch for MCP mega-tools and the agent-native CLI."""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from pathlib import Path
from typing import Any, cast

from logseq_matryca_parser.agent_writer import _deepest_line_end
from logseq_matryca_parser.graph import LogseqGraph
from logseq_matryca_parser.logos_core import LogseqNode
from loguru import logger

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
from ..graph.markdown_blocks import (
    OCCConflictError,
    OCCSnapshot,
    atomic_write_bytes_if_unchanged,
    canonical_line_suffix,
    graph_safe_page_path,
    read_file_mtime,
    strip_line_endings,
)
from ..graph.page_write_lock import page_rmw_lock
from ..graph.path_sandbox import (
    PathTraversalSecurityError,
    assert_path_within_graph,
    read_graph_file_text,
)
from ..graph.property_line_edit import edit_block_property_lines
from ..graph.reparent_blocks import refactor_logseq_blocks as run_reparent_logseq_blocks
from ..graph.split_large_blocks import refactor_large_blocks as run_refactor_large_blocks
from ..graph.tag_unify import lint_unify_logseq_tags as core_lint_unify_logseq_tags
from ..graph.unlinked_mentions import resolve_unlinked_mentions as scan_unlinked_mentions
from ..graph.wiki_lint import format_wiki_lint_report, lint_wiki_prefixed_pages
from ..rag.local_query import format_keyword_query_markdown
from ..rag.matryca_hooks import get_page_spatial_context
from ..utils.json_repair import loads_repaired_json
from .alias_state import resolve_pipe_target, resolve_target
from .git_snapshot import snapshot_git_working_tree
from .graph_tool_helpers import (
    MutateGraphAction,
    ReadGraphTarget,
    RefactorBlocksAction,
    RunLinterName,
    SearchGraphMethod,
    bounded_int_from_options,
    format_regex_search_markdown,
    graph_missing_dict,
    graph_missing_text,
    graph_path_from_env,
    parse_json_object,
    parse_optional_json_query,
    read_block_ast_markdown,
    read_xray_page_markdown,
)
from .l1_memory import read_l1_memory_async
from .llm_context_payload import cap_llm_payload_chars
from .quality_gate import advanced_query_security_violations, markdown_append_bounds_violations
from .routing_hint import (
    append_read_page_routing_hint,
    routing_hint_for_entity_alias_preflight,
    routing_hint_for_write_outline,
)


def _resolve_graph_node(graph: LogseqGraph, block_uuid: str) -> LogseqNode | None:
    """Resolve a block UUID against parser registry keys and on-disk ``id::`` values."""
    node = graph.get_node_by_uuid(block_uuid)
    if node is not None:
        return node
    return graph.get_node_by_embed_ref(block_uuid)


def _property_lines(properties: dict[str, str], block_uuid: str) -> list[str]:
    lines: list[str] = []
    for key, value in properties.items():
        prop_key = key if key.endswith("::") else f"{key}::"
        lines.append(f"{prop_key} {value}")
    if not any(line.strip().startswith("id::") for line in lines):
        lines.append(f"id:: {block_uuid}")
    return lines


def _resolve_chain_parent_uuid(
    graph_path: str | Path,
    parent_uuid: str,
    block_text: str,
    written_id: str,
) -> str:
    """Return a parent key the parser registry accepts for the next append."""
    graph_root = Path(graph_path).expanduser().resolve()
    graph: LogseqGraph | None = None
    for attempt in range(2):
        graph = LogseqGraph.load_directory(graph_root)
        by_id = graph.get_node_by_embed_ref(written_id)
        if by_id is not None:
            return written_id
        if attempt == 0:
            continue

    if graph is not None:
        parent = _resolve_graph_node(graph, parent_uuid)
        if parent is not None:
            for child in reversed(parent.children):
                if child.clean_text.strip() != block_text.strip():
                    continue
                source_uuid = getattr(child, "source_uuid", None)
                if isinstance(source_uuid, str) and source_uuid.strip():
                    return source_uuid.strip()
                return str(child.uuid)

    msg = (
        f"New block id::{written_id} was not indexed after write; "
        "cannot chain nested outline children."
    )
    raise ValueError(msg)


def _occ_snapshot_for_block(graph_path: str | Path, block_uuid: str) -> OCCSnapshot | None:
    """Phase-1 OCC snapshot for the page file hosting ``block_uuid``."""
    graph_root = Path(graph_path).expanduser().resolve()
    graph = LogseqGraph.load_directory(graph_root)
    node = _resolve_graph_node(graph, block_uuid)
    if node is None or not node.source_path:
        return None
    return OCCSnapshot.capture(node.source_path)


def _headless_append_child(
    graph_path: str | Path,
    parent_uuid: str,
    content: str,
    *,
    properties: dict[str, str] | None = None,
    occ: OCCSnapshot | None = None,
) -> str:
    """Append a child block on disk under ``parent_uuid``; return the new block UUID."""
    graph_root = Path(graph_path).expanduser().resolve()
    new_uuid = str(uuid_module.uuid4())
    graph = LogseqGraph.load_directory(graph_root)
    parent = _resolve_graph_node(graph, parent_uuid)
    if parent is None:
        msg = f"No node registered for uuid={parent_uuid}"
        raise ValueError(msg)
    source_path = parent.source_path
    if not source_path:
        msg = f"Node uuid={parent_uuid} has no source_path"
        raise ValueError(msg)

    props = _property_lines(dict(properties or {}), new_uuid)
    content_lines = content.splitlines()
    head = content_lines[0] if content_lines else ""
    tail = content_lines[1:]

    page_path = Path(source_path)
    if occ is None:
        occ = OCCSnapshot.capture(page_path)
    elif occ.drifted():
        raise OCCConflictError(
            page_path,
            baseline_mtime=occ.baseline_mtime,
            current_mtime=read_file_mtime(page_path),
        )

    with page_rmw_lock(source_path):
        if occ is not None and occ.drifted():
            raise OCCConflictError(
                page_path,
                baseline_mtime=occ.baseline_mtime,
                current_mtime=read_file_mtime(page_path),
            )
        graph = LogseqGraph.load_directory(graph_root)
        parent = _resolve_graph_node(graph, parent_uuid)
        if parent is None:
            msg = f"No node registered for uuid={parent_uuid}"
            raise ValueError(msg)
        parent_uuid_resolved = parent.uuid

        target_node = graph.get_node_by_uuid(parent_uuid_resolved)
        if target_node is None:
            msg = f"No node registered for uuid={parent_uuid_resolved}"
            raise ValueError(msg)
        insert_after_line = _deepest_line_end(target_node)
        child_level = target_node.indent_level + 1
        bullet_indent = " " * (child_level * graph.tab_size)
        body_indent = " " * ((child_level + 1) * graph.tab_size)
        path = Path(target_node.source_path or source_path)
        safe_path = assert_path_within_graph(path, graph_root)
        raw_text = read_graph_file_text(safe_path, graph_root)
        file_lines = raw_text.splitlines(keepends=True)
        insert_index = insert_after_line

        new_lines = [f"{bullet_indent}- {strip_line_endings(head)}\n"]
        new_lines.extend(f"{body_indent}{strip_line_endings(line)}\n" for line in tail)
        new_lines.extend(f"{body_indent}{line}\n" for line in props)

        for offset, line in enumerate(new_lines):
            file_lines.insert(insert_index + offset, line)

        updated = "".join(strip_line_endings(ln) + canonical_line_suffix(ln) for ln in file_lines)
        baseline_mtime = occ.baseline_mtime if occ is not None else read_file_mtime(path)
        if baseline_mtime is None or not atomic_write_bytes_if_unchanged(
            path,
            updated.encode("utf-8"),
            graph_root=graph_root,
            baseline_mtime=baseline_mtime,
        ):
            raise OCCConflictError(
                path,
                baseline_mtime=baseline_mtime or 0.0,
                current_mtime=read_file_mtime(path),
            )
        if occ is not None:
            occ.refresh_after_own_write()

    return new_uuid


def _headless_write_outline(
    graph_path: str,
    parent_block_uuid: str,
    outline: dict[str, Any],
) -> dict[str, Any]:
    """Depth-first headless outline write using the parser's atomic splice engine."""
    from .outline_models import OutlineNode, outline_block_count, validate_outline_for_write

    root = validate_outline_for_write(outline)
    git_snap: dict[str, object] = {
        "enabled": False,
        "skipped": True,
        "reason": "LOGSEQ_GRAPH_PATH unset",
        "committed": False,
    }
    if graph_path:
        git_snap = snapshot_git_working_tree(
            graph_path,
            message="matryca: AI pre-edit snapshot",
        )

    graph_root = Path(graph_path).expanduser().resolve()
    graph = LogseqGraph.load_directory(graph_root)
    parent_node = _resolve_graph_node(graph, parent_block_uuid)
    occ: OCCSnapshot | None = None
    if parent_node is not None and parent_node.source_path:
        occ = OCCSnapshot.capture(parent_node.source_path)

    created_ids: list[str] = []

    def walk(node: OutlineNode, parent_uuid: str) -> None:
        new_uuid = _headless_append_child(
            graph_path,
            parent_uuid,
            node.text,
            properties=dict(node.properties),
            occ=occ,
        )
        created_ids.append(new_uuid)
        chain_parent = _resolve_chain_parent_uuid(
            graph_path,
            parent_uuid,
            node.text,
            new_uuid,
        )
        for child in node.children:
            walk(child, chain_parent)

    walk(root, parent_block_uuid)
    logger.bind(
        blocks=len(created_ids),
        root_parent=parent_block_uuid,
    ).info("Applied headless Logseq outline with parent-chained UUIDs")
    join_hint = routing_hint_for_write_outline()
    if root.properties.get("type::") == "entity":
        join_hint = f"{join_hint}\n{routing_hint_for_entity_alias_preflight()}"
    return {
        "ok": True,
        "uuids": created_ids,
        "routing_hint": join_hint,
        "outline_block_count": outline_block_count(outline),
        "git_snapshot": git_snap,
    }


async def dispatch_read(
    wiki_config: MatrycaWikiConfig,
    target_type: ReadGraphTarget,
    query: str = "",
) -> str:
    """Route ``read_graph_data`` by ``target_type``."""
    if target_type == "memory":
        _labels, body = await read_l1_memory_async(wiki_config)
        if not _labels:
            return (
                "No L1 memory loaded. Set **MATRYCA_L1_PATH**, or **memory_path** in "
                "**matryca-wiki.yml**, or create **matryca-l1/*.md** next to your graph. "
                "See `SYSTEM_PROMPT.md` for L1 vs L2 routing."
            )
        logger.bind(files=len(_labels)).info("read_graph_data(memory) loaded L1 context")
        return cap_llm_payload_chars(body)

    graph_path = graph_path_from_env()
    if not graph_path:
        logger.warning("read_graph_data(%s) but LOGSEQ_GRAPH_PATH unset", target_type)
        return graph_missing_text()

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
                f"Spatial parser is not available (install `logseq-matryca-parser`). Detail: {exc}"
            )
        except OSError as exc:
            logger.bind(page=page_name).exception("read_graph_data OS error")
            return f"Could not read the page file from disk: {exc}"
        return append_read_page_routing_hint(cap_llm_payload_chars(markdown))

    if target_type == "xray_page":
        page_name = query.strip()
        if not page_name:
            return "For `target_type=xray_page`, set `query` to the Logseq page title."
        try:
            xray_md = await asyncio.to_thread(read_xray_page_markdown, graph_path, page_name)
        except FileNotFoundError:
            return "Page not found, you can create it."
        except ImportError as exc:
            logger.error("read_graph_data xray_page parser missing: {}", exc)
            return (
                f"Spatial parser is not available (install `logseq-matryca-parser`). Detail: {exc}"
            )
        except OSError as exc:
            logger.bind(page=page_name).exception("read_graph_data xray_page OS error")
            return f"Could not read the page file from disk: {exc}"
        return cap_llm_payload_chars(xray_md)

    if target_type == "block_ast":
        block_query = query.strip()
        if not block_query:
            return (
                "For `target_type=block_ast`, set `query` to `Page Title|block-uuid` "
                "or `Page Title|[n]` after `xray_page`."
            )
        return cap_llm_payload_chars(
            await asyncio.to_thread(read_block_ast_markdown, graph_path, block_query),
        )

    if target_type == "structural_hops":
        hop_opts = parse_optional_json_query(query)
        seeds_raw = str(hop_opts.get("seeds", query)).strip()
        seed_list = [s.strip() for s in seeds_raw.split(",") if s.strip()]
        if not seed_list:
            return (
                "For `target_type=structural_hops`, provide seed page titles in `query` "
                "(comma-separated) or JSON with `seeds`."
            )
        depth = wiki_config.max_depth
        if hop_opts.get("max_depth") is not None:
            depth_raw = bounded_int_from_options(
                hop_opts,
                "max_depth",
                default=depth,
                minimum=1,
                maximum=10,
            )
            if isinstance(depth_raw, str):
                return depth_raw
            depth = depth_raw
        per = wiki_config.structural_hop_max_per_level
        if hop_opts.get("max_per_level") is not None:
            per_raw = bounded_int_from_options(
                hop_opts,
                "max_per_level",
                default=per,
                minimum=1,
                maximum=500,
            )
            if isinstance(per_raw, str):
                return per_raw
            per = per_raw

        def _hops() -> str:
            return format_hop_report_markdown(
                graph_path,
                seed_list,
                max_depth=depth,
                max_per_level=per,
            )

        return cap_llm_payload_chars(await asyncio.to_thread(_hops))

    dashboard_md: str = await asyncio.to_thread(
        build_dashboard_markdown,
        graph_path,
        wiki_config,
    )
    logger.bind(graph=graph_path).info("read_graph_data(dashboard) completed")
    return cap_llm_payload_chars(dashboard_md)


async def dispatch_search(
    method: SearchGraphMethod,
    query: str = "",
) -> str | dict[str, Any]:
    """Route ``search_graph`` by ``method``."""
    graph_path = graph_path_from_env()
    if not graph_path:
        if method == "journal_tasks":
            return {
                "ok": False,
                "error": graph_missing_text(),
                "items": [],
                "task_review_markdown": "",
            }
        if method == "resolve_entity":
            return graph_missing_dict()
        return graph_missing_text()

    if method == "bm25":
        bm_opts = parse_optional_json_query(query)
        keyword = str(bm_opts.get("keyword", query)).strip()
        if not keyword:
            return "For `method=bm25`, set `query` to search keywords or JSON with `keyword`."
        limit_raw = bounded_int_from_options(
            bm_opts,
            "limit",
            default=15,
            minimum=1,
            maximum=100,
        )
        if isinstance(limit_raw, str):
            return limit_raw
        limit = limit_raw
        return await asyncio.to_thread(
            format_keyword_query_markdown,
            graph_path,
            keyword,
            limit=limit,
            mode="bm25",
        )

    if method == "regex":
        rx_opts = parse_optional_json_query(query)
        pattern = str(rx_opts.get("pattern", query)).strip()
        if not pattern:
            return "For `method=regex`, set `query` to a regex pattern or JSON with `pattern`."
        rx_limit_raw = bounded_int_from_options(
            rx_opts,
            "limit",
            default=50,
            minimum=1,
            maximum=200,
        )
        if isinstance(rx_limit_raw, str):
            return rx_limit_raw
        rx_limit = rx_limit_raw
        return await asyncio.to_thread(
            format_regex_search_markdown,
            graph_path,
            pattern,
            limit=rx_limit,
        )

    if method == "unlinked_mentions":
        um_opts = parse_optional_json_query(query)
        max_hits_raw = bounded_int_from_options(
            um_opts,
            "max_hits_per_file",
            default=80,
            minimum=1,
            maximum=500,
        )
        if isinstance(max_hits_raw, str):
            return max_hits_raw
        max_hits = max_hits_raw
        max_titles_raw = bounded_int_from_options(
            um_opts,
            "max_titles",
            default=500,
            minimum=1,
            maximum=2000,
        )
        if isinstance(max_titles_raw, str):
            return max_titles_raw
        max_titles = max_titles_raw

        def _unlinked() -> dict[str, Any]:
            return scan_unlinked_mentions(
                graph_path,
                max_hits_per_file=max_hits,
                max_titles=max_titles,
            )

        return await asyncio.to_thread(_unlinked)

    if method == "resolve_entity":
        candidate = query.strip()
        if not candidate:
            return "For `method=resolve_entity`, set `query` to a page title or `alias::` name."

        def _resolve_entity() -> dict[str, object]:
            from ..graph.generational_cache import cached_build_alias_index

            root = Path(graph_path).expanduser().resolve(strict=False)
            idx = cached_build_alias_index(root)
            return idx.resolve(candidate).as_dict()

        return await asyncio.to_thread(_resolve_entity)

    j_opts = parse_optional_json_query(query)
    days_default_raw = j_opts.get("days", query.strip() or 7)
    days_raw = bounded_int_from_options(
        {"days": days_default_raw},
        "days",
        default=7,
        minimum=1,
        maximum=90,
    )
    if isinstance(days_raw, str):
        return days_raw
    days = days_raw

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


def _mutate_error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


async def dispatch_mutate(
    action: MutateGraphAction,
    target: str,
    payload: str,
) -> dict[str, Any]:
    """Route ``mutate_graph`` by ``action`` (headless on-disk writes)."""
    graph_path = graph_path_from_env()
    try:
        resolved_target = (
            resolve_target(graph_path, target)
            if graph_path and action != "append_journal"
            else target
        )
    except ValueError as exc:
        return _mutate_error(str(exc))

    if action == "write_outline":
        if not graph_path:
            return graph_missing_dict()
        parent_uuid = resolved_target.strip()
        if not parent_uuid:
            return {"ok": False, "error": "`target` must be the parent block UUID."}
        outline = parse_json_object(payload, field_name="payload")
        try:
            return await asyncio.to_thread(
                _headless_write_outline,
                graph_path,
                parent_uuid,
                outline,
            )
        except (ValueError, OSError, OCCConflictError) as exc:
            return _mutate_error(str(exc))

    if action == "edit_property":
        if not graph_path:
            return {
                **graph_missing_dict(),
                "dry_run": True,
                "match_count": 0,
                "previews": [],
                "previous_size_bytes": 0,
                "current_size_bytes": 0,
                "lines_changed": 0,
            }
        try:
            pipe_target = resolve_pipe_target(graph_path, target)
        except ValueError as exc:
            return _mutate_error(str(exc))
        target_parts = [p.strip() for p in pipe_target.split("|", 1)]
        if len(target_parts) != 2 or not target_parts[0] or not target_parts[1]:
            return {
                "ok": False,
                "error": "For edit_property, `target` must be `Page Title|block-uuid`.",
            }
        page_ref, block_uuid = target_parts[0], target_parts[1]
        prop_opts = parse_json_object(payload, field_name="payload")
        search = str(prop_opts.get("search", ""))
        replacement = str(prop_opts.get("replacement", ""))
        if not search:
            return {"ok": False, "error": "payload must include non-empty `search`."}

        try:
            page_path = graph_safe_page_path(graph_path, page_ref)
        except PathTraversalSecurityError as exc:
            return {"ok": False, "code": "security_violation", "error": str(exc)}
        except ValueError as exc:
            return _mutate_error(str(exc))
        baseline_mtime = read_file_mtime(page_path) if page_path.is_file() else None

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
                baseline_mtime=baseline_mtime,
            ).as_dict()

        return cast(dict[str, Any], await asyncio.to_thread(_edit))

    if action == "append_journal":
        if not graph_path:
            return graph_missing_dict()
        body = payload
        dry_run = True
        if payload.strip().startswith("{"):
            journal_opts = parse_json_object(payload, field_name="payload")
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

    parent_block = resolved_target.strip()
    if not parent_block:
        return {
            "ok": False,
            "error": "For inject_query, `target` must be the parent block UUID.",
        }
    inject_opts = parse_json_object(payload, field_name="payload")
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

    if not graph_path:
        return graph_missing_dict()

    occ = _occ_snapshot_for_block(graph_path, parent_block)
    try:
        new_uuid = await asyncio.to_thread(
            _headless_append_child,
            graph_path,
            parent_block,
            markdown,
            occ=occ,
        )
    except (ValueError, OSError, OCCConflictError) as exc:
        return _mutate_error(str(exc))

    return {
        "ok": True,
        "dry_run": False,
        "uuid": new_uuid,
        "markdown": markdown,
        "routing_hint": routing_hint_for_write_outline(),
    }


async def dispatch_refactor(
    action: RefactorBlocksAction,
    target_uuid: str,
    payload: str = "",
) -> dict[str, Any]:
    """Route ``refactor_blocks`` by ``action`` (headless on-disk rewrites)."""
    graph_path = graph_path_from_env()
    if not graph_path:
        return graph_missing_dict()

    refactor_opts = parse_optional_json_query(payload)
    dry_run = bool(refactor_opts.get("dry_run", True))
    try:
        resolved_uuid = target_uuid
        if "|" in target_uuid:
            resolved_uuid = resolve_pipe_target(graph_path, target_uuid)
        elif target_uuid.strip():
            resolved_uuid = resolve_target(graph_path, target_uuid)
    except ValueError as exc:
        return _mutate_error(str(exc))

    if action == "split_large":
        page_ref = resolved_uuid.strip() or None
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
        reparent_page = resolved_uuid.strip()
        if not reparent_page:
            return {"ok": False, "error": "For reparent, `target_uuid` must be the page title."}
        groups_raw = refactor_opts.get("groups")
        if groups_raw is None and payload.strip().startswith("["):
            groups_raw = loads_repaired_json(payload)
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

    flash_parts = [p.strip() for p in resolved_uuid.split("|", 1)]
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


async def dispatch_lint(
    wiki_config: MatrycaWikiConfig,
    linter_name: RunLinterName,
) -> str | dict[str, Any]:
    """Route ``run_linter`` by ``linter_name``."""
    graph_path = graph_path_from_env()
    if not graph_path:
        if linter_name == "unify_tags":
            return graph_missing_dict()
        return graph_missing_text()

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

    def _wiki() -> str:
        findings = lint_wiki_prefixed_pages(graph_path, wiki_config)
        return format_wiki_lint_report(findings, prefix=wiki_config.wiki_file_prefix)

    wiki_report: str = await asyncio.to_thread(_wiki)
    logger.bind(graph=graph_path).info("run_linter(full_wiki_scan) completed")
    return wiki_report


__all__ = [
    "dispatch_lint",
    "dispatch_mutate",
    "dispatch_read",
    "dispatch_refactor",
    "dispatch_search",
]

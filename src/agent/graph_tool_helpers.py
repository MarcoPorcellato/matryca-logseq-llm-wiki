"""Shared helpers and literal types for MCP mega-tools and the agent CLI."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

from ..graph.markdown_blocks import locate_block_by_uuid
from ..graph.path_sandbox import graph_safe_page_path
from ..utils.regex_policy import validate_regex_pattern

MAX_REGEX_SCAN_BYTES = 5_000_000
_REGEX_SCAN_TIMEOUT_SECONDS = 5.0

ReadGraphTarget = Literal[
    "page",
    "memory",
    "block_ast",
    "structural_hops",
    "dashboard",
    "xray_page",
]
SearchGraphMethod = Literal[
    "bm25",
    "regex",
    "unlinked_mentions",
    "journal_tasks",
    "resolve_entity",
]
MutateGraphAction = Literal["write_outline", "edit_property", "append_journal", "inject_query"]
RefactorBlocksAction = Literal["split_large", "reparent", "generate_flashcards"]
RunLinterName = Literal["unify_tags", "block_refs", "full_wiki_scan"]


def graph_path_from_env() -> str:
    return os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()


def graph_missing_text() -> str:
    return (
        "LOGSEQ_GRAPH_PATH is not set; cannot access the graph on disk. "
        "Set it to your Logseq graph root (the folder that contains `pages/`), then retry."
    )


def graph_missing_dict() -> dict[str, Any]:
    return {"ok": False, "code": "graph_missing", "hint": graph_missing_text()}


def parse_json_object(payload: str, *, field_name: str = "payload") -> dict[str, Any]:
    raw = payload.strip()
    if not raw:
        msg = f"`{field_name}` must be a non-empty JSON object"
        raise ValueError(msg)
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = f"`{field_name}` must decode to a JSON object"
        raise TypeError(msg)
    return cast(dict[str, Any], data)


def parse_optional_json_query(query: str) -> dict[str, Any]:
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


def _persistable_block_uuid(node: object) -> str:
    """Prefer on-disk ``id::`` (``source_uuid``) over parser session ``uuid``."""
    source_uuid = getattr(node, "source_uuid", None)
    if isinstance(source_uuid, str) and source_uuid.strip():
        return source_uuid.strip()
    node_uuid = getattr(node, "uuid", None)
    if isinstance(node_uuid, str) and node_uuid.strip():
        return node_uuid.strip()
    msg = "Parsed block is missing both `source_uuid` and `uuid`"
    raise ValueError(msg)


def _persistable_alias_map(
    roots: list[object],
    parser_alias_map: dict[int, str],
) -> dict[int, str]:
    """Map session aliases to UUIDs safe for on-disk ``id::`` lookups."""
    by_parser_uuid: dict[str, object] = {}

    def walk(node: object) -> None:
        parser_uuid = getattr(node, "uuid", None)
        if isinstance(parser_uuid, str) and parser_uuid:
            by_parser_uuid[parser_uuid] = node
        for child in getattr(node, "children", None) or []:
            walk(child)

    for root in roots:
        walk(root)

    persistable: dict[int, str] = {}
    for alias, parser_uuid in parser_alias_map.items():
        node = by_parser_uuid.get(parser_uuid)
        if node is None:
            continue
        persistable[alias] = _persistable_block_uuid(node)
    return persistable


def read_xray_page_markdown(graph_path: str, page_name: str) -> str:
    """Parse a page, assign ``[n]`` aliases, persist them, and return X-Ray markdown."""
    from logseq_matryca_parser.agent_press import SessionAliasRegistry, to_xray_markdown

    from ..rag.matryca_hooks import get_spatial_context, resolve_logseq_page_md
    from .alias_state import alias_file_path, save_alias_registry

    title = page_name.strip()
    if not title:
        msg = "For `target_type=xray_page`, set `query` to the Logseq page title."
        raise ValueError(msg)

    path = resolve_logseq_page_md(graph_path, title)
    parsed = get_spatial_context(str(path))
    roots = getattr(parsed, "root_nodes", None) or []
    if not roots:
        return f"# X-Ray: [[{title}]]\n\n_Empty page — no outline blocks to alias._\n"

    registry = SessionAliasRegistry()
    parser_alias_map = registry.generate_aliases(roots)
    body = to_xray_markdown(roots, registry)
    persistable = _persistable_alias_map(roots, parser_alias_map)
    persist_registry = SessionAliasRegistry()
    for alias, block_uuid in persistable.items():
        persist_registry._alias_to_uuid[alias] = block_uuid  # noqa: SLF001
        persist_registry._uuid_to_alias[block_uuid] = alias  # noqa: SLF001
    save_alias_registry(graph_path, persist_registry)
    alias_count = len(persistable)
    state_name = alias_file_path(graph_path).name
    return (
        f"# X-Ray: [[{title}]]\n\n"
        f"**Aliases:** {alias_count} block(s) mapped to `[0]`…`[{alias_count - 1}]` "
        f"in `{state_name}` at the graph root. "
        "Pass `[n]` as `target` or in `Page Title|[n]` for `mutate_graph` / `refactor_blocks`.\n\n"
        f"{body}\n"
    )


def read_block_ast_markdown(graph_path: str, query: str) -> str:
    """Return the on-disk Markdown subtree for one block (page title + ``id::`` UUID)."""
    from .alias_state import resolve_pipe_target

    resolved_query = resolve_pipe_target(graph_path, query)
    parts = [p.strip() for p in resolved_query.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        msg = (
            f"Invalid block_ast query format: {query!r}. "
            "Expected `Page Title|block-uuid` or `Page Title|[n]` (X-Ray alias)."
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


def _scan_pages_for_regex(
    graph_path: str,
    compiled: re.Pattern[str],
    *,
    limit: int,
) -> list[tuple[str, int, str]]:
    from ..graph.alias_index import is_scannable_graph_markdown

    root = Path(graph_path).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return []

    hits: list[tuple[str, int, str]] = []
    scanned_bytes = 0
    for path in sorted(pages.rglob("*.md")):
        if not path.is_file() or not is_scannable_graph_markdown(path, root):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned_bytes += len(body.encode("utf-8", errors="replace"))
        if scanned_bytes > MAX_REGEX_SCAN_BYTES:
            break
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(body.splitlines(), start=1):
            if compiled.search(line):
                hits.append((rel, line_no, line.strip()[:240]))
                if len(hits) >= limit:
                    return hits
        if len(hits) >= limit:
            break
    return hits


def format_regex_search_markdown(graph_path: str, pattern: str, *, limit: int = 50) -> str:
    """Vault-wide ``pages/**/*.md`` line scan (orchestration; not the spatial parser)."""
    try:
        compiled = validate_regex_pattern(pattern)
    except ValueError as exc:
        raise ValueError(f"Invalid regex in `query`: {exc}") from exc

    root = Path(graph_path).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return f"{graph_missing_text()}\n\n`pages/` directory is missing."

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_scan_pages_for_regex, graph_path, compiled, limit=limit)
        try:
            hits = future.result(timeout=_REGEX_SCAN_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            msg = f"regex scan timed out after {_REGEX_SCAN_TIMEOUT_SECONDS}s"
            raise ValueError(msg) from exc

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


__all__ = [
    "MutateGraphAction",
    "ReadGraphTarget",
    "RefactorBlocksAction",
    "RunLinterName",
    "SearchGraphMethod",
    "format_regex_search_markdown",
    "graph_missing_dict",
    "graph_missing_text",
    "graph_path_from_env",
    "parse_json_object",
    "parse_optional_json_query",
    "read_block_ast_markdown",
    "read_xray_page_markdown",
]

"""RAG-facing adapter for Logseq spatial context.

Indentation-aware structure, spatial nesting, ``id::`` UUID extraction, block
reference handling, and related graph semantics are implemented exclusively in
the external **logseq-matryca-parser** package (single source of truth). This
module stays a thin orchestration boundary so **matryca-logseq-llm-wiki** can
consume parsed pages without duplicating parser logic—aligned with publishing
that library on PyPI later.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from loguru import logger

try:
    from logseq_matryca_parser import LogosParser as _LogosParser
except ImportError:  # pragma: no cover - exercised when optional dep missing
    _LogosParser = None
    logger.warning(
        "Optional dependency `logseq-matryca-parser` is not installed. "
        "Spatial read tools will raise ImportError until you install it "
        "(see project `pyproject.toml`)."
    )


def _require_logos_parser() -> type[Any]:
    """Return :class:`LogosParser` or raise if the external parser is unavailable."""
    if _LogosParser is None:
        msg = (
            "logseq-matryca-parser is not installed. Install the `logseq-matryca-parser` "
            "package to enable spatial page reads."
        )
        raise ImportError(msg)
    return cast(type[Any], _LogosParser)


def get_spatial_context(file_path: str) -> Any:
    """Load ``file_path`` and return the parser-owned page graph.

    All heavy lifting is delegated to ``logseq_matryca_parser``. The parser
    class is resolved at call time so optional-install environments fail with a
    clear :class:`ImportError` only when this function is used.

    Args:
        file_path: Path to a Logseq ``.md`` page on disk (absolute or relative).

    Returns:
        The parsed graph model from the external package (currently
        ``LogosParser().parse_page_file(...)`` → ``LogseqPage``).

    Raises:
        ImportError: If ``logseq-matryca-parser`` is not installed.
        OSError: If the file cannot be read (propagated from the parser).
    """
    parser_cls = _require_logos_parser()
    return parser_cls().parse_page_file(file_path)


def _page_md_candidates(pages_dir: Path, page_name: str) -> list[Path]:
    """Return likely on-disk filenames for a Logseq page title (OG Markdown graph)."""
    candidates: list[Path] = [
        pages_dir / f"{page_name}.md",
        pages_dir / f"{quote(page_name, safe='')}.md",
    ]
    if "/" in page_name:
        candidates.append(pages_dir / f"{page_name.replace('/', '_')}.md")
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def resolve_logseq_page_md(graph_root: str | Path, page_name: str) -> Path:
    """Resolve ``page_name`` to an existing ``pages/*.md`` file under ``graph_root``.

    Args:
        graph_root: Logseq graph directory (contains a ``pages/`` folder).
        page_name: Page title as in Logseq (e.g. ``My Topic`` or ``A/B``).

    Returns:
        Absolute path to the page markdown file.

    Raises:
        FileNotFoundError: If ``pages/`` is missing or no candidate file exists.
    """
    root = Path(graph_root).expanduser().resolve()
    pages_dir = root / "pages"
    if not pages_dir.is_dir():
        msg = f"Logseq graph has no pages/ directory: {pages_dir}"
        raise FileNotFoundError(msg)

    for candidate in _page_md_candidates(pages_dir, page_name):
        if candidate.is_file():
            return candidate

    tried = ", ".join(str(p.name) for p in _page_md_candidates(pages_dir, page_name))
    msg = f"No page markdown found for {page_name!r} under {pages_dir}. Tried filenames: {tried}"
    raise FileNotFoundError(msg)


def _format_node_markdown(node: Any, depth: int) -> list[str]:
    """Format a single parsed node subtree as readable Markdown lines."""
    indent = "  " * depth
    lines: list[str] = []

    uuid_val = getattr(node, "uuid", None) or getattr(node, "source_uuid", None)
    clean = (getattr(node, "clean_text", None) or "").strip()
    raw_content = (getattr(node, "content", None) or "").strip()
    body = clean if clean else raw_content

    header_bits: list[str] = []
    if uuid_val:
        header_bits.append(f"`id::` {uuid_val}")
    task = getattr(node, "task_status", None)
    if task:
        header_bits.append(f"task: {task}")

    if header_bits:
        lines.append(f"{indent}- **Block** ({', '.join(header_bits)})")
    else:
        lines.append(f"{indent}- **Block**")

    if body:
        lines.append(f"{indent}  - **Text:** {body}")

    props = getattr(node, "properties", None) or {}
    if isinstance(props, dict) and props:
        prop_lines = [f"{k} → {v}" for k, v in props.items() if k != "id"]
        if prop_lines:
            lines.append(f"{indent}  - **Properties:**")
            for pl in prop_lines:
                lines.append(f"{indent}    - {pl}")

    wikilinks = getattr(node, "wikilinks", None) or []
    if wikilinks:
        lines.append(f"{indent}  - **Wikilinks:** {', '.join(str(w) for w in wikilinks)}")

    block_refs = getattr(node, "block_refs", None) or []
    if block_refs:
        lines.append(f"{indent}  - **Block refs:** {', '.join(str(r) for r in block_refs)}")

    children = getattr(node, "children", None) or []
    for child in children:
        lines.extend(_format_node_markdown(child, depth + 1))

    return lines


def _format_dict_node(node: dict[str, Any], depth: int) -> list[str]:
    """Format a dumped node dict as Markdown (parallel to :func:`_format_node_markdown`)."""
    indent = "  " * depth
    lines: list[str] = []

    uuid_val = node.get("uuid") or node.get("source_uuid")
    clean = str(node.get("clean_text") or "").strip()
    raw_content = str(node.get("content") or "").strip()
    body = clean if clean else raw_content

    header_bits: list[str] = []
    if uuid_val:
        header_bits.append(f"`id::` {uuid_val}")
    task = node.get("task_status")
    if task:
        header_bits.append(f"task: {task}")

    if header_bits:
        lines.append(f"{indent}- **Block** ({', '.join(header_bits)})")
    else:
        lines.append(f"{indent}- **Block**")

    if body:
        lines.append(f"{indent}  - **Text:** {body}")

    props = node.get("properties") or {}
    if isinstance(props, dict) and props:
        prop_lines = [f"{k} → {v}" for k, v in props.items() if k != "id"]
        if prop_lines:
            lines.append(f"{indent}  - **Properties:**")
            for pl in prop_lines:
                lines.append(f"{indent}    - {pl}")

    wikilinks = node.get("wikilinks") or []
    if isinstance(wikilinks, list) and wikilinks:
        lines.append(f"{indent}  - **Wikilinks:** {', '.join(str(w) for w in wikilinks)}")

    block_refs = node.get("block_refs") or []
    if isinstance(block_refs, list) and block_refs:
        lines.append(f"{indent}  - **Block refs:** {', '.join(str(r) for r in block_refs)}")

    children = node.get("children") or []
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                lines.extend(_format_dict_node(cast(dict[str, Any], child), depth + 1))
            else:
                lines.extend(_format_node_markdown(child, depth + 1))

    return lines


def _format_parsed_page_markdown(parsed: Any) -> str:
    """Turn parser output (model object, dict dump, or raw string) into LLM-oriented Markdown."""
    if isinstance(parsed, str):
        return parsed.strip()

    if isinstance(parsed, dict):
        data = cast(dict[str, Any], parsed)
        lines = ["# Parsed page (dictionary view)", ""]
        title = str(data.get("title", "unknown"))
        lines.append(f"**Title:** [[{title}]]")
        lines.append("")
        raw = data.get("raw_content")
        if isinstance(raw, str) and raw.strip():
            lines.append("## Raw markdown")
            lines.append("")
            lines.append("```markdown")
            lines.append(raw.rstrip())
            lines.append("```")
            lines.append("")
        roots = data.get("root_nodes") or []
        if roots:
            lines.append("## Outline (spatial tree)")
            lines.append("")
            for item in roots:
                if isinstance(item, dict):
                    lines.extend(_format_dict_node(cast(dict[str, Any], item), depth=0))
                else:
                    lines.extend(_format_node_markdown(item, depth=0))
        return "\n".join(lines).strip()

    lines_out: list[str] = []
    title = getattr(parsed, "title", None) or "unknown"
    lines_out.append(f"# Spatial view: [[{title}]]")
    lines_out.append("")

    src = getattr(parsed, "source_path", None)
    if src:
        lines_out.append(f"**Source file:** `{src}`")
        lines_out.append("")

    page_props = getattr(parsed, "properties", None) or {}
    if isinstance(page_props, dict) and page_props:
        lines_out.append("## Page properties")
        lines_out.append("")
        for k, v in page_props.items():
            lines_out.append(f"- `{k}` → {v}")
        lines_out.append("")

    refs = getattr(parsed, "refs", None) or []
    if refs:
        lines_out.append("## Page-level references")
        lines_out.append("")
        lines_out.append(", ".join(str(r) for r in refs))
        lines_out.append("")

    roots = getattr(parsed, "root_nodes", None) or []
    if roots:
        lines_out.append("## Outline (indentation / block tree)")
        lines_out.append("")
        for root in roots:
            lines_out.extend(_format_node_markdown(root, depth=0))
    else:
        raw = getattr(parsed, "raw_content", None)
        if isinstance(raw, str) and raw.strip():
            lines_out.append("## Raw markdown (no structured roots)")
            lines_out.append("")
            lines_out.append("```markdown")
            lines_out.append(raw.rstrip())
            lines_out.append("```")

    return "\n".join(lines_out).strip()


async def get_page_spatial_context(page_name: str, graph_path: str) -> str:
    """Read a Logseq page from disk and return spatial context as Markdown.

    Resolves ``{graph_path}/pages/<Page>.md`` using common Logseq filename rules,
    parses via ``logseq-matryca-parser``, and formats a hierarchy-friendly summary
    for LLM consumption.

    Args:
        page_name: Logseq page title (as shown in the UI / wikilinks).
        graph_path: Absolute path to the Logseq graph root directory.

    Returns:
        Markdown string describing page metadata and the nested block tree.

    Raises:
        ImportError: If the external parser package is not installed.
        FileNotFoundError: If the page file cannot be resolved under ``pages/``.
        OSError: If the file cannot be read.
    """
    path = resolve_logseq_page_md(graph_path, page_name)
    logger.bind(page=page_name, path=str(path)).debug("Resolved Logseq page path for spatial read")

    def _load_and_format() -> str:
        parsed = get_spatial_context(str(path))
        return _format_parsed_page_markdown(parsed)

    return await asyncio.to_thread(_load_and_format)


__all__ = [
    "get_page_spatial_context",
    "get_spatial_context",
    "resolve_logseq_page_md",
]

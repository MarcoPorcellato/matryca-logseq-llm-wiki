"""Fast L1 memory: small Markdown files loaded every session (llm-wiki L1 layer)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ..config import MatrycaWikiConfig
from .llm_context_payload import cap_llm_payload_chars

# Guardrails so agents cannot accidentally load huge trees into context.
_MAX_FILES = 32
_MAX_BYTES_PER_FILE = 64 * 1024
_MAX_BYTES_TOTAL = 256 * 1024


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def collect_l1_markdown_paths(
    *,
    matryca_l1_path: str | None = None,
    logseq_graph_path: str | None = None,
    memory_path_from_yaml: str | None = None,
) -> list[Path]:
    """Resolve which Markdown files constitute L1 for this session.

    Resolution order:

    1. ``MATRYCA_L1_PATH`` — if it points to a ``.md`` file, that file alone; if a
       directory, all ``*.md`` in that directory (non-recursive), sorted by name.
    2. Else ``memory_path_from_yaml`` (from ``matryca-wiki.yml``) when non-empty.
    3. Else ``<parent of LOGSEQ_GRAPH_PATH>/matryca-l1/*.md`` when that directory
       exists.

    Args:
        matryca_l1_path: Raw ``MATRYCA_L1_PATH`` env value (optional).
        logseq_graph_path: Raw ``LOGSEQ_GRAPH_PATH`` env value (optional).
        memory_path_from_yaml: ``memory_path`` from :class:`~src.config.MatrycaWikiConfig`.

    Returns:
        Ordered list of files to read (may be empty).
    """
    l1_raw = (
        matryca_l1_path if matryca_l1_path is not None else os.environ.get("MATRYCA_L1_PATH", "")
    ).strip()
    if not l1_raw and memory_path_from_yaml:
        l1_raw = memory_path_from_yaml.strip()
    if l1_raw:
        p = _expand(l1_raw)
        if p.is_file():
            return [p] if p.suffix.lower() == ".md" else []
        if p.is_dir():
            return sorted(p.glob("*.md"))[:_MAX_FILES]

    graph_raw = (
        logseq_graph_path
        if logseq_graph_path is not None
        else os.environ.get("LOGSEQ_GRAPH_PATH", "")
    ).strip()
    if graph_raw:
        fallback = _expand(graph_raw).parent / "matryca-l1"
        if fallback.is_dir():
            return sorted(fallback.glob("*.md"))[:_MAX_FILES]

    return []


def read_l1_memory_text(
    paths: list[Path],
    *,
    max_files: int = _MAX_FILES,
    max_bytes_per_file: int = _MAX_BYTES_PER_FILE,
    max_bytes_total: int = _MAX_BYTES_TOTAL,
) -> tuple[list[str], str]:
    """Read UTF-8 Markdown from ``paths`` with size limits.

    Args:
        paths: Files to read (typically from :func:`collect_l1_markdown_paths`).
        max_files: Maximum number of files to read from ``paths``.
        max_bytes_per_file: Per-file byte cap (excess truncated with a notice).
        max_bytes_total: Total byte budget across all files.

    Returns:
        Tuple of (relative path labels for files actually read, concatenated Markdown
        body for the agent).
    """
    labels: list[str] = []
    chunks: list[str] = []
    total = 0

    for path in paths[:max_files]:
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            chunks.append(f"<!-- skipped (unreadable): {path} -->\n")
            continue

        truncated = False
        if len(raw) > max_bytes_per_file:
            raw = raw[:max_bytes_per_file]
            truncated = True

        if total + len(raw) > max_bytes_total:
            remaining = max_bytes_total - total
            if remaining <= 0:
                chunks.append("\n\n<!-- L1 total size cap reached; further files omitted. -->\n")
                break
            raw = raw[:remaining]
            truncated = True

        text = raw.decode("utf-8", errors="replace")
        labels.append(path.name)
        total += len(raw)
        notice = " _(truncated to size limit)_" if truncated else ""
        chunks.append(f"## L1: `{path.name}`{notice}\n\n{text.rstrip()}\n")

    if not labels:
        return [], ""

    header = f"# L1 memory (fast context)\n\n**Files loaded:** {', '.join(labels)}\n\n---\n\n"
    return labels, cap_llm_payload_chars(header + "\n".join(chunks).rstrip() + "\n")


def read_l1_memory_from_env(wiki_config: MatrycaWikiConfig | None = None) -> tuple[list[str], str]:
    """Load L1 using environment plus optional :class:`~src.config.MatrycaWikiConfig`."""
    mem_yaml = wiki_config.memory_path if wiki_config else None
    paths = collect_l1_markdown_paths(memory_path_from_yaml=mem_yaml)
    return read_l1_memory_text(paths)


async def read_l1_memory_async(
    wiki_config: MatrycaWikiConfig | None = None,
) -> tuple[list[str], str]:
    """Async wrapper that offloads disk reads to a worker thread."""
    return await asyncio.to_thread(read_l1_memory_from_env, wiki_config)


__all__ = [
    "collect_l1_markdown_paths",
    "read_l1_memory_async",
    "read_l1_memory_from_env",
    "read_l1_memory_text",
]

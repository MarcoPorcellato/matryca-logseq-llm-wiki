"""Lightweight relevance over on-disk Logseq pages (BM25 / substring; no vector DB)."""

from __future__ import annotations

import re
from pathlib import Path

_TOKEN = re.compile(r"[0-9a-z]+", re.IGNORECASE)


def _iter_page_files(graph_root: Path) -> list[Path]:
    from ..graph.alias_index import iter_scannable_pages_markdown

    return iter_scannable_pages_markdown(graph_root)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens for lexical scoring."""
    return [m.group(0).lower() for m in _TOKEN.finditer(text)]


def rank_pages_by_keyword(
    graph_root: str | Path,
    keyword: str,
    *,
    limit: int = 15,
) -> list[tuple[str, int]]:
    """Count case-insensitive substring hits per ``pages/**/*.md`` file."""
    needle = keyword.strip().lower()
    if not needle:
        return []

    root = Path(graph_root).expanduser().resolve(strict=False)
    scored: list[tuple[str, int]] = []
    for path in _iter_page_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        hits = text.count(needle)
        if hits:
            rel = path.relative_to(root).as_posix()
            scored.append((rel, hits))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[: max(1, min(limit, 100))]


def rank_pages_by_bm25(
    graph_root: str | Path,
    query: str,
    *,
    limit: int = 15,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[str, float]]:
    """Okapi BM25 over per-page token bags (in-memory, pure Python)."""
    from src.graph.generational_cache import get_cached_bm25_corpus, score_bm25_query

    root = Path(graph_root).expanduser().resolve(strict=False)
    corpus = get_cached_bm25_corpus(root)
    return score_bm25_query(corpus, query, limit=limit, k1=k1, b=b)


def format_keyword_query_markdown(
    graph_root: str | Path,
    keyword: str,
    *,
    limit: int = 15,
    mode: str = "bm25",
) -> str:
    """Readable Markdown list for MCP ``query_logseq_pages_local``."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    m = mode.strip().lower()
    lines = [
        "# Local page query",
        "",
        f"- **Graph:** `{root}`",
        f"- **Query:** `{keyword.strip()}`",
        f"- **Mode:** `{m}`",
        "",
    ]

    if m in ("bm25", "tfidf", "relevance"):
        bm_rows = rank_pages_by_bm25(root, keyword, limit=limit)
        lines.append(f"- **Matches:** {len(bm_rows)}")
        lines.append("")
        lines.append("## Ranked pages (BM25)")
        lines.append("")
        if not bm_rows:
            lines.append("_No lexical overlap in `pages/**/*.md`._")
            return "\n".join(lines) + "\n"
        for rel, score in bm_rows:
            lines.append(f"- `{rel}` — **{score:.4f}**")
        lines.append("")
        return "\n".join(lines) + "\n"

    sub_rows = rank_pages_by_keyword(root, keyword, limit=limit)
    lines.append(f"- **Matches:** {len(sub_rows)}")
    lines.append("")
    lines.append("## Ranked pages (substring count)")
    lines.append("")
    if not sub_rows:
        lines.append("_No hits in `pages/**/*.md`._")
        return "\n".join(lines) + "\n"

    for rel, score in sub_rows:
        lines.append(f"- `{rel}` — **{score}** hits")
    lines.append("")
    return "\n".join(lines) + "\n"


__all__ = [
    "format_keyword_query_markdown",
    "rank_pages_by_bm25",
    "rank_pages_by_keyword",
    "tokenize",
]

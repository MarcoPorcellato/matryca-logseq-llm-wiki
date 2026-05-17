"""Lightweight keyword scan over on-disk Logseq pages (no vector DB)."""

from __future__ import annotations

from pathlib import Path


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
    pages = root / "pages"
    if not pages.is_dir():
        return []

    scored: list[tuple[str, int]] = []
    for path in pages.rglob("*.md"):
        if not path.is_file():
            continue
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


def format_keyword_query_markdown(
    graph_root: str | Path,
    keyword: str,
    *,
    limit: int = 15,
) -> str:
    """Readable Markdown table-style list for MCP."""
    rows = rank_pages_by_keyword(graph_root, keyword, limit=limit)
    lines = [
        "# Local keyword scan",
        "",
        f"- **Graph:** `{Path(graph_root).expanduser().resolve(strict=False)}`",
        f"- **Keyword:** `{keyword.strip()}`",
        f"- **Matches:** {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("_No hits in `pages/**/*.md`._")
        return "\n".join(lines) + "\n"

    lines.append("## Ranked pages (substring count)")
    lines.append("")
    for rel, score in rows:
        lines.append(f"- `{rel}` — **{score}** hits")
    lines.append("")
    return "\n".join(lines) + "\n"


__all__ = ["format_keyword_query_markdown", "rank_pages_by_keyword"]

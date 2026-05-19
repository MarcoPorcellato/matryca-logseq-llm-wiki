"""Aggregate on-disk graph stats into a Logseq-friendly dashboard outline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from logseq_matryca_parser.graph import LogseqGraph

from ..config import MatrycaWikiConfig
from .block_ref_lint import lint_block_refs_in_graph


@dataclass(frozen=True, slots=True)
class DashboardStats:
    """Numeric snapshot of graph health."""

    graph_root: str
    page_count: int
    id_declaration_tally: int
    broken_block_refs: int
    newest_page_mtime_iso: str | None


def _iter_page_files(pages: Path) -> list[Path]:
    if not pages.is_dir():
        return []
    return sorted(p for p in pages.rglob("*.md") if p.is_file())


def collect_dashboard_stats(graph_root: str | Path) -> DashboardStats:
    """Scan ``pages/**/*.md`` for counts and block-ref lint totals."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages = root / "pages"
    paths = _iter_page_files(pages)

    id_tally = 0
    newest: float | None = None
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)

    graph = LogseqGraph.load_directory(root)
    id_tally = sum(
        1 for node in graph.query().execute() if node.source_uuid or node.properties.get("id")
    )

    lint = lint_block_refs_in_graph(root)
    broken = sum(1 for b in lint.broken if b.reason in {"invalid_uuid", "unresolved"})

    newest_iso: str | None = None
    if newest is not None:
        newest_iso = datetime.fromtimestamp(newest, tz=UTC).strftime("%Y-%m-%dT%H:%MZ")

    return DashboardStats(
        graph_root=str(root),
        page_count=len(paths),
        id_declaration_tally=id_tally,
        broken_block_refs=broken,
        newest_page_mtime_iso=newest_iso,
    )


def format_dashboard_outline(
    stats: DashboardStats,
    wiki_config: MatrycaWikiConfig | None = None,
) -> str:
    """Return outline Markdown suitable for a **[[Matryca Dashboard]]** page body."""
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    mtime = stats.newest_page_mtime_iso or "n/a"
    title = wiki_config.dashboard_page_title if wiki_config else "Matryca Dashboard"
    lines = [
        "- type:: hub",
        f"- updated:: {today}",
        f"- ## {title}",
        f"  - **Graph root:** `{stats.graph_root}`",
        f"  - **Pages (`pages/**/*.md`):** {stats.page_count}",
        f"  - **`id::` declarations (v4, counted):** {stats.id_declaration_tally}",
        f"  - **Broken `((uuid))` refs:** {stats.broken_block_refs}",
        f"  - **Newest page mtime (UTC):** {mtime}",
        "  - _Regenerate by running the MCP tool `render_logseq_dashboard`._",
    ]
    if wiki_config and wiki_config.namespaces:
        lines.append("  - **Configured namespaces (matryca-wiki.yml):**")
        for ns in wiki_config.namespaces:
            lines.append(f"    - [[{ns}]]")
    return "\n".join(lines) + "\n"


def build_dashboard_markdown(
    graph_root: str | Path,
    wiki_config: MatrycaWikiConfig | None = None,
) -> str:
    """Convenience: stats + outline in one call."""
    stats = collect_dashboard_stats(graph_root)
    return format_dashboard_outline(stats, wiki_config=wiki_config)


__all__ = [
    "DashboardStats",
    "build_dashboard_markdown",
    "collect_dashboard_stats",
    "format_dashboard_outline",
]

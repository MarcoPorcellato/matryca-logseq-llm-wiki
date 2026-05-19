"""Scan Logseq Markdown for broken ``((uuid))`` block refs via the parser graph index."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from logseq_matryca_parser.graph import LogseqGraph

from .logseq_uuid import is_logseq_block_uuid


@dataclass(frozen=True, slots=True)
class BrokenBlockRef:
    """A block reference that is not backed by any node in the global registry."""

    file_path: str
    ref_uuid: str
    reason: str  # "invalid_uuid" | "unresolved"


@dataclass(frozen=True, slots=True)
class BlockRefLintResult:
    """Aggregated scan of ``pages/**/*.md`` under a Logseq graph root."""

    pages_scanned: int
    defined_ids: int
    refs_checked: int
    broken: list[BrokenBlockRef]

    def format_report(self) -> str:
        """Human-readable Markdown summary for MCP tools."""
        lines = [
            "# Block reference lint (`((uuid))`)",
            "",
            f"- **Pages scanned:** {self.pages_scanned}",
            f"- **Distinct block nodes indexed:** {self.defined_ids}",
            f"- **Block refs parsed:** {self.refs_checked}",
            f"- **Issues:** {len(self.broken)}",
            "",
        ]
        if not self.broken:
            lines.append("No broken `((uuid))` references detected in the graph index.")
            return "\n".join(lines)

        lines.append("## Findings")
        lines.append("")
        for item in self.broken:
            if item.reason == "missing_pages_directory":
                lines.append(
                    f"- **{item.reason}** — expected a ``pages/`` directory under the graph "
                    f"root; looked for `{item.file_path}`.",
                )
            else:
                lines.append(
                    f"- `{item.file_path}` — `(({item.ref_uuid}))` — **{item.reason}**",
                )
        lines.append("")
        lines.append(
            "_Note: structural lint via `LogseqGraph.get_broken_references()`; "
            "journals and non-indexed paths follow parser discovery rules._"
        )
        return "\n".join(lines)


def _relative_source_path(graph_root: Path, source_path: str | None) -> str:
    if not source_path:
        return ""
    try:
        return Path(source_path).resolve().relative_to(graph_root).as_posix()
    except ValueError:
        return Path(source_path).name


def _ref_registered(graph: LogseqGraph, ref: str) -> bool:
    return graph.get_node_by_embed_ref(ref) is not None


def lint_block_refs_in_graph(graph_root: str | Path) -> BlockRefLintResult:
    """Flag ``((uuid))`` refs whose targets are absent from the parser's global node registry."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return BlockRefLintResult(
            pages_scanned=0,
            defined_ids=0,
            refs_checked=0,
            broken=[
                BrokenBlockRef(
                    file_path=str(pages),
                    ref_uuid="",
                    reason="missing_pages_directory",
                ),
            ],
        )

    graph = LogseqGraph.load_directory(root)
    pages_scanned = sum(1 for path in pages.rglob("*.md") if path.is_file())
    all_nodes = graph.query().execute()
    defined_ids = len(all_nodes)
    refs_checked = sum(len(node.block_refs) for node in all_nodes)

    broken: list[BrokenBlockRef] = []
    for node in graph.get_broken_references():
        rel = _relative_source_path(root, node.source_path)
        for ref in node.block_refs:
            if _ref_registered(graph, ref):
                continue
            reason = "invalid_uuid" if not is_logseq_block_uuid(ref) else "unresolved"
            broken.append(
                BrokenBlockRef(
                    file_path=rel,
                    ref_uuid=ref.lower(),
                    reason=reason,
                ),
            )

    return BlockRefLintResult(
        pages_scanned=pages_scanned,
        defined_ids=defined_ids,
        refs_checked=refs_checked,
        broken=broken,
    )


__all__ = [
    "BlockRefLintResult",
    "BrokenBlockRef",
    "lint_block_refs_in_graph",
]

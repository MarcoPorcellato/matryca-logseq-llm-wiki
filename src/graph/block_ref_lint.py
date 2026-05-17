"""Scan Logseq Markdown for ``((uuid))`` block refs missing a defining ``id::``."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

# Logseq block references: ((xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx))
_BLOCK_REF_RE = re.compile(
    r"\(\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)\)",
)
# Block ids declared as Logseq properties (line-oriented; avoids parsing full outline).
_ID_DECL_RE = re.compile(
    r"(?im)^\s*id::\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$",
)


def _is_uuid_v4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4


def collect_id_declarations(text: str) -> set[str]:
    """Return lowercase UUID v4 strings declared via ``id::`` properties."""
    found: set[str] = set()
    for match in _ID_DECL_RE.finditer(text):
        raw = match.group(1).strip()
        if _is_uuid_v4(raw):
            found.add(raw.lower())
    return found


def collect_block_ref_targets(text: str) -> list[tuple[str, bool]]:
    """Return each ``((uuid))`` target with whether the bracketed string is UUID v4."""
    out: list[tuple[str, bool]] = []
    for match in _BLOCK_REF_RE.finditer(text):
        raw = match.group(1)
        out.append((raw.lower(), _is_uuid_v4(raw)))
    return out


@dataclass(frozen=True, slots=True)
class BrokenBlockRef:
    """A block reference that is not backed by any scanned ``id::``."""

    file_path: str
    ref_uuid: str
    reason: str  # "invalid_uuid_v4" | "unresolved"


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
            f"- **Distinct `id::` (v4) seen:** {self.defined_ids}",
            f"- **Block refs parsed:** {self.refs_checked}",
            f"- **Issues:** {len(self.broken)}",
            "",
        ]
        if not self.broken:
            lines.append("No broken `((uuid))` references detected (among declared `id::`).")
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
            "_Note: only `pages/**/*.md` are scanned; journals or other folders are ignored._"
        )
        return "\n".join(lines)


def lint_block_refs_in_graph(graph_root: str | Path) -> BlockRefLintResult:
    """Two-pass scan: collect all ``id::`` v4 ids, then flag ``((uuid))`` refs not in that set.

    Args:
        graph_root: Logseq graph directory (must contain ``pages/``).

    Returns:
        :class:`BlockRefLintResult` suitable for :meth:`BlockRefLintResult.format_report`.
    """
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

    global_ids: set[str] = set()
    file_texts: list[tuple[Path, str]] = []
    scanned = 0

    for path in sorted(pages.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        global_ids |= collect_id_declarations(text)
        file_texts.append((path, text))

    broken: list[BrokenBlockRef] = []
    refs_checked = 0

    for path, text in file_texts:
        rel = str(path.relative_to(root))
        for ref_lower, is_v4 in collect_block_ref_targets(text):
            refs_checked += 1
            if not is_v4:
                broken.append(
                    BrokenBlockRef(
                        file_path=rel,
                        ref_uuid=ref_lower,
                        reason="invalid_uuid_v4",
                    ),
                )
                continue
            if ref_lower not in global_ids:
                broken.append(
                    BrokenBlockRef(
                        file_path=rel,
                        ref_uuid=ref_lower,
                        reason="unresolved",
                    ),
                )

    return BlockRefLintResult(
        pages_scanned=scanned,
        defined_ids=len(global_ids),
        refs_checked=refs_checked,
        broken=broken,
    )


__all__ = [
    "BlockRefLintResult",
    "BrokenBlockRef",
    "collect_block_ref_targets",
    "collect_id_declarations",
    "lint_block_refs_in_graph",
]

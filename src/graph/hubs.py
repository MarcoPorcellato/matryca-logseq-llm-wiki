"""Group flat Logseq ``pages/*.md`` filenames by triple-underscore namespace segments."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..config import MatrycaWikiConfig
from .page_path import filename_to_page_title


def build_namespace_index_markdown(
    graph_root: str | Path,
    wiki_config: MatrycaWikiConfig,
) -> str:
    """Return hub-style outline listing pages grouped by first ``___`` segment."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return "# Namespace index\n\n(No `pages/` directory under graph root.)\n"

    groups: dict[str, list[str]] = defaultdict(list)
    for path in sorted(pages.glob("*.md")):
        stem = path.stem
        key = stem.split("___", maxsplit=1)[0] if "___" in stem else "(flat)"
        groups[key].append(path.name)

    lines = [
        "# Namespace index",
        "",
        "- type:: hub",
        f"- wiki_file_prefix:: `{wiki_config.wiki_file_prefix}`",
        "",
    ]
    for ns in sorted(groups):
        lines.append(f"- ## {ns}")
        for fn in groups[ns][:200]:
            title = filename_to_page_title(fn)
            lines.append(f"  - [[{title}]]")
        if len(groups[ns]) > 200:
            lines.append("  - _(list truncated at 200 files)_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["build_namespace_index_markdown"]

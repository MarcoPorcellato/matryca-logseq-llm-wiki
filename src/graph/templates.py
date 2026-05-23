"""Read Logseq ``templates/*.md`` for agent-side template hydration (Templater-style)."""

from __future__ import annotations

from pathlib import Path

from .path_sandbox import (
    SECURITY_VIOLATION_MSG,
    PathTraversalSecurityError,
    assert_path_within_graph,
    resolved_graph_root,
)


def _safe_templates_dir(graph_root: Path, subdir: str) -> Path:
    root = resolved_graph_root(graph_root)
    rel = Path(subdir.strip().replace("\\", "/").strip("/"))
    td = root.joinpath(*rel.parts) if rel.parts else root
    return assert_path_within_graph(td, root)


def list_logseq_templates(graph_root: str | Path, *, subdir: str = "templates") -> list[str]:
    """Basenames ``*.md`` under ``<graph>/<subdir>/``."""
    td = _safe_templates_dir(Path(graph_root), subdir)
    if not td.is_dir():
        return []
    return sorted(p.name for p in sorted(td.glob("*.md")) if p.is_file())


def read_logseq_template(
    graph_root: str | Path,
    template_name: str,
    *,
    subdir: str = "templates",
) -> tuple[str, str]:
    """Return ``(relative_path, utf8_text)`` for one template file.

    Raises:
        FileNotFoundError: When the template is missing.
        ValueError: When ``template_name`` is not a plain basename.
    """
    name = template_name.strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        msg = "template_name must be a plain filename (e.g. daily.md)"
        raise ValueError(msg)
    if not name.endswith(".md"):
        name = f"{name}.md"

    td = _safe_templates_dir(Path(graph_root), subdir)
    path = assert_path_within_graph(td / name, graph_root)
    if not path.is_relative_to(td):
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)

    if not path.is_file():
        msg = f"template not found: {path}"
        raise FileNotFoundError(msg)

    text = path.read_text(encoding="utf-8", errors="replace")
    rel = path.relative_to(Path(graph_root).expanduser().resolve(strict=False))
    return (rel.as_posix(), text)


def _edn_escape_string(value: str) -> str:
    """Escape a user fragment for safe inclusion inside an EDN double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def advanced_query_preset_open_markers() -> str:
    """Inner EDN for blocks whose marker is TODO, LATER, or WAITING (live dashboard)."""
    return (
        '{:title "Open markers (TODO / LATER / WAITING)"\n'
        " :query [:find (pull ?b [*])\n"
        "         :where\n"
        "         [?b :block/marker ?m]\n"
        '         [(contains? #{"TODO" "LATER" "WAITING"} ?m)]]}'
    )


def advanced_query_preset_pages_tagged(tag: str) -> str:
    """Inner EDN: blocks whose content includes ``#tag`` (uses ``clojure.string/includes?``)."""
    bare = tag.lstrip("#").strip()
    safe_title = _edn_escape_string(bare)
    needle = f"#{bare}"
    needle_lit = _edn_escape_string(needle)
    return (
        f'{{:title "Blocks mentioning #{safe_title}"\n'
        f' :inputs ["{needle_lit}"]\n'
        " :query [:find (pull ?b [*])\n"
        "         :in $ ?needle\n"
        "         :where\n"
        "         [?b :block/content ?c]\n"
        "         [(clojure.string/includes? ?c ?needle)]]}"
    )


__all__ = [
    "advanced_query_preset_open_markers",
    "advanced_query_preset_pages_tagged",
    "list_logseq_templates",
    "read_logseq_template",
]

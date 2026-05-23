"""Heal dangling wikilinks by seeding minimal contextual pages (append-only create)."""

from __future__ import annotations

from pathlib import Path

from ...agent.plumber_config import PlumberLintConfig, apply_thermal_pause_cognitive
from ...graph.markdown_blocks import atomic_write_bytes
from ...graph.page_properties import inject_page_properties, stamp_plumber_authored_page
from ...graph.page_write_lock import page_rmw_lock
from ._shared import (
    ModuleOutcome,
    context_around_wikilink,
    extract_wikilink_targets,
    page_file_exists,
    resolve_page_path,
)


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def run_dangling_healer(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    content: str,
    *,
    llm: object,
    max_words: int,
    config: PlumberLintConfig | None = None,
) -> ModuleOutcome:
    """Create seed pages for wikilinks that do not yet exist on disk."""
    outcome = ModuleOutcome()
    if not hasattr(llm, "generate_contextual_seed"):
        return outcome

    for target in extract_wikilink_targets(content):
        if page_file_exists(graph_root, target):
            continue
        ctx = context_around_wikilink(content, target)
        seed = llm.generate_contextual_seed(
            link_title=target,
            source_page=page_title,
            context=ctx,
            max_words=max_words,
        )
        apply_thermal_pause_cognitive(config)
        definition = _truncate_words(seed.definition.strip(), max_words)
        if not definition:
            continue
        new_path = resolve_page_path(graph_root, target)
        if new_path is None:
            continue
        body = f"- {definition}\n"
        body = inject_page_properties(
            body,
            {
                "seeded-from": f"[[{page_title}]]",
                "matryca-seed": "contextual dangling-link healer",
            },
        )
        body = stamp_plumber_authored_page(body)
        with page_rmw_lock(new_path):
            if new_path.is_file():
                continue
            new_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(new_path, body.encode("utf-8"), graph_root=graph_root)
        outcome.pages_created.append(target)
        outcome.details.append(f"seeded:{target}")
    return outcome


__all__ = ["run_dangling_healer"]

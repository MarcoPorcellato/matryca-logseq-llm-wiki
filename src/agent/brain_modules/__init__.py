"""Matryca Brain cognitive lint plugins (env-gated)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..brain_config import BrainLintConfig
from .auto_split import run_auto_split
from .dangling_healer import run_dangling_healer
from .entity_consolidation import run_entity_consolidation
from .marpa_framework import run_marpa_framework
from .property_hygiene import run_property_hygiene


@dataclass
class CognitiveLintOutcome:
    """Aggregate result from all enabled cognitive modules."""

    modules_run: list[str] = field(default_factory=list)
    pages_created: list[str] = field(default_factory=list)
    pages_modified: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def run_cognitive_lint_pipeline(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    content: str,
    *,
    llm: object,
    config: BrainLintConfig,
) -> CognitiveLintOutcome:
    """Run enabled cognitive lint modules before the semantic index pass."""
    outcome = CognitiveLintOutcome()
    if not config.any_enabled:
        return outcome

    if config.marpa_framework:
        sub = run_marpa_framework(
            graph_root,
            page_path,
            page_title,
            content,
            llm=llm,
        )
        outcome.modules_run.append("marpa_framework")
        outcome.pages_modified.extend(sub.pages_modified)
        outcome.details.extend(sub.details)
        if page_path.is_file():
            content = page_path.read_text(encoding="utf-8", errors="replace")

    if config.heal_dangling:
        sub = run_dangling_healer(
            graph_root,
            page_path,
            page_title,
            content,
            llm=llm,
            max_words=config.dangling_max_words,
        )
        outcome.modules_run.append("heal_dangling")
        outcome.pages_created.extend(sub.pages_created)
        outcome.pages_modified.extend(sub.pages_modified)
        outcome.details.extend(sub.details)

    if config.entity_consolidation:
        sub = run_entity_consolidation(
            graph_root,
            page_path,
            page_title,
            content,
            llm=llm,
            threshold=config.similarity_threshold,
        )
        outcome.modules_run.append("entity_consolidation")
        outcome.pages_modified.extend(sub.pages_modified)
        outcome.details.extend(sub.details)

    if config.auto_split:
        sub = run_auto_split(
            graph_root,
            page_path,
            page_title,
            threshold=config.split_block_threshold,
        )
        outcome.modules_run.append("auto_split")
        outcome.pages_created.extend(sub.pages_created)
        outcome.pages_modified.extend(sub.pages_modified)
        outcome.details.extend(sub.details)
        if page_path.is_file():
            content = page_path.read_text(encoding="utf-8", errors="replace")

    if config.property_hygiene:
        sub = run_property_hygiene(
            graph_root,
            page_path,
            page_title,
            content,
            llm=llm,
            rules_path=config.property_rules_path,
            infer_missing=config.infer_missing_properties,
        )
        outcome.modules_run.append("property_hygiene")
        outcome.pages_modified.extend(sub.pages_modified)
        outcome.details.extend(sub.details)

    return outcome


__all__ = ["CognitiveLintOutcome", "run_cognitive_lint_pipeline"]

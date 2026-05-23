"""Consolidate near-duplicate entities via canonical ``alias::`` lines (no file merge)."""

from __future__ import annotations

from pathlib import Path

from ...agent.plumber_config import PlumberLintConfig, apply_thermal_pause_cognitive
from ...graph.alias_index import normalize_concept_key
from ...graph.generational_cache import patch_generational_caches_for_paths
from ...graph.markdown_blocks import graph_safe_page_path, occ_snapshot
from ...graph.property_line_edit import append_page_alias_line
from ._shared import ModuleOutcome, extract_wikilink_targets, page_file_exists


def run_entity_consolidation(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    content: str,
    *,
    llm: object,
    threshold: float,
    config: PlumberLintConfig | None = None,
    llm_context: str | None = None,
) -> ModuleOutcome:
    """Add ``alias::`` on canonical pages when the LLM detects semantic duplication."""
    outcome = ModuleOutcome()
    if not hasattr(llm, "assess_entity_overlap"):
        return outcome

    seen_pairs: set[tuple[str, str]] = set()

    llm_body = llm_context if llm_context is not None else content

    for target in extract_wikilink_targets(content):
        if not page_file_exists(graph_root, target):
            continue
        if normalize_concept_key(target) == normalize_concept_key(page_title):
            continue

        a_key = normalize_concept_key(page_title)
        b_key = normalize_concept_key(target)
        pair = (a_key, b_key) if a_key <= b_key else (b_key, a_key)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        baselines: dict[str, float | None] = {}
        for candidate in (page_title, target):
            try:
                candidate_path = graph_safe_page_path(graph_root, candidate)
            except ValueError:
                baselines[candidate] = None
            else:
                baselines[candidate] = (
                    occ_snapshot(candidate_path) if candidate_path.is_file() else None
                )

        assessment = llm.assess_entity_overlap(
            title_a=page_title,
            title_b=target,
            context=llm_body[:4000],
        )
        apply_thermal_pause_cognitive(config)
        if not assessment.should_merge_alias or assessment.overlap_score < threshold:
            continue

        canonical = assessment.canonical_title.strip()
        alias = assessment.alias_title.strip()
        if not canonical or not alias:
            continue
        if normalize_concept_key(canonical) == normalize_concept_key(alias):
            continue

        try:
            canonical_path = graph_safe_page_path(graph_root, canonical)
        except ValueError:
            continue
        alias_baseline = baselines.get(canonical)

        result = append_page_alias_line(
            graph_root,
            canonical,
            alias,
            dry_run=False,
            baseline_mtime=alias_baseline,
        )
        if result.ok and result.added:
            outcome.pages_modified.append(canonical)
            outcome.details.append(
                f"alias:{canonical}<-{alias} score={assessment.overlap_score:.2f}",
            )
            if canonical_path.is_file():
                patch_generational_caches_for_paths(graph_root, [canonical_path])

    return outcome


__all__ = ["run_entity_consolidation"]

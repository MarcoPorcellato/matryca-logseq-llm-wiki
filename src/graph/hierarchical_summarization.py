"""Hierarchical MapReduce summarization for giant Logseq outliner pages."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ..agent.plumber_config import PlumberLintConfig, apply_thermal_pause_bootstrap
from ..agent.plumber_llm import BootstrapSummaryResult, HarvestLLM
from ..agent.prompt_layout import build_cache_aligned_prompt
from .bootstrap_stop import BootstrapHarvestStopped

_ROOT_BULLET = re.compile(r"^[-*+]\s+")


def _is_root_bullet(line: str) -> bool:
    return bool(_ROOT_BULLET.match(line))


def _split_root_trees(content: str) -> list[str]:
    """Split page content into atomic root-level bullet subtrees."""
    lines = content.splitlines(keepends=True)
    if not lines:
        return []

    trees: list[str] = []
    preamble: list[str] = []
    index = 0

    while index < len(lines) and not _is_root_bullet(lines[index]):
        preamble.append(lines[index])
        index += 1

    while index < len(lines):
        if not _is_root_bullet(lines[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < len(lines):
            if _is_root_bullet(lines[index]):
                break
            index += 1
        trees.append("".join(lines[start:index]))

    if preamble:
        if trees:
            trees[0] = "".join(preamble) + trees[0]
        else:
            trees.append("".join(preamble))
    return trees


def chunk_outliner_content(content: str, max_chunk_chars: int = 15000) -> list[str]:
    """Pack root-level outliner trees into bounded chunks without splitting subtrees."""
    trees = _split_root_trees(content)
    if not trees:
        return [content] if content else []

    chunks: list[str] = []
    buffer = ""
    for tree in trees:
        if len(tree) > max_chunk_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(tree)
            continue

        candidate = f"{buffer}{tree}" if buffer else tree
        if buffer and len(candidate) > max_chunk_chars:
            chunks.append(buffer)
            buffer = tree
        else:
            buffer = candidate

    if buffer:
        chunks.append(buffer)
    return chunks


def _build_reduce_stable_block(partials: list[BootstrapSummaryResult]) -> str:
    lines: list[str] = []
    for index, partial in enumerate(partials, start=1):
        lines.append(f"Section {index} summary: {partial.summary.strip()}")
        if partial.suggested_tags:
            tag_text = ", ".join(partial.suggested_tags)
            lines.append(f"Section {index} tags: {tag_text}")
        if partial.domain:
            lines.append(f"Section {index} domain hint: {partial.domain}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_reduce_task(page_title: str) -> str:
    return (
        "MapReduce consolidation task: synthesize the section summaries above "
        f"into a single cohesive one-sentence summary for the entire page '{page_title}', "
        "and merge all discovered tags into a clean unique JSON array.\n"
        f"Page title: {page_title}"
    )


def build_mapreduce_reduce_prompt(
    page_title: str,
    partials: list[BootstrapSummaryResult],
) -> str:
    """Cache-aligned reduce pass: stable partial summaries first, task last."""
    return build_cache_aligned_prompt(
        content=_build_reduce_stable_block(partials),
        task_instruction=_build_reduce_task(page_title),
    )


def mapreduce_harvest_page_summary(
    llm: HarvestLLM,
    *,
    page_title: str,
    content: str,
    page_path: Path | None = None,
    graph_root: Path | None = None,
    config: PlumberLintConfig,
    stop_event: threading.Event | None = None,
) -> BootstrapSummaryResult:
    """Harvest a page summary via structural MapReduce when content exceeds the trigger."""

    def _harvest_turn(
        chunk_text: str,
        *,
        task_instruction: str | None = None,
    ) -> BootstrapSummaryResult:
        if stop_event is not None and stop_event.is_set():
            raise BootstrapHarvestStopped
        result = llm.harvest_page_summary(
            page_title,
            chunk_text,
            page_path=page_path,
            graph_root=graph_root,
            task_instruction=task_instruction,
        )
        apply_thermal_pause_bootstrap(config, stop_event=stop_event)
        return result

    if len(content) <= config.mapreduce_trigger_chars:
        return _harvest_turn(content)

    chunks = chunk_outliner_content(content, max_chunk_chars=config.mapreduce_chunk_chars)
    partials: list[BootstrapSummaryResult] = []
    for chunk in chunks:
        if stop_event is not None and stop_event.is_set():
            raise BootstrapHarvestStopped
        partial = _harvest_turn(chunk)
        reset_history = getattr(llm, "reset_execution_history", None)
        if reset_history is not None:
            reset_history()
        partials.append(partial)

    consolidated = _harvest_turn(
        _build_reduce_stable_block(partials),
        task_instruction=_build_reduce_task(page_title),
    )
    reset_history = getattr(llm, "reset_execution_history", None)
    if reset_history is not None:
        reset_history()

    merged_tags = list(
        dict.fromkeys(
            tag.strip() for partial in partials for tag in partial.suggested_tags if tag.strip()
        ),
    )
    if merged_tags and not consolidated.suggested_tags:
        consolidated = BootstrapSummaryResult(
            summary=consolidated.summary,
            suggested_tags=merged_tags,
            domain=consolidated.domain
            or next(
                (partial.domain for partial in partials if partial.domain),
                "",
            ),
        )
    elif not consolidated.domain:
        domain = next((partial.domain for partial in partials if partial.domain), "")
        if domain:
            consolidated = BootstrapSummaryResult(
                summary=consolidated.summary,
                suggested_tags=consolidated.suggested_tags or merged_tags,
                domain=domain,
            )
    return consolidated


__all__ = [
    "build_mapreduce_reduce_prompt",
    "chunk_outliner_content",
    "mapreduce_harvest_page_summary",
]

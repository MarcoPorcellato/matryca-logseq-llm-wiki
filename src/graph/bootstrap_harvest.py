"""Two-phase bootstrap harvesting pipeline for Matryca Plumber catalog scalability."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..agent.cooperative_yield import io_batch_pause_seconds, yield_host
from ..agent.plumber_config import PlumberLintConfig, load_plumber_lint_config
from ..agent.plumber_llm import BootstrapSummaryResult, HarvestLLM
from ..agent.plumber_modules.marpa_framework import detect_marpa_namespace
from .alias_index import iter_alias_source_paths, page_title_from_path
from .backlink_index import load_incoming_backlinks
from .bootstrap_stop import BootstrapHarvestStopped
from .generational_cache import patch_generational_caches_for_paths
from .hierarchical_summarization import mapreduce_harvest_page_summary
from .markdown_blocks import (
    atomic_write_bytes,
    atomic_write_bytes_if_unchanged,
    file_mtime_drifted,
    read_file_mtime,
)
from .markdown_io import mmap_graph_page, read_graph_page_text
from .master_catalog import (
    MASTER_INDEX_PAGE_TITLE,
    SEMANTIC_INDEX_HEADER,
    CatalogEntry,
    MasterCatalog,
    extract_catalog_fields_from_content,
    extract_catalog_fields_from_mmap,
    list_stale_page_paths,
    load_master_catalog,
    master_index_page_path,
    write_master_index_page,
)
from .page_write_lock import page_rmw_lock

_TYPE_LINE = re.compile(r"^\s*type::\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_MARPA_DOMAINS = frozenset({"mappa", "area", "risorsa", "progetto", "archivio"})
BOOTSTRAP_PROGRESS_INTERVAL = 50
BootstrapHarvestStatus = Literal["regex", "llm", "skipped", "error"]
BootstrapProgressCallback = Callable[
    [int, int, Path | None, BootstrapHarvestStatus | None],
    None,
]


def _progress_harvest_status(raw_status: str) -> BootstrapHarvestStatus | None:
    """Map per-page harvest outcomes to UI/bootstrap telemetry statuses."""
    if raw_status == "skipped_empty":
        return "skipped"
    if raw_status in ("regex", "llm", "skipped", "error"):
        return raw_status  # type: ignore[return-value]
    return None


@dataclass
class HarvestMetrics:
    """Counters from one bootstrap harvest pass."""

    scanned: int = 0
    regex_harvested: int = 0
    llm_harvested: int = 0
    skipped_empty: int = 0
    errors: int = 0
    pruned: int = 0
    index_rebuilt: bool = False
    files_created: int = 0
    error_messages: list[str] = field(default_factory=list)


def _snapshot_page_titles(graph_root: Path) -> set[str]:
    return {page_title_from_path(graph_root, path) for path in iter_alias_source_paths(graph_root)}


def _count_new_concept_pages(
    *,
    before: set[str],
    after: set[str],
) -> int:
    """Count newly created markdown pages, excluding the compiled master index."""
    created = after - before
    created.discard(MASTER_INDEX_PAGE_TITLE)
    return len(created)


def _normalize_domain(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in _MARPA_DOMAINS else ""


def _infer_domain_from_content(page_title: str, content: str) -> str:
    type_match = _TYPE_LINE.search(content)
    if type_match:
        domain = _normalize_domain(type_match.group(1))
        if domain:
            return domain
    hint = detect_marpa_namespace(page_title)
    if hint and hint in _MARPA_DOMAINS:
        return hint
    return ""


def _refresh_orphan_flags(graph_root: Path, catalog: MasterCatalog) -> None:
    incoming = load_incoming_backlinks(graph_root)
    for title, entry in catalog.pages.items():
        entry.orphan = incoming.get(title, 0) == 0


def _format_minimal_index_section(summary: BootstrapSummaryResult) -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "",
        SEMANTIC_INDEX_HEADER,
        f"- indexed-at:: {stamp}",
        f"- summary:: {summary.summary.strip()}",
    ]
    if summary.suggested_tags:
        tag_line = " ".join(
            t if t.startswith("#") else f"#{t.lstrip('#')}" for t in summary.suggested_tags
        )
        lines.append(f"- suggested-tags:: {tag_line}")
    lines.append("")
    return "\n".join(lines)


def _append_minimal_semantic_index(
    graph_root: Path,
    page_path: Path,
    summary: BootstrapSummaryResult,
    *,
    baseline_mtime: float | None = None,
) -> None:
    if baseline_mtime is None and page_path.is_file():
        baseline_mtime = read_file_mtime(page_path)
    if baseline_mtime is not None and file_mtime_drifted(page_path, baseline_mtime):
        return
    with page_rmw_lock(page_path):
        if baseline_mtime is not None and file_mtime_drifted(page_path, baseline_mtime):
            return
        if page_path.is_file():
            prev = page_path.read_text(encoding="utf-8", errors="replace")
        else:
            prev = ""
            baseline_mtime = None
        if not prev.strip():
            return
        if SEMANTIC_INDEX_HEADER in prev:
            return
        body = prev.rstrip("\n") + _format_minimal_index_section(summary)
        if baseline_mtime is not None and not atomic_write_bytes_if_unchanged(
            page_path,
            body.encode("utf-8"),
            graph_root=graph_root,
            baseline_mtime=baseline_mtime,
        ):
            return
        if baseline_mtime is None:
            atomic_write_bytes(page_path, body.encode("utf-8"), graph_root=graph_root)
    patch_generational_caches_for_paths(graph_root, [page_path])


def _catalog_entry_from_harvest(
    *,
    summary: str,
    tags: list[str],
    domain: str,
    mtime: int,
    orphan: bool,
) -> CatalogEntry:
    return CatalogEntry(
        summary=summary.strip(),
        domain=_normalize_domain(domain),
        tags=[t.lstrip("#").lower() for t in tags if t.strip()],
        last_mtime=mtime,
        orphan=orphan,
    )


def harvest_page_into_catalog(
    graph_root: Path,
    catalog: MasterCatalog,
    page_path: Path,
    *,
    llm: HarvestLLM | None = None,
    incoming_counts: dict[str, int] | None = None,
    config: PlumberLintConfig | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[str, bool, bool]:
    """Harvest one page into the catalog.

    Returns ``(status, changed, llm_called_this_turn)``.
    """
    title = page_title_from_path(graph_root, page_path)
    if not page_path.is_file():
        catalog.remove(title)
        return "missing", True, False

    try:
        mtime = int(page_path.stat().st_mtime)
    except OSError as exc:
        return f"error:{exc}", False, False

    incoming = incoming_counts or {}
    orphan = incoming.get(title, 0) == 0
    extracted: CatalogEntry | None = None
    content = ""
    try:
        from .markdown_io import graph_read_mmap_enabled

        if graph_read_mmap_enabled():
            with mmap_graph_page(page_path, graph_root) as view:
                extracted = extract_catalog_fields_from_mmap(view)
                content = view.decode_utf8(errors="replace")
        else:
            content = read_graph_page_text(page_path, graph_root, errors="replace")
            extracted = extract_catalog_fields_from_content(content)
    except OSError as exc:
        return f"error:{exc}", False, False

    if not content.strip():
        return "skipped_empty", False, False

    if extracted is None:
        extracted = extract_catalog_fields_from_content(content)
    if extracted is not None:
        extracted.last_mtime = mtime
        extracted.orphan = orphan
        catalog.upsert(title, extracted)
        return "regex", True, False

    if llm is None:
        return "pending_llm", False, False

    if stop_event is not None and stop_event.is_set():
        raise BootstrapHarvestStopped

    lint_config = config or load_plumber_lint_config()
    domain = _infer_domain_from_content(title, content)
    baseline_mtime = read_file_mtime(page_path)
    summary_result = mapreduce_harvest_page_summary(
        llm,
        page_title=title,
        content=content,
        page_path=page_path,
        graph_root=graph_root,
        config=lint_config,
        stop_event=stop_event,
    )
    reset_history = getattr(llm, "reset_execution_history", None)
    if reset_history is not None:
        reset_history()
    if not domain and summary_result.domain:
        domain = _normalize_domain(summary_result.domain)

    _append_minimal_semantic_index(
        graph_root,
        page_path,
        summary_result,
        baseline_mtime=baseline_mtime,
    )
    entry = _catalog_entry_from_harvest(
        summary=summary_result.summary,
        tags=summary_result.suggested_tags,
        domain=domain,
        mtime=int(page_path.stat().st_mtime),
        orphan=orphan,
    )
    catalog.upsert(title, entry)
    return "llm", True, True


def run_bootstrap_harvest(
    graph_root: Path,
    *,
    llm: HarvestLLM | None = None,
    incremental: bool = False,
    rebuild_index: bool = True,
    phase1_strict: bool = False,
    config: PlumberLintConfig | None = None,
    progress_interval: int = BOOTSTRAP_PROGRESS_INTERVAL,
    on_progress: BootstrapProgressCallback | None = None,
    on_page_cataloged: BootstrapProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
    stop_event: threading.Event | None = None,
) -> HarvestMetrics:
    """Scan the graph, populate the master catalog, and compile the master index."""
    root = graph_root.expanduser().resolve(strict=False)
    lint_config = config or load_plumber_lint_config()
    catalog = load_master_catalog(root, force_reload=True)
    metrics = HarvestMetrics()
    incoming = load_incoming_backlinks(root)
    titles_before = _snapshot_page_titles(root) if phase1_strict else set()

    paths = (
        list_stale_page_paths(root, catalog)
        if incremental
        else list(
            iter_alias_source_paths(root),
        )
    )
    metrics.pruned = catalog.prune_missing_pages()

    total_pages = len(paths)
    if on_progress is not None:
        on_progress(0, total_pages, None, None)

    changed = metrics.pruned > 0
    interval = max(1, progress_interval)
    stopped_early = False
    io_pause = io_batch_pause_seconds()
    for page_index, page_path in enumerate(paths):
        if should_stop is not None and should_stop():
            stopped_early = True
            break
        if stop_event is not None and stop_event.is_set():
            stopped_early = True
            break
        yield_host(page_index)
        metrics.scanned += 1
        try:
            status, page_changed, llm_called_this_turn = harvest_page_into_catalog(
                root,
                catalog,
                page_path,
                llm=llm,
                incoming_counts=incoming,
                config=lint_config,
                stop_event=stop_event,
            )
        except BootstrapHarvestStopped:
            stopped_early = True
            break
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            metrics.errors += 1
            metrics.error_messages.append(f"{page_path.name}: {exc}")
            if on_progress is not None and (
                metrics.scanned % interval == 0 or metrics.scanned == total_pages
            ):
                on_progress(metrics.scanned, total_pages, page_path, "error")
            continue

        if status == "regex":
            metrics.regex_harvested += 1
            if io_pause > 0:
                time.sleep(io_pause)
        elif status == "llm":
            metrics.llm_harvested += 1
        elif status == "skipped_empty":
            metrics.skipped_empty += 1
            if io_pause > 0:
                time.sleep(io_pause)
        changed = changed or page_changed
        harvest_status = _progress_harvest_status(status)
        if on_page_cataloged is not None:
            on_page_cataloged(metrics.scanned, total_pages, page_path, harvest_status)

        if on_progress is not None and (
            metrics.scanned % interval == 0 or metrics.scanned == total_pages
        ):
            on_progress(metrics.scanned, total_pages, page_path, harvest_status)

    _refresh_orphan_flags(root, catalog)

    if not stopped_early and (changed or not incremental):
        catalog.save()
        if rebuild_index:
            write_master_index_page(root, catalog)
            metrics.index_rebuilt = True
            patch_generational_caches_for_paths(
                root,
                [master_index_page_path(root)],
            )

    if phase1_strict and not stopped_early:
        titles_after = _snapshot_page_titles(root)
        metrics.files_created = _count_new_concept_pages(
            before=titles_before,
            after=titles_after,
        )

    return metrics


def run_incremental_catalog_refresh(
    graph_root: Path,
    *,
    llm: HarvestLLM | None = None,
) -> HarvestMetrics:
    """Refresh only pages whose mtime differs from the catalog row."""
    return run_bootstrap_harvest(
        graph_root,
        llm=llm,
        incremental=True,
        rebuild_index=True,
    )


__all__ = [
    "BOOTSTRAP_PROGRESS_INTERVAL",
    "BootstrapHarvestStatus",
    "BootstrapProgressCallback",
    "HarvestMetrics",
    "harvest_page_into_catalog",
    "run_bootstrap_harvest",
    "run_incremental_catalog_refresh",
]

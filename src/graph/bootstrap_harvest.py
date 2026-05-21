"""Two-phase bootstrap harvesting pipeline for Matryca Plumber catalog scalability."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..agent.plumber_llm import BootstrapSummaryResult, HarvestLLM
from ..agent.plumber_modules.marpa_framework import detect_marpa_namespace
from .alias_index import iter_alias_source_paths, page_title_from_path
from .generational_cache import patch_generational_caches_for_paths
from .markdown_blocks import atomic_write_bytes
from .master_catalog import (
    MASTER_INDEX_PAGE_TITLE,
    SEMANTIC_INDEX_HEADER,
    CatalogEntry,
    MasterCatalog,
    extract_catalog_fields_from_content,
    list_stale_page_paths,
    load_master_catalog,
    master_index_page_path,
    write_master_index_page,
)
from .page_write_lock import page_rmw_lock

_TYPE_LINE = re.compile(r"^\s*type::\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_MARPA_DOMAINS = frozenset({"mappa", "area", "risorsa", "progetto", "archivio"})


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


def _compute_incoming_backlinks(graph_root: Path) -> dict[str, int]:
    """Count incoming wikilink references per page title."""
    wikilink = re.compile(r"\[\[([^\]#|]+)(?:\|[^\]]+)?\]\]")
    incoming: dict[str, int] = {}
    pages_dir = graph_root / "pages"
    title_to_stem: dict[str, str] = {}
    if pages_dir.is_dir():
        for path in pages_dir.rglob("*.md"):
            title = page_title_from_path(graph_root, path)
            title_to_stem[title.casefold()] = path.stem

    for path in iter_alias_source_paths(graph_root):
        title = page_title_from_path(graph_root, path)
        incoming.setdefault(title, 0)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in wikilink.finditer(text):
            target = match.group(1).strip()
            key = target.casefold()
            if key in title_to_stem:
                canonical = page_title_from_path(
                    graph_root,
                    pages_dir / f"{title_to_stem[key]}.md",
                )
                incoming[canonical] = incoming.get(canonical, 0) + 1
    return incoming


def _refresh_orphan_flags(graph_root: Path, catalog: MasterCatalog) -> None:
    incoming = _compute_incoming_backlinks(graph_root)
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
) -> None:
    with page_rmw_lock(page_path):
        if page_path.is_file():
            prev = page_path.read_text(encoding="utf-8", errors="replace")
        else:
            prev = ""
        if SEMANTIC_INDEX_HEADER in prev:
            return
        body = prev.rstrip("\n") + _format_minimal_index_section(summary)
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
) -> tuple[str, bool]:
    """Harvest one page into the catalog. Returns ``(status, changed)``."""
    title = page_title_from_path(graph_root, page_path)
    if not page_path.is_file():
        catalog.remove(title)
        return "missing", True

    try:
        content = page_path.read_text(encoding="utf-8", errors="replace")
        mtime = int(page_path.stat().st_mtime)
    except OSError as exc:
        return f"error:{exc}", False

    if not content.strip():
        return "skipped_empty", False

    incoming = incoming_counts or {}
    orphan = incoming.get(title, 0) == 0
    extracted = extract_catalog_fields_from_content(content)
    if extracted is not None:
        extracted.last_mtime = mtime
        extracted.orphan = orphan
        catalog.upsert(title, extracted)
        return "regex", True

    if llm is None:
        return "pending_llm", False

    domain = _infer_domain_from_content(title, content)
    summary_result = llm.harvest_page_summary(
        title,
        content,
        page_path=page_path,
        graph_root=graph_root,
    )
    if not domain and summary_result.domain:
        domain = _normalize_domain(summary_result.domain)

    _append_minimal_semantic_index(graph_root, page_path, summary_result)
    entry = _catalog_entry_from_harvest(
        summary=summary_result.summary,
        tags=summary_result.suggested_tags,
        domain=domain,
        mtime=int(page_path.stat().st_mtime),
        orphan=orphan,
    )
    catalog.upsert(title, entry)
    return "llm", True


def run_bootstrap_harvest(
    graph_root: Path,
    *,
    llm: HarvestLLM | None = None,
    incremental: bool = False,
    rebuild_index: bool = True,
    phase1_strict: bool = False,
) -> HarvestMetrics:
    """Scan the graph, populate the master catalog, and compile the master index."""
    root = graph_root.expanduser().resolve(strict=False)
    catalog = load_master_catalog(root, force_reload=True)
    metrics = HarvestMetrics()
    incoming = _compute_incoming_backlinks(root)
    titles_before = _snapshot_page_titles(root) if phase1_strict else set()

    paths = (
        list_stale_page_paths(root, catalog)
        if incremental
        else list(
            iter_alias_source_paths(root),
        )
    )
    metrics.pruned = catalog.prune_missing_pages()

    changed = metrics.pruned > 0
    for page_path in paths:
        metrics.scanned += 1
        try:
            status, page_changed = harvest_page_into_catalog(
                root,
                catalog,
                page_path,
                llm=llm,
                incoming_counts=incoming,
            )
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            metrics.errors += 1
            metrics.error_messages.append(f"{page_path.name}: {exc}")
            continue

        if status == "regex":
            metrics.regex_harvested += 1
        elif status == "llm":
            metrics.llm_harvested += 1
        elif status == "skipped_empty":
            metrics.skipped_empty += 1
        changed = changed or page_changed

    _refresh_orphan_flags(root, catalog)

    if changed or not incremental:
        catalog.save()
        if rebuild_index:
            write_master_index_page(root, catalog)
            metrics.index_rebuilt = True
            patch_generational_caches_for_paths(
                root,
                [master_index_page_path(root)],
            )

    if phase1_strict:
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
    "HarvestMetrics",
    "harvest_page_into_catalog",
    "run_bootstrap_harvest",
    "run_incremental_catalog_refresh",
]

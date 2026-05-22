"""Graph Insights Engine — structural topology metrics and diagnostic dashboard."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..agent.plumber_llm import GraphInsightsLLMResult, InsightsLLM
from ..agent.prompt_constraints import finalize_system_prompt
from .alias_index import iter_alias_source_paths, page_title_from_path
from .generational_cache import patch_generational_caches_for_paths
from .link_tag_hop import (
    _WIKILINK,
    _extract_inline_tags,
    _resolve_target_to_stem,
    _tags_prop_values,
)
from .markdown_blocks import atomic_write_bytes
from .master_catalog import MasterCatalog, load_master_catalog
from .page_path import filename_to_page_title
from .path_sandbox import graph_safe_page_path

GRAPH_INSIGHTS_TITLE = "Matryca Graph Insights"
_DENSE_WIKILINK_THRESHOLD = 25
_DENSE_BLOCK_THRESHOLD = 120
_TAG_CLUSTER_MIN_SHARED = 2
_TAG_CLUSTER_MIN_SIZE = 3


@dataclass(frozen=True, slots=True)
class TagCluster:
    """Pages sharing overlapping tag signatures."""

    pages: list[str]
    shared_tags: list[str]
    overlap_score: float = 0.0


@dataclass(frozen=True, slots=True)
class TagClusterGroup:
    """Merged semantic cluster for insights reporting."""

    pages: list[str]
    shared_tags: list[str]


@dataclass
class TopologyMetrics:
    """Structural heuristics computed purely in Python."""

    page_count: int = 0
    orphan_pages: list[str] = field(default_factory=list)
    dense_by_wikilinks: list[tuple[str, int]] = field(default_factory=list)
    dense_by_blocks: list[tuple[str, int]] = field(default_factory=list)
    tag_clusters: list[TagClusterGroup] = field(default_factory=list)
    domain_distribution: dict[str, int] = field(default_factory=dict)
    catalog_coverage: float = 0.0


@dataclass
class InsightsRunResult:
    """Outcome of one insights engine execution."""

    metrics: TopologyMetrics
    output_path: Path
    llm_used: bool = False
    latency_seconds: float = 0.0


def _count_bullets(text: str) -> int:
    return sum(1 for line in text.splitlines() if re.match(r"^\s*[-*+]\s+", line))


def _incoming_backlink_counts(graph_root: Path) -> dict[str, int]:
    root = graph_root.expanduser().resolve(strict=False)
    pages_dir = root / "pages"
    stems = {p.stem for p in pages_dir.rglob("*.md")} if pages_dir.is_dir() else set()
    incoming: dict[str, int] = {}

    for path in iter_alias_source_paths(root):
        title = page_title_from_path(root, path)
        incoming.setdefault(title, 0)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _WIKILINK.finditer(text):
            other = _resolve_target_to_stem(match.group(1), pages_dir)
            if other and other in stems:
                target_title = filename_to_page_title(f"{other}.md")
                incoming[target_title] = incoming.get(target_title, 0) + 1
    return incoming


def _outgoing_wikilink_counts(graph_root: Path) -> dict[str, int]:
    root = graph_root.expanduser().resolve(strict=False)
    pages_dir = root / "pages"
    outgoing: dict[str, int] = {}

    for path in iter_alias_source_paths(root):
        title = page_title_from_path(root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            outgoing[title] = 0
            continue
        count = 0
        for match in _WIKILINK.finditer(text):
            other = _resolve_target_to_stem(match.group(1), pages_dir)
            if other:
                count += 1
        outgoing[title] = count
    return outgoing


def _tag_signatures(graph_root: Path) -> dict[str, set[str]]:
    signatures: dict[str, set[str]] = {}
    for path in iter_alias_source_paths(graph_root):
        title = page_title_from_path(graph_root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            signatures[title] = set()
            continue
        tags = _extract_inline_tags(text) | _tags_prop_values(text)
        signatures[title] = tags
    return signatures


def _find_tag_clusters(signatures: dict[str, set[str]]) -> list[TagCluster]:
    titles = sorted(signatures)
    clusters: list[TagCluster] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, title_a in enumerate(titles):
        tags_a = signatures[title_a]
        if not tags_a:
            continue
        for title_b in titles[i + 1 :]:
            if title_a == title_b:
                continue
            tags_b = signatures[title_b]
            shared = tags_a & tags_b
            if len(shared) < _TAG_CLUSTER_MIN_SHARED:
                continue
            pair = (title_a, title_b) if title_a < title_b else (title_b, title_a)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            clusters.append(
                TagCluster(
                    pages=list(pair),
                    shared_tags=sorted(shared),
                    overlap_score=round(len(shared) / max(len(tags_a | tags_b), 1), 3),
                ),
            )

    clusters.sort(key=lambda cluster: (-cluster.overlap_score, str(cluster.pages)))
    return clusters[:40]


def _merge_tag_clusters(clusters: list[TagCluster]) -> list[TagClusterGroup]:
    multi_tag_groups = [
        cluster
        for cluster in clusters
        if len(cluster.shared_tags) >= _TAG_CLUSTER_MIN_SHARED and len(cluster.pages) >= 2
    ]
    grouped: dict[tuple[str, ...], list[str]] = {}
    for cluster in multi_tag_groups:
        key = tuple(cluster.shared_tags)
        grouped.setdefault(key, []).extend(cluster.pages)
    merged: list[TagClusterGroup] = []
    for tags, pages in grouped.items():
        unique_pages = sorted(set(pages))
        if len(unique_pages) >= _TAG_CLUSTER_MIN_SIZE:
            merged.append(TagClusterGroup(shared_tags=list(tags), pages=unique_pages))
    return merged


def compute_topology_metrics(
    graph_root: Path,
    catalog: MasterCatalog | None = None,
) -> TopologyMetrics:
    """Execute structural topology heuristics on the completed JSON catalog."""
    root = graph_root.expanduser().resolve(strict=False)
    catalog = catalog or load_master_catalog(root)
    incoming = _incoming_backlink_counts(root)
    outgoing = _outgoing_wikilink_counts(root)
    signatures = _tag_signatures(root)

    orphan_pages = sorted(title for title, count in incoming.items() if count == 0)
    dense_wikilinks = sorted(
        ((title, count) for title, count in outgoing.items() if count >= _DENSE_WIKILINK_THRESHOLD),
        key=lambda item: (-item[1], item[0].lower()),
    )

    dense_blocks: list[tuple[str, int]] = []
    for path in iter_alias_source_paths(root):
        title = page_title_from_path(root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block_count = _count_bullets(text)
        if block_count >= _DENSE_BLOCK_THRESHOLD:
            dense_blocks.append((title, block_count))
    dense_blocks.sort(key=lambda item: (-item[1], item[0].lower()))

    domain_distribution: dict[str, int] = {}
    for entry in catalog.pages.values():
        domain = entry.domain or "uncategorized"
        domain_distribution[domain] = domain_distribution.get(domain, 0) + 1

    live_titles = {page_title_from_path(root, p) for p in iter_alias_source_paths(root)}
    covered = sum(
        1 for title in live_titles if title in catalog.pages and catalog.pages[title].summary
    )
    coverage = round(100.0 * covered / max(len(live_titles), 1), 1)

    tag_clusters = _merge_tag_clusters(_find_tag_clusters(signatures))

    return TopologyMetrics(
        page_count=len(live_titles),
        orphan_pages=orphan_pages[:100],
        dense_by_wikilinks=dense_wikilinks[:30],
        dense_by_blocks=dense_blocks[:30],
        tag_clusters=tag_clusters[:20],
        domain_distribution=domain_distribution,
        catalog_coverage=coverage,
    )


def _metrics_payload(metrics: TopologyMetrics, catalog: MasterCatalog) -> dict[str, object]:
    return {
        "page_count": metrics.page_count,
        "catalog_coverage_percent": metrics.catalog_coverage,
        "domain_distribution": metrics.domain_distribution,
        "orphan_page_count": len(metrics.orphan_pages),
        "orphan_pages_sample": metrics.orphan_pages[:25],
        "dense_wikilink_nodes": [
            {"title": title, "outgoing_wikilinks": count}
            for title, count in metrics.dense_by_wikilinks[:15]
        ],
        "dense_block_nodes": [
            {"title": title, "bullet_blocks": count}
            for title, count in metrics.dense_by_blocks[:15]
        ],
        "semantic_tag_clusters": [
            {"shared_tags": cluster.shared_tags, "pages": cluster.pages}
            for cluster in metrics.tag_clusters[:12]
        ],
        "catalog_orphans_flagged": sorted(
            title for title, entry in catalog.pages.items() if entry.orphan
        )[:25],
    }


def _fallback_insights(metrics: TopologyMetrics) -> GraphInsightsLLMResult:
    lines = [
        f"The graph contains {metrics.page_count} scannable pages "
        f"with {metrics.catalog_coverage}% catalog coverage.",
    ]
    if metrics.domain_distribution:
        top_domain = max(metrics.domain_distribution, key=metrics.domain_distribution.get)  # type: ignore[arg-type]
        lines.append(
            f"The dominant MARPA domain is **{top_domain}** "
            f"({metrics.domain_distribution[top_domain]} pages).",
        )
    if metrics.tag_clusters:
        sample = metrics.tag_clusters[0]
        lines.append(
            "A recurring semantic cluster shares tags "
            f"{sample.shared_tags} across pages such as {sample.pages[:3]}.",
        )
    suggestions: list[str] = []
    for title in metrics.orphan_pages[:5]:
        suggestions.append(
            f"Consider linking [[{title}]] from a relevant hub or MOC to reduce orphan status.",
        )
    for title, count in metrics.dense_by_blocks[:3]:
        suggestions.append(
            f"Page [[{title}]] has {count} bullet blocks — candidate for auto-split review.",
        )
    for cluster in metrics.tag_clusters[:3]:
        if len(cluster.pages) >= 2:
            a, b = cluster.pages[0], cluster.pages[1]
            suggestions.append(
                f"Consider aliasing [[{a}]] and [[{b}]] due to semantic tag overlap.",
            )
    return GraphInsightsLLMResult(
        ontology_report=" ".join(lines),
        cleanup_suggestions=suggestions,
    )


def format_graph_insights_markdown(
    metrics: TopologyMetrics,
    llm_result: GraphInsightsLLMResult,
) -> str:
    """Render the diagnostic dashboard page body."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        "- type:: hub",
        f"- updated:: {stamp}",
        "- # Matryca Graph Insights",
        "",
        "_Structural diagnostics compiled by Matryca Plumber._",
        "",
        "## 🧠 Implicit Ontology Report",
        "",
        llm_result.ontology_report.strip(),
        "",
        "## 🧹 Structural Cleanup Suggestions",
        "",
    ]

    if llm_result.cleanup_suggestions:
        for suggestion in llm_result.cleanup_suggestions:
            text = suggestion.strip()
            if not text.startswith("- "):
                text = f"- #todo [[Matryca Cleanup Opportunity]] — {text.lstrip('- ')}"
            lines.append(text)
    else:
        lines.append("- _No actionable cleanup suggestions at this time._")

    lines.extend(
        [
            "",
            "## 📊 Topology Snapshot",
            "",
            f"- Pages scanned: **{metrics.page_count}**",
            f"- Catalog coverage: **{metrics.catalog_coverage}%**",
            f"- Orphan pages (0 incoming backlinks): **{len(metrics.orphan_pages)}**",
            f"- Dense wikilink hubs (≥{_DENSE_WIKILINK_THRESHOLD} outgoing): "
            f"**{len(metrics.dense_by_wikilinks)}**",
            f"- Dense block pages (≥{_DENSE_BLOCK_THRESHOLD} bullets): "
            f"**{len(metrics.dense_by_blocks)}**",
            f"- Semantic tag clusters: **{len(metrics.tag_clusters)}**",
            "",
        ],
    )
    return "\n".join(lines).rstrip() + "\n"


def write_graph_insights_page(
    graph_root: Path,
    markdown: str,
) -> Path:
    path = graph_safe_page_path(graph_root, GRAPH_INSIGHTS_TITLE)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, markdown.encode("utf-8"), graph_root=graph_root)
    return path


def run_graph_insights_engine(
    graph_root: Path,
    *,
    llm: InsightsLLM | None = None,
    catalog: MasterCatalog | None = None,
) -> InsightsRunResult:
    """Compute topology metrics and write ``pages/Matryca Graph Insights.md``."""
    started = time.perf_counter()
    root = graph_root.expanduser().resolve(strict=False)
    catalog = catalog or load_master_catalog(root, force_reload=True)
    metrics = compute_topology_metrics(root, catalog)

    llm_used = False
    if llm is not None:
        payload = json.dumps(_metrics_payload(metrics, catalog), ensure_ascii=False, indent=2)
        try:
            llm_result = llm.generate_graph_insights(metrics_json=payload, graph_root=root)
            llm_used = True
        except Exception:  # noqa: BLE001 - fallback to deterministic report
            llm_result = _fallback_insights(metrics)
    else:
        llm_result = _fallback_insights(metrics)

    markdown = format_graph_insights_markdown(metrics, llm_result)
    output_path = write_graph_insights_page(root, markdown)
    patch_generational_caches_for_paths(root, [output_path])

    return InsightsRunResult(
        metrics=metrics,
        output_path=output_path,
        llm_used=llm_used,
        latency_seconds=time.perf_counter() - started,
    )


INSIGHTS_JSON_CONSTRAINT = (
    "\n\n[CRITICAL JSON OUTPUT CONSTRAINT]\n"
    "NEVER GENERATE NESTED UNESCAPED QUOTES OR TRAILING GARBAGE CONTEXT. "
    "TERMINATE THE JSON BLOCK CLEANLY IMMEDIATELY AFTER THE CLOSING OBJECT BRACKET. "
    "Return valid JSON only — no markdown fences, no prose after the closing brace."
)

INSIGHTS_SYSTEM_PROMPT = finalize_system_prompt(
    "You are Matryca Plumber's Graph Insights Engine. Analyze structural topology metrics "
    "for a personal Logseq knowledge graph. Write in clear, beautiful English prose. "
    "Surface hidden conceptual clusters, naming drift, and structural debt without "
    "prescribing destructive edits. Cleanup suggestions must be non-destructive and "
    "formatted as short actionable sentences (no markdown bullets)." + INSIGHTS_JSON_CONSTRAINT
)


__all__ = [
    "GRAPH_INSIGHTS_TITLE",
    "InsightsLLM",
    "InsightsRunResult",
    "TopologyMetrics",
    "compute_topology_metrics",
    "format_graph_insights_markdown",
    "run_graph_insights_engine",
    "write_graph_insights_page",
]

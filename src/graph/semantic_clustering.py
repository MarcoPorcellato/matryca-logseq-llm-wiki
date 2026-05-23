"""Deterministic zero-token semantic clustering for Matryca Plumber Phase 2.

Inspired by Microsoft GraphRAG's Louvain community isolation: pages are grouped
into dense semantic neighborhoods using pure-Python TF-IDF + tag Jaccard
similarity, then processed cluster-by-cluster to localize rolling LLM context.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from .json_flock import cross_process_json_flock
from .markdown_blocks import atomic_write_bytes

CLUSTERS_FILENAME = "semantic_clusters.json"
CLUSTERS_VERSION = 1
MIN_CLUSTER_SIZE = 5
DEFAULT_MAX_CLUSTER_SIZE = 35
LOUVAIN_MAX_ITERATIONS = 20
_SUMMARY_WEIGHT = 0.55
_TAG_WEIGHT = 0.45
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    {
        "a",
        "and",
        "con",
        "da",
        "di",
        "fra",
        "gli",
        "i",
        "il",
        "in",
        "la",
        "le",
        "lo",
        "logseq",
        "nota",
        "note",
        "page",
        "pagina",
        "per",
        "questa",
        "questo",
        "su",
        "the",
        "this",
        "tra",
    },
)


def semantic_clusters_path(graph_root: Path) -> Path:
    """Return the on-disk path for the semantic cluster map."""
    return graph_root / ".matryca_semantic_cache" / CLUSTERS_FILENAME


def _tokenize(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(text.lower()) if token not in _STOPWORDS]


def _build_tfidf_vectors(documents: dict[str, str]) -> dict[str, dict[str, float]]:
    """Sparse TF-IDF vectors keyed by page title."""
    if not documents:
        return {}
    df: Counter[str] = Counter()
    tf_maps: dict[str, Counter[str]] = {}
    for title, text in documents.items():
        tokens = _tokenize(text)
        tf = Counter(tokens)
        tf_maps[title] = tf
        for term in set(tokens):
            df[term] += 1
    n = len(documents)
    vectors: dict[str, dict[str, float]] = {}
    for title, tf in tf_maps.items():
        vec: dict[str, float] = {}
        for term, count in tf.items():
            idf = math.log((1 + n) / (1 + df[term])) + 1.0
            vec[term] = float(count) * idf
        norm = math.sqrt(sum(value * value for value in vec.values())) or 1.0
        vectors[title] = {term: value / norm for term, value in vec.items()}
    return vectors


def _sparse_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(value * b.get(term, 0.0) for term, value in a.items())


def _jaccard_tags(tags_a: set[str], tags_b: set[str]) -> float:
    if not tags_a and not tags_b:
        return 0.0
    union = tags_a | tags_b
    if not union:
        return 0.0
    return len(tags_a & tags_b) / len(union)


def _combined_similarity(
    title_a: str,
    title_b: str,
    *,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> float:
    cosine = _sparse_cosine(tfidf.get(title_a, {}), tfidf.get(title_b, {}))
    jaccard = _jaccard_tags(tag_sets.get(title_a, set()), tag_sets.get(title_b, set()))
    if tag_sets.get(title_a) and tag_sets.get(title_b):
        return _SUMMARY_WEIGHT * cosine + _TAG_WEIGHT * jaccard
    return cosine


def _primary_block_key(title: str, block_keys: dict[str, str]) -> str:
    return block_keys.get(title, title[:24])


def _build_similarity_graph(
    titles: list[str],
    *,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
    block_keys: dict[str, str],
    k_neighbors: int = 20,
    min_similarity: float = 0.04,
) -> dict[str, dict[str, float]]:
    """Build a sparse weighted adjacency graph from semantic similarity."""
    adjacency: dict[str, dict[str, float]] = {title: {} for title in titles}
    blocks: dict[str, list[str]] = defaultdict(list)
    for title in titles:
        blocks[_primary_block_key(title, block_keys)].append(title)

    for block_titles in blocks.values():
        if len(block_titles) < 2:
            continue
        block_set = set(block_titles)
        max_df = max(4, len(block_titles) // 2)
        term_to_titles: dict[str, list[str]] = defaultdict(list)
        for title in block_titles:
            for term in tfidf.get(title, {}):
                term_to_titles[term].append(title)

        candidate_pairs: set[tuple[str, str]] = set()
        for shared_titles in term_to_titles.values():
            if len(shared_titles) < 2 or len(shared_titles) > max_df:
                continue
            scoped = [title for title in shared_titles if title in block_set]
            if len(scoped) < 2:
                continue
            for index, title_a in enumerate(scoped):
                for title_b in scoped[index + 1 :]:
                    pair = (title_a, title_b) if title_a < title_b else (title_b, title_a)
                    candidate_pairs.add(pair)

        for title_a, title_b in candidate_pairs:
            sim = _combined_similarity(
                title_a,
                title_b,
                tfidf=tfidf,
                tag_sets=tag_sets,
            )
            if sim >= min_similarity:
                adjacency[title_a][title_b] = sim
                adjacency[title_b][title_a] = sim

    for title in titles:
        neighbors = adjacency[title]
        if len(neighbors) <= k_neighbors:
            continue
        ranked = sorted(neighbors.items(), key=lambda item: item[1], reverse=True)[:k_neighbors]
        adjacency[title] = dict(ranked)

    return adjacency


def _louvain_communities(
    adjacency: dict[str, dict[str, float]],
    *,
    max_iterations: int = LOUVAIN_MAX_ITERATIONS,
) -> dict[str, int]:
    """Lightweight Louvain modularity optimization (pure Python)."""
    nodes = list(adjacency)
    if not nodes:
        return {}
    if len(nodes) == 1:
        return {nodes[0]: 0}

    community: dict[str, int] = {node: index for index, node in enumerate(nodes)}
    degree: dict[str, float] = {node: sum(adjacency[node].values()) for node in nodes}
    total_weight = sum(degree.values())
    if total_weight == 0.0:
        return community

    improved = True
    iteration = 0
    while improved:
        if iteration >= max_iterations:
            break
        improved = False
        iteration += 1
        comm_totals: dict[int, float] = defaultdict(float)
        for node, comm in community.items():
            comm_totals[comm] += degree[node]

        for node in nodes:
            current_comm = community[node]
            comm_totals[current_comm] -= degree[node]

            neighbor_comms: dict[int, float] = defaultdict(float)
            for neighbor, weight in adjacency[node].items():
                neighbor_comms[community[neighbor]] += weight

            best_comm = current_comm
            best_gain = 0.0
            node_degree = degree[node]
            for comm, edge_weight in neighbor_comms.items():
                if comm == current_comm:
                    continue
                sigma_tot = comm_totals[comm]
                gain = edge_weight - (sigma_tot * node_degree) / total_weight
                if gain > best_gain:
                    best_gain = gain
                    best_comm = comm

            if best_comm != current_comm:
                community[node] = best_comm
                improved = True

            comm_totals[community[node]] += degree[node]

    unique = sorted(set(community.values()))
    remap = {old: new for new, old in enumerate(unique)}
    return {node: remap[community[node]] for node in nodes}


def _pair_similarity(
    title_a: str,
    title_b: str,
    *,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> float:
    return _combined_similarity(title_a, title_b, tfidf=tfidf, tag_sets=tag_sets)


def _cluster_avg_similarity(
    cluster_a: list[str],
    cluster_b: list[str],
    *,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> float:
    if not cluster_a or not cluster_b:
        return 0.0
    total = 0.0
    count = 0
    for title_a in cluster_a:
        for title_b in cluster_b:
            total += _pair_similarity(title_a, title_b, tfidf=tfidf, tag_sets=tag_sets)
            count += 1
    return total / count if count else 0.0


def _bisect_cluster(
    titles: list[str],
    *,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    if len(titles) < 2:
        return titles, []
    min_sim = float("inf")
    seed_a, seed_b = titles[0], titles[1]
    for i, title_a in enumerate(titles):
        for title_b in titles[i + 1 :]:
            sim = _pair_similarity(title_a, title_b, tfidf=tfidf, tag_sets=tag_sets)
            if sim < min_sim:
                min_sim = sim
                seed_a, seed_b = title_a, title_b
    group_a = [seed_a]
    group_b = [seed_b]
    for title in titles:
        if title in (seed_a, seed_b):
            continue
        sim_a = sum(
            _pair_similarity(title, member, tfidf=tfidf, tag_sets=tag_sets) for member in group_a
        )
        sim_b = sum(
            _pair_similarity(title, member, tfidf=tfidf, tag_sets=tag_sets) for member in group_b
        )
        if sim_a >= sim_b:
            group_a.append(title)
        else:
            group_b.append(title)
    return group_a, group_b


def _split_oversized_clusters(
    clusters: list[list[str]],
    *,
    max_cluster_size: int,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> list[list[str]]:
    output: list[list[str]] = []
    for cluster in clusters:
        pending = [cluster]
        while pending:
            current = pending.pop()
            if len(current) <= max_cluster_size:
                output.append(current)
                continue
            left, right = _bisect_cluster(current, tfidf=tfidf, tag_sets=tag_sets)
            if not right:
                output.append(current)
                continue
            pending.append(left)
            pending.append(right)
    return output


def _greedy_semantic_bin(
    seed: str,
    candidates: list[str],
    *,
    max_cluster_size: int,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> list[str]:
    """Grow one cluster from *seed* using highest-similarity candidates first."""
    if not candidates:
        return [seed]
    scored = sorted(
        candidates,
        key=lambda title: _pair_similarity(seed, title, tfidf=tfidf, tag_sets=tag_sets),
        reverse=True,
    )
    members = [seed, *scored[: max_cluster_size - 1]]
    return sorted(members)


def _finalize_cluster_balance(
    clusters: list[list[str]],
    *,
    min_cluster_size: int,
    max_cluster_size: int,
    tfidf: dict[str, dict[str, float]],
    tag_sets: dict[str, set[str]],
) -> list[list[str]]:
    """Merge undersized clusters and pack leftovers into balanced semantic bins."""
    kept: list[list[str]] = []
    pool: list[str] = []
    for cluster in clusters:
        if len(cluster) >= min_cluster_size:
            kept.append(sorted(cluster))
        else:
            pool.extend(cluster)

    pool = sorted(set(pool))
    while pool:
        attached = False
        page = pool[0]
        best_idx = -1
        best_score = -1.0
        for index, cluster in enumerate(kept):
            if len(cluster) >= max_cluster_size:
                continue
            score = _cluster_avg_similarity(
                [page],
                cluster,
                tfidf=tfidf,
                tag_sets=tag_sets,
            )
            if score > best_score:
                best_score = score
                best_idx = index
        if best_idx >= 0 and best_score > 0.0:
            kept[best_idx].append(page)
            kept[best_idx] = sorted(kept[best_idx])
            pool.pop(0)
            attached = True
        if attached:
            continue

        remaining = len(pool)
        target_size = min(
            max_cluster_size,
            max(min_cluster_size, remaining),
        )
        if remaining > max_cluster_size and remaining - target_size < min_cluster_size:
            target_size = remaining - min_cluster_size
        seed = pool.pop(0)
        picked = _greedy_semantic_bin(
            seed,
            pool,
            max_cluster_size=target_size,
            tfidf=tfidf,
            tag_sets=tag_sets,
        )
        for title in picked[1:]:
            if title in pool:
                pool.remove(title)
        kept.append(picked)

    kept = _split_oversized_clusters(
        kept,
        max_cluster_size=max_cluster_size,
        tfidf=tfidf,
        tag_sets=tag_sets,
    )
    return kept


def _communities_to_clusters(
    assignments: dict[str, int],
) -> list[list[str]]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for title, comm in assignments.items():
        grouped[comm].append(title)
    return [sorted(members) for members in grouped.values()]


def compute_semantic_clusters(
    catalog_data: dict[str, Any],
    *,
    max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> dict[str, list[str]]:
    """Partition catalog pages into balanced semantic neighborhoods.

    Reads one-sentence summaries and tags from ``catalog_data["pages"]``,
    builds a TF-IDF + tag Jaccard similarity graph, runs Louvain community
    detection, then splits/merges to enforce size bounds.
    """
    raw_pages = catalog_data.get("pages", {})
    if not isinstance(raw_pages, dict) or not raw_pages:
        return {}

    titles = sorted(str(title) for title in raw_pages)
    summaries: dict[str, str] = {}
    tag_sets: dict[str, set[str]] = {}
    block_keys: dict[str, str] = {}
    for title in titles:
        record = raw_pages[title]
        if not isinstance(record, dict):
            summaries[title] = ""
            tag_sets[title] = set()
            block_keys[title] = title[:24]
            continue
        summaries[title] = str(record.get("summary", ""))
        raw_tags = record.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(tag).lower() for tag in raw_tags]
            tag_sets[title] = set(tags)
            block_keys[title] = tags[0] if tags else title[:24]
        else:
            tag_sets[title] = set()
            block_keys[title] = title[:24]

    if len(titles) == 1:
        return {"cluster_001": titles}

    tfidf = _build_tfidf_vectors(summaries)
    adjacency = _build_similarity_graph(
        titles,
        tfidf=tfidf,
        tag_sets=tag_sets,
        block_keys=block_keys,
    )
    assignments = _louvain_communities(adjacency)
    clusters = _communities_to_clusters(assignments)

    effective_min = min(min_cluster_size, max_cluster_size)
    if len(titles) < effective_min:
        effective_min = 1

    clusters = _split_oversized_clusters(
        clusters,
        max_cluster_size=max_cluster_size,
        tfidf=tfidf,
        tag_sets=tag_sets,
    )
    if effective_min > 1:
        for _ in range(3):
            clusters = _finalize_cluster_balance(
                clusters,
                min_cluster_size=effective_min,
                max_cluster_size=max_cluster_size,
                tfidf=tfidf,
                tag_sets=tag_sets,
            )
            if all(len(cluster) >= effective_min for cluster in clusters):
                break

    clusters.sort(key=lambda members: members[0] if members else "")
    result: dict[str, list[str]] = {}
    for index, members in enumerate(clusters, start=1):
        result[f"cluster_{index:03d}"] = sorted(members)
    return result


def save_semantic_clusters(
    graph_root: Path,
    clusters: dict[str, list[str]],
    *,
    catalog_updated_at: str | None = None,
    max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE,
) -> Path:
    """Persist cluster map atomically under ``.matryca_semantic_cache/``."""
    payload = {
        "version": CLUSTERS_VERSION,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "catalog_updated_at": catalog_updated_at,
        "max_cluster_size": max_cluster_size,
        "clusters": clusters,
    }
    path = semantic_clusters_path(graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with cross_process_json_flock(path):
        atomic_write_bytes(path, data.encode("utf-8"), graph_root=graph_root)
    return path


def load_semantic_clusters(graph_root: Path) -> dict[str, list[str]] | None:
    """Load cached clusters when present and well-formed."""
    path = semantic_clusters_path(graph_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring corrupt semantic cluster cache at {}: {}", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    raw_clusters = payload.get("clusters")
    if not isinstance(raw_clusters, dict):
        return None
    clusters: dict[str, list[str]] = {}
    for cluster_id, titles in raw_clusters.items():
        if isinstance(titles, list):
            clusters[str(cluster_id)] = [str(title) for title in titles]
    return clusters or None


def load_or_compute_semantic_clusters(
    graph_root: Path,
    *,
    catalog_data: dict[str, Any] | None = None,
    max_cluster_size: int = DEFAULT_MAX_CLUSTER_SIZE,
    force_recompute: bool = False,
) -> dict[str, list[str]]:
    """Return cached clusters when fresh; otherwise compute and persist."""
    from .master_catalog import MasterCatalog, load_master_catalog

    catalog = (
        MasterCatalog.from_json(graph_root, catalog_data)
        if catalog_data is not None
        else load_master_catalog(graph_root, force_reload=True)
    )
    catalog_json = catalog.to_json()
    catalog_updated_at = catalog_json.get("updated_at")

    if not force_recompute:
        path = semantic_clusters_path(graph_root)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                cached_at = payload.get("catalog_updated_at")
                cached_size = int(payload.get("max_cluster_size", 0) or 0)
                if cached_at == catalog_updated_at and cached_size == max_cluster_size:
                    loaded = load_semantic_clusters(graph_root)
                    if loaded is not None:
                        return loaded

    clusters = compute_semantic_clusters(
        catalog_json,
        max_cluster_size=max_cluster_size,
    )
    save_semantic_clusters(
        graph_root,
        clusters,
        catalog_updated_at=str(catalog_updated_at) if catalog_updated_at else None,
        max_cluster_size=max_cluster_size,
    )
    return clusters


def _extract_catalog_metadata(
    catalog_data: dict[str, Any],
    titles: list[str],
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, str]]:
    """Return summaries, tag sets, and block keys for *titles*."""
    raw_pages = catalog_data.get("pages", {})
    summaries: dict[str, str] = {}
    tag_sets: dict[str, set[str]] = {}
    block_keys: dict[str, str] = {}
    for title in titles:
        record = raw_pages.get(title) if isinstance(raw_pages, dict) else None
        if not isinstance(record, dict):
            summaries[title] = ""
            tag_sets[title] = set()
            block_keys[title] = title[:24]
            continue
        summaries[title] = str(record.get("summary", ""))
        raw_tags = record.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(tag).lower() for tag in raw_tags]
            tag_sets[title] = set(tags)
            block_keys[title] = tags[0] if tags else title[:24]
        else:
            tag_sets[title] = set()
            block_keys[title] = title[:24]
    return summaries, tag_sets, block_keys


def _cluster_hub_anchor(
    titles: list[str],
    catalog_data: dict[str, Any],
) -> str | None:
    """Return the highest weighted-degree page within *titles*."""
    if len(titles) < 2:
        return titles[0] if titles else None
    summaries, tag_sets, block_keys = _extract_catalog_metadata(catalog_data, titles)
    tfidf = _build_tfidf_vectors(summaries)
    adjacency = _build_similarity_graph(
        titles,
        tfidf=tfidf,
        tag_sets=tag_sets,
        block_keys=block_keys,
    )
    best_title: str | None = None
    best_degree = -1.0
    for title in titles:
        weighted_degree = sum(adjacency.get(title, {}).values())
        if weighted_degree > best_degree:
            best_degree = weighted_degree
            best_title = title
    if best_title is None and titles:
        return titles[0]
    return best_title


def format_cluster_neighborhood(
    catalog_data: dict[str, Any],
    titles: list[str],
) -> str:
    """Build the cluster boundary prompt block for Ermes context injection."""
    raw_pages = catalog_data.get("pages", {})
    hub_anchor = _cluster_hub_anchor(titles, catalog_data)
    anchor_title: str | None = hub_anchor if hub_anchor else None
    lines: list[str] = []
    for title in sorted(titles):
        prefix = (
            "[CLUSTER FOCUS ANCHOR NODE] "
            if anchor_title is not None and title == anchor_title
            else ""
        )
        record = raw_pages.get(title) if isinstance(raw_pages, dict) else None
        if isinstance(record, dict):
            summary = str(record.get("summary", "")).strip() or "(no summary)"
            raw_tags = record.get("tags", [])
            tags = (
                ", ".join(str(tag) for tag in raw_tags)
                if isinstance(raw_tags, list) and raw_tags
                else "none"
            )
            lines.append(f"- {prefix}**{title}**: {summary} [tags: {tags}]")
        else:
            lines.append(f"- {prefix}**{title}**: (no catalog entry)")
    return "\n".join(lines)


__all__ = [
    "CLUSTERS_FILENAME",
    "DEFAULT_MAX_CLUSTER_SIZE",
    "LOUVAIN_MAX_ITERATIONS",
    "MIN_CLUSTER_SIZE",
    "compute_semantic_clusters",
    "format_cluster_neighborhood",
    "load_or_compute_semantic_clusters",
    "load_semantic_clusters",
    "save_semantic_clusters",
    "semantic_clusters_path",
]

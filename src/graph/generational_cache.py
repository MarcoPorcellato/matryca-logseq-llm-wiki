"""Process-lifetime caches keyed by filesystem mtimes (incremental-style invalidation)."""

from __future__ import annotations

import math
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .alias_index import AliasIndex, build_alias_index, iter_alias_source_paths

_lock = threading.Lock()
_alias_cache: dict[str, tuple[frozenset[tuple[str, int]], AliasIndex]] = {}
_bm25_cache: dict[str, tuple[frozenset[tuple[str, int]], Bm25Corpus]] = {}


def clear_generational_caches() -> None:
    """Drop all cached graph artifacts (for tests / hot reload)."""
    with _lock:
        _alias_cache.clear()
        _bm25_cache.clear()


def _mtime_ns(path: Path) -> int | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))


def _signature(paths: list[Path], root: Path) -> frozenset[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for p in paths:
        mt = _mtime_ns(p)
        if mt is None:
            continue
        rel = p.relative_to(root).as_posix()
        pairs.append((rel, mt))
    return frozenset(pairs)


def cached_build_alias_index(graph_root: str | Path) -> AliasIndex:
    """Return :class:`AliasIndex`, reusing memory when source mtimes are unchanged."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    key = str(root)
    with _lock:
        paths = iter_alias_source_paths(root)
        sig = _signature(paths, root)
        hit = _alias_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        idx = build_alias_index(root)
        sig_after = _signature(iter_alias_source_paths(root), root)
        _alias_cache[key] = (sig_after, idx)
        return idx


def _bm25_page_paths(root: Path) -> list[Path]:
    pages = root / "pages"
    if not pages.is_dir():
        return []
    return sorted(p for p in pages.rglob("*.md") if p.is_file())


@dataclass(slots=True)
class Bm25Corpus:
    """Pre-tokenized page bag for Okapi BM25."""

    rels: list[str]
    docs_tokens: list[list[str]]
    doc_lens: list[int]
    df: dict[str, int]
    n_docs: int
    avgdl: float


def _build_bm25_corpus(root: Path) -> Bm25Corpus:
    from src.rag.local_query import tokenize

    paths = _bm25_page_paths(root)
    docs_tokens: list[list[str]] = []
    rels: list[str] = []
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        toks = tokenize(raw)
        if not toks:
            continue
        docs_tokens.append(toks)
        rels.append(path.relative_to(root).as_posix())

    n_docs = len(docs_tokens)
    if n_docs == 0:
        return Bm25Corpus(rels=[], docs_tokens=[], doc_lens=[], df={}, n_docs=0, avgdl=1.0)

    doc_lens = [len(d) for d in docs_tokens]
    avgdl = sum(doc_lens) / n_docs
    df: dict[str, int] = {}
    for toks in docs_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    return Bm25Corpus(
        rels=rels,
        docs_tokens=docs_tokens,
        doc_lens=doc_lens,
        df=df,
        n_docs=n_docs,
        avgdl=avgdl,
    )


def get_cached_bm25_corpus(graph_root: str | Path) -> Bm25Corpus:
    """Token bags + DF for BM25; rebuilt when any page ``st_mtime`` changes."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    key = str(root)
    with _lock:
        paths = _bm25_page_paths(root)
        sig = _signature(paths, root)
        hit = _bm25_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        corpus = _build_bm25_corpus(root)
        sig_after = _signature(_bm25_page_paths(root), root)
        _bm25_cache[key] = (sig_after, corpus)
        return corpus


def score_bm25_query(
    corpus: Bm25Corpus,
    query: str,
    *,
    limit: int,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[str, float]]:
    """Run BM25 scoring using a pre-built corpus (shared with :mod:`src.rag.local_query`)."""
    from src.rag.local_query import tokenize

    q_tokens = tokenize(query)
    if not q_tokens or corpus.n_docs == 0:
        return []

    n_docs = corpus.n_docs
    avgdl = corpus.avgdl
    scores: list[tuple[str, float]] = []
    for rel, toks, dl in zip(corpus.rels, corpus.docs_tokens, corpus.doc_lens, strict=True):
        tf = Counter(toks)
        score = 0.0
        for t in q_tokens:
            freq = tf.get(t, 0)
            if freq == 0:
                continue
            idf = math.log((n_docs - corpus.df.get(t, 0) + 0.5) / (corpus.df.get(t, 0) + 0.5) + 1.0)
            denom = freq + k1 * (1.0 - b + b * (dl / avgdl if avgdl else 1.0))
            score += idf * ((freq * (k1 + 1.0)) / denom)
        if score > 0.0:
            scores.append((rel, score))

    scores.sort(key=lambda item: (-item[1], item[0]))
    return scores[: max(1, min(limit, 100))]


__all__ = [
    "Bm25Corpus",
    "cached_build_alias_index",
    "clear_generational_caches",
    "get_cached_bm25_corpus",
    "score_bm25_query",
]

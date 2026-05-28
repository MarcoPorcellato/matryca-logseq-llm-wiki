"""Process RAM telemetry and phase-boundary release hooks (16GB laptop profile)."""

from __future__ import annotations

import gc
import os
import resource
import sys
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..graph.generational_cache import clear_generational_caches, release_bm25_corpus
from ..graph.master_catalog import unload_master_catalog
from .plumber_config import _env_int
from .plumber_modules.semantic_cache_router import (
    clear_semantic_cache_memory,
    purge_expired_semantic_cache,
)

_RAM_BUDGET_MB_ENV = "MATRYCA_RAM_BUDGET_MB"
_DEFAULT_RAM_BUDGET_MB = 4096


def ram_budget_mb() -> int:
    return max(512, _env_int(_RAM_BUDGET_MB_ENV, _DEFAULT_RAM_BUDGET_MB))


def rss_bytes() -> int:
    """Resident set size for this process (platform-specific units normalized to bytes)."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(usage.ru_maxrss)
    if sys.platform == "darwin":
        return rss
    return rss * 1024


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    rss_bytes: int
    rss_mb: float
    budget_mb: int
    over_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rss_bytes": self.rss_bytes,
            "rss_mb": round(self.rss_mb, 2),
            "budget_mb": self.budget_mb,
            "over_budget": self.over_budget,
        }


def snapshot() -> MemorySnapshot:
    raw = rss_bytes()
    mb = raw / (1024 * 1024)
    budget = ram_budget_mb()
    return MemorySnapshot(
        rss_bytes=raw,
        rss_mb=mb,
        budget_mb=budget,
        over_budget=mb > budget,
    )


def log_snapshot(*, label: str) -> MemorySnapshot:
    snap = snapshot()
    if snap.over_budget:
        logger.warning(
            "Memory budget exceeded [{}]: RSS {:.1f} MB > budget {} MB",
            label,
            snap.rss_mb,
            snap.budget_mb,
        )
    else:
        logger.debug(
            "Memory snapshot [{}]: RSS {:.1f} MB / budget {} MB",
            label,
            snap.rss_mb,
            snap.budget_mb,
        )
    return snap


def release_phase1_memory(graph_root: os.PathLike[str] | str) -> None:
    """Drop heavy Phase 1 artifacts after bootstrap harvest completes."""
    root = str(graph_root)
    clear_generational_caches()
    release_bm25_corpus(root)
    clear_semantic_cache_memory()
    purge_expired_semantic_cache(graph_root)  # type: ignore[arg-type]
    unload_master_catalog(graph_root)  # type: ignore[arg-type]
    gc.collect()


def maybe_release_after_cycle(*, llm_turns: int, graph_root: os.PathLike[str] | str) -> None:
    """Lightweight release when a daemon cycle used LLM (optional catalog trim)."""
    if llm_turns <= 0:
        return
    purge_expired_semantic_cache(graph_root)  # type: ignore[arg-type]
    snap = snapshot()
    if snap.over_budget:
        gc.collect()
        log_snapshot(label="post_cycle_gc")


__all__ = [
    "MemorySnapshot",
    "log_snapshot",
    "maybe_release_after_cycle",
    "ram_budget_mb",
    "release_phase1_memory",
    "rss_bytes",
    "snapshot",
]

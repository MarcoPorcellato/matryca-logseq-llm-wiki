"""Filesystem-backed semantic inference cache (VRAM / attention saver for local LLM)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

from ...graph.json_flock import cross_process_json_flock

_CACHE_DIRNAME = ".matryca_semantic_cache"
_DEFAULT_TTL_SECONDS = 86_400
_RESERVED_CACHE_JSON = frozenset(
    {
        "master_catalog.json",
        "backlink_counts.json",
        "semantic_clusters.json",
    },
)
_DEFAULT_MEMORY_ENTRIES = 512
_DEFAULT_MAX_PAYLOAD_BYTES = 262_144
_lock = threading.Lock()
_memory: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _max_cache_payload_bytes() -> int:
    raw = os.environ.get("MATRYCA_SEMANTIC_CACHE_MAX_PAYLOAD_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_PAYLOAD_BYTES
    try:
        return max(4096, min(2_097_152, int(raw)))
    except ValueError:
        return _DEFAULT_MAX_PAYLOAD_BYTES


def _payload_byte_size(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return _max_cache_payload_bytes() + 1


def _payload_within_limit(payload: dict[str, Any]) -> bool:
    return _payload_byte_size(payload) <= _max_cache_payload_bytes()


def _memory_max_entries() -> int:
    raw = os.environ.get("MATRYCA_SEMANTIC_CACHE_MEMORY_ENTRIES", "").strip()
    if not raw:
        return _DEFAULT_MEMORY_ENTRIES
    try:
        return max(16, int(raw))
    except ValueError:
        return _DEFAULT_MEMORY_ENTRIES


def _memory_put(digest: str, expires_at: float, payload: dict[str, Any]) -> None:
    _memory[digest] = (expires_at, payload)
    _memory.move_to_end(digest)
    limit = _memory_max_entries()
    while len(_memory) > limit:
        _memory.popitem(last=False)


@dataclass(frozen=True, slots=True)
class CacheNode:
    """One cached inference payload keyed by graph operation fingerprint."""

    namespace: str
    cache_key: str
    payload: dict[str, Any]
    created_at: float
    ttl_seconds: int

    def is_expired(self, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        return ts - self.created_at > self.ttl_seconds


def _cache_root(graph_root: Path) -> Path:
    return graph_root / _CACHE_DIRNAME


def _digest(namespace: str, cache_key: str) -> str:
    raw = f"{namespace}:{cache_key}".encode()
    return hashlib.sha256(raw).hexdigest()


def semantic_cache_key(page_path: Path, operation: str) -> str:
    """Build a stable key from page path + mtime + operation name."""
    try:
        mtime_ns = page_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    rel = page_path.name
    return f"{operation}:{rel}:{mtime_ns}"


def cache_get(graph_root: Path, namespace: str, cache_key: str) -> dict[str, Any] | None:
    """Return cached JSON payload or ``None`` when missing / expired."""
    digest = _digest(namespace, cache_key)
    now = time.time()
    ttl = _env_ttl()

    with _lock:
        mem_hit = _memory.get(digest)
        if mem_hit is not None:
            expires_at, payload = mem_hit
            if now <= expires_at:
                if _payload_within_limit(payload):
                    return payload
                cache_evict(graph_root, namespace, cache_key)
                return None
            _memory.pop(digest, None)

    path = _cache_root(graph_root) / f"{digest}.json"
    if not path.is_file():
        return None
    try:
        with cross_process_json_flock(path):
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    created = float(raw.get("created_at", 0.0))
    node_ttl = int(raw.get("ttl_seconds", ttl))
    if now - created > node_ttl:
        with cross_process_json_flock(path):
            path.unlink(missing_ok=True)
        return None
    raw_payload = raw.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    payload = raw_payload
    if not _payload_within_limit(payload):
        logger.warning(
            "Semantic cache entry oversize ({} bytes) for namespace={} — evicting",
            _payload_byte_size(payload),
            namespace,
        )
        cache_evict(graph_root, namespace, cache_key)
        return None
    with _lock:
        _memory_put(digest, created + node_ttl, payload)
    return payload


def cache_evict(graph_root: Path, namespace: str, cache_key: str) -> None:
    """Remove one cache entry from RAM and disk."""
    digest = _digest(namespace, cache_key)
    with _lock:
        _memory.pop(digest, None)
    path = _cache_root(graph_root) / f"{digest}.json"
    with contextlib.suppress(OSError), cross_process_json_flock(path):
        path.unlink(missing_ok=True)


def validate_cached_model[T: BaseModel](
    cached: dict[str, Any],
    model_cls: type[T],
    *,
    graph_root: Path,
    namespace: str,
    cache_key: str,
) -> T | None:
    """Load a cached Pydantic model; evict poisoned or pre-fix degenerate entries."""
    if not _payload_within_limit(cached):
        cache_evict(graph_root, namespace, cache_key)
        return None
    try:
        return model_cls.model_validate(cached)
    except ValidationError as exc:
        logger.warning(
            "Semantic cache schema mismatch for {} (evicting): {}",
            cache_key,
            exc,
        )
        cache_evict(graph_root, namespace, cache_key)
        return None


def cache_put(
    graph_root: Path,
    namespace: str,
    cache_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int | None = None,
) -> CacheNode | None:
    """Persist inference payload to disk and in-process memory."""
    if not _payload_within_limit(payload):
        logger.warning(
            "Semantic cache put skipped: payload exceeds {} bytes",
            _max_cache_payload_bytes(),
        )
        return None
    ttl = ttl_seconds if ttl_seconds is not None else _env_ttl()
    now = time.time()
    digest = _digest(namespace, cache_key)
    envelope = {
        "namespace": namespace,
        "cache_key": cache_key,
        "created_at": now,
        "ttl_seconds": ttl,
        "payload": payload,
    }
    root = _cache_root(graph_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest}.json"
    tmp = path.with_suffix(".tmp")
    with cross_process_json_flock(path):
        tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    with _lock:
        _memory_put(digest, now + ttl, payload)
    return CacheNode(
        namespace=namespace,
        cache_key=cache_key,
        payload=payload,
        created_at=now,
        ttl_seconds=ttl,
    )


def get_or_compute_model[T: BaseModel](
    graph_root: Path,
    namespace: str,
    cache_key: str,
    model_cls: type[T],
    compute: Callable[[], T],
) -> tuple[T, bool]:
    """Return ``(model, cache_hit)`` using the semantic cache when enabled."""
    cached = cache_get(graph_root, namespace, cache_key)
    if cached is not None:
        loaded = validate_cached_model(
            cached,
            model_cls,
            graph_root=graph_root,
            namespace=namespace,
            cache_key=cache_key,
        )
        if loaded is not None:
            return loaded, True
    result = compute()
    cache_put(graph_root, namespace, cache_key, result.model_dump())
    return result, False


def _env_ttl() -> int:
    raw = os.environ.get("MATRYCA_LINT_SEMANTIC_CACHE_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def purge_expired_semantic_cache(graph_root: Path) -> int:
    """Remove expired on-disk cache entries proactively (returns count removed)."""
    root = _cache_root(graph_root)
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in root.glob("*.json"):
        if path.name in _RESERVED_CACHE_JSON:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if not isinstance(raw, dict):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if "payload" not in raw or "namespace" not in raw:
            continue
        try:
            created = float(raw.get("created_at", 0.0))
            ttl = int(raw.get("ttl_seconds", _env_ttl()))
        except (TypeError, ValueError):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if now - created > ttl:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def clear_semantic_cache_memory() -> None:
    """Drop in-process LRU only (Phase teardown / RAM budget)."""
    with _lock:
        _memory.clear()


def clear_semantic_cache(graph_root: Path | None = None) -> None:
    """Drop in-process entries and optional on-disk cache directory."""
    clear_semantic_cache_memory()
    if graph_root is None:
        return
    root = _cache_root(graph_root)
    if root.is_dir():
        for path in root.glob("*.json"):
            if path.name in _RESERVED_CACHE_JSON:
                continue
            path.unlink(missing_ok=True)


__all__ = [
    "CacheNode",
    "cache_evict",
    "cache_get",
    "cache_put",
    "validate_cached_model",
    "clear_semantic_cache",
    "clear_semantic_cache_memory",
    "get_or_compute_model",
    "purge_expired_semantic_cache",
    "semantic_cache_key",
]

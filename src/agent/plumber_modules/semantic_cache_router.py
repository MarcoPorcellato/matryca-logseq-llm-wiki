"""Filesystem-backed semantic inference cache (VRAM / attention saver for local LLM)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_CACHE_DIRNAME = ".matryca_semantic_cache"
_DEFAULT_TTL_SECONDS = 86_400
_lock = threading.Lock()
_memory: dict[str, tuple[float, dict[str, Any]]] = {}


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
                return payload
            _memory.pop(digest, None)

    path = _cache_root(graph_root) / f"{digest}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    created = float(raw.get("created_at", 0.0))
    node_ttl = int(raw.get("ttl_seconds", ttl))
    if now - created > node_ttl:
        path.unlink(missing_ok=True)
        return None
    raw_payload = raw.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    payload = raw_payload
    with _lock:
        _memory[digest] = (created + node_ttl, payload)
    return payload


def cache_put(
    graph_root: Path,
    namespace: str,
    cache_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int | None = None,
) -> CacheNode:
    """Persist inference payload to disk and in-process memory."""
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
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _memory[digest] = (now + ttl, payload)
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
        return model_cls.model_validate(cached), True
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


def clear_semantic_cache(graph_root: Path | None = None) -> None:
    """Drop in-process entries and optional on-disk cache directory."""
    with _lock:
        _memory.clear()
    if graph_root is None:
        return
    root = _cache_root(graph_root)
    if root.is_dir():
        for path in root.glob("*.json"):
            path.unlink(missing_ok=True)


__all__ = [
    "CacheNode",
    "cache_get",
    "cache_put",
    "clear_semantic_cache",
    "get_or_compute_model",
    "purge_expired_semantic_cache",
    "semantic_cache_key",
]

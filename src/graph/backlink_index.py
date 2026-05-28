"""Persistent incoming wikilink counts (avoids full-graph rescans during bootstrap)."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, TypedDict

from .alias_index import (
    iter_alias_source_paths,
    iter_scannable_pages_markdown,
    page_title_from_path,
)
from .json_flock import cross_process_json_flock
from .markdown_blocks import atomic_write_bytes

_INDEX_VERSION = 1
_INDEX_FILENAME = "backlink_counts.json"
_wikilink = re.compile(r"\[\[([^\]#|]+)(?:\|[^\]]+)?\]\]")
_lock = threading.Lock()


class _BacklinkCacheEntry(TypedDict):
    _sig: dict[str, int]
    counts: dict[str, int]


_memory: dict[str, _BacklinkCacheEntry] = {}


def _index_path(graph_root: Path) -> Path:
    return graph_root / ".matryca_semantic_cache" / _INDEX_FILENAME


def _signature(paths: list[Path], root: Path) -> dict[str, int]:
    sig: dict[str, int] = {}
    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
            sig[rel] = int(path.stat().st_mtime_ns)
        except OSError:
            continue
    return sig


def _compute_incoming_full(graph_root: Path) -> dict[str, int]:
    """Count incoming wikilink references per page title (full scan)."""
    root = graph_root.expanduser().resolve(strict=False)
    incoming: dict[str, int] = {}
    pages_dir = root / "pages"
    title_to_stem: dict[str, str] = {}
    if pages_dir.is_dir():
        for path in iter_scannable_pages_markdown(root):
            title = page_title_from_path(root, path)
            title_to_stem[title.casefold()] = path.stem

    for path in iter_alias_source_paths(root):
        title = page_title_from_path(root, path)
        incoming.setdefault(title, 0)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _wikilink.finditer(text):
            target = match.group(1).strip()
            key = target.casefold()
            if key in title_to_stem:
                canonical = page_title_from_path(
                    root,
                    pages_dir / f"{title_to_stem[key]}.md",
                )
                incoming[canonical] = incoming.get(canonical, 0) + 1
    return incoming


def _load_disk(graph_root: Path) -> dict[str, Any] | None:
    path = _index_path(graph_root)
    if not path.is_file():
        return None
    try:
        with cross_process_json_flock(path):
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_disk(graph_root: Path, payload: dict[str, Any]) -> None:
    path = _index_path(graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with cross_process_json_flock(path):
        atomic_write_bytes(path, data.encode("utf-8"), graph_root=graph_root)


def load_incoming_backlinks(graph_root: Path, *, force_rebuild: bool = False) -> dict[str, int]:
    """Return title → incoming link count, rebuilding when signature is stale."""
    root = graph_root.expanduser().resolve(strict=False)
    key = str(root)
    paths = list(iter_alias_source_paths(root))
    sig = _signature(paths, root)

    if not force_rebuild:
        with _lock:
            cached = _memory.get(key)
        if cached is not None and cached.get("_sig") == sig:
            counts = cached.get("counts")
            if isinstance(counts, dict):
                return {str(k): int(v) for k, v in counts.items()}

        disk = _load_disk(root)
        if disk is not None and disk.get("version") == _INDEX_VERSION:
            disk_sig = disk.get("signature")
            if isinstance(disk_sig, dict) and disk_sig == sig:
                raw_counts = disk.get("counts", {})
                if isinstance(raw_counts, dict):
                    counts = {str(k): int(v) for k, v in raw_counts.items()}
                    with _lock:
                        _memory[key] = {"_sig": sig, "counts": counts}
                    return counts

    counts = _compute_incoming_full(root)
    payload = {
        "version": _INDEX_VERSION,
        "signature": sig,
        "counts": counts,
    }
    _save_disk(root, payload)
    with _lock:
        _memory[key] = {"_sig": sig, "counts": counts}
    return counts


def patch_backlink_index_for_paths(
    graph_root: Path,
    changed_paths: list[Path],
) -> bool:
    """Invalidate cached backlink index when pages change (full rebuild on next load)."""
    if not changed_paths:
        return False
    root = graph_root.expanduser().resolve(strict=False)
    key = str(root)
    with _lock:
        _memory.pop(key, None)
    path = _index_path(root)
    if path.is_file():
        with cross_process_json_flock(path):
            path.unlink(missing_ok=True)
    return True


def clear_backlink_index_cache(graph_root: Path | None = None) -> None:
    with _lock:
        if graph_root is None:
            _memory.clear()
            return
        key = str(graph_root.expanduser().resolve(strict=False))
        _memory.pop(key, None)


__all__ = [
    "clear_backlink_index_cache",
    "load_incoming_backlinks",
    "patch_backlink_index_for_paths",
]

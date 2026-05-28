"""Memory-mapped read helpers for large Logseq markdown graphs."""

from __future__ import annotations

import mmap
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .path_sandbox import assert_path_within_graph

_MMAP_MIN_BYTES_ENV = "MATRYCA_MMAP_MIN_BYTES"
_DEFAULT_MMAP_MIN_BYTES = 4096


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def graph_read_mmap_enabled() -> bool:
    return _env_bool("MATRYCA_GRAPH_READ_MMAP", True)


def mmap_min_bytes() -> int:
    return max(0, _env_int(_MMAP_MIN_BYTES_ENV, _DEFAULT_MMAP_MIN_BYTES))


@dataclass(slots=True)
class MmapTextView:
    """Read-only mmap view of a UTF-8 markdown file."""

    path: Path
    _mmap: mmap.mmap

    def decode_utf8(self, *, errors: str = "replace") -> str:
        return self._mmap[:].decode("utf-8", errors=errors)

    def search(self, pattern: str | bytes, flags: int = 0) -> re.Match[bytes] | None:
        data = self._mmap[:]
        if isinstance(pattern, str):
            pattern = pattern.encode("utf-8")
        return re.search(pattern, data, flags)

    def close(self) -> None:
        self._mmap.close()


@contextmanager
def mmap_graph_page(
    path: Path | str,
    graph_root: Path | str,
    *,
    encoding: str = "utf-8",
) -> Iterator[MmapTextView]:
    """Map a graph page file read-only after sandbox validation."""
    _ = encoding
    safe = assert_path_within_graph(path, graph_root)
    if safe.stat().st_size == 0:
        with safe.open("rb") as handle:
            mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        view = MmapTextView(path=safe, _mmap=mapped)
        try:
            yield view
        finally:
            view.close()
        return
    with safe.open("rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        view = MmapTextView(path=safe, _mmap=mapped)
        try:
            yield view
        finally:
            view.close()


def read_graph_page_text(
    path: Path | str,
    graph_root: Path | str,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Read page text via mmap when enabled and file is large enough."""
    safe = assert_path_within_graph(path, graph_root)
    if not safe.is_file():
        return ""
    use_mmap = graph_read_mmap_enabled() and safe.stat().st_size >= mmap_min_bytes()
    if use_mmap:
        with mmap_graph_page(safe, graph_root, encoding=encoding) as view:
            return view.decode_utf8(errors=errors)
    return safe.read_text(encoding=encoding, errors=errors)


__all__ = [
    "MmapTextView",
    "graph_read_mmap_enabled",
    "mmap_graph_page",
    "mmap_min_bytes",
    "read_graph_page_text",
]

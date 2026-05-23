"""Line-oriented helpers for Logseq Markdown blocks (bullets + ``id::`` spans)."""

from __future__ import annotations

import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .io_retry import IO_RETRY_ATTEMPTS, IO_RETRY_INITIAL_DELAY_S, IO_RETRY_MAX_DELAY_S
from .logseq_uuid import assert_valid_block_refs_in_markdown
from .path_sandbox import assert_path_within_graph
from .path_sandbox import graph_safe_page_path as _graph_safe_page_path

_BULLET = re.compile(r"^(\s*)[-*+]\s+")
# mkstemp(prefix=f".{basename}.", suffix=".tmp") → ``.{name}.{token}.tmp``
_ATOMIC_TMP_NAME = re.compile(r"^\.[^/\\]+\..+\.tmp$")


def strip_line_endings(line: str) -> str:
    """Strip trailing ``\\r`` / ``\\n`` (CRLF-safe) before comparisons or regex matching."""
    return line.rstrip("\r\n")


def strip_lines_for_match(lines: list[str]) -> list[str]:
    """Return logical line cores for every entry in ``lines`` (with or without keepends)."""
    return [strip_line_endings(ln) for ln in lines]


def canonical_line_suffix(original: str) -> str:
    """Return ``\\n`` when ``original`` had any line terminator, else ``""`` (never ``\\r\\n``)."""
    if original.endswith("\r\n") or original.endswith("\n") or original.endswith("\r"):
        return "\n"
    return ""


# Re-exported from :mod:`src.graph.path_sandbox` (single sandbox implementation).
def graph_safe_page_path(graph_root: Path | str, page_ref: str) -> Path:
    return _graph_safe_page_path(graph_root, page_ref)


def find_id_line_index(lines: list[str], block_uuid: str) -> int | None:
    u = block_uuid.strip().lower()
    pat = re.compile(rf"^\s*id::\s*{re.escape(u)}\s*$", re.IGNORECASE)
    for i, line in enumerate(lines):
        if pat.match(strip_line_endings(line)):
            return i
    return None


def block_bullet_index(lines: list[str], id_line_idx: int) -> int | None:
    for j in range(id_line_idx - 1, -1, -1):
        m = _BULLET.match(strip_line_endings(lines[j]))
        if m:
            return j
    return None


def block_subtree_end(lines: list[str], id_line_idx: int, bullet_idx: int) -> int:
    """First line index *after* this block's subtree (exclusive)."""
    bm = _BULLET.match(strip_line_endings(lines[bullet_idx]))
    if not bm:
        return len(lines)
    base = len(bm.group(1))
    for k in range(id_line_idx + 1, len(lines)):
        m = _BULLET.match(strip_line_endings(lines[k]))
        if m and len(m.group(1)) <= base:
            return k
    return len(lines)


def block_body_start(lines: list[str], bullet_idx: int, id_line_idx: int) -> int:
    """First line after the list bullet (usually properties / body before ``id::``)."""
    return bullet_idx + 1 if bullet_idx + 1 <= id_line_idx else id_line_idx


def block_property_insert_index(
    stripped: list[str],
    bullet_idx: int,
    block_end: int,
) -> int:
    """Line index for a new block property: after text continuations, before child bullets."""
    bullet_match = _BULLET.match(stripped[bullet_idx])
    if not bullet_match:
        return bullet_idx + 1
    child_bullet_min_indent = len(bullet_match.group(1)) + len(
        bullet_indent_unit(stripped, bullet_idx),
    )
    insert_at = bullet_idx + 1
    for i in range(bullet_idx + 1, min(block_end, len(stripped))):
        line = stripped[i]
        child_match = _BULLET.match(line)
        if child_match and len(child_match.group(1)) >= child_bullet_min_indent:
            break
        insert_at = i + 1
    return insert_at


def read_page_lines(
    graph_root: str | Path,
    page_ref: str,
) -> tuple[Path | None, list[str] | None, str | None]:
    root = Path(graph_root).expanduser().resolve(strict=False)
    try:
        path = graph_safe_page_path(root, page_ref)
    except ValueError:
        return None, None, "path_escapes_graph"
    if not path.is_file():
        return None, None, "page_not_found"
    text = path.read_text(encoding="utf-8", errors="replace")
    return path, text.splitlines(keepends=True), None


def locate_block_by_uuid(
    lines: list[str],
    block_uuid: str,
) -> tuple[int, int, int] | None:
    """Return ``(bullet_idx, id_line_idx, subtree_end_exclusive)`` or ``None``."""
    stripped = strip_lines_for_match(lines)
    id_idx = find_id_line_index(stripped, block_uuid)
    if id_idx is None:
        return None
    b_idx = block_bullet_index(stripped, id_idx)
    if b_idx is None:
        return None
    end = block_subtree_end(stripped, id_idx, b_idx)
    return b_idx, id_idx, end


def bullet_indent_unit(lines: list[str], bullet_idx: int) -> str:
    m = _BULLET.match(strip_line_endings(lines[bullet_idx]))
    if not m:
        return "  "
    depth = len(m.group(1))
    if depth >= 2 and lines[bullet_idx].startswith("\t"):
        return "\t"
    return "  "


def read_file_mtime(file_path: str | Path) -> float | None:
    """Return ``st_mtime`` for ``file_path``, or ``None`` when the path is unreadable."""
    try:
        return Path(file_path).stat().st_mtime
    except OSError:
        return None


def occ_snapshot(file_path: str | Path) -> float | None:
    """Phase 1: capture ``st_mtime`` before expensive work (LLM inference)."""
    return read_file_mtime(file_path)


class OCCConflictError(Exception):
    """On-disk mtime drifted since the Phase-1 snapshot (user edit during inference)."""

    def __init__(
        self,
        file_path: str | Path,
        *,
        baseline_mtime: float,
        current_mtime: float | None,
    ) -> None:
        self.file_path = Path(file_path)
        self.baseline_mtime = baseline_mtime
        self.current_mtime = current_mtime
        super().__init__(
            f"OCC conflict: {self.file_path} modified "
            f"(baseline={baseline_mtime}, current={current_mtime})",
        )


@dataclass
class OCCSnapshot:
    """Phase-1 mtime snapshot for two-phase optimistic concurrency control."""

    file_path: Path
    baseline_mtime: float

    @classmethod
    def capture(cls, file_path: str | Path) -> OCCSnapshot | None:
        mtime = read_file_mtime(file_path)
        if mtime is None:
            return None
        return cls(file_path=Path(file_path), baseline_mtime=mtime)

    def drifted(self) -> bool:
        return file_mtime_drifted(self.file_path, self.baseline_mtime)

    def refresh_after_own_write(self) -> None:
        """Re-baseline after our own committed write (multi-step same-request edits)."""
        current = read_file_mtime(self.file_path)
        if current is not None:
            self.baseline_mtime = current


def occ_verify_before_write(
    file_path: str | Path,
    baseline_mtime: float,
    *,
    raise_on_conflict: bool = False,
) -> bool:
    """Phase 2: return ``False`` when ``baseline_mtime`` no longer matches on disk."""
    if file_mtime_drifted(file_path, baseline_mtime):
        if raise_on_conflict:
            raise OCCConflictError(
                file_path,
                baseline_mtime=baseline_mtime,
                current_mtime=read_file_mtime(file_path),
            )
        return False
    return True


def file_mtime_drifted(file_path: str | Path, baseline_mtime: float) -> bool:
    """Return whether on-disk mtime differs from ``baseline_mtime`` (user edit during inference)."""
    current = read_file_mtime(file_path)
    if current is None:
        return True
    return not math.isclose(baseline_mtime, current, rel_tol=0.0, abs_tol=1e-6)


def atomic_write_bytes_if_unchanged(
    file_path: str | Path,
    data: bytes,
    *,
    graph_root: str | Path,
    baseline_mtime: float,
    validate_block_refs: bool = True,
) -> bool:
    """Commit only when ``baseline_mtime`` still matches. Returns ``True`` when written."""
    if file_mtime_drifted(file_path, baseline_mtime):
        return False
    atomic_write_bytes(
        file_path,
        data,
        graph_root=graph_root,
        validate_block_refs=validate_block_refs,
    )
    return True


def atomic_write_bytes(
    file_path: str | Path,
    data: bytes,
    *,
    graph_root: str | Path,
    validate_block_refs: bool = True,
) -> None:
    """Write ``data`` to ``file_path`` via temp file, ``fsync``, and atomic ``os.replace``.

    The temp file is created in the target directory so ``replace`` stays on one volume.
    Parent directories are created when missing (same as typical journal append).

    Raises:
        ValueError: When ``file_path`` escapes ``graph_root`` or a ``*.md`` payload contains a
            malformed ``((uuid))`` block reference (unless ``validate_block_refs=False``).
    """
    is_markdown = Path(file_path).suffix.lower() == ".md"
    if validate_block_refs and is_markdown and b"((" in data:
        try:
            text = data.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            pass
        else:
            assert_valid_block_refs_in_markdown(text)

    path = assert_path_within_graph(file_path, graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _commit_once() -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        committed = False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            committed = True
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if not committed:
                tmp_path.unlink(missing_ok=True)

    delay = IO_RETRY_INITIAL_DELAY_S
    last_exc: OSError | None = None
    for attempt in range(IO_RETRY_ATTEMPTS):
        try:
            _commit_once()
            return
        except OSError as exc:
            last_exc = exc
            if attempt >= IO_RETRY_ATTEMPTS - 1:
                break
            logger.warning(
                "Atomic write retry {}/{} for {} ({}): {}",
                attempt + 1,
                IO_RETRY_ATTEMPTS - 1,
                path,
                type(exc).__name__,
                exc,
            )
            time.sleep(min(IO_RETRY_MAX_DELAY_S, delay))
            delay = min(IO_RETRY_MAX_DELAY_S, delay * 2)
    assert last_exc is not None
    logger.warning("Atomic write aborted for {} after retries: {}", path, last_exc)
    raise last_exc


def atomic_write_file(
    file_path: str | Path,
    contents: str | bytes,
    *,
    graph_root: str | Path,
    encoding: str = "utf-8",
    validate_block_refs: bool = True,
) -> None:
    """UTF-8 text or raw bytes; same durability guarantees as :func:`atomic_write_bytes`."""
    if isinstance(contents, bytes):
        atomic_write_bytes(
            file_path,
            contents,
            graph_root=graph_root,
            validate_block_refs=validate_block_refs,
        )
    else:
        atomic_write_bytes(
            file_path,
            contents.encode(encoding, errors="replace"),
            graph_root=graph_root,
            validate_block_refs=validate_block_refs,
        )


def sweep_dangling_atomic_tmp_files(graph_root: str | Path) -> int:
    """Remove orphan ``.{name}.{token}.tmp`` files left by ungraceful process termination.

    Scans ``pages/`` and ``journals/`` (including nested folders). Returns the count
    of files unlinked.
    """
    root = Path(graph_root).expanduser().resolve(strict=False)
    removed = 0
    for sub in ("pages", "journals"):
        base = root / sub
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                assert_path_within_graph(candidate, root)
            except ValueError:
                continue
            if _ATOMIC_TMP_NAME.match(candidate.name):
                candidate.unlink(missing_ok=True)
                removed += 1
    return removed


def iter_graph_markdown_files(graph_root: str | Path) -> list[Path]:
    """All scannable ``*.md`` under ``pages/`` and ``journals/`` (nested, backup-safe)."""
    from .alias_index import iter_alias_source_paths

    return iter_alias_source_paths(graph_root)


__all__ = [
    "OCCConflictError",
    "OCCSnapshot",
    "atomic_write_bytes",
    "atomic_write_bytes_if_unchanged",
    "atomic_write_file",
    "block_body_start",
    "block_bullet_index",
    "block_property_insert_index",
    "block_subtree_end",
    "bullet_indent_unit",
    "canonical_line_suffix",
    "file_mtime_drifted",
    "find_id_line_index",
    "graph_safe_page_path",
    "iter_graph_markdown_files",
    "locate_block_by_uuid",
    "occ_snapshot",
    "occ_verify_before_write",
    "read_file_mtime",
    "read_page_lines",
    "strip_line_endings",
    "strip_lines_for_match",
    "sweep_dangling_atomic_tmp_files",
]

"""Line-oriented helpers for Logseq Markdown blocks (bullets + ``id::`` spans)."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .logseq_uuid import assert_valid_block_refs_in_markdown

_BULLET = re.compile(r"^(\s*)[-*+]\s+")
# mkstemp(prefix=f".{basename}.", suffix=".tmp") → ``.{name}.{token}.tmp``
_ATOMIC_TMP_NAME = re.compile(r"^\.[^/\\]+\..+\.tmp$")


# Same resolution rules as :mod:`src.graph.property_line_edit`.
def graph_safe_page_path(graph_root: Path, page_ref: str) -> Path:
    root = graph_root.expanduser().resolve(strict=False)
    raw = page_ref.strip().replace("\\", "/")
    if raw.startswith("pages/"):
        candidate = (root / raw).resolve()
    else:
        candidate = (root / "pages" / (raw if raw.endswith(".md") else f"{raw}.md")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        msg = "path_escapes_graph"
        raise ValueError(msg) from exc
    return candidate


def find_id_line_index(lines: list[str], block_uuid: str) -> int | None:
    u = block_uuid.strip().lower()
    pat = re.compile(rf"^\s*id::\s*{re.escape(u)}\s*$", re.IGNORECASE)
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def block_bullet_index(lines: list[str], id_line_idx: int) -> int | None:
    for j in range(id_line_idx - 1, -1, -1):
        m = _BULLET.match(lines[j])
        if m:
            return j
    return None


def block_subtree_end(lines: list[str], id_line_idx: int, bullet_idx: int) -> int:
    """First line index *after* this block's subtree (exclusive)."""
    bm = _BULLET.match(lines[bullet_idx])
    if not bm:
        return len(lines)
    base = len(bm.group(1))
    for k in range(id_line_idx + 1, len(lines)):
        m = _BULLET.match(lines[k])
        if m and len(m.group(1)) <= base:
            return k
    return len(lines)


def block_body_start(lines: list[str], bullet_idx: int, id_line_idx: int) -> int:
    """First line after the list bullet (usually properties / body before ``id::``)."""
    return bullet_idx + 1 if bullet_idx + 1 <= id_line_idx else id_line_idx


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
    stripped = [ln.rstrip("\n") for ln in lines]
    id_idx = find_id_line_index(stripped, block_uuid)
    if id_idx is None:
        return None
    b_idx = block_bullet_index(stripped, id_idx)
    if b_idx is None:
        return None
    end = block_subtree_end(stripped, id_idx, b_idx)
    return b_idx, id_idx, end


def bullet_indent_unit(lines: list[str], bullet_idx: int) -> str:
    m = _BULLET.match(lines[bullet_idx].rstrip("\n"))
    if not m:
        return "  "
    depth = len(m.group(1))
    if depth >= 2 and lines[bullet_idx].startswith("\t"):
        return "\t"
    return "  "


def atomic_write_bytes(file_path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``file_path`` via temp file, ``fsync``, and atomic ``os.replace``.

    The temp file is created in the target directory so ``replace`` stays on one volume.
    Parent directories are created when missing (same as typical journal append).

    Raises:
        ValueError: When UTF-8 markdown contains a malformed ``((uuid))`` block reference.
    """
    if b"((" in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            assert_valid_block_refs_in_markdown(text)

    path = Path(file_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    finally:
        if not committed:
            tmp_path.unlink(missing_ok=True)


def atomic_write_file(
    file_path: str | Path,
    contents: str | bytes,
    *,
    encoding: str = "utf-8",
) -> None:
    """UTF-8 text or raw bytes; same durability guarantees as :func:`atomic_write_bytes`."""
    if isinstance(contents, bytes):
        atomic_write_bytes(file_path, contents)
    else:
        atomic_write_bytes(file_path, contents.encode(encoding))


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
            if _ATOMIC_TMP_NAME.match(candidate.name):
                candidate.unlink(missing_ok=True)
                removed += 1
    return removed


def iter_graph_markdown_files(graph_root: str | Path) -> list[Path]:
    """All ``*.md`` under ``pages/`` and ``journals/`` when present."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    out: list[Path] = []
    for sub in ("pages", "journals"):
        d = root / sub
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.md") if p.is_file()))
    return out


__all__ = [
    "atomic_write_bytes",
    "atomic_write_file",
    "block_body_start",
    "block_bullet_index",
    "block_subtree_end",
    "bullet_indent_unit",
    "find_id_line_index",
    "iter_graph_markdown_files",
    "locate_block_by_uuid",
    "graph_safe_page_path",
    "read_page_lines",
    "sweep_dangling_atomic_tmp_files",
]

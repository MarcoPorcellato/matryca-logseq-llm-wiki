"""Size-based log rotation with zip compression for append-only JSONL writers."""

from __future__ import annotations

import zipfile
from pathlib import Path

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_RETENTION = 5


def rotate_file_if_oversized(
    path: Path,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    retention: int = _DEFAULT_RETENTION,
) -> None:
    """Rotate ``path`` into numbered ``.zip`` archives when it exceeds ``max_bytes``."""
    if retention <= 0 or not path.is_file():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    oldest = path.with_name(f"{path.name}.{retention}.zip")
    oldest.unlink(missing_ok=True)

    for index in range(retention - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}.zip")
        dst = path.with_name(f"{path.name}.{index + 1}.zip")
        if src.is_file():
            src.replace(dst)

    archive = path.with_name(f"{path.name}.1.zip")
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=path.name)
    path.unlink(missing_ok=True)
    path.touch()


__all__ = ["rotate_file_if_oversized"]

"""Persistent session alias registry for stateless CLI / MCP invocations."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ALIAS_FILE_NAME = ".matryca_aliases.json"
_ALIAS_TARGET_RE = re.compile(r"^\[\s*(\d+)\s*\]$")


def alias_file_path(graph_root: str | Path) -> Path:
    """Hidden alias map file at the Logseq graph root."""
    return Path(graph_root).expanduser().resolve(strict=False) / _ALIAS_FILE_NAME


def save_alias_map(graph_root: str | Path, alias_map: dict[int, str]) -> Path:
    """Persist ``alias -> uuid`` mapping to ``.matryca_aliases.json``."""
    path = alias_file_path(graph_root)
    payload = {str(alias): uuid for alias, uuid in sorted(alias_map.items())}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_alias_map(graph_root: str | Path) -> dict[int, str]:
    """Load alias map from disk; returns empty dict when the file is missing."""
    path = alias_file_path(graph_root)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Invalid alias registry in {path.name}: expected a JSON object"
        raise ValueError(msg)
    out: dict[int, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.isdigit():
            continue
        if isinstance(value, str) and value.strip():
            out[int(key)] = value.strip()
    return out


def resolve_target(graph_root: str | Path, target: str) -> str:
    """Resolve ``[n]`` session aliases to Logseq UUIDs; pass through other targets."""
    raw = target.strip()
    match = _ALIAS_TARGET_RE.fullmatch(raw)
    if not match:
        return target
    alias = int(match.group(1))
    mapping = load_alias_map(graph_root)
    uuid = mapping.get(alias)
    if uuid is None:
        msg = (
            f"Unknown session alias {raw!r}. Run `read_graph_data` with "
            f'`target_type="xray_page"` on the page first to refresh `.matryca_aliases.json`.'
        )
        raise ValueError(msg)
    return uuid


def resolve_pipe_target(graph_root: str | Path, target: str) -> str:
    """Resolve aliases in ``Page Title|block-uuid`` (or ``Page Title|[n]``) targets."""
    parts = [segment.strip() for segment in target.split("|", 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0]}|{resolve_target(graph_root, parts[1])}"
    return resolve_target(graph_root, target)


__all__ = [
    "alias_file_path",
    "load_alias_map",
    "resolve_pipe_target",
    "resolve_target",
    "save_alias_map",
]

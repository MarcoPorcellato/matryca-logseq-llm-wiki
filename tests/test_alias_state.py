"""Tests for persistent X-Ray alias registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from logseq_matryca_parser.agent_press import XRAY_STATE_FILENAME, SessionAliasRegistry
from src.agent.alias_state import (
    alias_file_path,
    load_alias_registry,
    resolve_pipe_target,
    resolve_target,
    save_alias_registry,
)

BLOCK_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _registry_with(mapping: dict[int, str]) -> SessionAliasRegistry:
    registry = SessionAliasRegistry()
    for alias, block_uuid in mapping.items():
        registry._alias_to_uuid[alias] = block_uuid  # noqa: SLF001
        registry._uuid_to_alias[block_uuid] = alias  # noqa: SLF001
    return registry


def test_save_and_load_alias_registry(tmp_path: Path) -> None:
    registry = _registry_with(
        {0: BLOCK_UUID, 1: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
    )
    save_alias_registry(tmp_path, registry)
    path = alias_file_path(tmp_path)
    assert path.name == XRAY_STATE_FILENAME
    assert path.is_file()
    loaded = load_alias_registry(tmp_path)
    assert loaded.resolve_alias(0) == BLOCK_UUID
    assert loaded.resolve_alias(1) == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["0"] == BLOCK_UUID


def test_resolve_target_passes_through_uuid(tmp_path: Path) -> None:
    save_alias_registry(tmp_path, _registry_with({0: BLOCK_UUID}))
    assert resolve_target(tmp_path, BLOCK_UUID) == BLOCK_UUID
    assert resolve_target(tmp_path, "My Page") == "My Page"


def test_resolve_target_unknown_alias_raises(tmp_path: Path) -> None:
    save_alias_registry(tmp_path, _registry_with({0: BLOCK_UUID}))
    with pytest.raises(ValueError, match=r"\[9\]"):
        resolve_target(tmp_path, "[9]")


def test_resolve_pipe_target(tmp_path: Path) -> None:
    save_alias_registry(tmp_path, _registry_with({0: BLOCK_UUID}))
    resolved = resolve_pipe_target(tmp_path, "Demo Page|[0]")
    assert resolved == f"Demo Page|{BLOCK_UUID}"

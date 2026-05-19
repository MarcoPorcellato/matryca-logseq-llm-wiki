"""Tests for persistent X-Ray alias registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.agent.alias_state import (
    alias_file_path,
    load_alias_map,
    resolve_pipe_target,
    resolve_target,
    save_alias_map,
)

BLOCK_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_save_and_load_alias_map(tmp_path: Path) -> None:
    save_alias_map(tmp_path, {0: BLOCK_UUID, 1: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"})
    path = alias_file_path(tmp_path)
    assert path.is_file()
    loaded = load_alias_map(tmp_path)
    assert loaded == {0: BLOCK_UUID, 1: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["0"] == BLOCK_UUID


def test_resolve_target_passes_through_uuid(tmp_path: Path) -> None:
    save_alias_map(tmp_path, {0: BLOCK_UUID})
    assert resolve_target(tmp_path, BLOCK_UUID) == BLOCK_UUID
    assert resolve_target(tmp_path, "My Page") == "My Page"


def test_resolve_target_unknown_alias_raises(tmp_path: Path) -> None:
    save_alias_map(tmp_path, {0: BLOCK_UUID})
    with pytest.raises(ValueError, match=r"\[9\]"):
        resolve_target(tmp_path, "[9]")


def test_resolve_pipe_target(tmp_path: Path) -> None:
    save_alias_map(tmp_path, {0: BLOCK_UUID})
    resolved = resolve_pipe_target(tmp_path, "Demo Page|[0]")
    assert resolved == f"Demo Page|{BLOCK_UUID}"

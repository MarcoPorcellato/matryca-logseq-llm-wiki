"""Phase 4 red-team remediation: path sandbox, JSON extraction, log rotation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.agent.maintenance_daemon import (
    DaemonState,
    FileState,
    load_daemon_state,
    normalize_daemon_state_file_keys,
    save_daemon_state,
)
from src.graph.alias_index import is_scannable_graph_markdown
from src.graph.path_sandbox import (
    PathTraversalSecurityError,
    assert_path_within_graph,
    graph_relative_path_key,
    read_graph_file_text,
)
from src.utils.json_repair import extract_json_payload_regex, safe_parse_llm_json_dict
from src.utils.rotating_file import rotate_file_if_oversized


def _make_graph(tmp_path: Path) -> Path:
    graph = tmp_path / "graph"
    (graph / "pages").mkdir(parents=True)
    return graph


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    return _make_graph(tmp_path)


def test_assert_path_within_graph_blocks_symlink_escape(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = graph / "pages" / "Linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Platform does not support symlinks in this environment")

    with pytest.raises(PathTraversalSecurityError):
        assert_path_within_graph(link, graph)


def test_is_scannable_graph_markdown_rejects_symlink_escape(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = graph / "pages" / "Linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Platform does not support symlinks in this environment")

    assert is_scannable_graph_markdown(link, graph) is False


def test_read_graph_file_text_blocks_symlink_escape(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = graph / "pages" / "Linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Platform does not support symlinks in this environment")

    with pytest.raises(PathTraversalSecurityError):
        read_graph_file_text(link, graph)


def test_extract_json_payload_regex_strips_conversational_filler() -> None:
    raw = (
        "Sure! Here is the JSON you asked for:\n"
        "```json\n"
        '{"assigned_domain": "risorsa", "detected_tags": ["ai"]}\n'
        "```\n"
        "Hope that helps."
    )
    extracted = extract_json_payload_regex(raw)
    payload = json.loads(extracted)
    assert payload["assigned_domain"] == "risorsa"
    assert payload["detected_tags"] == ["ai"]


def test_safe_parse_llm_json_dict_returns_empty_on_garbage() -> None:
    assert safe_parse_llm_json_dict("not json at all", context="test") == {}


def test_daemon_state_keys_use_graph_relative_posix_paths(graph_root: Path) -> None:
    page = graph_root / "pages" / "Alpha.md"
    page.write_text("- alpha\n", encoding="utf-8")
    key = graph_relative_path_key(page, graph_root)
    assert key == "pages/Alpha.md"
    assert "\\" not in key


def test_normalize_daemon_state_file_keys_migrates_absolute_paths(graph_root: Path) -> None:
    page = graph_root / "pages" / "Beta.md"
    page.write_text("- beta\n", encoding="utf-8")
    absolute = str(page.resolve())
    state = DaemonState(
        files={
            absolute: FileState(
                mtime=page.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    assert normalize_daemon_state_file_keys(graph_root, state) is True
    assert "pages/Beta.md" in state.files
    assert absolute not in state.files


def test_save_daemon_state_persists_relative_file_keys(graph_root: Path) -> None:
    page = graph_root / "pages" / "Gamma.md"
    page.write_text("- gamma\n", encoding="utf-8")
    state = DaemonState(
        files={
            graph_relative_path_key(page, graph_root): FileState(
                mtime=page.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    save_daemon_state(graph_root, state)
    loaded = load_daemon_state(graph_root)
    assert "pages/Gamma.md" in loaded.files


def test_rotate_file_if_oversized_creates_zip_archive(tmp_path: Path) -> None:
    log_path = tmp_path / "ops.log"
    log_path.write_bytes(b"x" * 128)
    rotate_file_if_oversized(log_path, max_bytes=64, retention=3)
    assert log_path.is_file()
    assert log_path.stat().st_size == 0
    assert (tmp_path / "ops.log.1.zip").is_file()

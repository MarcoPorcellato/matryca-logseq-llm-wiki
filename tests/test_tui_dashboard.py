"""Regression tests for the Matryca Plumber Rich TUI dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from src.agent.maintenance_daemon import DaemonState, FileState, save_daemon_state
from src.cli.tui_dashboard import collect_snapshot, collect_snapshot_safe
from src.utils.token_logger import TokenLogger


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    (tmp_path / "pages").mkdir()
    return tmp_path


def _write_page(graph_root: Path, title: str, body: str) -> None:
    (graph_root / "pages" / f"{title}.md").write_text(body, encoding="utf-8")


def test_collect_snapshot_reflects_updated_daemon_state(graph_root: Path) -> None:
    _write_page(graph_root, "Alpha", "- alpha\n")
    _write_page(graph_root, "Beta", "- beta\n")
    logger = TokenLogger(log_path=graph_root / "ops.log")

    first = collect_snapshot(graph_root=graph_root, token_logger=logger)
    assert first.total_pages == 2
    assert first.processed_pages == 0

    alpha_path = graph_root / "pages" / "Alpha.md"
    state = DaemonState(
        files={
            str(alpha_path.resolve()): FileState(
                mtime=alpha_path.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    save_daemon_state(graph_root, state)

    second = collect_snapshot(graph_root=graph_root, token_logger=logger)
    assert second.total_pages == 2
    assert second.processed_pages == 1
    assert second.processed_pages != first.processed_pages


def test_collect_snapshot_activity_feed_updates_when_log_appends(graph_root: Path) -> None:
    _write_page(graph_root, "Snap", "- snap\n")
    log_path = graph_root / "ops.log"
    logger = TokenLogger(log_path=log_path)
    log_path.write_text(json.dumps({"message": "first-event"}) + "\n", encoding="utf-8")

    first = collect_snapshot(graph_root=graph_root, token_logger=logger)
    assert first.activity_lines
    assert "first-event" in first.activity_lines[-1]

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"message": "second-event"}) + "\n")

    second = collect_snapshot(graph_root=graph_root, token_logger=logger)
    assert second.activity_lines
    assert "second-event" in second.activity_lines[-1]
    assert second.activity_lines != first.activity_lines


def test_collect_snapshot_uses_checkpoint_processed_count_despite_mtime_drift(
    graph_root: Path,
) -> None:
    path = graph_root / "pages" / "Drift.md"
    path.write_text("- drift\n", encoding="utf-8")
    stored_mtime = path.stat().st_mtime - 3600.0
    state = DaemonState(
        status="running",
        files={
            str(path.resolve()): FileState(
                mtime=stored_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    save_daemon_state(graph_root, state)
    logger = TokenLogger(log_path=graph_root / "ops.log")

    snap = collect_snapshot(graph_root=graph_root, token_logger=logger)

    assert snap.processed_pages == 1
    assert snap.total_pages == 1
    assert snap.pending_backlog == 0


def test_collect_snapshot_includes_refresh_timestamp(graph_root: Path) -> None:
    _write_page(graph_root, "Tick", "- tick\n")
    snap = collect_snapshot(
        graph_root=graph_root,
        token_logger=TokenLogger(log_path=graph_root / "ops.log"),
    )
    assert snap.refreshed_at
    assert len(snap.refreshed_at) == 8


def test_collect_snapshot_phase2_panel_fields(graph_root: Path) -> None:
    _write_page(graph_root, "Phase", "- phase\n")
    state = DaemonState(
        bootstrap_complete=True,
        status="running",
        current_cluster="cluster-3",
        phase2_llm_turns=2,
        session_prompt_tokens=50,
        session_completion_tokens=10,
    )
    save_daemon_state(graph_root, state)
    logger = TokenLogger(log_path=graph_root / "ops.log")

    snap = collect_snapshot(graph_root=graph_root, token_logger=logger)

    assert snap.bootstrap_complete is True
    assert snap.current_cluster == "cluster-3"
    assert snap.phase2_llm_turns == 2
    assert snap.session_prompt_tokens == 50
    assert snap.session_completion_tokens == 10


def test_collect_snapshot_phase2_cluster_progress_bar(graph_root: Path) -> None:
    _write_page(graph_root, "Phase", "- phase\n")
    state = DaemonState(
        bootstrap_complete=True,
        status="running",
        current_cluster="cluster-9",
        current_cluster_files_total=4,
        current_cluster_files_done=2,
        phase2_llm_turns=10,
    )
    save_daemon_state(graph_root, state)
    logger = TokenLogger(log_path=graph_root / "ops.log")

    snap = collect_snapshot(graph_root=graph_root, token_logger=logger)

    assert snap.current_cluster_files_total == 4
    assert snap.current_cluster_files_done == 2
    assert snap.percent_complete == 50.0


def test_collect_snapshot_safe_falls_back_to_last_good_state_on_read_failure(
    graph_root: Path,
) -> None:
    _write_page(graph_root, "Safe", "- safe\n")
    logger = TokenLogger(log_path=graph_root / "ops.log")
    good_state = DaemonState(status="running", session_prompt_tokens=42)
    save_daemon_state(graph_root, good_state)

    first, cached_state = collect_snapshot_safe(
        graph_root=graph_root,
        token_logger=logger,
    )
    assert first.session_prompt_tokens == 42
    assert cached_state is not None

    with patch(
        "src.cli.tui_dashboard.load_daemon_state",
        side_effect=OSError("state file busy"),
    ):
        second, still_cached = collect_snapshot_safe(
            graph_root=graph_root,
            token_logger=logger,
            last_good_state=cached_state,
        )

    assert second.session_prompt_tokens == 42
    assert still_cached is cached_state

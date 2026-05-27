"""Tests for shared control-room progress resolution."""

from __future__ import annotations

from src.agent.control_room_progress import (
    ControlRoomProgress,
    resolve_control_room_progress,
)
from src.agent.maintenance_daemon import DaemonState, FileState


def test_phase1_bootstrap_counters() -> None:
    state = DaemonState(
        bootstrap_complete=False,
        bootstrap_scanned=25,
        bootstrap_total=100,
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase1_catalog"
    assert progress.done == 25
    assert progress.total == 100
    assert progress.percent == 25.0


def test_phase1_files_fallback() -> None:
    state = DaemonState(
        bootstrap_complete=False,
        files={
            "a.md": FileState(
                mtime=1.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
            "b.md": FileState(
                mtime=1.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="pending",
            ),
        },
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase1_catalog"
    assert progress.done == 1
    assert progress.total == 2
    assert progress.percent == 50.0


def test_phase2_vault_mode() -> None:
    state = DaemonState(
        bootstrap_complete=True,
        phase2_cognitive_total=10,
        phase2_cognitive_done=3,
        current_cluster_files_total=0,
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase2_vault"
    assert progress.done == 3
    assert progress.total == 10
    assert progress.percent == 30.0
    assert "Cognitive Indexing" in progress.title


def test_phase2_cluster_mid_flight() -> None:
    state = DaemonState(
        bootstrap_complete=True,
        status="running",
        current_cluster="cluster-9",
        current_cluster_files_total=5,
        current_cluster_files_done=2,
        phase2_cluster_file_in_flight=True,
        last_file="pages/ActivePage.md",
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase2_cluster"
    assert progress.percent == 40.0
    assert "ActivePage.md" in progress.subtitle
    assert "Processing" in progress.subtitle


def test_phase2_cluster_without_in_flight() -> None:
    state = DaemonState(
        bootstrap_complete=True,
        current_cluster="cluster-1",
        current_cluster_files_total=4,
        current_cluster_files_done=2,
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase2_cluster"
    assert progress.percent == 50.0
    assert progress.subtitle == "2 / 4 cluster files"


def test_legacy_checkpoint_vault_fallback() -> None:
    state = DaemonState(
        bootstrap_complete=True,
        phase2_llm_turns=2,
        files={
            "x.md": FileState(
                mtime=1.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    progress = resolve_control_room_progress(state)
    assert progress.mode == "phase2_vault"
    assert progress.done == 1
    assert progress.total == 1


def test_to_api_fields() -> None:
    progress = ControlRoomProgress(
        mode="phase2_cluster",
        title="T",
        subtitle="S",
        done=1,
        total=2,
        percent=50.0,
    )
    fields = progress.to_api_fields()
    assert fields["progress_mode"] == "phase2_cluster"
    assert fields["progress_percent"] == 50.0

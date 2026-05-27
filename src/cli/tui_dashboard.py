"""Snapshot helpers for monitoring the Matryca Plumber maintenance daemon."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..agent.control_room_progress import resolve_control_room_progress
from ..agent.maintenance_daemon import (
    DEFAULT_MODEL,
    PID_FILENAME,
    DaemonState,
    compute_phase2_progress_metrics,
    compute_scan_metrics,
    is_process_alive,
    load_daemon_state,
    read_pid_file,
    resolve_graph_root,
)
from ..agent.plumber_config import resolve_lm_model
from ..utils.token_logger import TokenLogger, resolve_plumber_log_path

MONITOR_TITLE = "[MATRYCA PLUMBER MONITOR]"


@dataclass
class DashboardSnapshot:
    """Point-in-time metrics for one TUI refresh."""

    title: str = MONITOR_TITLE
    status: str = "Unknown"
    model: str = DEFAULT_MODEL
    bootstrap_complete: bool = False
    total_pages: int = 0
    processed_pages: int = 0
    skipped_pages: int = 0
    error_pages: int = 0
    pending_backlog: int = 0
    percent_complete: float = 0.0
    activity_lines: list[str] | None = None
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    current_cluster: str | None = None
    current_cluster_files_total: int = 0
    current_cluster_files_done: int = 0
    phase2_llm_turns: int = 0
    last_file: str | None = None
    pid: int | None = None
    pid_file: str = PID_FILENAME
    log_file: str = ""
    refreshed_at: str = ""

    def __post_init__(self) -> None:
        if self.activity_lines is None:
            self.activity_lines = []


def _resolve_graph_root_safe() -> Path | None:
    try:
        return resolve_graph_root()
    except ValueError:
        return None


def _tally_checkpoint_files(state: DaemonState) -> tuple[int, int, int]:
    processed = 0
    skipped = 0
    errors = 0
    for record in state.files.values():
        if record.status == "processed":
            processed += 1
        elif record.status == "skipped":
            skipped += 1
        elif record.status == "error":
            errors += 1
    return processed, skipped, errors


def _plumber_token_logger() -> TokenLogger:
    return TokenLogger(log_path=resolve_plumber_log_path())


def _try_load_daemon_state(
    graph_root: Path,
    *,
    last_good: DaemonState | None,
) -> DaemonState:
    """Load daemon state without blocking the TUI on transient read or parse failures."""
    try:
        return load_daemon_state(graph_root)
    except Exception:
        if last_good is not None:
            return last_good
        return DaemonState()


def collect_snapshot(
    *,
    graph_root: Path | None = None,
    token_logger: TokenLogger | None = None,
    last_good_state: DaemonState | None = None,
) -> DashboardSnapshot:
    """Build a dashboard snapshot from on-disk state and logs."""
    root = graph_root or _resolve_graph_root_safe()
    logger = token_logger or _plumber_token_logger()
    log_file = str(logger.log_path)
    if root is None:
        return DashboardSnapshot(
            status="Error",
            activity_lines=["LOGSEQ_GRAPH_PATH is not set"],
            log_file=log_file,
        )

    state = _try_load_daemon_state(root, last_good=last_good_state)
    try:
        metrics = compute_scan_metrics(root, state)
    except Exception:
        metrics = None

    checkpoint_processed, skipped, errors = _tally_checkpoint_files(state)
    total_pages = metrics.total if metrics is not None else 0
    if state.bootstrap_complete:
        try:
            phase2_total, phase2_done, phase2_pending, phase2_skipped = (
                compute_phase2_progress_metrics(root, state)
            )
        except Exception:
            phase2_total = total_pages
            phase2_done = checkpoint_processed
            phase2_pending = max(0, total_pages - checkpoint_processed - skipped - errors)
            phase2_skipped = skipped
        processed_pages = phase2_done
        skipped = phase2_skipped
        pending_backlog = phase2_pending
        total_pages = phase2_total
        percent_complete = round(100.0 * phase2_done / max(phase2_total, 1), 1)
    elif metrics is not None and total_pages > 0:
        # Checkpoint tally reflects completed daemon work; scan metrics only count
        # pages whose on-disk mtime still matches the checkpoint (mtime drift =>
        # pending re-index). The TUI progress bar tracks work done, not strict freshness.
        processed_pages = checkpoint_processed
        pending_backlog = max(0, total_pages - checkpoint_processed - skipped - errors)
        percent_complete = round(100.0 * processed_pages / total_pages, 1)
    else:
        processed_pages = checkpoint_processed
        pending_backlog = 0
        percent_complete = 0.0

    log_prompt_tokens, log_completion_tokens = logger.session_token_totals_from_log()
    session_prompt_tokens = max(state.session_prompt_tokens, log_prompt_tokens)
    session_completion_tokens = max(
        state.session_completion_tokens,
        log_completion_tokens,
    )

    pid = read_pid_file(root)
    running = pid is not None and is_process_alive(pid)
    status = "Stopped"
    if running:
        if state.status == "running":
            status = "Running"
        elif state.status == "idle":
            status = "Idle"
        else:
            status = state.status.capitalize()
        if (
            status == "Idle"
            and state.bootstrap_complete
            and (session_prompt_tokens or session_completion_tokens or pending_backlog > 0)
        ):
            status = "Running"

    last_file = None
    if state.last_file:
        last_file = Path(state.last_file).name

    activity_lines: list[str] = []
    with contextlib.suppress(Exception):
        activity_lines = logger.tail_activity_summaries(5)

    control_room = resolve_control_room_progress(state)

    return DashboardSnapshot(
        status=status,
        model=state.model or resolve_lm_model(),
        bootstrap_complete=state.bootstrap_complete,
        total_pages=total_pages,
        processed_pages=processed_pages,
        skipped_pages=skipped,
        error_pages=errors,
        pending_backlog=pending_backlog,
        percent_complete=control_room.percent if state.bootstrap_complete else percent_complete,
        activity_lines=activity_lines,
        session_prompt_tokens=session_prompt_tokens,
        session_completion_tokens=session_completion_tokens,
        current_cluster=state.current_cluster,
        current_cluster_files_total=state.current_cluster_files_total,
        current_cluster_files_done=state.current_cluster_files_done,
        phase2_llm_turns=state.phase2_llm_turns,
        last_file=last_file,
        pid=pid,
        pid_file=PID_FILENAME,
        log_file=log_file,
        refreshed_at=datetime.now(tz=UTC).strftime("%H:%M:%S"),
    )


def collect_snapshot_safe(
    *,
    graph_root: Path | None = None,
    token_logger: TokenLogger | None = None,
    last_good_state: DaemonState | None = None,
) -> tuple[DashboardSnapshot, DaemonState | None]:
    """Collect a snapshot, preserving the last known good daemon state on read failures."""
    root = graph_root or _resolve_graph_root_safe()
    logger = token_logger or _plumber_token_logger()
    try:
        snapshot = collect_snapshot(
            graph_root=root,
            token_logger=logger,
            last_good_state=last_good_state,
        )
    except Exception:
        return DashboardSnapshot(
            status="Error",
            activity_lines=["Dashboard refresh failed"],
            log_file=str(logger.log_path),
            refreshed_at=datetime.now(tz=UTC).strftime("%H:%M:%S"),
        ), last_good_state

    if root is not None:
        with contextlib.suppress(Exception):
            last_good_state = load_daemon_state(root)
    return snapshot, last_good_state


__all__ = [
    "MONITOR_TITLE",
    "DashboardSnapshot",
    "collect_snapshot",
    "collect_snapshot_safe",
]

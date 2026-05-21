"""Rich TUI dashboard for monitoring the Matryca Plumber maintenance daemon."""

from __future__ import annotations

import contextlib
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from rich.console import Console, Group, ScreenContext
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from ..agent.maintenance_daemon import (
    DEFAULT_MODEL,
    PID_FILENAME,
    DaemonState,
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
    if metrics is not None and total_pages > 0:
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
            and (state.session_prompt_tokens or state.session_completion_tokens)
        ):
            status = "Running"

    last_file = None
    if state.last_file:
        last_file = Path(state.last_file).name

    activity_lines: list[str] = []
    with contextlib.suppress(Exception):
        activity_lines = logger.tail_activity_summaries(5)

    phase2_progress_total = max(state.phase2_llm_turns + pending_backlog, 1)
    phase2_percent = round(100.0 * state.phase2_llm_turns / phase2_progress_total, 1)

    return DashboardSnapshot(
        status=status,
        model=state.model or resolve_lm_model(),
        bootstrap_complete=state.bootstrap_complete,
        total_pages=total_pages,
        processed_pages=processed_pages,
        skipped_pages=skipped,
        error_pages=errors,
        pending_backlog=pending_backlog,
        percent_complete=phase2_percent if state.bootstrap_complete else percent_complete,
        activity_lines=activity_lines,
        session_prompt_tokens=state.session_prompt_tokens,
        session_completion_tokens=state.session_completion_tokens,
        current_cluster=state.current_cluster,
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


def _init_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=8),
    )
    layout["body"].split_row(
        Layout(name="progress", ratio=2),
        Layout(name="tokens", ratio=1),
    )
    return layout


def _token_style(snapshot: DashboardSnapshot) -> str:
    if snapshot.session_prompt_tokens > 0 or snapshot.session_completion_tokens > 0:
        return "bold green"
    return ""


def _apply_snapshot(layout: Layout, snapshot: DashboardSnapshot) -> None:
    status_style = (
        "green"
        if snapshot.status == "Running"
        else ("green" if snapshot.status == "Idle" else "yellow")
    )
    header_text = Text.assemble(
        (snapshot.title, "bold cyan"),
        "\n",
        ("Status: ", "bold"),
        (snapshot.status, status_style),
        "   ",
        ("Bootstrap: ", "bold"),
        (
            "Complete" if snapshot.bootstrap_complete else "In progress",
            "green" if snapshot.bootstrap_complete else "yellow",
        ),
        "   ",
        ("Model: ", "bold"),
        (snapshot.model, "magenta"),
    )
    if snapshot.pid:
        header_text.append(f"   PID: {snapshot.pid}", style="dim")
    if snapshot.refreshed_at:
        header_text.append(f"   Updated: {snapshot.refreshed_at}", style="dim cyan")
    layout["header"].update(Panel(header_text, border_style="cyan"))

    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        expand=True,
    )
    if snapshot.bootstrap_complete:
        phase2_total = max(snapshot.phase2_llm_turns + snapshot.pending_backlog, 1)
        task_label = "Phase 2 cognitive"
        if snapshot.current_cluster:
            task_label = f"Phase 2 · cluster {snapshot.current_cluster}"
        task_id = progress.add_task(task_label, total=phase2_total)
        progress.update(task_id, completed=snapshot.phase2_llm_turns)
    else:
        task_id = progress.add_task("Graph indexing (Phase 1)", total=max(snapshot.total_pages, 1))
        progress.update(task_id, completed=snapshot.processed_pages)

    stats = Table.grid(padding=(0, 1))
    if snapshot.bootstrap_complete:
        stats.add_row("Phase", "2 — cognitive")
        stats.add_row("Semantic cluster", snapshot.current_cluster or "—")
        stats.add_row("LLM turns (session)", str(snapshot.phase2_llm_turns))
        stats.add_row("Pending backlog", str(snapshot.pending_backlog))
        stats.add_row("Completion", f"{snapshot.percent_complete}%")
        stats.add_row("Catalog processed", str(snapshot.processed_pages))
        stats.add_row("Catalog skipped", str(snapshot.skipped_pages))
    else:
        stats.add_row("Total pages", str(snapshot.total_pages))
        stats.add_row("Processed", str(snapshot.processed_pages))
        stats.add_row("Skipped", str(snapshot.skipped_pages))
        stats.add_row("Errors", str(snapshot.error_pages))
        stats.add_row("Pending backlog", str(snapshot.pending_backlog))
        stats.add_row("Completion", f"{snapshot.percent_complete}%")
    if snapshot.last_file:
        stats.add_row("Current file", snapshot.last_file)
    stats.add_row("PID file", snapshot.pid_file)
    if snapshot.log_file:
        stats.add_row("Ops log", snapshot.log_file)

    progress_title = "Phase 2 Progress" if snapshot.bootstrap_complete else "Progress"
    layout["progress"].update(
        Panel(
            Group(progress, "", stats),
            title=progress_title,
            border_style="blue",
        ),
    )

    token_style = _token_style(snapshot)
    token_table = Table.grid(padding=(0, 1))
    token_table.add_row(
        "Prompt tokens",
        Text(str(snapshot.session_prompt_tokens), style=token_style),
    )
    token_table.add_row(
        "Completion tokens",
        Text(str(snapshot.session_completion_tokens), style=token_style),
    )
    token_table.add_row(
        "Session total",
        Text(
            str(snapshot.session_prompt_tokens + snapshot.session_completion_tokens),
            style=token_style,
        ),
    )
    layout["tokens"].update(Panel(token_table, title="Token Counter", border_style="green"))

    feed = "\n".join(snapshot.activity_lines or ["(no activity yet)"])
    layout["footer"].update(Panel(feed, title="Activity Feed (last 5 ops)", border_style="dim"))


def _build_layout(snapshot: DashboardSnapshot) -> Layout:
    layout = _init_layout()
    _apply_snapshot(layout, snapshot)
    return layout


def _clear_display(console: Console) -> None:
    """Clear the terminal using Rich or a raw ANSI fallback."""
    if console.is_dumb_terminal:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        return
    console.clear(home=True)


def _paint_dashboard(
    console: Console,
    screen: ScreenContext | None,
    snapshot: DashboardSnapshot,
) -> None:
    """Redraw one dashboard frame."""
    layout = _build_layout(snapshot)
    if screen is not None:
        screen.update(layout)
        return
    _clear_display(console)
    console.print(layout)


def run_dashboard(
    *,
    graph_root: Path | None = None,
    refresh_hz: float = 1.0,
) -> None:
    """Run the live TUI until interrupted."""
    interval = 1.0 / max(refresh_hz, 0.1)
    logger = _plumber_token_logger()
    console = Console()
    last_good_state: DaemonState | None = None
    screen_ctx: ScreenContext | None = None

    previous_sigwinch = None
    if hasattr(signal, "SIGWINCH"):
        previous_sigwinch = signal.getsignal(signal.SIGWINCH)

    def _refresh_frame() -> DashboardSnapshot:
        nonlocal last_good_state
        snapshot, last_good_state = collect_snapshot_safe(
            graph_root=graph_root,
            token_logger=logger,
            last_good_state=last_good_state,
        )
        _paint_dashboard(console, screen_ctx, snapshot)
        return snapshot

    try:
        with console.screen() as screen:
            screen_ctx = screen

            def _handle_sigwinch(signum: int, frame_info: FrameType | None) -> None:
                with contextlib.suppress(Exception):
                    _refresh_frame()
                if callable(previous_sigwinch):
                    previous_sigwinch(signum, frame_info)

            if hasattr(signal, "SIGWINCH"):
                signal.signal(signal.SIGWINCH, _handle_sigwinch)

            _refresh_frame()
            while True:
                time.sleep(interval)
                _refresh_frame()
    except KeyboardInterrupt:
        return
    finally:
        if hasattr(signal, "SIGWINCH") and previous_sigwinch is not None:
            signal.signal(signal.SIGWINCH, previous_sigwinch)
        with contextlib.suppress(Exception):
            console.show_cursor(True)


__all__ = [
    "MONITOR_TITLE",
    "DashboardSnapshot",
    "collect_snapshot",
    "collect_snapshot_safe",
    "run_dashboard",
]

"""Rich TUI dashboard for monitoring the Matryca Plumber maintenance daemon."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
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
    last_file: str | None = None
    pid: int | None = None
    pid_file: str = PID_FILENAME
    log_file: str = ""

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


def collect_snapshot(
    *,
    graph_root: Path | None = None,
    token_logger: TokenLogger | None = None,
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

    state = load_daemon_state(root)
    metrics = compute_scan_metrics(root, state)
    _, skipped, errors = _tally_checkpoint_files(state)
    pid = read_pid_file(root)
    running = pid is not None and is_process_alive(pid)
    status = "Stopped"
    if running:
        if state.status == "idle":
            status = "Idle"
        elif state.status == "running":
            status = "Running"
        else:
            status = state.status.capitalize()

    last_file = None
    if state.last_file:
        last_file = Path(state.last_file).name

    return DashboardSnapshot(
        status=status,
        model=state.model or resolve_lm_model(),
        bootstrap_complete=state.bootstrap_complete,
        total_pages=metrics.total,
        processed_pages=metrics.processed,
        skipped_pages=skipped,
        error_pages=errors,
        pending_backlog=metrics.pending,
        percent_complete=metrics.percent_complete,
        activity_lines=logger.tail_summaries(5),
        session_prompt_tokens=state.session_prompt_tokens or logger.session_prompt_tokens,
        session_completion_tokens=(
            state.session_completion_tokens or logger.session_completion_tokens
        ),
        last_file=last_file,
        pid=pid,
        pid_file=PID_FILENAME,
        log_file=log_file,
    )


def _build_layout(snapshot: DashboardSnapshot) -> Layout:
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

    header_text = Text.assemble(
        (snapshot.title, "bold cyan"),
        "\n",
        ("Status: ", "bold"),
        (snapshot.status, "green" if snapshot.status in {"Running", "Idle"} else "yellow"),
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
    layout["header"].update(Panel(header_text, border_style="cyan"))

    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        expand=True,
    )
    task_id = progress.add_task("Graph indexing", total=max(snapshot.total_pages, 1))
    progress.update(task_id, completed=snapshot.processed_pages)

    stats = Table.grid(padding=(0, 1))
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

    layout["progress"].update(
        Panel(
            Group(progress, "", stats),
            title="Progress",
            border_style="blue",
        ),
    )

    token_table = Table.grid(padding=(0, 1))
    token_table.add_row("Prompt tokens", str(snapshot.session_prompt_tokens))
    token_table.add_row("Completion tokens", str(snapshot.session_completion_tokens))
    token_table.add_row(
        "Session total",
        str(snapshot.session_prompt_tokens + snapshot.session_completion_tokens),
    )
    layout["tokens"].update(Panel(token_table, title="Token Counter", border_style="green"))

    feed = "\n".join(snapshot.activity_lines or ["(no activity yet)"])
    layout["footer"].update(Panel(feed, title="Activity Feed (last 5 ops)", border_style="dim"))
    return layout


def run_dashboard(
    *,
    graph_root: Path | None = None,
    refresh_hz: float = 2.0,
) -> None:
    """Run the live TUI until interrupted."""
    interval = 1.0 / max(refresh_hz, 0.1)
    logger = _plumber_token_logger()
    initial = _build_layout(collect_snapshot(graph_root=graph_root, token_logger=logger))
    with Live(initial, refresh_per_second=4, screen=True) as live:
        try:
            while True:
                time.sleep(interval)
                snap = collect_snapshot(graph_root=graph_root, token_logger=logger)
                live.update(_build_layout(snap))
        except KeyboardInterrupt:
            return


__all__ = [
    "MONITOR_TITLE",
    "DashboardSnapshot",
    "collect_snapshot",
    "run_dashboard",
]

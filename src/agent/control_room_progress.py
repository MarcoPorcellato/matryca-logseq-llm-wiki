"""Shared control-room progress resolution for TUI, API, and Sovereign UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .maintenance_daemon import DaemonState

ControlRoomProgressMode = Literal["phase1_catalog", "phase2_cluster", "phase2_vault"]


@dataclass(frozen=True)
class ControlRoomProgress:
    """Canonical progress view for Matryca Plumber dashboards."""

    mode: ControlRoomProgressMode
    title: str
    subtitle: str
    done: int
    total: int
    percent: float

    def to_api_fields(self) -> dict[str, Any]:
        """Serialize for ``DaemonStateResponse`` / ``GET /api/state``."""
        return {
            "progress_mode": self.mode,
            "progress_title": self.title,
            "progress_subtitle": self.subtitle,
            "progress_done": self.done,
            "progress_total": self.total,
            "progress_percent": self.percent,
        }


def _percent(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(min(100.0, 100.0 * done / total), 1)


def _basename_from_path(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).name


def _phase1_files_tally(state: DaemonState) -> tuple[int, int]:
    entries = list(state.files.values())
    processed = sum(1 for rec in entries if rec.status == "processed")
    return processed, len(entries)


def _legacy_phase2_vault_fallback(state: DaemonState) -> tuple[int, int]:
    """Fallback when persisted vault counters are missing (old checkpoints)."""
    processed, total = _phase1_files_tally(state)
    if total > 0:
        return processed, total
    turns = max(state.phase2_llm_turns, 0)
    return turns, max(turns, 1)


def resolve_control_room_progress(state: DaemonState) -> ControlRoomProgress:
    """Resolve progress bar metrics from a daemon checkpoint (no graph scan)."""
    if not state.bootstrap_complete:
        bootstrap_total = state.bootstrap_total
        bootstrap_scanned = state.bootstrap_scanned
        if bootstrap_total > 0:
            done = bootstrap_scanned
            total = bootstrap_total
            return ControlRoomProgress(
                mode="phase1_catalog",
                title="Phase 1: Cataloging Graph",
                subtitle=f"{done} / {total} pages indexed",
                done=done,
                total=total,
                percent=_percent(done, total),
            )
        done, total = _phase1_files_tally(state)
        return ControlRoomProgress(
            mode="phase1_catalog",
            title="Phase 1: Cataloging Graph",
            subtitle=f"{done} / {total} pages indexed",
            done=done,
            total=total,
            percent=_percent(done, total),
        )

    cluster_total = state.current_cluster_files_total
    if cluster_total > 0:
        cluster_id = state.current_cluster or "—"
        done = min(state.current_cluster_files_done, cluster_total)
        in_flight = bool(
            state.phase2_cluster_file_in_flight
            and state.status == "running"
            and state.last_file
            and done < cluster_total
        )
        subtitle = f"{done} / {cluster_total} cluster files"
        if in_flight:
            basename = _basename_from_path(state.last_file)
            if basename:
                subtitle = f"Processing {basename}… ({done} / {cluster_total} cluster files)"
        return ControlRoomProgress(
            mode="phase2_cluster",
            title=f"Phase 2: Semantic Neighborhood Cluster [{cluster_id}]",
            subtitle=subtitle,
            done=done,
            total=cluster_total,
            percent=_percent(done, cluster_total),
        )

    vault_total = state.phase2_cognitive_total
    vault_done = state.phase2_cognitive_done
    if vault_total <= 0:
        vault_done, vault_total = _legacy_phase2_vault_fallback(state)

    vault_done = min(vault_done, vault_total) if vault_total > 0 else vault_done
    return ControlRoomProgress(
        mode="phase2_vault",
        title="Phase 2: Cognitive Indexing",
        subtitle=f"{vault_done} / {vault_total} pages cognitively indexed",
        done=vault_done,
        total=max(vault_total, 1),
        percent=_percent(vault_done, max(vault_total, 1)),
    )


def refresh_phase2_cognitive_totals(graph_root: Path, state: DaemonState) -> None:
    """Recompute vault-wide Phase 2 counters (O(pages); call at bootstrap / cycle start)."""
    from .maintenance_daemon import compute_phase2_progress_metrics

    total, done, _pending, _skipped = compute_phase2_progress_metrics(graph_root, state)
    state.phase2_cognitive_total = total
    state.phase2_cognitive_done = done

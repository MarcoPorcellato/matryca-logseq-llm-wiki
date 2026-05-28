"""Matryca Plumber Maintenance Daemon — progressive local LLM graph indexing."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from loguru import logger
from pydantic import BaseModel, Field

from ..config import load_matryca_wiki_config
from ..graph.alias_index import (
    AliasIndex,
    iter_alias_source_paths,
    resolve_canonical_page_title,
)
from ..graph.bootstrap_harvest import (
    run_bootstrap_harvest,
    run_incremental_catalog_refresh,
)
from ..graph.concurrency_probe import probe_concurrency_capability
from ..graph.generational_cache import (
    cached_build_alias_index,
    gc_generational_alias_cache,
    patch_generational_caches_for_paths,
)
from ..graph.global_fence_scanner import compute_page_protected_line_indices
from ..graph.graph_analytics import reconcile_telemetry_ledger
from ..graph.insights_engine import (
    run_graph_insights_engine,
)
from ..graph.json_flock import cross_process_json_flock
from ..graph.logseq_uuid import find_malformed_block_refs, is_malformed_block_ref_error
from ..graph.markdown_blocks import (
    atomic_write_bytes,
    atomic_write_bytes_if_unchanged,
    block_property_insert_index,
    bullet_indent_unit,
    file_mtime_drifted,
    locate_block_by_uuid,
    occ_snapshot,
    occ_verify_before_write,
    read_file_mtime,
    strip_lines_for_match,
)
from ..graph.master_catalog import (
    SEMANTIC_INDEX_HEADER,
    SEMANTIC_INDEX_HEADING,
    extract_catalog_fields_from_content,
    is_bootstrap_catalog_complete,
    load_master_catalog,
    master_index_page_path,
)
from ..graph.mldoc_properties import parse_logseq_property_line
from ..graph.page_path import page_title_from_path
from ..graph.page_write_lock import (
    PageLockUnavailableError,
    clear_page_write_locks,
    page_rmw_lock,
    probe_page_rmw_lock,
    sweep_matryca_lock_sidecars,
)
from ..graph.path_sandbox import (
    graph_relative_path_key,
    normalize_daemon_file_key,
)
from ..graph.semantic_clustering import (
    format_cluster_neighborhood,
    load_or_compute_semantic_clusters,
)
from ..utils.console_sanitize import sanitize_for_console
from ..utils.logging_config import configure_loguru
from ..utils.runtime_bootstrap import prepare_matryca_runtime
from ..utils.token_logger import OperationType, TokenLogger
from .control_room_progress import refresh_phase2_cognitive_totals
from .cooperative_yield import bootstrap_checkpoint_every
from .llm_client import (
    InstructorLLMClient as _BaseInstructorLLMClient,
)
from .llm_client import (
    LLMResponseError,
    StructuredOutputExhaustedError,
    ThermalProfile,
)
from .llm_context_payload import prepare_llm_context_payload
from .memory_budget import log_snapshot, maybe_release_after_cycle, release_phase1_memory
from .page_prompt_session import PagePromptSession, build_page_prompt_session
from .plumber_config import (
    DEFAULT_LLM_MODEL_NAME,
    DEFAULT_LM_BASE_URL,
    DEFAULT_LM_MODEL,
    PlumberLintConfig,
    bootstrap_phase_lint_config,
    load_plumber_lint_config,
    reload_plumber_dotenv,
    resolve_llm_model_name,
)
from .plumber_llm import (
    BootstrapSummaryResult,
    GraphInsightsLLMResult,
)
from .plumber_modules import CognitiveLintOutcome, run_cognitive_lint_pipeline
from .plumber_modules.backlink_backpropagator import BacklinkCorrection, run_backlink_backpropagator
from .plumber_modules.semantic_cache_router import (
    cache_get,
    cache_put,
    purge_expired_semantic_cache,
    semantic_cache_key,
)
from .process_priority import apply_cpu_sandbox, apply_plumber_priority, resolve_cpu_sandbox_config
from .prompt_layout import build_cache_aligned_prompt
from .semantic_lint_prompts import build_semantic_lint_system_prompt

STATE_FILENAME = ".matryca_daemon_state.json"
STATE_TMP_FILENAME = f"{STATE_FILENAME}.tmp"
STATE_BAK_FILENAME = f"{STATE_FILENAME}.bak"
PID_FILENAME = ".matryca_plumber_daemon.pid"
DAEMON_LOCK_FILENAME = ".matryca_plumber_daemon.lock"
PLUMBER_PID_MARKER = "matryca-plumber-daemon"
DEFAULT_STOP_GRACE_SECONDS = 130.0
DEFAULT_STOP_SIGKILL_AFTER_SECONDS = 125.0
SHUTDOWN_INFLIGHT_TIMEOUT_SECONDS = 120.0
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FILE_STATUS_PRIORITY = {
    "processed": 5,
    "error": 4,
    "skipped": 3,
    "lock_backoff": 2,
    "pending": 1,
}
_LOCK_BACKOFF_INITIAL_S = 30.0
_LOCK_BACKOFF_MAX_S = 300.0
STRUCTURAL_LINT_HEADING = "### Matryca Structural Lint"
STRUCTURAL_LINT_HEADER = f"- {STRUCTURAL_LINT_HEADING}"
DEFAULT_MODEL = DEFAULT_LLM_MODEL_NAME  # backward-compatible alias for CLI/TUI
DEFAULT_POLL_SECONDS = 30.0
MATRYCA_GENERATED_PAGE_TITLES = frozenset(
    {"Matryca Master Index", "Matryca Graph Insights"},
)

_fcntl: Any
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows and other non-Unix platforms
    _fcntl = None

_msvcrt: Any
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows platforms
    _msvcrt = None

_daemon_process_lock_fd: int | None = None


def _semantic_index_section_present(content: str) -> bool:
    return SEMANTIC_INDEX_HEADING in content


def _structural_lint_section_present(content: str) -> bool:
    return STRUCTURAL_LINT_HEADING in content


_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ID_LINE = re.compile(r"^\s*id::\s*(.+?)\s*$", re.IGNORECASE)
_MATRYCA_PLUMBER_LINE = re.compile(r"^\s*matryca-plumber::\s*", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[([^\]#|]+)(?:\|[^\]]+)?\]\]")

DaemonStatus = Literal["running", "idle", "stopped", "error"]
LintType = Literal["auto_wikilink", "tag_hygiene", "anomaly_warning"]


class SemanticLintCorrection(BaseModel):
    """Single semantic lint fix targeting one outliner block."""

    block_uuid: str = Field(description="UUID of the block to enrich (from id:: line)")
    original_text: str = Field(description="Exact bullet-line text excluding id:: properties")
    corrected_text: str = Field(description="Bullet text with added [[WikiLinks]] or #tags")
    lint_type: LintType = Field(description="Lint rule that produced this correction")
    reason: str = Field(description="Brief explanation of why the rule fired")


class SemanticCrossRef(BaseModel):
    """One semantic link extracted from page content."""

    concept: str = Field(description="Short concept label")
    relation: str = Field(description="Relationship type, e.g. related_to, see_also")
    target: str = Field(
        description=(
            "Existing [[Canonical Page Title]] or #tag from AliasIndex; "
            "never invent a new page title when an alias or canonical match exists"
        ),
    )


class SemanticIndexResult(BaseModel):
    """Structured semantic index + Karpathy-style lint payload from the local LLM."""

    summary: str = Field(description="One-sentence page summary")
    cross_references: list[SemanticCrossRef] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    moc_pointers: list[str] = Field(
        default_factory=list,
        description=(
            "Suggested [[Map of Content]] links that must already exist in AliasIndex; "
            "prefer alias:: over new pages"
        ),
    )
    semantic_corrections: list[SemanticLintCorrection] = Field(
        default_factory=list,
        description="Safe per-block micro-corrections (additive WikiLinks / tags only)",
    )


class LLMClient(Protocol):
    """Minimal LLM surface used by the daemon (for tests)."""

    def index_page(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        alias_index: AliasIndex | None = None,
        enable_semantic_routing: bool = False,
        llm_context: str | None = None,
        prompt_session: PagePromptSession | None = None,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        """Return structured index and token usage dict with prompt/completion keys."""
        ...

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        task_instruction: str | None = None,
    ) -> BootstrapSummaryResult:
        """Return a one-sentence bootstrap summary."""
        ...

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult:
        """Return structured graph diagnostics."""
        ...


FileStatus = Literal["processed", "skipped", "error", "pending", "lock_backoff"]


@dataclass
class FileState:
    """Processing record for one markdown file."""

    mtime: float
    processed_at: str
    status: FileStatus = "processed"
    error: str | None = None
    lock_backoff_until: float | None = None
    lock_backoff_seconds: float | None = None


BootstrapHarvestStatus = Literal["regex", "llm", "skipped", "error"]
BOOTSTRAP_RECENT_MAX = 30


@dataclass
class BootstrapRecentEntry:
    """Recent Phase 1 catalog page for control-room pills."""

    harvest: BootstrapHarvestStatus
    processed_at: str


def upsert_bootstrap_recent(
    state: DaemonState,
    path_key: str,
    harvest: BootstrapHarvestStatus,
) -> None:
    """Record one cataloged page; evict oldest entries beyond ``BOOTSTRAP_RECENT_MAX``."""
    now = datetime.now(tz=UTC).isoformat()
    state.bootstrap_recent[path_key] = BootstrapRecentEntry(harvest=harvest, processed_at=now)
    if len(state.bootstrap_recent) <= BOOTSTRAP_RECENT_MAX:
        return
    oldest_key = min(
        state.bootstrap_recent,
        key=lambda key: state.bootstrap_recent[key].processed_at,
    )
    del state.bootstrap_recent[oldest_key]


def record_bootstrap_harvest_impact(
    state: DaemonState,
    harvest: BootstrapHarvestStatus | None,
) -> None:
    """Increment session ledger when a catalog summary is written."""
    if harvest in ("regex", "llm"):
        state.page_summaries_created += 1


@dataclass
class DaemonState:
    """Persistent daemon checkpoint stored inside the graph root."""

    version: int = 1
    files: dict[str, FileState] = field(default_factory=dict)
    status: DaemonStatus = "idle"
    model: str = DEFAULT_LM_MODEL
    bootstrap_complete: bool = False
    bootstrap_scanned: int = 0
    bootstrap_total: int = 0
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    current_cluster: str | None = None
    current_cluster_files_total: int = 0
    current_cluster_files_done: int = 0
    phase2_cognitive_total: int = 0
    phase2_cognitive_done: int = 0
    phase2_cluster_file_in_flight: bool = False
    phase2_llm_turns: int = 0
    last_scan_at: str | None = None
    last_file: str | None = None
    ai_pages_created: int = 0
    ai_links_injected: int = 0
    ai_blocks_healed: int = 0
    hygiene_corrections: int = 0
    page_summaries_created: int = 0
    bootstrap_recent: dict[str, BootstrapRecentEntry] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "model": self.model,
            "bootstrap_complete": self.bootstrap_complete,
            "bootstrap_scanned": self.bootstrap_scanned,
            "bootstrap_total": self.bootstrap_total,
            "session_prompt_tokens": self.session_prompt_tokens,
            "session_completion_tokens": self.session_completion_tokens,
            "current_cluster": self.current_cluster,
            "current_cluster_files_total": self.current_cluster_files_total,
            "current_cluster_files_done": self.current_cluster_files_done,
            "phase2_cognitive_total": self.phase2_cognitive_total,
            "phase2_cognitive_done": self.phase2_cognitive_done,
            "phase2_cluster_file_in_flight": self.phase2_cluster_file_in_flight,
            "phase2_llm_turns": self.phase2_llm_turns,
            "last_scan_at": self.last_scan_at,
            "last_file": self.last_file,
            "ai_pages_created": self.ai_pages_created,
            "ai_links_injected": self.ai_links_injected,
            "ai_blocks_healed": self.ai_blocks_healed,
            "hygiene_corrections": self.hygiene_corrections,
            "page_summaries_created": self.page_summaries_created,
            "bootstrap_recent": {
                path: {
                    "harvest": rec.harvest,
                    "processed_at": rec.processed_at,
                }
                for path, rec in self.bootstrap_recent.items()
            },
            "files": {
                path: {
                    "mtime": rec.mtime,
                    "processed_at": rec.processed_at,
                    "status": rec.status,
                    "error": rec.error,
                    "lock_backoff_until": rec.lock_backoff_until,
                    "lock_backoff_seconds": rec.lock_backoff_seconds,
                }
                for path, rec in self.files.items()
            },
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> DaemonState:
        files: dict[str, FileState] = {}
        raw_files = payload.get("files", {})
        if isinstance(raw_files, dict):
            for path, rec in raw_files.items():
                if not isinstance(rec, dict):
                    continue
                backoff_until = rec.get("lock_backoff_until")
                backoff_seconds = rec.get("lock_backoff_seconds")
                files[str(path)] = FileState(
                    mtime=float(rec.get("mtime", 0.0)),
                    processed_at=str(rec.get("processed_at", "")),
                    status=str(rec.get("status", "processed")),  # type: ignore[arg-type]
                    error=rec.get("error") if rec.get("error") is not None else None,
                    lock_backoff_until=(
                        float(backoff_until) if backoff_until is not None else None
                    ),
                    lock_backoff_seconds=(
                        float(backoff_seconds) if backoff_seconds is not None else None
                    ),
                )
        return cls(
            version=int(payload.get("version", 1)),
            files=files,
            status=str(payload.get("status", "idle")),  # type: ignore[arg-type]
            model=str(payload.get("model", DEFAULT_LM_MODEL)),
            bootstrap_complete=bool(payload.get("bootstrap_complete", False)),
            bootstrap_scanned=int(payload.get("bootstrap_scanned", 0)),
            bootstrap_total=int(payload.get("bootstrap_total", 0)),
            session_prompt_tokens=int(payload.get("session_prompt_tokens", 0)),
            session_completion_tokens=int(payload.get("session_completion_tokens", 0)),
            current_cluster=(
                str(payload["current_cluster"])
                if payload.get("current_cluster") not in (None, "")
                else None
            ),
            current_cluster_files_total=int(payload.get("current_cluster_files_total", 0)),
            current_cluster_files_done=int(payload.get("current_cluster_files_done", 0)),
            phase2_cognitive_total=int(payload.get("phase2_cognitive_total", 0)),
            phase2_cognitive_done=int(payload.get("phase2_cognitive_done", 0)),
            phase2_cluster_file_in_flight=bool(payload.get("phase2_cluster_file_in_flight", False)),
            phase2_llm_turns=int(payload.get("phase2_llm_turns", 0)),
            last_scan_at=payload.get("last_scan_at"),
            last_file=payload.get("last_file"),
            ai_pages_created=int(payload.get("ai_pages_created", 0)),
            ai_links_injected=int(
                payload.get("ai_links_injected", payload.get("links_backpropagated", 0))
            ),
            ai_blocks_healed=int(payload.get("ai_blocks_healed", payload.get("blocks_healed", 0))),
            hygiene_corrections=int(payload.get("hygiene_corrections", 0)),
            page_summaries_created=int(payload.get("page_summaries_created", 0)),
            bootstrap_recent=_bootstrap_recent_from_json(payload.get("bootstrap_recent")),
        )


def _bootstrap_recent_from_json(
    raw: object,
) -> dict[str, BootstrapRecentEntry]:
    if not isinstance(raw, dict):
        return {}
    recent: dict[str, BootstrapRecentEntry] = {}
    for path, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        harvest = str(rec.get("harvest", ""))
        if harvest not in ("regex", "llm", "skipped", "error"):
            continue
        recent[str(path)] = BootstrapRecentEntry(
            harvest=harvest,  # type: ignore[arg-type]
            processed_at=str(rec.get("processed_at", "")),
        )
    return recent


def record_daemon_impact(
    state: DaemonState,
    *,
    cognitive: CognitiveLintOutcome | None = None,
    lint: CorrectionOutcome | None = None,
    links_backpropagated: int = 0,
) -> None:
    """Accumulate provenance counters from one Plumber cycle turn."""
    if cognitive is not None:
        state.ai_pages_created += len(cognitive.pages_created)
        for detail in cognitive.details:
            if (
                detail.startswith("properties:")
                or detail.startswith("alias:")
                or detail.startswith("marpa:")
            ):
                state.hygiene_corrections += 1
            elif detail.startswith("split:"):
                state.ai_blocks_healed += 1
    if lint is not None:
        state.ai_blocks_healed += lint.applied
    if links_backpropagated > 0:
        state.ai_links_injected += links_backpropagated


def _lock_backoff_active(rec: FileState) -> bool:
    if rec.status != "lock_backoff":
        return False
    if rec.lock_backoff_until is None:
        return False
    return time.time() < rec.lock_backoff_until


def _next_lock_backoff_seconds(rec: FileState | None) -> float:
    if rec is None or rec.status != "lock_backoff":
        return _LOCK_BACKOFF_INITIAL_S
    previous = rec.lock_backoff_seconds or _LOCK_BACKOFF_INITIAL_S
    return min(previous * 2.0, _LOCK_BACKOFF_MAX_S)


def _record_page_lock_backoff(
    state: DaemonState,
    *,
    key: str,
    mtime: float,
    message: str,
    prior: FileState | None,
) -> None:
    interval = _next_lock_backoff_seconds(prior)
    state.files[key] = FileState(
        mtime=mtime,
        processed_at=datetime.now(tz=UTC).isoformat(),
        status="lock_backoff",
        error=message,
        lock_backoff_until=time.time() + interval,
        lock_backoff_seconds=interval,
    )


def _merge_file_state(existing: FileState, incoming: FileState) -> FileState:
    """Merge duplicate ledger keys preferring higher-status, newer records."""
    existing_prio = _FILE_STATUS_PRIORITY.get(existing.status, 0)
    incoming_prio = _FILE_STATUS_PRIORITY.get(incoming.status, 0)
    if incoming_prio > existing_prio:
        return incoming
    if incoming_prio < existing_prio:
        return existing
    if incoming.mtime > existing.mtime:
        return incoming
    if incoming.mtime < existing.mtime:
        return existing
    return incoming if incoming.processed_at >= existing.processed_at else existing


def normalize_daemon_state_file_keys(graph_root: Path, state: DaemonState) -> bool:
    """Rewrite legacy absolute file keys to graph-relative POSIX paths."""
    if not state.files:
        return False
    migrated: dict[str, FileState] = {}
    changed = False
    for key, rec in state.files.items():
        new_key = normalize_daemon_file_key(graph_root, key)
        if not new_key:
            logger.warning(
                "Dropping unmapped or invalid ledger key during migration: {}",
                key,
            )
            changed = True
            continue
        if new_key != key:
            changed = True
        if new_key in migrated:
            migrated[new_key] = _merge_file_state(migrated[new_key], rec)
            changed = True
        else:
            migrated[new_key] = rec
    if changed:
        state.files = migrated
    return changed


def _daemon_file_key(graph_root: Path, path: Path) -> str:
    return graph_relative_path_key(path, graph_root)


def _lookup_file_state(
    graph_root: Path,
    state: DaemonState,
    path: Path,
) -> tuple[str, FileState | None]:
    """Resolve file ledger entry using graph-relative keys with legacy fallback."""
    key = _daemon_file_key(graph_root, path)
    rec = state.files.get(key)
    if rec is not None:
        return key, rec
    legacy = str(path.resolve())
    return key, state.files.get(legacy)


def heal_daemon_state_ledger(graph_root: Path, state: DaemonState) -> bool:
    """Clamp ledger counters when live graph totals fall below persisted AI impact."""
    snapshot = reconcile_telemetry_ledger(
        graph_root,
        ai_links_injected=state.ai_links_injected,
        ai_blocks_healed=state.ai_blocks_healed,
        ai_pages_created=state.ai_pages_created,
        page_summaries_created=state.page_summaries_created,
    )
    if not snapshot.healed:
        return False
    state.ai_links_injected = snapshot.ai_links_injected
    state.ai_blocks_healed = snapshot.ai_blocks_healed
    state.ai_pages_created = snapshot.ai_pages_created
    state.page_summaries_created = snapshot.page_summaries_created
    return True


def resolve_graph_root() -> Path:
    """Return validated ``LOGSEQ_GRAPH_PATH`` (must contain ``pages/``)."""
    from ..graph.graph_path_validate import validate_logseq_graph_path

    raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if not raw:
        msg = "LOGSEQ_GRAPH_PATH must be set for the Matryca Plumber daemon"
        raise ValueError(msg)
    return validate_logseq_graph_path(raw)


def state_path(graph_root: Path) -> Path:
    return graph_root / STATE_FILENAME


def state_bak_path(graph_root: Path) -> Path:
    return graph_root / STATE_BAK_FILENAME


def pid_path(graph_root: Path) -> Path:
    return graph_root / PID_FILENAME


def daemon_lock_path(graph_root: Path) -> Path:
    return graph_root / DAEMON_LOCK_FILENAME


def _try_acquire_daemon_process_lock_windows(graph_root: Path) -> int | None:
    """Acquire an exclusive daemon lock on platforms without ``fcntl`` (Windows)."""
    path = daemon_lock_path(graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            holder = _read_lock_holder_pid(path)
            if holder is not None and is_plumber_process(holder):
                return None
            if holder is None or not is_process_alive(holder):
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                continue
            return None
        except OSError:
            return None
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        if _msvcrt is not None:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return None
        return fd
    return None


def _try_acquire_daemon_process_lock(graph_root: Path) -> int | None:
    """Acquire an exclusive daemon lock; return ``None`` when another process holds it."""
    if _fcntl is None:
        return _try_acquire_daemon_process_lock_windows(graph_root)
    path = daemon_lock_path(graph_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_daemon_process_lock(graph_root: Path) -> None:
    """Drop the cross-process daemon lock held by this process."""
    global _daemon_process_lock_fd
    if _daemon_process_lock_fd is not None and _daemon_process_lock_fd >= 0:
        with contextlib.suppress(OSError):
            if _fcntl is not None:
                _fcntl.flock(_daemon_process_lock_fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                _msvcrt.locking(_daemon_process_lock_fd, _msvcrt.LK_UNLCK, 1)
            os.close(_daemon_process_lock_fd)
        _daemon_process_lock_fd = None
    lock_path = daemon_lock_path(graph_root)
    with contextlib.suppress(OSError):
        lock_path.unlink(missing_ok=True)


def sync_daemon_state_from_env(state: DaemonState) -> DaemonState:
    """Ensure persisted daemon state reflects the current ``LLM_MODEL_NAME`` env value."""
    state.model = resolve_llm_model_name()
    return state


def _read_daemon_state_payload(path: Path) -> dict[str, Any] | None:
    """Load JSON payload from disk, retrying once on transient empty or malformed reads."""
    for attempt in range(2):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if not raw.strip():
            if attempt == 0:
                continue
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            if attempt == 0:
                continue
            return None
        if isinstance(payload, dict):
            return payload
        return None
    return None


def load_daemon_state(graph_root: Path) -> DaemonState:
    from loguru import logger

    path = state_path(graph_root)
    bak_path = state_bak_path(graph_root)
    if not path.is_file() and not bak_path.is_file():
        return sync_daemon_state_from_env(DaemonState())

    payload = _read_daemon_state_payload(path) if path.is_file() else None
    if payload is None and bak_path.is_file():
        logger.warning(
            "[METADATA CORRUPTION DETECTED] Primary checkpoint unreadable; "
            "attempting recovery from .bak backup."
        )
        payload = _read_daemon_state_payload(bak_path)
        if payload is not None:
            with contextlib.suppress(OSError):
                shutil.copy2(bak_path, path)

    if payload is None:
        logger.warning(
            "[METADATA CORRUPTION DETECTED] Checkpoint and backup both unreadable; "
            "initializing a fresh instance."
        )
        return sync_daemon_state_from_env(DaemonState())
    state = sync_daemon_state_from_env(DaemonState.from_json(payload))
    if normalize_daemon_state_file_keys(graph_root, state):
        logger.info("Migrated daemon file ledger keys to graph-relative POSIX paths")
    return state


def save_daemon_state(graph_root: Path, state: DaemonState) -> None:
    """Persist daemon state via POSIX atomic write-and-replace under a cross-process flock."""
    path = state_path(graph_root)
    normalize_daemon_state_file_keys(graph_root, state)
    payload = json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_json_flock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(path))
            committed = True
            bak_path = state_bak_path(graph_root)
            with contextlib.suppress(OSError):
                shutil.copy2(path, bak_path)
        finally:
            if not committed:
                tmp_path.unlink(missing_ok=True)


def _read_lock_holder_pid(lock_path: Path) -> int | None:
    """Best-effort PID stored in a daemon lock sidecar."""
    if not lock_path.is_file():
        return None
    try:
        first_line = lock_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        return int(first_line)
    except (OSError, ValueError, IndexError):
        return None


def _process_command_line(pid: int) -> str:
    """Return a best-effort command line for ``pid`` (platform-specific)."""
    if sys.platform == "win32":
        result = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return result.stdout or ""
    if sys.platform == "darwin":
        result = subprocess.run(  # noqa: S603
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return (result.stdout or "").strip()
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.is_file():
        return proc_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    return ""


def is_plumber_process(pid: int) -> bool:
    """Return whether ``pid`` appears to be a Matryca Plumber daemon worker."""
    if pid <= 0 or not is_process_alive(pid):
        return False
    cmd = _process_command_line(pid).lower()
    if not cmd:
        return False
    if "maintenance_daemon" in cmd:
        return True
    return "src.cli" in cmd and "plumber" in cmd


def write_pid_file(graph_root: Path) -> None:
    path = pid_path(graph_root)
    payload = json.dumps({"pid": os.getpid(), "marker": PLUMBER_PID_MARKER}) + "\n"
    atomic_write_bytes(
        path,
        payload.encode("utf-8"),
        graph_root=graph_root,
        validate_block_refs=False,
    )


def read_pid_file(graph_root: Path) -> int | None:
    path = pid_path(graph_root)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(payload, dict):
            pid = payload.get("pid")
            if isinstance(pid, int):
                return pid
            if isinstance(pid, str) and pid.strip().isdigit():
                return int(pid.strip())
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def remove_pid_file(graph_root: Path) -> None:
    path = pid_path(graph_root)
    if path.is_file():
        path.unlink(missing_ok=True)


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_daemon(
    graph_root: Path,
    *,
    grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    sigkill_after: float = DEFAULT_STOP_SIGKILL_AFTER_SECONDS,
) -> dict[str, Any]:
    """Gracefully stop a running daemon via SIGTERM, escalating to SIGKILL when needed."""
    pid = read_pid_file(graph_root)
    if pid is None:
        return {"ok": True, "code": "not_running", "message": "No PID file found"}
    if not is_process_alive(pid):
        remove_pid_file(graph_root)
        state = load_daemon_state(graph_root)
        state.status = "stopped"
        save_daemon_state(graph_root, state)
        return {"ok": True, "code": "stale_pid_removed", "pid": pid}
    if not is_plumber_process(pid):
        remove_pid_file(graph_root)
        return {
            "ok": False,
            "code": "foreign_pid",
            "pid": pid,
            "message": f"PID {pid} is not a Matryca Plumber daemon process",
        }

    os.kill(pid, signal.SIGTERM)
    sigkill_sent = False
    deadline = time.monotonic() + max(1.0, grace_seconds)
    sigkill_at = time.monotonic() + max(1.0, sigkill_after)
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            break
        if not sigkill_sent and time.monotonic() >= sigkill_at:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            sigkill_sent = True
        time.sleep(0.1)

    if is_process_alive(pid):
        return {
            "ok": False,
            "code": "stop_failed",
            "pid": pid,
            "message": "Daemon process still alive after SIGTERM/SIGKILL",
        }

    remove_pid_file(graph_root)
    state = load_daemon_state(graph_root)
    state.status = "stopped"
    save_daemon_state(graph_root, state)
    code = "killed" if sigkill_sent else "signaled"
    signal_name = "SIGKILL" if sigkill_sent else "SIGTERM"
    return {"ok": True, "code": code, "pid": pid, "signal": signal_name}


def _page_title_from_path(graph_root: Path, path: Path) -> str:
    return page_title_from_path(graph_root, path)


def _normalize_block_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _bullet_inline_text(line: str) -> str:
    match = _BULLET.match(line.rstrip("\n"))
    if not match:
        return ""
    return match.group(2)


def _set_bullet_inline_text(line: str, new_text: str) -> str:
    match = _BULLET.match(line.rstrip("\n"))
    if not match:
        return line
    newline = "\n" if line.endswith("\n") else ""
    return f"{match.group(1)}- {new_text}{newline}"


def _stamp_matryca_plumber_property(
    lines: list[str],
    bullet_idx: int,
    id_idx: int,
    block_end: int,
) -> None:
    """Append ``matryca-plumber:: true`` under the modified block (Logseq audit trail)."""
    stripped = strip_lines_for_match(lines)
    insert_at = block_property_insert_index(stripped, bullet_idx, block_end)
    for i in range(bullet_idx + 1, insert_at):
        if _MATRYCA_PLUMBER_LINE.match(stripped[i]):
            return
    bullet_match = _BULLET.match(stripped[bullet_idx])
    base_ws = bullet_match.group(1) if bullet_match else ""
    indent = base_ws + bullet_indent_unit(stripped, bullet_idx)
    lines.insert(insert_at, f"{indent}matryca-plumber:: true\n")


def _enumerate_blocks_for_prompt(content: str) -> str:
    """Serialize blocks with UUIDs so the LLM can target surgical lint corrections."""
    lines = content.splitlines()
    blocks: list[str] = []
    for idx, line in enumerate(lines):
        id_match = _ID_LINE.match(line)
        if not id_match:
            continue
        block_uuid = id_match.group(1).strip()
        bullet_text = ""
        for j in range(idx - 1, -1, -1):
            bullet_match = _BULLET.match(lines[j])
            if bullet_match:
                bullet_text = bullet_match.group(2)
                break
        blocks.append(
            f"- block_uuid: {block_uuid}\n  original_text: {bullet_text}",
        )
    if not blocks:
        return "(no blocks with id:: found — omit semantic_corrections)"
    return "\n".join(blocks)


def _build_index_task_instruction(page_title: str, content: str) -> str:
    block_catalog = _enumerate_blocks_for_prompt(content)
    return "\n".join(
        [
            "Task: semantic indexing and safe per-block lint for one Logseq OG outliner page.",
            "Output: structured JSON only.",
            "Steps:",
            "1. Read the page title, block catalog, AliasIndex, and page content above.",
            "2. Extract summary, cross_references, suggested_tags, and moc_pointers.",
            "3. Propose semantic_corrections only when every system-prompt safety rule "
            "is satisfied.",
            "4. Prefer existing canonical titles and alias:: over inventing new page names.",
            "",
            f"Page title: {page_title}",
            "",
            f"Blocks available for semantic_corrections:\n{block_catalog}",
        ],
    )


def _build_index_prompt(
    page_title: str,
    content: str,
    *,
    llm_body: str | None = None,
    session: PagePromptSession | None = None,
) -> str:
    if session is not None:
        return session.build_task_prompt(_build_index_task_instruction(page_title, content))
    display_body = llm_body if llm_body is not None else content
    return build_cache_aligned_prompt(
        content=display_body[:8000],
        task_instruction=_build_index_task_instruction(page_title, content),
    )


def _strip_wikilink_brackets(text: str) -> str:
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)


def _rewrite_wikilinks_with_alias_index(text: str, alias_index: AliasIndex | None) -> str:
    if alias_index is None or not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        canonical = resolve_canonical_page_title(alias_index, target)
        if canonical != target:
            return f"[[{canonical}]]"
        return match.group(0)

    return _WIKILINK.sub(_replace, text)


def _normalize_index_result_aliases(
    result: SemanticIndexResult,
    alias_index: AliasIndex | None,
) -> SemanticIndexResult:
    if alias_index is None:
        return result

    cross_refs: list[SemanticCrossRef] = []
    for ref in result.cross_references:
        target = ref.target
        if target.startswith("[["):
            inner = target.strip("[]")
            canonical = resolve_canonical_page_title(alias_index, inner)
            target = f"[[{canonical}]]" if canonical != inner else target
        cross_refs.append(
            SemanticCrossRef(concept=ref.concept, relation=ref.relation, target=target),
        )

    moc_pointers = [
        (
            f"[[{resolve_canonical_page_title(alias_index, pointer.strip('[]'))}]]"
            if pointer.startswith("[[")
            else pointer
        )
        for pointer in result.moc_pointers
    ]

    corrections: list[SemanticLintCorrection] = []
    for correction in result.semantic_corrections:
        corrections.append(
            SemanticLintCorrection(
                block_uuid=correction.block_uuid,
                original_text=correction.original_text,
                corrected_text=_rewrite_wikilinks_with_alias_index(
                    correction.corrected_text,
                    alias_index,
                ),
                lint_type=correction.lint_type,
                reason=correction.reason,
            ),
        )

    return SemanticIndexResult(
        summary=result.summary,
        cross_references=cross_refs,
        suggested_tags=list(result.suggested_tags),
        moc_pointers=moc_pointers,
        semantic_corrections=corrections,
    )


def _is_safe_additive_correction(original: str, corrected: str, lint_type: LintType) -> bool:
    if lint_type == "anomaly_warning":
        return _normalize_block_text(original) == _normalize_block_text(corrected)
    orig_norm = _normalize_block_text(original)
    corr_norm = _normalize_block_text(corrected)
    if lint_type == "auto_wikilink":
        return orig_norm == _normalize_block_text(_strip_wikilink_brackets(corrected))
    if lint_type == "tag_hygiene":
        orig_plain = re.sub(r"#\S+", "", orig_norm).strip()
        corr_plain = re.sub(r"#\S+", "", _strip_wikilink_brackets(corrected)).strip()
        return orig_plain == corr_plain and len(corr_norm) >= len(orig_norm)
    return orig_norm in corr_norm and len(corr_norm) >= len(orig_norm)


@dataclass
class CorrectionOutcome:
    """Result of applying semantic lint corrections to one page."""

    applied: int = 0
    skipped: int = 0
    skip_reasons: list[str] = field(default_factory=list)
    applied_details: list[str] = field(default_factory=list)
    write_aborted: bool = False
    links_backpropagated: int = 0


def _direct_block_property_lines(
    lines: list[str],
    bullet_idx: int,
    block_end: int,
) -> list[str]:
    """Non-bullet property lines under a block that must survive bullet text edits."""
    stripped = [ln.rstrip("\n") for ln in lines]
    props: list[str] = []
    for i in range(bullet_idx + 1, min(block_end, len(lines))):
        line = stripped[i]
        if _BULLET.match(line):
            continue
        if parse_logseq_property_line(line) or _ID_LINE.match(line):
            props.append(line)
    return props


def apply_semantic_corrections_to_lines(
    lines: list[str],
    corrections: list[SemanticLintCorrection],
) -> CorrectionOutcome:
    """Apply micro-corrections in-place on ``lines`` (bullet text only, UUID-anchored)."""
    outcome = CorrectionOutcome()
    if not corrections:
        return outcome

    protected_lines = compute_page_protected_line_indices("".join(lines))
    stripped = [ln.rstrip("\n") for ln in lines]
    pending: list[tuple[int, int, int, SemanticLintCorrection]] = []
    for correction in corrections:
        located = locate_block_by_uuid(stripped, correction.block_uuid)
        if located is None:
            outcome.skipped += 1
            outcome.skip_reasons.append(f"uuid_not_found:{correction.block_uuid}")
            continue
        bullet_idx, id_idx, block_end = located
        if bullet_idx in protected_lines or any(
            idx in protected_lines for idx in range(bullet_idx, block_end)
        ):
            outcome.skipped += 1
            outcome.skip_reasons.append(f"protected_fence:{correction.block_uuid}")
            continue
        pending.append((bullet_idx, id_idx, block_end, correction))

    for bullet_idx, id_idx, block_end, correction in sorted(
        pending,
        key=lambda item: item[0],
        reverse=True,
    ):
        current_text = _bullet_inline_text(lines[bullet_idx])
        if _normalize_block_text(current_text) != _normalize_block_text(correction.original_text):
            outcome.skipped += 1
            outcome.skip_reasons.append(f"original_mismatch:{correction.block_uuid}")
            continue
        if correction.lint_type == "anomaly_warning":
            outcome.skipped += 1
            outcome.skip_reasons.append(f"anomaly_only:{correction.block_uuid}")
            continue
        if not _is_safe_additive_correction(
            correction.original_text,
            correction.corrected_text,
            correction.lint_type,
        ):
            outcome.skipped += 1
            outcome.skip_reasons.append(f"unsafe_correction:{correction.block_uuid}")
            continue
        if _normalize_block_text(current_text) == _normalize_block_text(correction.corrected_text):
            outcome.skipped += 1
            outcome.skip_reasons.append(f"no_change:{correction.block_uuid}")
            continue
        original_bullet = lines[bullet_idx]
        property_snapshot = _direct_block_property_lines(lines, bullet_idx, block_end)
        lines[bullet_idx] = _set_bullet_inline_text(lines[bullet_idx], correction.corrected_text)

        relocated = locate_block_by_uuid(
            [ln.rstrip("\n") for ln in lines],
            correction.block_uuid,
        )
        if relocated is None:
            lines[bullet_idx] = original_bullet
            outcome.skipped += 1
            outcome.skip_reasons.append(f"id_orphaned:{correction.block_uuid}")
            continue
        _, _, new_block_end = relocated
        preserved = _direct_block_property_lines(lines, bullet_idx, new_block_end)
        if property_snapshot and not all(item in preserved for item in property_snapshot):
            lines[bullet_idx] = original_bullet
            outcome.skipped += 1
            outcome.skip_reasons.append(f"property_lost:{correction.block_uuid}")
            continue

        _stamp_matryca_plumber_property(lines, bullet_idx, id_idx, block_end)
        outcome.applied += 1
        outcome.applied_details.append(
            f"{correction.lint_type}:{correction.block_uuid}:{correction.reason}",
        )
    return outcome


def _format_index_section(
    result: SemanticIndexResult,
    *,
    lint_outcome: CorrectionOutcome | None = None,
) -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "",
        SEMANTIC_INDEX_HEADER,
        f"- indexed-at:: {stamp}",
        f"- summary:: {result.summary.strip()}",
    ]
    if result.suggested_tags:
        tag_line = " ".join(
            t if t.startswith("#") else f"#{t.lstrip('#')}" for t in result.suggested_tags
        )
        lines.append(f"- suggested-tags:: {tag_line}")
    if result.moc_pointers:
        lines.append("- moc-pointers::")
        for pointer in result.moc_pointers:
            target = pointer if pointer.startswith("[[") else f"[[{pointer.strip('[]')}]]"
            lines.append(f"  - {target}")
    if result.cross_references:
        lines.append("- cross-references::")
        for ref in result.cross_references:
            target = ref.target
            if not (target.startswith("[[") or target.startswith("#")):
                target = f"[[{target.strip('[]')}]]"
            lines.append(f"  - {ref.concept} ({ref.relation}) → {target}")
    if lint_outcome is not None and lint_outcome.applied_details:
        lines.append("- semantic-lint-applied::")
        for detail in lint_outcome.applied_details:
            lines.append(f"  - {detail}")
    warnings = [c for c in result.semantic_corrections if c.lint_type == "anomaly_warning"]
    if warnings:
        lines.append("- semantic-lint-warnings::")
        for warning in warnings:
            lines.append(f"  - {warning.reason} (block {warning.block_uuid[:8]}…)")
    lines.append("")
    return "\n".join(lines)


def append_structural_lint_warning(
    graph_root: Path,
    page_path: Path,
    malformed_refs: list[str],
) -> bool:
    """Append a non-destructive structural lint section (preserves existing malformed refs)."""
    from loguru import logger

    if not page_path.is_file() or not malformed_refs:
        return False
    baseline_mtime = occ_snapshot(page_path)
    if baseline_mtime is None:
        return False
    if not occ_verify_before_write(page_path, baseline_mtime):
        logger.warning(
            "OCC Conflict: User modified {} during inference. Aborting write.",
            page_path,
        )
        return False

    with page_rmw_lock(page_path):
        if file_mtime_drifted(page_path, baseline_mtime):
            logger.warning(
                "OCC Conflict: User modified {} during inference. Aborting write.",
                page_path,
            )
            return False
        text = page_path.read_text(encoding="utf-8", errors="replace")
        if _structural_lint_section_present(text):
            return False

        sample = malformed_refs[:5]
        section_lines = [
            "",
            STRUCTURAL_LINT_HEADER,
            "- malformed-block-refs::",
        ]
        for ref in sample:
            section_lines.append(f"  - (({ref}))")
        if len(malformed_refs) > len(sample):
            section_lines.append(f"  - ... and {len(malformed_refs) - len(sample)} more")
        section_lines.extend(
            [
                "- todo:: #todo [[Matryca Broken Reference]] — fix ((uuid)) typos in Logseq",
                "",
            ],
        )
        new_text = text.rstrip("\n") + "\n" + "\n".join(section_lines)
        return atomic_write_bytes_if_unchanged(
            page_path,
            new_text.encode("utf-8"),
            graph_root=graph_root,
            baseline_mtime=baseline_mtime,
            validate_block_refs=False,
        )


def apply_semantic_page_result(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    result: SemanticIndexResult,
    *,
    backpropagate: bool = False,
    alias_index: AliasIndex | None = None,
    disable_semantic_corrections: bool = True,
    baseline_mtime: float | None = None,
) -> CorrectionOutcome:
    """Lint blocks surgically, then append the semantic index (one locked transaction)."""
    from loguru import logger

    lint_outcome = CorrectionOutcome()
    if baseline_mtime is None and page_path.is_file():
        baseline_mtime = occ_snapshot(page_path)
    if baseline_mtime is not None and not occ_verify_before_write(page_path, baseline_mtime):
        logger.warning(
            "OCC Conflict: User modified {} during inference. Aborting write.",
            page_path,
        )
        lint_outcome.write_aborted = True
        return lint_outcome

    with page_rmw_lock(page_path):
        if page_path.is_file():
            prev = page_path.read_text(encoding="utf-8", errors="replace")
            if baseline_mtime is None:
                baseline_mtime = read_file_mtime(page_path)
        else:
            prev = ""
            baseline_mtime = None
        if not prev.strip():
            return lint_outcome
        if baseline_mtime is not None and file_mtime_drifted(page_path, baseline_mtime):
            logger.warning(
                "OCC Conflict: User modified {} during inference. Aborting write.",
                page_path,
            )
            lint_outcome.write_aborted = True
            return lint_outcome
        lines = prev.splitlines(keepends=True)
        if disable_semantic_corrections:
            pass
        else:
            lint_outcome = apply_semantic_corrections_to_lines(lines, result.semantic_corrections)
        body = "".join(lines)
        if not _semantic_index_section_present(body):
            body = body.rstrip("\n") + _format_index_section(result, lint_outcome=lint_outcome)
        if baseline_mtime is not None and not atomic_write_bytes_if_unchanged(
            page_path,
            body.encode("utf-8"),
            graph_root=graph_root,
            baseline_mtime=baseline_mtime,
        ):
            logger.warning(
                "File modified by user during inference, aborting write to prevent data loss: {}",
                page_path,
            )
            lint_outcome.write_aborted = True
            return lint_outcome
        if baseline_mtime is None:
            atomic_write_bytes(page_path, body.encode("utf-8"), graph_root=graph_root)
    patch_generational_caches_for_paths(graph_root, [page_path])

    links_backpropagated = 0
    if backpropagate and lint_outcome.applied > 0:
        backlink_corrections = [
            BacklinkCorrection(
                block_uuid=item.block_uuid,
                original_text=item.original_text,
                corrected_text=item.corrected_text,
                lint_type=item.lint_type,
                reason=item.reason,
            )
            for item in result.semantic_corrections
        ]
        backprop_outcome = run_backlink_backpropagator(
            graph_root,
            page_path,
            page_title,
            backlink_corrections,
            lint_outcome.applied_details,
            alias_index=alias_index,
        )
        links_backpropagated = sum(
            1 for detail in backprop_outcome.details if detail.startswith("backprop:")
        )
    lint_outcome.links_backpropagated = links_backpropagated
    return lint_outcome


def append_semantic_index(
    graph_root: Path,
    page_path: Path,
    result: SemanticIndexResult,
) -> None:
    """Append a semantic index section without modifying existing user content."""
    if page_path.is_file():
        prev = page_path.read_text(encoding="utf-8", errors="replace")
        if _semantic_index_section_present(prev):
            return
    apply_semantic_page_result(
        graph_root,
        page_path,
        _page_title_from_path(graph_root, page_path),
        result,
    )


class InstructorLLMClient(_BaseInstructorLLMClient):
    """Daemon LLM client; adds semantic ``index_page`` with alias normalization."""

    def index_page(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        alias_index: AliasIndex | None = None,
        enable_semantic_routing: bool = False,
        llm_context: str | None = None,
        prompt_session: PagePromptSession | None = None,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        config = load_plumber_lint_config()
        session = prompt_session
        if session is None and graph_root is not None:
            session = build_page_prompt_session(
                graph_root,
                page_title,
                content,
                config=config,
                stable_system=build_semantic_lint_system_prompt(),
                page_path=page_path,
                alias_index=alias_index,
            )
        elif session is None and llm_context is not None:
            session = None
        if session is None and llm_context is None and graph_root is not None:
            llm_context, _ = prepare_llm_context_payload(
                graph_root,
                page_title,
                content,
                config=config,
            )
        prompt = _build_index_prompt(
            page_title,
            content,
            llm_body=llm_context,
            session=session,
        )
        kv_prefix_hash: str | None = None
        if session is not None:
            session.frozen.verify_unchanged()
            kv_prefix_hash = session.prefix_sha256
        started = time.perf_counter()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        routing_enabled = enable_semantic_routing and config.semantic_routing
        try:
            if routing_enabled and page_path is not None and graph_root is not None:
                key = semantic_cache_key(page_path, "semantic_index")
                cached = cache_get(graph_root, "index", key)
                if cached is not None:
                    result = SemanticIndexResult.model_validate(cached)
                    result = _normalize_index_result_aliases(result, alias_index)
                    return result, usage

            result, completion = self._completion_with_structured_output(
                prompt=prompt,
                response_model=SemanticIndexResult,
                system_prompt=(
                    session.stable_system
                    if session is not None
                    else build_semantic_lint_system_prompt()
                ),
                stateless=True,
                telemetry_target=page_title,
                telemetry_operation="Concept Indexing",
                log_tokens=False,
                kv_prefix_hash=kv_prefix_hash,
            )
            result = _normalize_index_result_aliases(result, alias_index)
            latency = time.perf_counter() - started
            usage_obj = getattr(completion, "usage", None)
            if usage_obj is not None:
                usage["prompt_tokens"] = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
                usage["completion_tokens"] = int(getattr(usage_obj, "completion_tokens", 0) or 0)
            operation: OperationType = (
                "Semantic Linting" if result.semantic_corrections else "Concept Indexing"
            )
            self.token_logger.log_turn(
                target_file=page_title,
                operation=operation,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                prompt=prompt,
                response=result.model_dump_json(),
                latency_seconds=latency,
                model=self.model,
                kv_prefix_hash=kv_prefix_hash,
            )
            if routing_enabled and page_path is not None and graph_root is not None:
                key = semantic_cache_key(page_path, "semantic_index")
                cache_put(graph_root, "index", key, result.model_dump())
            return result, usage
        except Exception as exc:  # noqa: BLE001 - log and re-raise for daemon loop
            latency = time.perf_counter() - started
            self.token_logger.log_turn(
                target_file=page_title,
                operation="Concept Indexing",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                prompt=prompt,
                response="",
                latency_seconds=latency,
                model=self.model,
                ok=False,
                error=str(exc),
            )
            raise


@dataclass
class ScanMetrics:
    """Graph scan counters for the TUI."""

    total: int
    processed: int
    pending: int

    @property
    def percent_complete(self) -> float:
        if self.total == 0:
            return 100.0
        return round(100.0 * self.processed / self.total, 1)


def _mtime_matches(stored: float, current: float) -> bool:
    """Return whether a checkpoint mtime still matches the on-disk value."""
    if stored == current:
        return True
    # JSON checkpoint round-trip can shave sub-microsecond float precision.
    return math.isclose(stored, current, rel_tol=0.0, abs_tol=1e-6)


def _read_page_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _page_is_terminal_skip(content: str) -> bool:
    """Pages that should never enter the LLM queue (empty or structurally unsafe)."""
    if not content.strip():
        return True
    protected_lines = compute_page_protected_line_indices(content)
    return bool(find_malformed_block_refs(content, protected_lines=protected_lines))


def page_needs_phase2_cognitive(
    graph_root: Path,
    path: Path,
    state: DaemonState,
    *,
    content: str | None = None,
) -> bool:
    """Return whether a page still needs the Phase 2 cognitive pass."""
    title = _page_title_from_path(graph_root, path)
    if title in MATRYCA_GENERATED_PAGE_TITLES:
        return False

    text = content if content is not None else _read_page_content(path)
    if _page_is_terminal_skip(text):
        return False

    _key, rec = _lookup_file_state(graph_root, state, path)
    mtime = path.stat().st_mtime
    if rec is None:
        return True
    if not _mtime_matches(rec.mtime, mtime):
        return True
    if rec.status == "processed":
        return False
    if rec.status == "error":
        return False
    if _lock_backoff_active(rec):
        return False
    if rec.status == "skipped":
        return _semantic_index_section_present(text)
    return True


def compute_phase2_progress_metrics(
    graph_root: Path,
    state: DaemonState,
) -> tuple[int, int, int, int]:
    """Return total, cognitive_done, cognitive_pending, terminal_skipped page counts."""
    total = 0
    cognitive_done = 0
    cognitive_pending = 0
    terminal_skipped = 0
    for path in iter_alias_source_paths(graph_root):
        title = _page_title_from_path(graph_root, path)
        if title in MATRYCA_GENERATED_PAGE_TITLES:
            continue
        total += 1
        _key, rec = _lookup_file_state(graph_root, state, path)
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        if rec is not None and _mtime_matches(rec.mtime, mtime) and rec.status == "processed":
            cognitive_done += 1
        elif page_needs_phase2_cognitive(graph_root, path, state):
            cognitive_pending += 1
        else:
            terminal_skipped += 1
    return total, cognitive_done, cognitive_pending, terminal_skipped


def compute_scan_metrics(graph_root: Path, state: DaemonState) -> ScanMetrics:
    files = iter_alias_source_paths(graph_root)
    total = len(files)
    processed = 0
    pending = 0
    for path in files:
        _key, rec = _lookup_file_state(graph_root, state, path)
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        if rec is not None and _mtime_matches(rec.mtime, mtime):
            if rec.status == "processed":
                processed += 1
            elif rec.status in {"skipped", "error"}:
                pass
            else:
                pending += 1
        else:
            pending += 1
    return ScanMetrics(total=total, processed=processed, pending=pending)


def clear_phase1_error_backoff(state: DaemonState) -> int:
    """Drop Phase 1 error records so unchanged files can retry after daemon restart."""
    cleared = 0
    for key, rec in list(state.files.items()):
        if rec.status == "error":
            del state.files[key]
            cleared += 1
    return cleared


def list_pending_files(
    graph_root: Path,
    state: DaemonState,
    *,
    bootstrap_complete: bool | None = None,
) -> list[Path]:
    pending: list[Path] = []
    phase2 = state.bootstrap_complete if bootstrap_complete is None else bootstrap_complete
    settled_statuses = frozenset({"processed", "skipped"})
    for path in iter_alias_source_paths(graph_root):
        title = _page_title_from_path(graph_root, path)
        if title in MATRYCA_GENERATED_PAGE_TITLES:
            continue
        if phase2:
            if page_needs_phase2_cognitive(graph_root, path, state):
                pending.append(path)
            continue
        _key, rec = _lookup_file_state(graph_root, state, path)
        mtime = path.stat().st_mtime
        if rec is not None and _mtime_matches(rec.mtime, mtime):
            if rec.status in settled_statuses:
                continue
            if rec.status == "error":
                continue
            if _lock_backoff_active(rec):
                continue
        pending.append(path)
    return pending


def prune_stale_daemon_file_entries(state: DaemonState, graph_root: Path) -> int:
    """Remove ghost paths from daemon state (deleted or renamed markdown files)."""
    live_keys = {_daemon_file_key(graph_root, path) for path in iter_alias_source_paths(graph_root)}
    ghosts = [key for key in state.files if key not in live_keys]
    for key in ghosts:
        del state.files[key]
    return len(ghosts)


class MaintenanceDaemon:
    """Long-polling daemon that indexes Logseq pages with a local LLM."""

    def __init__(
        self,
        graph_root: Path,
        *,
        llm_client: LLMClient | None = None,
        token_logger: TokenLogger | None = None,
        poll_seconds: float | None = None,
        max_files_per_cycle: int = 1,
    ) -> None:
        self.graph_root = graph_root
        self.token_logger = token_logger or TokenLogger()
        self.llm_client = llm_client or InstructorLLMClient(token_logger=self.token_logger)
        self.poll_seconds = poll_seconds or float(
            os.environ.get("MATRYCA_PLUMBER_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)),
        )
        self.max_files_per_cycle = max_files_per_cycle
        self._stop_requested = False
        self._shutdown_in_progress = False
        self._shutdown_event = threading.Event()
        self._inflight_writes = threading.Condition()
        self._active_write_count = 0
        self._state_persist_failed = False
        self.bootstrap_complete = False
        self._ensure_shared_token_logger()
        capability = probe_concurrency_capability()
        if capability.mode == "full":
            logger.debug("Concurrency: {}", capability.message)
        elif capability.degradation_allowed:
            logger.info("Concurrency: {}", capability.message)
        else:
            logger.warning("Concurrency: {}", capability.message)

    def _ensure_shared_token_logger(self) -> None:
        """Bind the LLM client to the daemon's central token logger."""
        if isinstance(self.llm_client, InstructorLLMClient):
            self.llm_client.token_logger = self.token_logger
            self.llm_client.thermal_stop_event = self._shutdown_event

    def _absorb_token_logger_delta(
        self,
        other: TokenLogger,
        *,
        baseline_prompt: int,
        baseline_completion: int,
    ) -> None:
        """Merge token deltas from a submodule logger into the central session counters."""
        if other is self.token_logger:
            return
        delta_prompt = other.session_prompt_tokens - baseline_prompt
        delta_completion = other.session_completion_tokens - baseline_completion
        if delta_prompt:
            self.token_logger.session_prompt_tokens += delta_prompt
        if delta_completion:
            self.token_logger.session_completion_tokens += delta_completion

    def _hydrate_token_logger_from_state(self, state: DaemonState) -> None:
        """Restore in-memory session counters from the last persisted checkpoint."""
        self.token_logger.session_prompt_tokens = max(
            self.token_logger.session_prompt_tokens,
            state.session_prompt_tokens,
        )
        self.token_logger.session_completion_tokens = max(
            self.token_logger.session_completion_tokens,
            state.session_completion_tokens,
        )
        log_prompt, log_completion = self.token_logger.session_token_totals_from_log()
        self.token_logger.session_prompt_tokens = max(
            self.token_logger.session_prompt_tokens,
            log_prompt,
        )
        self.token_logger.session_completion_tokens = max(
            self.token_logger.session_completion_tokens,
            log_completion,
        )

    def _hydrate_bootstrap_phase(self, state: DaemonState) -> None:
        """Restore Phase 2 readiness from persisted state or a complete on-disk catalog."""
        if state.bootstrap_complete or is_bootstrap_catalog_complete(self.graph_root):
            self.bootstrap_complete = True
            state.bootstrap_complete = True
        elif not self.bootstrap_complete:
            self.bootstrap_complete = False

    def _persist_bootstrap_complete(self, state: DaemonState) -> None:
        state.bootstrap_complete = True
        state.bootstrap_scanned = 0
        state.bootstrap_total = 0
        state.bootstrap_recent = {}
        try:
            refresh_phase2_cognitive_totals(self.graph_root, state)
        except OSError as exc:
            logger.warning("Phase 2 progress totals skipped after bootstrap: {}", exc)
        self._sync_live_telemetry(state)
        save_daemon_state(self.graph_root, state)

    def _effective_lint_config(self) -> PlumberLintConfig:
        """Return env lint config, or an all-disabled override during Phase 1."""
        if not self.bootstrap_complete:
            return bootstrap_phase_lint_config()
        return load_plumber_lint_config()

    def _compiled_alias_index(self) -> AliasIndex:
        return cached_build_alias_index(self.graph_root)

    def _register_daemon_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers before the polling loop starts."""
        signal.signal(signal.SIGTERM, self._handle_daemon_graceful_shutdown)
        signal.signal(signal.SIGINT, self._handle_daemon_graceful_shutdown)

    def _handle_daemon_graceful_shutdown(self, signum: int, _frame: object) -> None:
        """Request cooperative shutdown; the main loop performs flush and cleanup."""
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self._stop_requested = True
        self._shutdown_event.set()

        with contextlib.suppress(Exception):
            self.token_logger.log_daemon_shutdown(signum)

    def _begin_phase2_write(self) -> None:
        with self._inflight_writes:
            self._active_write_count += 1

    def _end_phase2_write(self) -> None:
        with self._inflight_writes:
            self._active_write_count -= 1
            self._inflight_writes.notify_all()

    def _wait_for_inflight_writes(
        self,
        *,
        timeout_s: float = SHUTDOWN_INFLIGHT_TIMEOUT_SECONDS,
    ) -> None:
        """Block until active Phase-2 writes finish or ``timeout_s`` elapses."""
        deadline = time.monotonic() + timeout_s
        with self._inflight_writes:
            while self._active_write_count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Shutdown timed out waiting for {} in-flight Phase-2 write(s)",
                        self._active_write_count,
                    )
                    return
                self._inflight_writes.wait(timeout=remaining)

    def _finalize_graceful_shutdown(self, state: DaemonState | None = None) -> None:
        """Flush ledger telemetry and release daemon resources after the loop exits."""
        self._wait_for_inflight_writes()

        with contextlib.suppress(Exception):
            load_master_catalog(self.graph_root).save()

        checkpoint = state or load_daemon_state(self.graph_root)
        checkpoint.status = "stopped"
        self._sync_live_telemetry(checkpoint)
        with contextlib.suppress(Exception):
            save_daemon_state(self.graph_root, checkpoint)

        remove_pid_file(self.graph_root)
        _release_daemon_process_lock(self.graph_root)
        clear_page_write_locks()
        sweep_matryca_lock_sidecars(self.graph_root)

    def _sync_runtime_config(self, state: DaemonState) -> DaemonState:
        """Align LLM client and persisted state with the active environment block."""
        reload_plumber_dotenv(override=True)
        self._ensure_shared_token_logger()
        if isinstance(self.llm_client, InstructorLLMClient):
            self.llm_client.refresh_config()
            self.llm_client.bind_lint_config(self._effective_lint_config())
        return sync_daemon_state_from_env(state)

    def request_stop(self) -> None:
        self._stop_requested = True
        self._shutdown_event.set()

    def run_bootstrap_pipeline(self, state: DaemonState | None = None) -> None:
        """Phase 1 bootstrap harvest (read/append only) before Phase 2 polling."""
        if self.bootstrap_complete:
            return

        checkpoint = state or load_daemon_state(self.graph_root)
        self._hydrate_bootstrap_phase(checkpoint)
        if self.bootstrap_complete:
            return

        checkpoint.status = "running"
        self._sync_live_telemetry(checkpoint, mark_running=True)
        save_daemon_state(self.graph_root, checkpoint)

        def _on_page_cataloged(
            scanned: int,
            total: int,
            last_path: Path | None,
            harvest_status: BootstrapHarvestStatus | None = None,
        ) -> None:
            checkpoint.bootstrap_scanned = scanned
            checkpoint.bootstrap_total = total
            if last_path is None:
                return
            key = graph_relative_path_key(last_path, self.graph_root)
            checkpoint.last_file = key
            if harvest_status is not None:
                upsert_bootstrap_recent(checkpoint, key, harvest_status)
            record_bootstrap_harvest_impact(checkpoint, harvest_status)
            if scanned % bootstrap_checkpoint_every() == 0:
                checkpoint.status = "running"
                save_daemon_state(self.graph_root, checkpoint)

        def _persist_bootstrap_progress(
            scanned: int,
            total: int,
            last_path: Path | None,
            harvest_status: BootstrapHarvestStatus | None = None,
        ) -> None:
            del last_path, harvest_status
            checkpoint.bootstrap_scanned = scanned
            checkpoint.bootstrap_total = total
            checkpoint.status = "running"
            self._sync_live_telemetry(checkpoint, mark_running=True)
            save_daemon_state(self.graph_root, checkpoint)

        max_attempts = 3
        backoff_s = 1.0
        last_exc: Exception | None = None
        metrics = None
        for attempt in range(1, max_attempts + 1):
            try:
                metrics = run_bootstrap_harvest(
                    self.graph_root,
                    llm=self.llm_client,
                    incremental=False,
                    rebuild_index=True,
                    phase1_strict=True,
                    on_progress=_persist_bootstrap_progress,
                    on_page_cataloged=_on_page_cataloged,
                    should_stop=lambda: self._stop_requested,
                    stop_event=self._shutdown_event,
                )
                break
            except Exception as exc:  # noqa: BLE001 - bootstrap must not block daemon start
                last_exc = exc
                if attempt >= max_attempts:
                    self.token_logger.log_structural_lint_warning(
                        target_file=self.graph_root,
                        message=(f"Bootstrap harvest failed after {max_attempts} attempts: {exc}"),
                        malformed_refs=[],
                    )
                    return
                time.sleep(backoff_s)
                backoff_s *= 2.0

        if metrics is None:
            if last_exc is not None:
                self.token_logger.log_structural_lint_warning(
                    target_file=self.graph_root,
                    message=f"Bootstrap harvest failed: {last_exc}",
                    malformed_refs=[],
                )
            return

        if self._stop_requested:
            checkpoint.status = "stopped"
            self._sync_live_telemetry(checkpoint)
            save_daemon_state(self.graph_root, checkpoint)
            return

        master_index = master_index_page_path(self.graph_root)
        if not master_index.is_file() or metrics.files_created != 0:
            self.token_logger.log_structural_lint_warning(
                target_file=self.graph_root,
                message=(
                    "Bootstrap Phase 1 incomplete: master index missing or unexpected "
                    f"concept pages created (files_created={metrics.files_created})."
                ),
                malformed_refs=[],
            )
            return

        self.bootstrap_complete = True
        self._persist_bootstrap_complete(checkpoint)
        try:
            load_or_compute_semantic_clusters(self.graph_root)
        except Exception as exc:  # noqa: BLE001 - deferral must not block daemon
            logger.warning("Semantic cluster precompute after bootstrap skipped: {}", exc)
        try:
            release_phase1_memory(self.graph_root)
            log_snapshot(label="post_bootstrap_teardown")
        except Exception as exc:  # noqa: BLE001 - teardown must not block Phase 2
            logger.warning("Post-bootstrap memory release skipped: {}", exc)
        self._run_phase2_graph_insights()

    def _run_phase2_graph_insights(self) -> None:
        """Run graph insights after Phase 1 completes (Phase 2 entry)."""
        try:
            run_graph_insights_engine(self.graph_root, llm=self.llm_client)
        except Exception as exc:  # noqa: BLE001 - insights must not block daemon start
            self.token_logger.log_structural_lint_warning(
                target_file=self.graph_root,
                message=f"Graph insights engine failed: {exc}",
                malformed_refs=[],
            )

    def refresh_catalog_if_stale(self) -> None:
        """Incrementally sync catalog rows when page mtimes change."""
        try:
            run_incremental_catalog_refresh(self.graph_root, llm=self.llm_client)
        except Exception as exc:  # noqa: BLE001 - never abort daemon cycle
            self.token_logger.log_structural_lint_warning(
                target_file=self.graph_root,
                message=f"Incremental catalog refresh failed: {exc}",
                malformed_refs=[],
            )

    def _prune_stale_catalog_entries(self) -> None:
        """Drop ghost catalog rows and purge warm alias cache entries after each scan."""
        try:
            catalog = load_master_catalog(self.graph_root)
            pruned = catalog.prune_missing_pages()
            alias_purged = gc_generational_alias_cache(self.graph_root)
            if pruned > 0:
                catalog.save()
            if pruned > 0 or alias_purged > 0:
                logger.debug(
                    "Catalog GC pruned {} catalog row(s) and {} alias cache row(s)",
                    pruned,
                    alias_purged,
                )
        except Exception as exc:  # noqa: BLE001 - never abort daemon cycle
            self.token_logger.log_structural_lint_warning(
                target_file=self.graph_root,
                message=f"Catalog prune failed: {exc}",
                malformed_refs=[],
            )

    def _sync_catalog_after_page_write(self, path: Path, title: str) -> None:
        """Upsert catalog row from on-disk semantic index immediately after a page write."""
        try:
            new_text = path.read_text(encoding="utf-8", errors="replace")
            mtime = int(path.stat().st_mtime)
        except OSError as exc:
            self.token_logger.log_structural_lint_warning(
                target_file=path,
                message=f"Catalog sync read failed for {title}: {exc}",
                malformed_refs=[],
            )
            return

        extracted = extract_catalog_fields_from_content(new_text)
        if extracted is None:
            return

        catalog = load_master_catalog(self.graph_root)
        extracted.last_mtime = mtime
        existing = catalog.get(title)
        if existing is not None:
            extracted.orphan = existing.orphan
        catalog.upsert(title, extracted)
        catalog.save()

    def _use_semantic_cluster_cycle(self, lint_config: PlumberLintConfig) -> bool:
        """Phase 2 cluster isolation when cognitive routing modules are active."""
        return self.bootstrap_complete and (
            lint_config.semantic_routing or lint_config.backpropagate_links
        )

    def _group_pending_by_cluster(self, pending: list[Path]) -> list[tuple[str, list[Path]]]:
        """Order pending files into semantic cluster execution blocks."""
        clusters = load_or_compute_semantic_clusters(self.graph_root)
        title_to_cluster: dict[str, str] = {}
        for cluster_id, titles in clusters.items():
            for title in titles:
                title_to_cluster[title] = cluster_id

        by_cluster: dict[str, list[Path]] = {}
        unclustered: list[Path] = []
        for path in pending:
            title = _page_title_from_path(self.graph_root, path)
            mapped_cluster = title_to_cluster.get(title)
            if mapped_cluster is None:
                unclustered.append(path)
                continue
            by_cluster.setdefault(mapped_cluster, []).append(path)

        groups: list[tuple[str, list[Path]]] = [
            (cluster_id, by_cluster[cluster_id]) for cluster_id in sorted(by_cluster)
        ]
        if unclustered:
            groups.append(("unclustered", unclustered))
        return groups

    def _begin_cluster_context(self, cluster_id: str, cluster_paths: list[Path]) -> None:
        """Hard-reset LLM history and inject neighborhood map for one cluster."""
        if not isinstance(self.llm_client, InstructorLLMClient):
            return
        catalog = load_master_catalog(self.graph_root)
        clusters = load_or_compute_semantic_clusters(self.graph_root)
        cluster_titles = clusters.get(cluster_id)
        if cluster_titles is None:
            cluster_titles = [
                _page_title_from_path(self.graph_root, path) for path in cluster_paths
            ]
        neighborhood = format_cluster_neighborhood(catalog.to_json(), cluster_titles)
        self.llm_client.inject_cluster_focus_context(neighborhood)

    def _sync_live_telemetry(
        self,
        state: DaemonState,
        *,
        cluster_id: str | None = None,
        mark_running: bool = False,
    ) -> None:
        """Mirror in-memory token totals and cluster focus onto the checkpoint plane."""
        state.session_prompt_tokens = self.token_logger.session_prompt_tokens
        state.session_completion_tokens = self.token_logger.session_completion_tokens
        if cluster_id is not None:
            state.current_cluster = cluster_id
        if mark_running and state.status != "stopped":
            state.status = "running"

    def _save_cycle_checkpoint(self, state: DaemonState, *, path: Path | None = None) -> None:
        """Persist daemon state after one file settles in the cycle flywheel.

        Uses :func:`save_daemon_state` (POSIX ``os.replace``) so concurrent FastAPI
        readers never observe a truncated checkpoint mid-write.
        """
        self._sync_live_telemetry(state)
        try:
            save_daemon_state(self.graph_root, state)
            self._state_persist_failed = False
        except Exception as save_exc:  # noqa: BLE001 - log and continue cycle
            self._state_persist_failed = True
            target = path if path is not None else self.graph_root
            self.token_logger.log_structural_lint_warning(
                target_file=target,
                message=f"Checkpoint save failed: {save_exc}",
                malformed_refs=[],
            )

    def _try_fast_track_cycle_file(self, path: Path, state: DaemonState) -> bool:
        """Settle a pending page without LLM tokens (quarantine, cache, empty)."""
        key = _daemon_file_key(self.graph_root, path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False

        state.last_file = sanitize_for_console(key)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

        protected_lines = compute_page_protected_line_indices(content)
        malformed_refs = find_malformed_block_refs(
            content,
            protected_lines=protected_lines,
        )
        if malformed_refs:
            self._quarantine_structural_lint(
                path=path,
                key=key,
                mtime=mtime,
                message="Malformed UUID detected in block ref. Must be standard 36-char format.",
                malformed_refs=malformed_refs,
                state=state,
            )
            return True
        if _semantic_index_section_present(content) and not self.bootstrap_complete:
            state.files[key] = FileState(
                mtime=mtime,
                processed_at=datetime.now(tz=UTC).isoformat(),
                status="skipped",
            )
            return True
        if not content.strip():
            state.files[key] = FileState(
                mtime=mtime,
                processed_at=datetime.now(tz=UTC).isoformat(),
                status="skipped",
            )
            return True
        return False

    def _process_llm_cycle_file(
        self,
        path: Path,
        state: DaemonState,
        lint_config: PlumberLintConfig,
        *,
        reset_history_after: bool,
        cluster_id: str | None = None,
    ) -> bool:
        """Index one pending page via LLM path. Returns whether inference ran this turn."""
        key = _daemon_file_key(self.graph_root, path)
        mtime = path.stat().st_mtime
        title = _page_title_from_path(self.graph_root, path)
        state.last_file = sanitize_for_console(key)
        if cluster_id is not None:
            state.phase2_cluster_file_in_flight = True
        self._sync_live_telemetry(
            state,
            cluster_id=cluster_id,
            mark_running=True,
        )
        self._save_cycle_checkpoint(state, path=path)
        content = ""
        prompt_before = self.token_logger.session_prompt_tokens
        completion_before = self.token_logger.session_completion_tokens
        llm_called_from_usage = False
        write_aborted = False
        self._begin_phase2_write()
        try:
            baseline_mtime = occ_snapshot(path)
            content = path.read_text(encoding="utf-8", errors="replace")
            cognitive_outcome: CognitiveLintOutcome | None = None
            prompt_session: PagePromptSession | None = None
            if self.bootstrap_complete and lint_config.any_enabled:
                cognitive_outcome, prompt_session = run_cognitive_lint_pipeline(
                    self.graph_root,
                    path,
                    title,
                    content,
                    llm=self.llm_client,
                    config=lint_config,
                )
                record_daemon_impact(state, cognitive=cognitive_outcome)
                if isinstance(self.llm_client, InstructorLLMClient):
                    self._absorb_token_logger_delta(
                        self.llm_client.token_logger,
                        baseline_prompt=prompt_before,
                        baseline_completion=completion_before,
                    )
                self._sync_live_telemetry(
                    state,
                    cluster_id=cluster_id,
                    mark_running=True,
                )
                self._save_cycle_checkpoint(state, path=path)
                content = path.read_text(encoding="utf-8", errors="replace")
                refreshed_mtime = occ_snapshot(path)
                if refreshed_mtime is not None:
                    baseline_mtime = refreshed_mtime
            alias_index = self._compiled_alias_index() if self.bootstrap_complete else None
            enable_semantic_routing = self.bootstrap_complete and lint_config.semantic_routing
            enable_backprop = self.bootstrap_complete and lint_config.backpropagate_links
            if baseline_mtime is not None and file_mtime_drifted(path, baseline_mtime):
                logger.warning(
                    "OCC Conflict: User modified {} during inference. Aborting write.",
                    path,
                )
                write_aborted = True
                return False
            _key, prior_rec = _lookup_file_state(self.graph_root, state, path)
            try:
                probe_page_rmw_lock(path)
            except PageLockUnavailableError as exc:
                self.token_logger.log_structural_lint_warning(
                    target_file=path,
                    message=f"Page lock unavailable before inference: {exc}",
                    malformed_refs=[],
                )
                _record_page_lock_backoff(
                    state,
                    key=key,
                    mtime=mtime,
                    message=str(exc),
                    prior=prior_rec,
                )
                return False
            if prompt_session is None and self.graph_root is not None:
                prompt_session = build_page_prompt_session(
                    self.graph_root,
                    title,
                    content,
                    config=lint_config,
                    stable_system=build_semantic_lint_system_prompt(),
                    page_path=path,
                    alias_index=alias_index,
                )
            result, usage = self.llm_client.index_page(
                title,
                content,
                page_path=path,
                graph_root=self.graph_root,
                alias_index=alias_index,
                enable_semantic_routing=enable_semantic_routing,
                prompt_session=prompt_session,
            )
            llm_called_from_usage = (
                int(usage.get("prompt_tokens", 0) or 0)
                + int(usage.get("completion_tokens", 0) or 0)
            ) > 0
            lint_outcome = apply_semantic_page_result(
                self.graph_root,
                path,
                title,
                result,
                backpropagate=enable_backprop,
                alias_index=alias_index,
                disable_semantic_corrections=lint_config.disable_semantic_corrections,
                baseline_mtime=baseline_mtime,
            )
            record_daemon_impact(
                state,
                lint=lint_outcome,
                links_backpropagated=lint_outcome.links_backpropagated,
            )
            if lint_outcome.write_aborted:
                write_aborted = True
                llm_called_from_logger = (
                    self.token_logger.session_prompt_tokens > prompt_before
                    or self.token_logger.session_completion_tokens > completion_before
                )
                return llm_called_from_usage or llm_called_from_logger
            state.files[key] = FileState(
                mtime=path.stat().st_mtime,
                processed_at=datetime.now(tz=UTC).isoformat(),
                status="processed",
            )
            self._sync_catalog_after_page_write(path, title)
        except PageLockUnavailableError as exc:
            self.token_logger.log_structural_lint_warning(
                target_file=path,
                message=f"Page lock unavailable after retries: {exc}",
                malformed_refs=[],
            )
            _key, prior_rec = _lookup_file_state(self.graph_root, state, path)
            _record_page_lock_backoff(
                state,
                key=_key,
                mtime=mtime,
                message=str(exc),
                prior=prior_rec,
            )
        except Exception as exc:  # noqa: BLE001 - quarantine per-file; never abort cycle
            if is_malformed_block_ref_error(exc):
                protected_lines = compute_page_protected_line_indices(content)
                malformed_refs = find_malformed_block_refs(
                    content,
                    protected_lines=protected_lines,
                )
                self._quarantine_structural_lint(
                    path=path,
                    key=key,
                    mtime=mtime,
                    message=str(exc),
                    malformed_refs=malformed_refs or [],
                    state=state,
                )
            else:
                state.files[key] = FileState(
                    mtime=mtime,
                    processed_at=datetime.now(tz=UTC).isoformat(),
                    status="error",
                    error=str(exc),
                )
        finally:
            self._end_phase2_write()
            if cluster_id is not None:
                state.phase2_cluster_file_in_flight = False
            if reset_history_after and isinstance(self.llm_client, InstructorLLMClient):
                self.llm_client.reset_execution_history()
        llm_called_from_logger = (
            self.token_logger.session_prompt_tokens > prompt_before
            or self.token_logger.session_completion_tokens > completion_before
        )
        llm_called = llm_called_from_usage or llm_called_from_logger
        if llm_called and self.bootstrap_complete and not write_aborted:
            state.phase2_llm_turns += 1
        self._sync_live_telemetry(state, cluster_id=cluster_id, mark_running=True)
        return llm_called

    def _quarantine_structural_lint(
        self,
        *,
        path: Path,
        key: str,
        mtime: float,
        message: str,
        malformed_refs: list[str],
        state: DaemonState,
    ) -> None:
        """Log, optionally annotate, and skip a page with malformed ``((uuid))`` refs."""
        rel_path = path.relative_to(self.graph_root).as_posix()
        self.token_logger.log_structural_lint_warning(
            target_file=path,
            message=f"{message} file={rel_path}",
            malformed_refs=malformed_refs,
        )
        try:
            append_structural_lint_warning(self.graph_root, path, malformed_refs)
        except Exception as inj_exc:  # noqa: BLE001 - never crash daemon on annotation
            self.token_logger.log_structural_lint_warning(
                target_file=path,
                message=f"Warning injection failed for {rel_path}: {inj_exc}",
                malformed_refs=malformed_refs,
            )
        recorded_mtime = path.stat().st_mtime if path.is_file() else mtime
        state.files[key] = FileState(
            mtime=recorded_mtime,
            processed_at=datetime.now(tz=UTC).isoformat(),
            status="skipped",
            error=message,
        )

    def run_cycle(self, state: DaemonState | None = None) -> DaemonState:
        """Drain fast-skippable pending files, then up to ``max_files_per_cycle`` LLM turns."""
        state = self._sync_runtime_config(state or load_daemon_state(self.graph_root))
        self._hydrate_bootstrap_phase(state)
        self._hydrate_token_logger_from_state(state)
        normalize_daemon_state_file_keys(self.graph_root, state)
        try:
            purge_expired_semantic_cache(self.graph_root)
        except OSError as exc:
            logger.warning("Semantic cache purge skipped: {}", exc)
        try:
            prune_stale_daemon_file_entries(state, self.graph_root)
        except (OSError, FileNotFoundError) as exc:
            logger.error("Graph root inaccessible during prune: {}", exc)
            state.status = "error"
            self._sync_live_telemetry(state)
            save_daemon_state(self.graph_root, state)
            return state

        state.status = "running"
        state.last_scan_at = datetime.now(tz=UTC).isoformat()
        if self.bootstrap_complete:
            try:
                refresh_phase2_cognitive_totals(self.graph_root, state)
            except OSError as exc:
                logger.warning("Phase 2 progress totals skipped at cycle start: {}", exc)
        self._sync_live_telemetry(state, mark_running=True)
        save_daemon_state(self.graph_root, state)
        try:
            pending = list_pending_files(
                self.graph_root,
                state,
                bootstrap_complete=self.bootstrap_complete,
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error("Graph root inaccessible during pending scan: {}", exc)
            state.status = "error"
            self._sync_live_telemetry(state)
            save_daemon_state(self.graph_root, state)
            return state

        if not pending:
            self.refresh_catalog_if_stale()
            self._prune_stale_catalog_entries()
            heal_daemon_state_ledger(self.graph_root, state)
            state.status = "idle"
            save_daemon_state(self.graph_root, state)
            return state

        cycle_budget = max(1, self.max_files_per_cycle)
        for path in pending:
            if self._stop_requested:
                state.status = "stopped"
                break
            if cycle_budget <= 0:
                break
            if self._try_fast_track_cycle_file(path, state):
                self._save_cycle_checkpoint(state, path=path)
                cycle_budget -= 1

        if self._stop_requested:
            self._prune_stale_catalog_entries()
            self._sync_live_telemetry(state)
            save_daemon_state(self.graph_root, state)
            return state

        pending_llm = list_pending_files(
            self.graph_root,
            state,
            bootstrap_complete=self.bootstrap_complete,
        )
        lint_config = self._effective_lint_config()
        cluster_cycle = self._use_semantic_cluster_cycle(lint_config)
        llm_turns_this_cycle = 0
        pending_groups: list[tuple[str, list[Path]]]
        if cluster_cycle:
            pending_groups = self._group_pending_by_cluster(pending_llm)
        else:
            pending_groups = [("flat", pending_llm)]

        for cluster_id, cluster_paths in pending_groups:
            if self._stop_requested:
                state.status = "stopped"
                break
            if llm_turns_this_cycle >= cycle_budget:
                break
            if cluster_cycle:
                self._begin_cluster_context(cluster_id, cluster_paths)
                state.current_cluster_files_total = len(cluster_paths)
                state.current_cluster_files_done = 0
                state.phase2_cluster_file_in_flight = False
                self._sync_live_telemetry(state, cluster_id=cluster_id, mark_running=True)
                self._save_cycle_checkpoint(state)
            else:
                state.current_cluster = None
                state.current_cluster_files_total = 0
                state.current_cluster_files_done = 0
                state.phase2_cluster_file_in_flight = False
            for path in cluster_paths:
                if self._stop_requested:
                    state.status = "stopped"
                    break
                if llm_turns_this_cycle >= cycle_budget:
                    break
                llm_called_this_turn = self._process_llm_cycle_file(
                    path,
                    state,
                    lint_config,
                    reset_history_after=not cluster_cycle,
                    cluster_id=cluster_id if cluster_cycle else None,
                )
                if cluster_cycle:
                    state.current_cluster_files_done += 1
                self._sync_live_telemetry(state, mark_running=True)
                self._save_cycle_checkpoint(state, path=path)
                if llm_called_this_turn:
                    llm_turns_this_cycle += 1

        self._prune_stale_catalog_entries()
        heal_daemon_state_ledger(self.graph_root, state)
        state.phase2_cluster_file_in_flight = False
        if self.bootstrap_complete:
            with contextlib.suppress(OSError):
                refresh_phase2_cognitive_totals(self.graph_root, state)
        self._sync_live_telemetry(state)
        if not self._stop_requested:
            state.status = "idle"
        save_daemon_state(self.graph_root, state)
        maybe_release_after_cycle(
            llm_turns=llm_turns_this_cycle,
            graph_root=self.graph_root,
        )
        return state

    def run_forever(self) -> None:
        """Infinite polling loop until stop is requested."""
        self._register_daemon_signal_handlers()
        prepare_matryca_runtime(
            graph_root=self.graph_root,
            wiki_config=load_matryca_wiki_config(),
        )
        write_pid_file(self.graph_root)
        state = self._sync_runtime_config(load_daemon_state(self.graph_root))
        llm_config = load_plumber_lint_config()
        logger.info(
            "LLM Engine starting... Provider URL: {} | Target Model: {}",
            llm_config.lm_base_url,
            llm_config.lm_model,
        )
        cleared_errors = clear_phase1_error_backoff(state)
        if cleared_errors:
            logger.info(
                "Cleared {} Phase 1 error record(s) for retry on daemon restart",
                cleared_errors,
            )
            save_daemon_state(self.graph_root, state)
        self._hydrate_bootstrap_phase(state)
        self._hydrate_token_logger_from_state(state)
        state.status = "running"
        save_daemon_state(self.graph_root, state)
        log_snapshot(label="daemon_start")

        try:
            self.run_bootstrap_pipeline(state)
            while not self._stop_requested:
                self.run_cycle(state)
                if not self._state_persist_failed:
                    state = load_daemon_state(self.graph_root)
                else:
                    logger.warning(
                        "Skipping checkpoint reload after persist failure; "
                        "retaining in-memory daemon state",
                    )
                if self._stop_requested:
                    break
                if self._shutdown_event.wait(timeout=self.poll_seconds):
                    break
        finally:
            self._finalize_graceful_shutdown(state)


def run_plumber_cluster(
    graph_root: Path | None = None,
    *,
    max_cluster_size: int = 35,
    force_recompute: bool = False,
) -> dict[str, Any]:
    """Manual entrypoint: compute or audit semantic cluster neighborhoods."""
    root = graph_root or resolve_graph_root()
    prepare_matryca_runtime(graph_root=root, wiki_config=load_matryca_wiki_config())
    catalog = load_master_catalog(root, force_reload=True)
    clusters = load_or_compute_semantic_clusters(
        root,
        catalog_data=catalog.to_json(),
        max_cluster_size=max_cluster_size,
        force_recompute=force_recompute,
    )
    sizes = [len(titles) for titles in clusters.values()]
    return {
        "ok": True,
        "graph_root": str(root),
        "cluster_count": len(clusters),
        "page_count": sum(sizes),
        "min_cluster_size": min(sizes) if sizes else 0,
        "max_cluster_size": max(sizes) if sizes else 0,
        "avg_cluster_size": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
        "clusters_path": str(
            (root / ".matryca_semantic_cache" / "semantic_clusters.json").relative_to(root),
        ),
        "catalog_updated_at": catalog.updated_at,
    }


def run_plumber_audit(graph_root: Path | None = None) -> dict[str, Any]:
    """Manual entrypoint: refresh catalog and compile graph insights dashboard."""
    root = graph_root or resolve_graph_root()
    prepare_matryca_runtime(graph_root=root, wiki_config=load_matryca_wiki_config())
    llm = InstructorLLMClient()
    run_bootstrap_harvest(root, llm=llm, incremental=True, rebuild_index=True)
    result = run_graph_insights_engine(root, llm=llm)
    return {
        "ok": True,
        "graph_root": str(root),
        "insights_path": str(result.output_path.relative_to(root)),
        "page_count": result.metrics.page_count,
        "orphan_pages": len(result.metrics.orphan_pages),
        "catalog_coverage": result.metrics.catalog_coverage,
        "llm_used": result.llm_used,
        "latency_seconds": round(result.latency_seconds, 3),
    }


def start_daemon_foreground(graph_root: Path | None = None) -> None:
    """Run the daemon in the current process (foreground)."""
    reload_plumber_dotenv()
    configure_loguru()
    root = graph_root or resolve_graph_root()
    prepare_matryca_runtime(graph_root=root, wiki_config=load_matryca_wiki_config())
    config = load_plumber_lint_config()
    sandbox = resolve_cpu_sandbox_config(config)
    if sandbox.enabled:
        apply_cpu_sandbox(sandbox)
    else:
        apply_plumber_priority(config)
    lock_fd = _try_acquire_daemon_process_lock(root)
    if lock_fd is None:
        logger.error(
            "Matryca Plumber daemon already running (lock held) for graph_root={!r}",
            root,
        )
        sys.exit(1)
    global _daemon_process_lock_fd
    _daemon_process_lock_fd = lock_fd
    daemon = MaintenanceDaemon(root)
    if isinstance(daemon.llm_client, InstructorLLMClient):
        daemon.llm_client.probe_backend()
    daemon.run_forever()


def start_daemon_detached(graph_root: Path | None = None) -> dict[str, Any]:
    """Launch a background daemon worker via subprocess (cross-platform)."""
    root = graph_root or resolve_graph_root()
    existing = read_pid_file(root)
    if existing is not None and is_plumber_process(existing):
        return {
            "ok": False,
            "code": "already_running",
            "pid": existing,
            "message": f"Matryca Plumber daemon already running (pid {existing})",
        }
    if existing is not None and is_process_alive(existing):
        return {
            "ok": False,
            "code": "foreign_pid",
            "pid": existing,
            "message": f"PID file references a live non-plumber process (pid {existing})",
        }
    if existing is not None:
        remove_pid_file(root)

    env = os.environ.copy()
    env["LOGSEQ_GRAPH_PATH"] = str(root)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "src.cli", "plumber", "start", "--foreground"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            return {
                "ok": False,
                "code": "startup_failed",
                "message": f"Daemon worker exited immediately with code {exit_code}",
            }
        pid = read_pid_file(root)
        if pid is not None and is_plumber_process(pid):
            return {"ok": True, "code": "started", "pid": pid, "graph_root": str(root)}
        time.sleep(0.15)

    return {
        "ok": False,
        "code": "startup_timeout",
        "message": "Daemon did not publish a live PID in time",
    }


__all__ = [
    "DEFAULT_LM_BASE_URL",
    "DEFAULT_MODEL",
    "CorrectionOutcome",
    "DaemonState",
    "FileState",
    "InstructorLLMClient",
    "LLMClient",
    "LLMResponseError",
    "StructuredOutputExhaustedError",
    "LintType",
    "ThermalProfile",
    "MaintenanceDaemon",
    "PID_FILENAME",
    "DAEMON_LOCK_FILENAME",
    "SEMANTIC_INDEX_HEADER",
    "STRUCTURAL_LINT_HEADER",
    "ScanMetrics",
    "SemanticIndexResult",
    "SemanticLintCorrection",
    "apply_semantic_corrections_to_lines",
    "apply_semantic_page_result",
    "append_semantic_index",
    "append_structural_lint_warning",
    "clear_phase1_error_backoff",
    "compute_scan_metrics",
    "load_daemon_state",
    "normalize_daemon_state_file_keys",
    "is_plumber_process",
    "prune_stale_daemon_file_entries",
    "read_pid_file",
    "resolve_graph_root",
    "run_plumber_audit",
    "save_daemon_state",
    "start_daemon_detached",
    "start_daemon_foreground",
    "stop_daemon",
    "sync_daemon_state_from_env",
]

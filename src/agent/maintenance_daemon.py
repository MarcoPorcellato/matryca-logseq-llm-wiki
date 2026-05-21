"""Matryca Plumber Maintenance Daemon — progressive local LLM graph indexing."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from ..graph.alias_index import (
    AliasIndex,
    format_alias_index_for_prompt,
    iter_alias_source_paths,
    resolve_canonical_page_title,
)
from ..graph.bootstrap_harvest import (
    run_bootstrap_harvest,
    run_incremental_catalog_refresh,
)
from ..graph.generational_cache import cached_build_alias_index, patch_generational_caches_for_paths
from ..graph.global_fence_scanner import compute_page_protected_line_indices
from ..graph.insights_engine import (
    INSIGHTS_SYSTEM_PROMPT,
    run_graph_insights_engine,
)
from ..graph.logseq_uuid import find_malformed_block_refs, is_malformed_block_ref_error
from ..graph.markdown_blocks import atomic_write_bytes, locate_block_by_uuid
from ..graph.master_catalog import (
    extract_catalog_fields_from_content,
    is_bootstrap_catalog_complete,
    load_master_catalog,
    master_index_page_path,
)
from ..graph.page_write_lock import (
    clear_page_write_locks,
    page_rmw_lock,
    sweep_matryca_lock_sidecars,
)
from ..utils.token_logger import OperationType, TokenLogger
from .context_compressor import (
    COMPRESSION_SYSTEM_PROMPT,
    MAX_EXECUTION_HISTORY_MESSAGES,
    ChatMessage,
    condense_messages,
    extract_persisted_history,
)
from .plumber_config import (
    DEFAULT_LM_BASE_URL,
    DEFAULT_LM_MODEL,
    PlumberLintConfig,
    bootstrap_phase_lint_config,
    load_plumber_lint_config,
    resolve_lm_base_url,
    resolve_lm_model,
)
from .plumber_llm import (
    BootstrapSummaryResult,
    ContextualSeedResult,
    EntityOverlapResult,
    GraphInsightsLLMResult,
    InferredPropertiesResult,
    MarpaClassificationResult,
)
from .plumber_modules import run_cognitive_lint_pipeline
from .plumber_modules.backlink_backpropagator import BacklinkCorrection, run_backlink_backpropagator
from .plumber_modules.marpa_framework import (
    build_marpa_classify_system_prompt,
    build_marpa_classify_user_prompt,
)
from .plumber_modules.semantic_cache_router import (
    cache_get,
    cache_put,
    semantic_cache_key,
)
from .prompt_constraints import ALIAS_FIRST_LINK_CONSTRAINT, finalize_system_prompt

STATE_FILENAME = ".matryca_daemon_state.json"
PID_FILENAME = ".matryca_plumber_daemon.pid"
SEMANTIC_INDEX_HEADER = "### Matryca Semantic Index"
STRUCTURAL_LINT_HEADER = "### Matryca Structural Lint"
DEFAULT_MODEL = DEFAULT_LM_MODEL  # backward-compatible alias for CLI/TUI
DEFAULT_POLL_SECONDS = 30.0
MATRYCA_GENERATED_PAGE_TITLES = frozenset(
    {"Matryca Master Index", "Matryca Graph Insights"},
)

_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ID_LINE = re.compile(r"^\s*id::\s*(.+?)\s*$", re.IGNORECASE)
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


@dataclass
class FileState:
    """Processing record for one markdown file."""

    mtime: float
    processed_at: str
    status: Literal["processed", "skipped", "error", "pending"] = "processed"
    error: str | None = None


@dataclass
class DaemonState:
    """Persistent daemon checkpoint stored inside the graph root."""

    version: int = 1
    files: dict[str, FileState] = field(default_factory=dict)
    status: DaemonStatus = "idle"
    model: str = DEFAULT_LM_MODEL
    bootstrap_complete: bool = False
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    last_scan_at: str | None = None
    last_file: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "model": self.model,
            "bootstrap_complete": self.bootstrap_complete,
            "session_prompt_tokens": self.session_prompt_tokens,
            "session_completion_tokens": self.session_completion_tokens,
            "last_scan_at": self.last_scan_at,
            "last_file": self.last_file,
            "files": {
                path: {
                    "mtime": rec.mtime,
                    "processed_at": rec.processed_at,
                    "status": rec.status,
                    "error": rec.error,
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
                files[str(path)] = FileState(
                    mtime=float(rec.get("mtime", 0.0)),
                    processed_at=str(rec.get("processed_at", "")),
                    status=str(rec.get("status", "processed")),  # type: ignore[arg-type]
                    error=rec.get("error"),
                )
        return cls(
            version=int(payload.get("version", 1)),
            files=files,
            status=str(payload.get("status", "idle")),  # type: ignore[arg-type]
            model=str(payload.get("model", DEFAULT_LM_MODEL)),
            bootstrap_complete=bool(payload.get("bootstrap_complete", False)),
            session_prompt_tokens=int(payload.get("session_prompt_tokens", 0)),
            session_completion_tokens=int(payload.get("session_completion_tokens", 0)),
            last_scan_at=payload.get("last_scan_at"),
            last_file=payload.get("last_file"),
        )


def resolve_graph_root() -> Path:
    """Return ``LOGSEQ_GRAPH_PATH`` or raise when unset."""
    raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if not raw:
        msg = "LOGSEQ_GRAPH_PATH must be set for the Matryca Plumber daemon"
        raise ValueError(msg)
    return Path(raw).expanduser().resolve(strict=False)


def state_path(graph_root: Path) -> Path:
    return graph_root / STATE_FILENAME


def pid_path(graph_root: Path) -> Path:
    return graph_root / PID_FILENAME


def sync_daemon_state_from_env(state: DaemonState) -> DaemonState:
    """Ensure persisted daemon state reflects the current ``MATRYCA_LM_MODEL`` env value."""
    state.model = resolve_lm_model()
    return state


def load_daemon_state(graph_root: Path) -> DaemonState:
    from loguru import logger

    path = state_path(graph_root)
    if not path.is_file():
        return sync_daemon_state_from_env(DaemonState())
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return sync_daemon_state_from_env(DaemonState())
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "[METADATA CORRUPTION DETECTED] File state was malformed, "
            "self-healing by initializing a fresh instance."
        )
        return sync_daemon_state_from_env(DaemonState())
    if not isinstance(payload, dict):
        return sync_daemon_state_from_env(DaemonState())
    return sync_daemon_state_from_env(DaemonState.from_json(payload))


def save_daemon_state(graph_root: Path, state: DaemonState) -> None:
    """Persist daemon state via atomic write under graph root."""
    path = state_path(graph_root)
    data = json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(
        path,
        data.encode("utf-8"),
        graph_root=graph_root,
        validate_block_refs=False,
    )


def write_pid_file(graph_root: Path) -> None:
    path = pid_path(graph_root)
    payload = f"{os.getpid()}\n"
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
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
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


def stop_daemon(graph_root: Path) -> dict[str, Any]:
    """Gracefully stop a running daemon via SIGTERM."""
    pid = read_pid_file(graph_root)
    if pid is None:
        return {"ok": True, "code": "not_running", "message": "No PID file found"}
    if not is_process_alive(pid):
        remove_pid_file(graph_root)
        state = load_daemon_state(graph_root)
        state.status = "stopped"
        save_daemon_state(graph_root, state)
        return {"ok": True, "code": "stale_pid_removed", "pid": pid}
    os.kill(pid, signal.SIGTERM)
    pid_file = pid_path(graph_root)
    for _ in range(20):
        if not is_process_alive(pid) and not pid_file.is_file():
            break
        time.sleep(0.1)
    return {"ok": True, "code": "signaled", "pid": pid, "signal": "SIGTERM"}


def _page_title_from_path(graph_root: Path, path: Path) -> str:
    rel = path.relative_to(graph_root)
    stem = rel.with_suffix("").as_posix()
    if stem.startswith("pages/"):
        return stem.removeprefix("pages/")
    if stem.startswith("journals/"):
        return stem.removeprefix("journals/")
    return stem


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


def _build_semantic_lint_system_prompt(*, alias_index_context: str | None = None) -> str:
    instructions = (
        "You are Matryca Plumber, a semantic linter and indexer for Logseq OG outliner pages. "
        "Behave like a strict compiler: analyze block-by-block, propose only safe additive "
        "micro-corrections, never rewrite whole files. "
        "Rules for semantic_corrections: "
        "(1) block_uuid must match an id:: line from the page; "
        "(2) original_text must copy the bullet-line text verbatim "
        "(exclude id:: / property lines); "
        "(3) corrected_text must preserve original_text unchanged "
        "and only add [[WikiLinks]] or #tags; "
        "(4) never delete, shorten, or paraphrase user prose; "
        "(5) if unsure, omit the correction. "
        "lint_type auto_wikilink: wrap recognizable concepts in [[Page Title]] links. "
        "lint_type tag_hygiene: normalize inline #tags without removing words. "
        "lint_type anomaly_warning: flag issues only — set corrected_text equal to original_text. "
        "Also populate summary, cross_references, suggested_tags, and moc_pointers. "
        f"{ALIAS_FIRST_LINK_CONSTRAINT}"
    )
    if alias_index_context:
        instructions += (
            "\n\nCompiled AliasIndex (canonical page titles and alias:: values):\n"
            f"{alias_index_context}"
        )
    return finalize_system_prompt(instructions)


def _build_index_prompt(
    page_title: str,
    content: str,
    *,
    alias_index_context: str | None = None,
) -> str:
    block_catalog = _enumerate_blocks_for_prompt(content)
    alias_section = ""
    if alias_index_context:
        alias_section = (
            "\nAliasIndex (resolve every wikilink against this map before suggesting links):\n"
            f"{alias_index_context}\n"
        )
    return (
        "Task: semantic indexing and safe per-block lint for one Logseq OG outliner page.\n"
        "Output: structured JSON only.\n"
        "Steps:\n"
        "1. Read the page title, block catalog, AliasIndex, and full content below.\n"
        "2. Extract summary, cross_references, suggested_tags, and moc_pointers.\n"
        "3. Propose semantic_corrections only when every system-prompt "
        "safety rule is satisfied.\n"
        "4. Prefer existing canonical titles and alias:: over inventing new page names.\n\n"
        f"Page title: {page_title}\n\n"
        f"Blocks available for semantic_corrections:\n{block_catalog}\n"
        f"{alias_section}\n"
        f"Full page content:\n{content[:8000]}"
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


def apply_semantic_corrections_to_lines(
    lines: list[str],
    corrections: list[SemanticLintCorrection],
) -> CorrectionOutcome:
    """Apply micro-corrections in-place on ``lines`` (bullet text only, UUID-anchored)."""
    outcome = CorrectionOutcome()
    if not corrections:
        return outcome

    stripped = [ln.rstrip("\n") for ln in lines]
    pending: list[tuple[int, SemanticLintCorrection]] = []
    for correction in corrections:
        located = locate_block_by_uuid(stripped, correction.block_uuid)
        if located is None:
            outcome.skipped += 1
            outcome.skip_reasons.append(f"uuid_not_found:{correction.block_uuid}")
            continue
        bullet_idx, _id_idx, _end = located
        pending.append((bullet_idx, correction))

    for bullet_idx, correction in sorted(pending, key=lambda item: item[0], reverse=True):
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
        lines[bullet_idx] = _set_bullet_inline_text(lines[bullet_idx], correction.corrected_text)
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
    if not page_path.is_file() or not malformed_refs:
        return False
    text = page_path.read_text(encoding="utf-8", errors="replace")
    if STRUCTURAL_LINT_HEADER in text:
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
    atomic_write_bytes(
        page_path,
        new_text.encode("utf-8"),
        graph_root=graph_root,
        validate_block_refs=False,
    )
    return True


def apply_semantic_page_result(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    result: SemanticIndexResult,
    *,
    backpropagate: bool = False,
    alias_index: AliasIndex | None = None,
) -> CorrectionOutcome:
    """Lint blocks surgically, then append the semantic index (one locked transaction)."""
    with page_rmw_lock(page_path):
        if page_path.is_file():
            prev = page_path.read_text(encoding="utf-8", errors="replace")
        else:
            prev = ""
        lines = prev.splitlines(keepends=True)
        lint_outcome = apply_semantic_corrections_to_lines(lines, result.semantic_corrections)
        body = "".join(lines)
        if SEMANTIC_INDEX_HEADER not in body:
            body = body.rstrip("\n") + _format_index_section(result, lint_outcome=lint_outcome)
        atomic_write_bytes(page_path, body.encode("utf-8"), graph_root=graph_root)
    patch_generational_caches_for_paths(graph_root, [page_path])

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
        run_backlink_backpropagator(
            graph_root,
            page_path,
            page_title,
            backlink_corrections,
            lint_outcome.applied_details,
            alias_index=alias_index,
        )
    return lint_outcome


def append_semantic_index(
    graph_root: Path,
    page_path: Path,
    result: SemanticIndexResult,
) -> None:
    """Append a semantic index section without modifying existing user content."""
    if page_path.is_file():
        prev = page_path.read_text(encoding="utf-8", errors="replace")
        if SEMANTIC_INDEX_HEADER in prev:
            return
    apply_semantic_page_result(
        graph_root,
        page_path,
        _page_title_from_path(graph_root, page_path),
        result,
    )


class InstructorLLMClient:
    """Local LM Studio client using instructor + OpenAI SDK."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        token_logger: TokenLogger | None = None,
    ) -> None:
        self.token_logger = token_logger or TokenLogger()
        self._explicit_model = model
        self._explicit_base_url = base_url
        self.base_url = resolve_lm_base_url(override=base_url)
        self.model = resolve_lm_model(override=model)
        self._raw_client: OpenAI = OpenAI(base_url=self.base_url, api_key="lm-studio")
        self._execution_history: list[ChatMessage] = []

    def refresh_config(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Reload LM Studio settings from env (explicit ctor overrides win over env)."""
        resolved_model = model if model is not None else self._explicit_model
        resolved_base = base_url if base_url is not None else self._explicit_base_url
        self.base_url = resolve_lm_base_url(override=resolved_base)
        self.model = resolve_lm_model(override=resolved_model)
        self._raw_client = OpenAI(base_url=self.base_url, api_key="lm-studio")

    def reset_execution_history(self) -> None:
        """Drop per-page session history (constant memory footprint across daemon uptime)."""
        self._execution_history.clear()

    def _trim_execution_history(self) -> None:
        if len(self._execution_history) > MAX_EXECUTION_HISTORY_MESSAGES:
            self._execution_history = self._execution_history[-MAX_EXECUTION_HISTORY_MESSAGES:]

    def _append_execution_turn(self, prompt: str, response: str) -> None:
        self._execution_history.append({"role": "user", "content": prompt})
        self._execution_history.append({"role": "assistant", "content": response})
        self._trim_execution_history()

    def _compress_history_via_llm(self, compression_prompt: str) -> str:
        """Send isolated history to LM Studio for epistemic condensation."""
        response = self._raw_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": COMPRESSION_SYSTEM_PROMPT},
                {"role": "user", "content": compression_prompt},
            ],
            temperature=0.1,
        )
        content = response.choices[0].message.content
        return content.strip() if content else ""

    def _completion_with_structured_output[T: BaseModel](
        self,
        *,
        prompt: str,
        response_model: type[T],
        system_prompt: str,
    ) -> tuple[T, object]:
        """Call LM Studio with JSON schema mode, falling back when parsing fails."""
        self.refresh_config()
        config = load_plumber_lint_config()
        if not config.context_compression:
            self.reset_execution_history()
            messages: list[ChatMessage] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        else:
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self._execution_history)
            messages.append({"role": "user", "content": prompt})
            messages, compression_event = condense_messages(
                messages,
                trigger=config.compression_trigger,
                target=config.compression_target,
                compress_fn=self._compress_history_via_llm,
                warn_fn=lambda msg: self.token_logger.log_compression_warning(
                    msg,
                    model=self.model,
                ),
            )
            if compression_event is not None:
                self._execution_history = extract_persisted_history(messages)
                self.token_logger.log_compression_event(
                    initial_tokens=compression_event.initial_tokens,
                    post_tokens=compression_event.post_tokens,
                    latency_seconds=compression_event.latency_seconds,
                    compression_ratio=compression_event.compression_ratio,
                    messages_before=compression_event.messages_before,
                    messages_after=compression_event.messages_after,
                    model=self.model,
                )

        errors: list[str] = []
        for mode in _instructor_modes_to_try():
            client = instructor.from_openai(self._raw_client, mode=mode)
            try:
                parsed, completion = client.chat.completions.create_with_completion(
                    model=self.model,
                    messages=messages,
                    response_model=response_model,
                )
                if config.context_compression:
                    self._append_execution_turn(prompt, parsed.model_dump_json())
                return parsed, completion
            except Exception as exc:  # noqa: BLE001 - try next instructor mode
                errors.append(f"{mode.name}: {exc}")
        msg = "Structured output failed for all instructor modes: " + "; ".join(errors)
        raise RuntimeError(msg)

    def generate_contextual_seed(
        self,
        *,
        link_title: str,
        source_page: str,
        context: str,
        max_words: int,
    ) -> ContextualSeedResult:
        prompt = (
            "Task: write a contextual seed definition for a dangling wikilink.\n"
            f"Target link: [[{link_title}]]\n"
            f"Source page: [[{source_page}]]\n"
            f"Max length: {max_words} words.\n\n"
            f"Surrounding blocks:\n{context}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=ContextualSeedResult,
            system_prompt=finalize_system_prompt(
                "You seed new Logseq pages from local context. Return JSON only. "
                "Write a neutral, concise definition without markdown headings."
            ),
        )
        return result

    def assess_entity_overlap(
        self,
        *,
        title_a: str,
        title_b: str,
        context: str,
    ) -> EntityOverlapResult:
        prompt = (
            "Task: decide whether two page titles refer to the same concept.\n"
            f"Title A: {title_a}\n"
            f"Title B: {title_b}\n\n"
            f"Page context:\n{context[:3000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=EntityOverlapResult,
            system_prompt=finalize_system_prompt(
                "You are an entity consolidation linter for Logseq. Prefer one canonical title "
                "and register the other as alias. Never suggest merging file contents."
            ),
        )
        return result

    def infer_tag_properties(
        self,
        *,
        tag: str,
        required_keys: list[str],
        page_title: str,
        content: str,
    ) -> InferredPropertiesResult:
        keys = ", ".join(required_keys)
        prompt = (
            "Task: infer Logseq block property values from page context.\n"
            f"Page: [[{page_title}]]\n"
            f"Tag: #{tag}\n"
            f"Required property keys: {keys}\n\n"
            f"Content:\n{content[:5000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=InferredPropertiesResult,
            system_prompt=finalize_system_prompt(
                "Infer Logseq block properties from context. Use empty string when unknown. "
                "Return JSON with a properties object."
            ),
        )
        return result

    def classify_marpa_page(
        self,
        *,
        page_title: str,
        content: str,
        namespace_hint: str | None,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> MarpaClassificationResult:
        prompt = build_marpa_classify_user_prompt(
            page_title,
            content,
            namespace_hint=namespace_hint,
        )
        config = load_plumber_lint_config()
        if config.semantic_routing and page_path is not None and graph_root is not None:
            key = semantic_cache_key(page_path, "marpa_classify")
            cached = cache_get(graph_root, "marpa", key)
            if cached is not None:
                return MarpaClassificationResult.model_validate(cached)

        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=MarpaClassificationResult,
            system_prompt=build_marpa_classify_system_prompt(),
        )
        if config.semantic_routing and page_path is not None and graph_root is not None:
            key = semantic_cache_key(page_path, "marpa_classify")
            cache_put(graph_root, "marpa", key, result.model_dump())
        return result

    def index_page(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        alias_index: AliasIndex | None = None,
        enable_semantic_routing: bool = False,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        alias_context = (
            format_alias_index_for_prompt(alias_index, page_content=content)
            if alias_index is not None
            else None
        )
        prompt = _build_index_prompt(
            page_title,
            content,
            alias_index_context=alias_context,
        )
        started = time.perf_counter()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        config = load_plumber_lint_config()
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
                system_prompt=_build_semantic_lint_system_prompt(
                    alias_index_context=alias_context,
                ),
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

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> BootstrapSummaryResult:
        """Extract a one-sentence summary for bootstrap harvesting."""
        prompt = (
            "Task: extract a concise one-sentence summary for catalog indexing.\n"
            "Return JSON with summary, suggested_tags (0-5 tags), and optional MARPA domain "
            "(mappa|area|risorsa|progetto|archivio) when clearly inferable.\n\n"
            f"Page title: {page_title}\n\n"
            f"Content:\n{content[:6000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=BootstrapSummaryResult,
            system_prompt=finalize_system_prompt(
                "You are Matryca Plumber's bootstrap harvester. Return JSON only. "
                "Write one crisp English sentence summarizing the page."
            ),
        )
        _ = (page_path, graph_root)
        return result

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult:
        """Generate structured graph diagnostics from topology aggregates."""
        prompt = (
            "Task: produce a panoramic ontology report and non-destructive cleanup suggestions "
            "from the structural metrics below.\n\n"
            f"Topology metrics JSON:\n{metrics_json[:12000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=GraphInsightsLLMResult,
            system_prompt=INSIGHTS_SYSTEM_PROMPT,
        )
        _ = graph_root
        return result


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


def compute_scan_metrics(graph_root: Path, state: DaemonState) -> ScanMetrics:
    files = iter_alias_source_paths(graph_root)
    total = len(files)
    processed = 0
    pending = 0
    for path in files:
        key = str(path.resolve())
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        rec = state.files.get(key)
        if rec is not None and rec.mtime == mtime:
            if rec.status == "processed":
                processed += 1
            elif rec.status in {"skipped", "error"}:
                pass
            else:
                pending += 1
        else:
            pending += 1
    return ScanMetrics(total=total, processed=processed, pending=pending)


def list_pending_files(graph_root: Path, state: DaemonState) -> list[Path]:
    pending: list[Path] = []
    settled_statuses = frozenset({"processed", "skipped"})
    for path in iter_alias_source_paths(graph_root):
        title = _page_title_from_path(graph_root, path)
        if title in MATRYCA_GENERATED_PAGE_TITLES:
            continue
        key = str(path.resolve())
        mtime = path.stat().st_mtime
        rec = state.files.get(key)
        if rec is not None and rec.mtime == mtime:
            if rec.status in settled_statuses:
                continue
            if rec.status == "error":
                continue
        pending.append(path)
    return pending


def prune_stale_daemon_file_entries(state: DaemonState, graph_root: Path) -> int:
    """Remove ghost paths from daemon state (deleted or renamed markdown files)."""
    live_keys = {str(path.resolve()) for path in iter_alias_source_paths(graph_root)}
    ghosts = [key for key in state.files if key not in live_keys]
    for key in ghosts:
        del state.files[key]
    return len(ghosts)


def _resolve_instructor_mode(name: str) -> instructor.Mode:
    mapping = {
        "JSON": instructor.Mode.JSON,
        "JSON_SCHEMA": instructor.Mode.JSON_SCHEMA,
        "MD_JSON": instructor.Mode.MD_JSON,
        "TOOLS": instructor.Mode.TOOLS,
    }
    return mapping.get(name.strip().upper(), instructor.Mode.JSON_SCHEMA)


def _instructor_modes_to_try() -> list[instructor.Mode]:
    primary = os.environ.get("MATRYCA_LM_INSTRUCTOR_MODE", "JSON_SCHEMA")
    modes = [_resolve_instructor_mode(primary)]
    fallback = os.environ.get("MATRYCA_LM_INSTRUCTOR_FALLBACK", "MD_JSON")
    fallback_mode = _resolve_instructor_mode(fallback)
    if fallback_mode not in modes:
        modes.append(fallback_mode)
    return modes


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
        self.bootstrap_complete = False

    def _hydrate_bootstrap_phase(self, state: DaemonState) -> None:
        """Restore Phase 2 readiness from persisted state or a complete on-disk catalog."""
        if state.bootstrap_complete or is_bootstrap_catalog_complete(self.graph_root):
            self.bootstrap_complete = True
            state.bootstrap_complete = True
        else:
            self.bootstrap_complete = False

    def _persist_bootstrap_complete(self, state: DaemonState) -> None:
        state.bootstrap_complete = True
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
        """Safe evacuation: log, persist catalog, drop PID/locks, then exit."""
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self._stop_requested = True

        with contextlib.suppress(Exception):
            self.token_logger.log_daemon_shutdown(signum)

        try:
            load_master_catalog(self.graph_root).save()
        except Exception as exc:  # noqa: BLE001 - log and continue evacuation
            with contextlib.suppress(Exception):
                self.token_logger.log_structural_lint_warning(
                    target_file=self.graph_root,
                    message=f"Catalog evacuation save failed: {exc}",
                    malformed_refs=[],
                )

        try:
            state = load_daemon_state(self.graph_root)
            state.status = "stopped"
            save_daemon_state(self.graph_root, state)
        except Exception:  # noqa: BLE001
            pass

        remove_pid_file(self.graph_root)
        clear_page_write_locks()
        sweep_matryca_lock_sidecars(self.graph_root)
        sys.exit(0)

    def _sync_runtime_config(self, state: DaemonState) -> DaemonState:
        """Align LLM client and persisted state with the active environment block."""
        if isinstance(self.llm_client, InstructorLLMClient):
            self.llm_client.refresh_config()
        return sync_daemon_state_from_env(state)

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_bootstrap_pipeline(self, state: DaemonState | None = None) -> None:
        """Phase 1 bootstrap harvest (read/append only) before Phase 2 polling."""
        if self.bootstrap_complete:
            return

        checkpoint = state or load_daemon_state(self.graph_root)
        self._hydrate_bootstrap_phase(checkpoint)
        if self.bootstrap_complete:
            return

        try:
            metrics = run_bootstrap_harvest(
                self.graph_root,
                llm=self.llm_client,
                incremental=False,
                rebuild_index=True,
                phase1_strict=True,
            )
        except Exception as exc:  # noqa: BLE001 - bootstrap must not block daemon start
            self.token_logger.log_structural_lint_warning(
                target_file=self.graph_root,
                message=f"Bootstrap harvest failed: {exc}",
                malformed_refs=[],
            )
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
        """Drop ghost catalog rows for deleted or renamed markdown files."""
        try:
            catalog = load_master_catalog(self.graph_root)
            pruned = catalog.prune_missing_pages()
            if pruned > 0:
                catalog.save()
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
        """Process up to ``max_files_per_cycle`` pending files."""
        state = self._sync_runtime_config(state or load_daemon_state(self.graph_root))
        prune_stale_daemon_file_entries(state, self.graph_root)
        state.status = "running"
        state.last_scan_at = datetime.now(tz=UTC).isoformat()
        pending = list_pending_files(self.graph_root, state)[: self.max_files_per_cycle]

        if not pending:
            self.refresh_catalog_if_stale()
            self._prune_stale_catalog_entries()
            state.status = "idle"
            save_daemon_state(self.graph_root, state)
            return state

        for path in pending:
            if self._stop_requested:
                state.status = "stopped"
                break
            key = str(path.resolve())
            mtime = path.stat().st_mtime
            title = _page_title_from_path(self.graph_root, path)
            state.last_file = key
            content = ""
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
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
                        message=(
                            "Malformed UUID detected in block ref. Must be standard 36-char format."
                        ),
                        malformed_refs=malformed_refs,
                        state=state,
                    )
                    continue
                if SEMANTIC_INDEX_HEADER in content:
                    state.files[key] = FileState(
                        mtime=mtime,
                        processed_at=datetime.now(tz=UTC).isoformat(),
                        status="skipped",
                    )
                    continue
                if not content.strip():
                    state.files[key] = FileState(
                        mtime=mtime,
                        processed_at=datetime.now(tz=UTC).isoformat(),
                        status="skipped",
                    )
                    continue
                lint_config = self._effective_lint_config()
                if self.bootstrap_complete and lint_config.any_enabled:
                    run_cognitive_lint_pipeline(
                        self.graph_root,
                        path,
                        title,
                        content,
                        llm=self.llm_client,
                        config=lint_config,
                    )
                    content = path.read_text(encoding="utf-8", errors="replace")
                alias_index = self._compiled_alias_index() if self.bootstrap_complete else None
                enable_semantic_routing = self.bootstrap_complete and lint_config.semantic_routing
                enable_backprop = self.bootstrap_complete and lint_config.backpropagate_links
                result, _usage = self.llm_client.index_page(
                    title,
                    content,
                    page_path=path,
                    graph_root=self.graph_root,
                    alias_index=alias_index,
                    enable_semantic_routing=enable_semantic_routing,
                )
                apply_semantic_page_result(
                    self.graph_root,
                    path,
                    title,
                    result,
                    backpropagate=enable_backprop,
                    alias_index=alias_index,
                )
                state.files[key] = FileState(
                    mtime=path.stat().st_mtime,
                    processed_at=datetime.now(tz=UTC).isoformat(),
                    status="processed",
                )
                self._sync_catalog_after_page_write(path, title)
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
                if isinstance(self.llm_client, InstructorLLMClient):
                    self.llm_client.reset_execution_history()
                try:
                    save_daemon_state(self.graph_root, state)
                except Exception as save_exc:  # noqa: BLE001 - log and continue cycle
                    self.token_logger.log_structural_lint_warning(
                        target_file=path,
                        message=f"Checkpoint save failed after {key}: {save_exc}",
                        malformed_refs=[],
                    )

        self._prune_stale_catalog_entries()
        state.session_prompt_tokens = self.token_logger.session_prompt_tokens
        state.session_completion_tokens = self.token_logger.session_completion_tokens
        if not self._stop_requested:
            state.status = "idle"
        save_daemon_state(self.graph_root, state)
        return state

    def run_forever(self) -> None:
        """Infinite polling loop until stop is requested."""
        self._register_daemon_signal_handlers()
        write_pid_file(self.graph_root)
        state = self._sync_runtime_config(load_daemon_state(self.graph_root))
        self._hydrate_bootstrap_phase(state)
        state.status = "running"
        save_daemon_state(self.graph_root, state)

        try:
            self.run_bootstrap_pipeline(state)
            while not self._stop_requested:
                self.run_cycle(state)
                state = load_daemon_state(self.graph_root)
                if self._stop_requested:
                    break
                time.sleep(self.poll_seconds)
        finally:
            state = load_daemon_state(self.graph_root)
            state.status = "stopped"
            save_daemon_state(self.graph_root, state)
            remove_pid_file(self.graph_root)


def run_plumber_audit(graph_root: Path | None = None) -> dict[str, Any]:
    """Manual entrypoint: refresh catalog and compile graph insights dashboard."""
    root = graph_root or resolve_graph_root()
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
    from dotenv import load_dotenv

    load_dotenv()
    root = graph_root or resolve_graph_root()
    daemon = MaintenanceDaemon(root)
    daemon.run_forever()


def start_daemon_detached(graph_root: Path | None = None) -> dict[str, Any]:
    """Fork a background daemon process and return its PID."""
    root = graph_root or resolve_graph_root()
    existing = read_pid_file(root)
    if existing is not None and is_process_alive(existing):
        return {
            "ok": False,
            "code": "already_running",
            "pid": existing,
            "message": f"Matryca Plumber daemon already running (pid {existing})",
        }

    pid = os.fork()
    if pid > 0:
        return {"ok": True, "code": "started", "pid": pid, "graph_root": str(root)}

    os.setsid()
    fork_pid = os.fork()
    if fork_pid > 0:
        os._exit(0)

    sys.stdin = open(os.devnull)  # noqa: SIM115
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115
    sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    with contextlib.suppress(Exception):
        from loguru import logger

        logger.remove()

    from dotenv import load_dotenv

    load_dotenv()
    os.environ["LOGSEQ_GRAPH_PATH"] = str(root)
    MaintenanceDaemon(root).run_forever()
    os._exit(0)


__all__ = [
    "DEFAULT_LM_BASE_URL",
    "DEFAULT_MODEL",
    "CorrectionOutcome",
    "DaemonState",
    "FileState",
    "InstructorLLMClient",
    "LLMClient",
    "LintType",
    "MaintenanceDaemon",
    "PID_FILENAME",
    "ScanMetrics",
    "SemanticIndexResult",
    "SemanticLintCorrection",
    "apply_semantic_corrections_to_lines",
    "apply_semantic_page_result",
    "append_semantic_index",
    "append_structural_lint_warning",
    "compute_scan_metrics",
    "load_daemon_state",
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

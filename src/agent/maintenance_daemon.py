"""Matryca Brain Maintenance Daemon — progressive local LLM graph indexing."""

from __future__ import annotations

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

from ..graph.alias_index import iter_alias_source_paths
from ..graph.generational_cache import patch_generational_caches_for_paths
from ..graph.markdown_blocks import atomic_write_bytes, locate_block_by_uuid
from ..graph.page_write_lock import page_rmw_lock
from ..utils.token_logger import OperationType, TokenLogger
from .brain_config import (
    DEFAULT_LM_BASE_URL,
    DEFAULT_LM_MODEL,
    load_brain_lint_config,
    resolve_lm_base_url,
    resolve_lm_model,
)
from .brain_llm import (
    ContextualSeedResult,
    EntityOverlapResult,
    InferredPropertiesResult,
    MarpaClassificationResult,
)
from .brain_modules import run_cognitive_lint_pipeline
from .brain_modules.backlink_backpropagator import BacklinkCorrection, run_backlink_backpropagator
from .brain_modules.semantic_cache_router import (
    cache_get,
    cache_put,
    semantic_cache_key,
)
from .context_compressor import (
    COMPRESSION_SYSTEM_PROMPT,
    MAX_EXECUTION_HISTORY_MESSAGES,
    ChatMessage,
    condense_messages,
    extract_persisted_history,
)

STATE_FILENAME = ".matryca_daemon_state.json"
PID_FILENAME = ".matryca_brain_daemon.pid"
SEMANTIC_INDEX_HEADER = "### Matryca Semantic Index"
DEFAULT_MODEL = DEFAULT_LM_MODEL  # backward-compatible alias for CLI/TUI
DEFAULT_POLL_SECONDS = 30.0

_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ID_LINE = re.compile(r"^\s*id::\s*(.+?)\s*$", re.IGNORECASE)

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
    target: str = Field(description="[[Page Title]] or #tag reference")


class SemanticIndexResult(BaseModel):
    """Structured semantic index + Karpathy-style lint payload from the local LLM."""

    summary: str = Field(description="One-sentence page summary")
    cross_references: list[SemanticCrossRef] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    moc_pointers: list[str] = Field(
        default_factory=list,
        description="Suggested [[Map of Content]] page links",
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
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        """Return structured index and token usage dict with prompt/completion keys."""
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
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    last_scan_at: str | None = None
    last_file: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "model": self.model,
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
            session_prompt_tokens=int(payload.get("session_prompt_tokens", 0)),
            session_completion_tokens=int(payload.get("session_completion_tokens", 0)),
            last_scan_at=payload.get("last_scan_at"),
            last_file=payload.get("last_file"),
        )


def resolve_graph_root() -> Path:
    """Return ``LOGSEQ_GRAPH_PATH`` or raise when unset."""
    raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if not raw:
        msg = "LOGSEQ_GRAPH_PATH must be set for the Matryca Brain daemon"
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
    path = state_path(graph_root)
    if not path.is_file():
        return sync_daemon_state_from_env(DaemonState())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return sync_daemon_state_from_env(DaemonState())
    if not isinstance(payload, dict):
        return sync_daemon_state_from_env(DaemonState())
    return sync_daemon_state_from_env(DaemonState.from_json(payload))


def save_daemon_state(graph_root: Path, state: DaemonState) -> None:
    """Persist daemon state via atomic write under graph root."""
    path = state_path(graph_root)
    data = json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, data.encode("utf-8"), graph_root=graph_root)


def write_pid_file(graph_root: Path) -> None:
    path = pid_path(graph_root)
    payload = f"{os.getpid()}\n"
    atomic_write_bytes(path, payload.encode("utf-8"), graph_root=graph_root)


def read_pid_file(graph_root: Path) -> int | None:
    path = pid_path(graph_root)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
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


def _build_semantic_lint_system_prompt() -> str:
    return (
        "You are Matryca Brain, a semantic linter and indexer for Logseq OG outliner pages. "
        "Behave like Andrej Karpathy's nanochat compiler: analyze block-by-block, propose only "
        "safe, additive micro-corrections, never rewrite whole files. "
        "Rules for semantic_corrections: "
        "(1) block_uuid must match an id:: line from the page; "
        "(2) original_text must copy the bullet-line text verbatim "
        "(exclude id:: / property lines); "
        "(3) corrected_text must contain original_text unchanged "
        "and only add [[WikiLinks]] or #tags; "
        "(4) never delete, shorten, or paraphrase user prose; "
        "(5) if unsure, omit the correction. "
        "lint_type auto_wikilink: wrap recognizable concepts in [[Page Title]] links. "
        "lint_type tag_hygiene: normalize inline #tags without removing words. "
        "lint_type anomaly_warning: flag issues only — set corrected_text equal to original_text. "
        "Also populate summary, cross_references, suggested_tags, and moc_pointers."
    )


def _build_index_prompt(page_title: str, content: str) -> str:
    block_catalog = _enumerate_blocks_for_prompt(content)
    return (
        "Analyze this Logseq outliner page. Extract semantic connections AND propose safe "
        "per-block lint corrections.\n"
        "Return structured JSON only.\n\n"
        f"Page title: {page_title}\n\n"
        f"Blocks available for semantic_corrections:\n{block_catalog}\n\n"
        f"Full page content:\n{content[:8000]}"
    )


def _strip_wikilink_brackets(text: str) -> str:
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)


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


def apply_semantic_page_result(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    result: SemanticIndexResult,
    *,
    backpropagate: bool = False,
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
        config = load_brain_lint_config()
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
            f"Create a contextual seed definition for dangling wikilink [[{link_title}]] "
            f"born on page [[{source_page}]]. Max {max_words} words.\n\n"
            f"Surrounding blocks:\n{context}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=ContextualSeedResult,
            system_prompt=(
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
            f"Do these two page titles refer to the same concept?\n"
            f"A: {title_a}\nB: {title_b}\n\nPage context:\n{context[:3000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=EntityOverlapResult,
            system_prompt=(
                "You are an entity consolidation linter. Prefer one canonical title and "
                "register the other as alias. Never suggest merging file contents."
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
            f"Page [[{page_title}]] is tagged #{tag}. Infer values for: {keys}.\n\n"
            f"Content:\n{content[:5000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=InferredPropertiesResult,
            system_prompt=(
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
        prompt = (
            "Classify this Logseq page into exactly one MARPA domain:\n"
            "- mappa: strategic vision, V2MOM, high-level OKRs\n"
            "- area: continuous operational responsibilities without hard deadlines\n"
            "- risorsa: timeless intellectual capital, specs, reusable assets\n"
            "- progetto: time-bounded tactical work with deadlines and deliverables\n"
            "- archivio: completed/cancelled nodes removed from active focus\n\n"
            f"Page title: {page_title}\n"
            f"Namespace hint: {namespace_hint or '(none)'}\n\n"
            f"Content:\n{content[:7000]}"
        )
        config = load_brain_lint_config()
        if config.semantic_routing and page_path is not None and graph_root is not None:
            key = semantic_cache_key(page_path, "marpa_classify")
            cached = cache_get(graph_root, "marpa", key)
            if cached is not None:
                return MarpaClassificationResult.model_validate(cached)

        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=MarpaClassificationResult,
            system_prompt=(
                "You are the MARPA semantic compiler for Logseq. Return JSON only. "
                "Populate inferred_properties for missing structural fields (deadline, status)."
            ),
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
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        prompt = _build_index_prompt(page_title, content)
        started = time.perf_counter()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        config = load_brain_lint_config()
        try:
            if config.semantic_routing and page_path is not None and graph_root is not None:
                key = semantic_cache_key(page_path, "semantic_index")
                cached = cache_get(graph_root, "index", key)
                if cached is not None:
                    result = SemanticIndexResult.model_validate(cached)
                    return result, usage

            result, completion = self._completion_with_structured_output(
                prompt=prompt,
                response_model=SemanticIndexResult,
                system_prompt=_build_semantic_lint_system_prompt(),
            )
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
            if config.semantic_routing and page_path is not None and graph_root is not None:
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


def compute_scan_metrics(graph_root: Path, state: DaemonState) -> ScanMetrics:
    files = iter_alias_source_paths(graph_root)
    total = len(files)
    processed = 0
    pending = 0
    for path in files:
        key = str(path.resolve())
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        rec = state.files.get(key)
        if rec is not None and rec.mtime == mtime and rec.status == "processed":
            processed += 1
        else:
            pending += 1
    return ScanMetrics(total=total, processed=processed, pending=pending)


def list_pending_files(graph_root: Path, state: DaemonState) -> list[Path]:
    pending: list[Path] = []
    for path in iter_alias_source_paths(graph_root):
        key = str(path.resolve())
        mtime = path.stat().st_mtime
        rec = state.files.get(key)
        if rec is None or rec.mtime != mtime or rec.status != "processed":
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
            os.environ.get("MATRYCA_BRAIN_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)),
        )
        self.max_files_per_cycle = max_files_per_cycle
        self._stop_requested = False

    def _sync_runtime_config(self, state: DaemonState) -> DaemonState:
        """Align LLM client and persisted state with the active environment block."""
        if isinstance(self.llm_client, InstructorLLMClient):
            self.llm_client.refresh_config()
        return sync_daemon_state_from_env(state)

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_cycle(self, state: DaemonState | None = None) -> DaemonState:
        """Process up to ``max_files_per_cycle`` pending files."""
        state = self._sync_runtime_config(state or load_daemon_state(self.graph_root))
        prune_stale_daemon_file_entries(state, self.graph_root)
        state.status = "running"
        state.last_scan_at = datetime.now(tz=UTC).isoformat()
        pending = list_pending_files(self.graph_root, state)[: self.max_files_per_cycle]

        if not pending:
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
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
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
                lint_config = load_brain_lint_config()
                if lint_config.any_enabled:
                    run_cognitive_lint_pipeline(
                        self.graph_root,
                        path,
                        title,
                        content,
                        llm=self.llm_client,
                        config=lint_config,
                    )
                    content = path.read_text(encoding="utf-8", errors="replace")
                result, _usage = self.llm_client.index_page(
                    title,
                    content,
                    page_path=path,
                    graph_root=self.graph_root,
                )
                apply_semantic_page_result(
                    self.graph_root,
                    path,
                    title,
                    result,
                    backpropagate=lint_config.backpropagate_links,
                )
                state.files[key] = FileState(
                    mtime=path.stat().st_mtime,
                    processed_at=datetime.now(tz=UTC).isoformat(),
                    status="processed",
                )
            except Exception as exc:  # noqa: BLE001
                state.files[key] = FileState(
                    mtime=mtime,
                    processed_at=datetime.now(tz=UTC).isoformat(),
                    status="error",
                    error=str(exc),
                )
            finally:
                if isinstance(self.llm_client, InstructorLLMClient):
                    self.llm_client.reset_execution_history()

        state.session_prompt_tokens = self.token_logger.session_prompt_tokens
        state.session_completion_tokens = self.token_logger.session_completion_tokens
        if not self._stop_requested:
            state.status = "idle"
        save_daemon_state(self.graph_root, state)
        return state

    def run_forever(self) -> None:
        """Infinite polling loop until stop is requested."""
        write_pid_file(self.graph_root)
        state = self._sync_runtime_config(load_daemon_state(self.graph_root))
        state.status = "running"
        save_daemon_state(self.graph_root, state)

        def _handle_signal(_signum: int, _frame: object) -> None:
            self.request_stop()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        try:
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
            "message": f"Matryca Brain daemon already running (pid {existing})",
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
    from dotenv import load_dotenv

    load_dotenv()
    os.environ["LOGSEQ_GRAPH_PATH"] = str(root)
    start_daemon_foreground(root)
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
    "ScanMetrics",
    "SemanticIndexResult",
    "SemanticLintCorrection",
    "apply_semantic_corrections_to_lines",
    "apply_semantic_page_result",
    "append_semantic_index",
    "compute_scan_metrics",
    "load_daemon_state",
    "prune_stale_daemon_file_entries",
    "read_pid_file",
    "resolve_graph_root",
    "save_daemon_state",
    "start_daemon_detached",
    "start_daemon_foreground",
    "stop_daemon",
    "sync_daemon_state_from_env",
]

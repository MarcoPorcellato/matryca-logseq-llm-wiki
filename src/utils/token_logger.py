"""Token-accurate JSONL logger for local LLM operations (Matryca Plumber)."""

from __future__ import annotations

import json
import os
import signal
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..utils.console_sanitize import sanitize_for_console

OperationType = Literal[
    "Concept Indexing",
    "Semantic Linting",
    "Structural Refactoring",
    "Health Check",
    "Daemon Lifecycle",
    "Context Compression",
]

DEFAULT_LOG_PATH = Path("logs") / "matryca_plumber_ops.log"
_LOG_TAG_PREFIXES = (
    "[COMPRESSION LLM FAULT]",
    "[COMPRESSION WARN]",
    "[CONTEXT COMPRESSION EVENT]",
    "[STRUCTURAL LINT WARN]",
    "[DAEMON SHUTDOWN RECEIVED]",
)
_MAX_PROMPT_CHARS = 12_000
_MAX_RESPONSE_CHARS = 12_000

_write_guard = threading.Lock()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def resolve_plumber_log_path() -> Path:
    """Resolve the Matryca Plumber ops log path from env or the default JSONL location."""
    override = os.environ.get("MATRYCA_PLUMBER_LOG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_LOG_PATH


def _default_log_path() -> Path:
    return resolve_plumber_log_path()


def format_activity_summary(payload: dict[str, Any]) -> str:
    """Render one JSONL ops-log record for the Plumber TUI activity feed."""
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        if message.startswith(_LOG_TAG_PREFIXES):
            target = payload.get("target_file")
            if target:
                target_name = sanitize_for_console(Path(str(target)).name)
                return sanitize_for_console(f"{message[:96]} · {target_name}")
            return sanitize_for_console(message[:120])
        return sanitize_for_console(message[:120])

    op = str(payload.get("operation", "?"))
    target = sanitize_for_console(Path(str(payload.get("target_file", "?"))).name)
    prompt_tokens = int(payload.get("prompt_tokens", 0) or 0)
    completion_tokens = int(payload.get("completion_tokens", 0) or 0)
    latency = payload.get("latency_seconds", 0)
    return sanitize_for_console(
        f"{op} · {target} · p={prompt_tokens} c={completion_tokens} · {latency}s"
    )


@dataclass
class TokenLogEntry:
    """One LLM transaction record."""

    timestamp: str
    target_file: str
    operation: OperationType
    prompt_tokens: int
    completion_tokens: int
    prompt: str
    response: str
    latency_seconds: float
    model: str = ""
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "target_file": self.target_file,
            "operation": self.operation,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "prompt": self.prompt,
            "response": self.response,
            "latency_seconds": round(self.latency_seconds, 4),
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class TokenLogger:
    """Append-only JSONL logger with in-memory session counters."""

    log_path: Path = field(default_factory=_default_log_path)
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0

    def log_turn(
        self,
        *,
        target_file: str | Path,
        operation: OperationType,
        prompt_tokens: int,
        completion_tokens: int,
        prompt: str,
        response: str,
        latency_seconds: float,
        model: str = "",
        ok: bool = True,
        error: str | None = None,
    ) -> TokenLogEntry:
        """Persist one LLM turn and update session token totals."""
        entry = TokenLogEntry(
            timestamp=datetime.now(tz=UTC).isoformat(),
            target_file=sanitize_for_console(str(target_file)),
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt=_truncate(sanitize_for_console(prompt), _MAX_PROMPT_CHARS),
            response=_truncate(sanitize_for_console(response), _MAX_RESPONSE_CHARS),
            latency_seconds=latency_seconds,
            model=model,
            ok=ok,
            error=sanitize_for_console(error) if error else None,
        )
        self.session_prompt_tokens += prompt_tokens
        self.session_completion_tokens += completion_tokens
        self._append(entry)
        return entry

    def _append(self, entry: TokenLogEntry) -> None:
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with _write_guard:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def tail_lines(self, count: int = 5) -> list[str]:
        """Return the last ``count`` non-empty log lines (raw JSONL).

        Reads backward from the file end in fixed-size blocks so memory use
        stays bounded regardless of log file size. Each call opens, reads, and
        closes the log file independently so the TUI always sees the latest
        on-disk tail without reusing a stale file pointer or cached offset.
        """
        if count <= 0:
            return []

        log_path = self.log_path
        try:
            if not log_path.is_file():
                return []
        except OSError:
            return []

        block_size = 8192
        collected: list[bytes] = []
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                if position == 0:
                    return []

                pending = b""
                while position > 0 and len(collected) < count:
                    read_size = min(block_size, position)
                    position -= read_size
                    handle.seek(position)
                    pending = handle.read(read_size) + pending
                    parts = pending.split(b"\n")
                    pending = parts[0]
                    for part in reversed(parts[1:]):
                        if part.strip():
                            collected.append(part)
                            if len(collected) >= count:
                                break

                if len(collected) < count and pending.strip():
                    collected.append(pending)
        except OSError:
            return []

        return [line.decode("utf-8", errors="replace") for line in reversed(collected[:count])]

    def tail_summaries(self, count: int = 5) -> list[str]:
        """Human-readable summaries for the TUI activity feed."""
        summaries: list[str] = []
        for raw in self.tail_lines(count):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                summaries.append(sanitize_for_console(raw[:120]))
                continue
            if not isinstance(payload, dict):
                summaries.append(sanitize_for_console(raw[:120]))
                continue
            summaries.append(format_activity_summary(payload))
        return summaries

    def tail_activity_summaries(self, count: int = 5) -> list[str]:
        """Like :meth:`tail_summaries` but skips repetitive daemon lifecycle noise."""
        _lifecycle_noise = (
            "[DAEMON SHUTDOWN RECEIVED]",
            "[DAEMON LIFECYCLE]",
        )
        summaries: list[str] = []
        for raw in self.tail_lines(max(count * 8, count)):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                line = sanitize_for_console(raw[:120])
            else:
                if not isinstance(payload, dict):
                    line = sanitize_for_console(raw[:120])
                else:
                    line = format_activity_summary(payload)
            if any(tag in line for tag in _lifecycle_noise):
                continue
            summaries.append(line)
            if len(summaries) >= count:
                break
        return summaries

    def log_compression_event(
        self,
        *,
        initial_tokens: int,
        post_tokens: int,
        latency_seconds: float,
        compression_ratio: float,
        messages_before: int,
        messages_after: int,
        model: str = "",
    ) -> None:
        """Persist a ``[CONTEXT COMPRESSION EVENT]`` record to the ops log."""
        summary = (
            "[CONTEXT COMPRESSION EVENT] "
            f"initial={initial_tokens} post={post_tokens} "
            f"ratio={compression_ratio:.4f} latency={latency_seconds:.4f}s "
            f"messages={messages_before}->{messages_after}"
        )
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": "Context Compression",
            "message": summary,
            "initial_tokens": initial_tokens,
            "post_tokens": post_tokens,
            "compression_ratio": round(compression_ratio, 4),
            "latency_seconds": round(latency_seconds, 4),
            "messages_before": messages_before,
            "messages_after": messages_after,
            "model": model,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _write_guard:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def log_compression_warning(self, message: str, *, model: str = "") -> None:
        """Persist a compression fallback record to the ops log."""
        if message.startswith(("[COMPRESSION WARN]", "[COMPRESSION LLM FAULT]")):
            warn_text = message
        else:
            warn_text = f"[COMPRESSION WARN] {message}"
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": "Context Compression",
            "message": warn_text,
            "model": model,
            "fallback": True,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _write_guard:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def log_daemon_shutdown(self, signum: int) -> None:
        """Persist a critical ``[DAEMON SHUTDOWN RECEIVED]`` record to the ops log."""
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": "Daemon Lifecycle",
            "message": "[DAEMON SHUTDOWN RECEIVED] Executing safe evacuation...",
            "signal": sig_name,
            "critical": True,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _write_guard:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def session_token_totals_from_log(self, *, since_iso: str | None = None) -> tuple[int, int]:
        """Sum prompt/completion tokens from JSONL records after a session boundary."""
        if not self.log_path.is_file():
            return 0, 0

        cutoff = since_iso
        raw_lines: list[str] = []
        try:
            raw_lines = self.tail_lines(2000)
        except OSError:
            return 0, 0
        if not raw_lines:
            return 0, 0

        if cutoff is None:
            for raw in reversed(raw_lines):
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                message = str(payload.get("message", ""))
                if message.startswith("[DAEMON SHUTDOWN RECEIVED]"):
                    cutoff = str(payload.get("timestamp", ""))
                    break

        prompt_total = 0
        completion_total = 0
        for raw in raw_lines:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            timestamp = str(payload.get("timestamp", ""))
            if cutoff and timestamp <= cutoff:
                continue
            prompt_total += int(payload.get("prompt_tokens", 0) or 0)
            completion_total += int(payload.get("completion_tokens", 0) or 0)
        return prompt_total, completion_total

    def log_structural_lint_warning(
        self,
        *,
        target_file: str | Path,
        message: str,
        malformed_refs: list[str] | None = None,
    ) -> None:
        """Persist a ``[STRUCTURAL LINT WARN]`` record to the ops log."""
        warn_text = (
            message
            if message.startswith("[STRUCTURAL LINT WARN]")
            else f"[STRUCTURAL LINT WARN] {message}"
        )
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "operation": "Health Check",
            "target_file": str(target_file),
            "message": warn_text,
            "malformed_refs": malformed_refs or [],
            "fallback": True,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _write_guard:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


__all__ = [
    "DEFAULT_LOG_PATH",
    "OperationType",
    "TokenLogEntry",
    "TokenLogger",
    "format_activity_summary",
    "resolve_plumber_log_path",
]

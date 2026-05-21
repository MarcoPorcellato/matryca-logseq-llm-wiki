"""Token-accurate JSONL logger for local LLM operations (Matryca Plumber)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

OperationType = Literal[
    "Concept Indexing",
    "Semantic Linting",
    "Structural Refactoring",
    "Health Check",
    "Daemon Lifecycle",
    "Context Compression",
]

DEFAULT_LOG_PATH = Path("logs") / "matryca_plumber_ops.log"
_MAX_PROMPT_CHARS = 12_000
_MAX_RESPONSE_CHARS = 12_000

_write_guard = threading.Lock()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _default_log_path() -> Path:
    override = os.environ.get("MATRYCA_PLUMBER_LOG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_LOG_PATH


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
            target_file=str(target_file),
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt=_truncate(prompt, _MAX_PROMPT_CHARS),
            response=_truncate(response, _MAX_RESPONSE_CHARS),
            latency_seconds=latency_seconds,
            model=model,
            ok=ok,
            error=error,
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
        """Return the last ``count`` non-empty log lines (raw JSONL)."""
        if not self.log_path.is_file():
            return []
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-count:]

    def tail_summaries(self, count: int = 5) -> list[str]:
        """Human-readable summaries for the TUI activity feed."""
        summaries: list[str] = []
        for raw in self.tail_lines(count):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                summaries.append(raw[:120])
                continue
            op = payload.get("operation", "?")
            target = Path(str(payload.get("target_file", "?"))).name
            pt = payload.get("prompt_tokens", 0)
            ct = payload.get("completion_tokens", 0)
            lat = payload.get("latency_seconds", 0)
            summaries.append(f"{op} · {target} · p={pt} c={ct} · {lat}s")
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
        """Persist a ``[COMPRESSION WARN]`` fallback record to the ops log."""
        warn_text = (
            message if message.startswith("[COMPRESSION WARN]") else f"[COMPRESSION WARN] {message}"
        )
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
]

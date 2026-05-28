"""Per-page stable LLM prefix for KV-cache reuse across cognitive tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..graph.alias_index import AliasIndex, format_alias_index_for_prompt
from .llm_context_payload import PayloadSource, prepare_llm_context_payload
from .plumber_config import PlumberLintConfig, _env_int
from .prompt_layout import CANONICAL_TASK_HEADER, normalize_stable_text

_STABLE_CONTENT_HEADER = "Page content:\n"

_ALIAS_PROMPT_MAX_CHARS_ENV = "MATRYCA_ALIAS_PROMPT_MAX_CHARS"
_DEFAULT_ALIAS_PROMPT_MAX_CHARS = 2048


class PrefixDriftError(RuntimeError):
    """Raised when the stable KV prefix bytes changed before an LLM call."""


def alias_prompt_max_chars() -> int:
    return max(256, _env_int(_ALIAS_PROMPT_MAX_CHARS_ENV, _DEFAULT_ALIAS_PROMPT_MAX_CHARS))


def _cap_alias_footer(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 32] + "\n... [alias index truncated]"


def _prefix_digest(system_text: str, stable_user_prefix: str) -> str:
    canonical = (
        normalize_stable_text(system_text) + "\x1e" + normalize_stable_text(stable_user_prefix)
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenPromptPrefix:
    """Immutable [system] + stable user prefix (no task tail) for KV-cache reuse."""

    system_text: str
    stable_user_prefix: str
    prefix_sha256: str

    @staticmethod
    def build(*, system_text: str, stable_user_prefix: str) -> FrozenPromptPrefix:
        stable = normalize_stable_text(stable_user_prefix)
        return FrozenPromptPrefix(
            system_text=normalize_stable_text(system_text),
            stable_user_prefix=stable,
            prefix_sha256=_prefix_digest(system_text, stable),
        )

    def verify_unchanged(self) -> None:
        current = _prefix_digest(self.system_text, self.stable_user_prefix)
        if current != self.prefix_sha256:
            raise PrefixDriftError(
                f"stable prefix drifted (expected {self.prefix_sha256[:16]}, got {current[:16]})",
            )

    def append_task(self, task_instruction: str) -> str:
        """Append task tail without mutating the frozen stable prefix bytes."""
        self.verify_unchanged()
        task = task_instruction.strip()
        if not task:
            return self.stable_user_prefix
        return f"{self.stable_user_prefix}{CANONICAL_TASK_HEADER}{task}"


@dataclass(frozen=True, slots=True)
class PagePromptSession:
    """Stable page block + fingerprint for consecutive LLM turns on one file."""

    frozen: FrozenPromptPrefix
    page_fingerprint: str
    payload_source: PayloadSource

    @property
    def stable_system(self) -> str:
        return self.frozen.system_text

    @property
    def stable_page_block(self) -> str:
        return self.frozen.stable_user_prefix

    @property
    def prefix_sha256(self) -> str:
        return self.frozen.prefix_sha256

    def build_task_prompt(self, task_instruction: str) -> str:
        """Return cache-aligned user prompt: stable page block + dynamic task tail."""
        return self.frozen.append_task(task_instruction)


def page_fingerprint(
    *,
    page_path: Path | None,
    payload: str,
) -> str:
    """Hash path + mtime + payload length for observability."""
    mtime_ns = 0
    rel = ""
    if page_path is not None:
        rel = page_path.name
        try:
            mtime_ns = page_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
    raw = f"{rel}:{mtime_ns}:{len(payload)}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def build_page_prompt_session(
    graph_root: Path,
    page_title: str,
    content: str,
    *,
    config: PlumberLintConfig,
    stable_system: str,
    page_path: Path | None = None,
    alias_index: AliasIndex | None = None,
) -> PagePromptSession:
    """Build one session per page before running cognitive modules."""
    payload, source = prepare_llm_context_payload(
        graph_root,
        page_title,
        content,
        config=config,
    )
    stable_body = normalize_stable_text(payload)
    if alias_index is not None:
        alias_text = format_alias_index_for_prompt(alias_index, page_content=content)
        if alias_text.strip():
            footer = _cap_alias_footer(
                f"\n\nAliasIndex (resolve wikilinks against this map):\n{alias_text}",
                max_chars=alias_prompt_max_chars(),
            )
            stable_body = f"{stable_body.rstrip()}{footer}"
    frozen = FrozenPromptPrefix.build(
        system_text=stable_system,
        stable_user_prefix=f"{_STABLE_CONTENT_HEADER}{stable_body}",
    )
    return PagePromptSession(
        frozen=frozen,
        page_fingerprint=page_fingerprint(page_path=page_path, payload=stable_body),
        payload_source=source,
    )


__all__ = [
    "FrozenPromptPrefix",
    "PagePromptSession",
    "PrefixDriftError",
    "alias_prompt_max_chars",
    "build_page_prompt_session",
    "page_fingerprint",
]

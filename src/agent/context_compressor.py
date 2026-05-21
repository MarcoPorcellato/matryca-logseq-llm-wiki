"""Active context compression for Matryca Plumber (Ermes Agent–inspired epistemic condensation)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from loguru import logger

from .prompt_constraints import finalize_system_prompt

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_MESSAGE_OVERHEAD_TOKENS = 4
_PRESERVE_TAIL_TURNS = 2
# Conservative buffer: heuristic underestimates code/CJK-heavy prompts ~10-15%.
TOKEN_ESTIMATE_SAFETY_MULTIPLIER = 1.12
MAX_EXECUTION_HISTORY_MESSAGES = 48

_COMPRESSION_SYSTEM_INSTRUCTIONS = (
    "You are Matryca Plumber Context Compressor. Condense maintenance-session history "
    "into dense markdown titled '## Consolidated Epistemic State'. Preserve: page titles "
    "processed, MARPA domains assigned, lint corrections applied, entity merges, dangling "
    "links healed, block UUIDs referenced, errors/skips, and open tasks. "
    "Use bullet lists and short headings. Omit filler. Output markdown only."
)

COMPRESSION_SYSTEM_PROMPT = finalize_system_prompt(_COMPRESSION_SYSTEM_INSTRUCTIONS)


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class CompressionEvent:
    """Metrics captured when a context compression pass executes."""

    initial_tokens: int
    post_tokens: int
    latency_seconds: float
    messages_before: int
    messages_after: int

    @property
    def compression_ratio(self) -> float:
        if self.initial_tokens == 0:
            return 1.0
        return round(self.post_tokens / self.initial_tokens, 4)


def estimate_tokens(text: str) -> int:
    """Fast token estimate tuned for Qwen/Gemma-style byte-level BPE (~local, no deps)."""
    if not text:
        return 0
    cjk_chars = len(_CJK_RE.findall(text))
    other_chars = len(text) - cjk_chars
    # Qwen/Gemma: ~1.2-1.4 tokens per CJK char; ~3.8 Latin chars per token.
    raw = max(1, int(cjk_chars * 1.35 + other_chars / 3.8))
    return max(1, int(raw * TOKEN_ESTIMATE_SAFETY_MULTIPLIER))


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """Sum estimated tokens for a chat message list including per-message overhead."""
    total = 0
    for msg in messages:
        total += _MESSAGE_OVERHEAD_TOKENS + estimate_tokens(msg["content"])
    return total


def serialize_history_for_compression(messages: list[ChatMessage]) -> str:
    """Format intermediate turns into a single block for the compression LLM."""
    parts: list[str] = []
    for idx, msg in enumerate(messages, start=1):
        parts.append(f"#### Turn {idx} ({msg['role']})\n{msg['content']}")
    return "\n\n".join(parts)


def build_compression_user_prompt(history_text: str, *, target_tokens: int) -> str:
    """Build the user payload for the compression call."""
    return (
        "Compress the Matryca Plumber maintenance history below into dense markdown under "
        f"`## Consolidated Epistemic State`. Target <= {target_tokens} estimated tokens. "
        "Preserve factual details; do not invent events.\n\n"
        f"{history_text}"
    )


def _split_for_compression(
    messages: list[ChatMessage],
    *,
    preserve_tail_turns: int,
) -> tuple[ChatMessage, list[ChatMessage], list[ChatMessage], ChatMessage | None] | None:
    """Split messages into system, compressible middle, preserved tail, and current user."""
    if not messages or messages[0]["role"] != "system":
        return None

    system = messages[0]
    rest = messages[1:]
    if not rest:
        return system, [], [], None

    current_user: ChatMessage | None = None
    history = rest
    if rest[-1]["role"] == "user":
        current_user = rest[-1]
        history = rest[:-1]

    tail_size = preserve_tail_turns * 2
    if len(history) <= tail_size:
        return None

    middle = history[:-tail_size]
    tail = history[-tail_size:]
    if not middle:
        return None

    return system, middle, tail, current_user


def truncate_messages_to_budget(
    messages: list[ChatMessage],
    *,
    target: int,
    preserve_tail_turns: int = _PRESERVE_TAIL_TURNS,
) -> list[ChatMessage]:
    """Drop compressible middle history when LLM compression is unavailable."""
    split = _split_for_compression(messages, preserve_tail_turns=preserve_tail_turns)
    if split is None:
        return messages

    system, _middle, tail, current_user = split
    while True:
        truncated: list[ChatMessage] = [system, *tail]
        if current_user is not None:
            truncated.append(current_user)
        if estimate_messages_tokens(truncated) <= target or len(tail) <= 2:
            return truncated
        tail = tail[2:]


def condense_messages(
    messages: list[ChatMessage],
    *,
    trigger: int,
    target: int,
    compress_fn: Callable[[str], str],
    preserve_tail_turns: int = _PRESERVE_TAIL_TURNS,
    warn_fn: Callable[[str], None] | None = None,
) -> tuple[list[ChatMessage], CompressionEvent | None]:
    """Compress intermediate history when token count exceeds ``trigger``."""
    initial_tokens = estimate_messages_tokens(messages)
    if initial_tokens <= trigger:
        return messages, None

    split = _split_for_compression(messages, preserve_tail_turns=preserve_tail_turns)
    if split is None:
        return messages, None

    system, middle, tail, current_user = split
    started = time.perf_counter()
    history_text = serialize_history_for_compression(middle)
    try:
        summary = compress_fn(build_compression_user_prompt(history_text, target_tokens=target))
    except Exception as exc:  # noqa: BLE001 - fall back so daemon keeps running
        latency = time.perf_counter() - started
        warn_msg = (
            "[COMPRESSION WARN] "
            f"compression LLM failed after {latency:.4f}s: {exc}; "
            "falling back to truncation/uncompressed history"
        )
        logger.warning(warn_msg)
        if warn_fn is not None:
            warn_fn(warn_msg)
        truncated = truncate_messages_to_budget(
            messages,
            target=target,
            preserve_tail_turns=preserve_tail_turns,
        )
        if truncated != messages:
            return truncated, None
        return messages, None
    latency = time.perf_counter() - started

    consolidated: list[ChatMessage] = [
        {
            "role": "user",
            "content": (
                "## Consolidated Epistemic State\n\n"
                f"{summary.strip()}\n\n"
                "<!-- matryca-compressed-history -->"
            ),
        },
        {
            "role": "assistant",
            "content": "Consolidated maintenance context acknowledged.",
        },
    ]

    compressed: list[ChatMessage] = [system, *consolidated, *tail]
    if current_user is not None:
        compressed.append(current_user)

    post_tokens = estimate_messages_tokens(compressed)
    event = CompressionEvent(
        initial_tokens=initial_tokens,
        post_tokens=post_tokens,
        latency_seconds=latency,
        messages_before=len(messages),
        messages_after=len(compressed),
    )
    return compressed, event


def extract_persisted_history(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Return non-system history excluding the trailing current user prompt."""
    body = messages[1:]
    if body and body[-1]["role"] == "user":
        return list(body[:-1])
    return list(body)


__all__ = [
    "COMPRESSION_SYSTEM_PROMPT",
    "ChatMessage",
    "CompressionEvent",
    "MAX_EXECUTION_HISTORY_MESSAGES",
    "TOKEN_ESTIMATE_SAFETY_MULTIPLIER",
    "build_compression_user_prompt",
    "condense_messages",
    "estimate_messages_tokens",
    "estimate_tokens",
    "extract_persisted_history",
    "serialize_history_for_compression",
    "truncate_messages_to_budget",
]

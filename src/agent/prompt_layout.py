"""Cache-aligned prompt layout for LM Studio / llama.cpp KV prefix reuse."""

from __future__ import annotations

_CONTENT_HEADER = "Page content:\n"
CANONICAL_TASK_HEADER = "\n\nTask:\n"


def normalize_stable_text(text: str) -> str:
    """Normalize line endings once; do not strip stable page bodies."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_cache_aligned_prompt(*, content: str, task_instruction: str) -> str:
    """Place stable page content first and dynamic task instructions last.

    Layout: ``[FILE_CONTENT] + [DYNAMIC_TASK_INSTRUCTION]`` so consecutive LLM
    turns on the same file share the heaviest token prefix for prompt caching.
    """
    body = normalize_stable_text(content)
    task = task_instruction.strip()
    if not body.strip():
        return task
    if not task:
        return f"{_CONTENT_HEADER}{body}"
    return f"{_CONTENT_HEADER}{body}{CANONICAL_TASK_HEADER}{task}"


__all__ = [
    "CANONICAL_TASK_HEADER",
    "build_cache_aligned_prompt",
    "normalize_stable_text",
]

"""Shared cross-lingual constraints for Matryca Brain LLM system prompts."""

from __future__ import annotations

CROSS_LINGUAL_OUTPUT_CONSTRAINT = (
    "\n\n[CRITICAL LANGUAGE CONSTRAINT]\n"
    "Analyze the language of the provided input document. You MUST generate all "
    "human-readable output text fields (such as 'summary', 'reason', 'corrected_text') "
    "in that EXACT same language. Do not translate the user's content into English. "
    "System-level keys, tags, and properties (like 'type:: area') must remain in their "
    "standardized format."
)


def finalize_system_prompt(instructions: str) -> str:
    """Append the mandatory cross-lingual output constraint to a system prompt."""
    text = instructions.rstrip()
    if "[CRITICAL LANGUAGE CONSTRAINT]" in text:
        return text
    return text + CROSS_LINGUAL_OUTPUT_CONSTRAINT


__all__ = ["CROSS_LINGUAL_OUTPUT_CONSTRAINT", "finalize_system_prompt"]

"""Shared cross-lingual constraints for Matryca Plumber LLM system prompts."""

from __future__ import annotations

ALIAS_FIRST_LINK_CONSTRAINT = (
    "Before suggesting any new topic link, you must verify if the concept already exists "
    "as a canonical page or an alias inside the AliasIndex. You must aggressively prefer "
    "linking to an existing canonical node or recommending an alias:: property over "
    "creating a new physical markdown file. New page files are an absolute last resort."
)


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


__all__ = [
    "ALIAS_FIRST_LINK_CONSTRAINT",
    "CROSS_LINGUAL_OUTPUT_CONSTRAINT",
    "finalize_system_prompt",
]

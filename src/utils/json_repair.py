"""Lenient JSON repair for malformed local LLM structured outputs."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_DOUBLE_ESCAPED_QUOTE_RUN_RE = re.compile(r'\\""\s*,\s*\\"')
_LEAKED_JSON_KEY_IN_STRING_RE = re.compile(
    r'(?<=\\"),\s*\\"(?=[a-zA-Z_][a-zA-Z0-9_]*\\"\s*:)',
)
_TRAILING_GARBAGE_RE = re.compile(r"\}\s*[\{\[\"].*$", re.DOTALL)
_DEGENERATE_LITERAL_BACKSLASH_N_RUN_RE = re.compile(r"(?:\\n){16,}")
_DEGENERATE_LITERAL_BACKSLASH_T_RUN_RE = re.compile(r"(?:\\t){16,}")
_DEGENERATE_LITERAL_BACKSLASH_QUOTE_RUN_RE = re.compile(r'(?:\\"){32,}')
_GEMMA_LEAKED_LITERAL_NL_BEFORE_KEY_RE = re.compile(r"([{,]\s*)\\n\s*\\\"")
_DEGENERATE_REAL_NEWLINE_RUN_RE = re.compile(r"\n{24,}")
_DEGENERATE_REAL_SPACE_RUN_RE = re.compile(r" {48,}")
_DEGENERATE_REAL_TAB_RUN_RE = re.compile(r"\t{16,}")
_FALLBACK_UNBALANCED_JSON_CHARS = 8192


def max_consecutive_literal_backslash_n(text: str) -> int:
    """Longest run of the two-char sequence backslash + n in ``text``."""
    best = 0
    current = 0
    index = 0
    while index < len(text):
        if text.startswith("\\n", index):
            current += 1
            index += 2
        else:
            best = max(best, current)
            current = 0
            index += 1
    return max(best, current)


def strip_code_fence(raw: str) -> str:
    """Remove optional markdown JSON code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = _CODE_FENCE_RE.sub("", text).strip()
    return text


def _scan_balanced_end(
    text: str,
    start: int,
    *,
    open_char: str,
    close_char: str,
) -> int | None:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_balanced_object_end(text: str, start: int) -> int | None:
    return _scan_balanced_end(text, start, open_char="{", close_char="}")


def _find_balanced_array_end(text: str, start: int) -> int | None:
    return _scan_balanced_end(text, start, open_char="[", close_char="]")


def _recover_unbalanced_json_slice(text: str, *, open_char: str, close_char: str) -> str:
    """Salvage prefix when the model never closed braces (truncation or tail loop)."""
    collapsed = collapse_all_degenerate_llm_runs(text)
    start = collapsed.find(open_char)
    if start == -1:
        return collapsed
    close_idx = collapsed.rfind(close_char)
    if close_idx > start:
        return collapsed[start : close_idx + 1]
    if len(collapsed) - start > _FALLBACK_UNBALANCED_JSON_CHARS:
        logger.warning(
            "Unbalanced JSON slice capped at {} chars (no closing {})",
            _FALLBACK_UNBALANCED_JSON_CHARS,
            close_char,
        )
        return collapsed[start : start + _FALLBACK_UNBALANCED_JSON_CHARS]
    return collapsed[start:]


def extract_json_object(raw: str) -> str:
    """Return the outermost JSON object slice from free-form LLM text."""
    text = strip_code_fence(raw)
    start = text.find("{")
    if start == -1:
        return text
    end = _find_balanced_object_end(text, start)
    if end is not None:
        return text[start : end + 1]
    return _recover_unbalanced_json_slice(text[start:], open_char="{", close_char="}")


def extract_json_array(raw: str) -> str:
    """Return the outermost JSON array slice from free-form LLM text."""
    text = strip_code_fence(raw)
    start = text.find("[")
    if start == -1:
        return text
    end = _find_balanced_array_end(text, start)
    if end is not None:
        return text[start : end + 1]
    return _recover_unbalanced_json_slice(text[start:], open_char="[", close_char="]")


def extract_json_payload_regex(raw: str) -> str:
    """Extract the first JSON value using balanced delimiters (never greedy ``.*``)."""
    text = strip_code_fence(raw)
    if "{" in text:
        balanced = extract_json_object(text)
        if balanced.startswith("{"):
            return balanced
    if "[" in text:
        balanced = extract_json_array(text)
        if balanced.startswith("["):
            return balanced
    return text


def fix_double_escaped_quote_runs(text: str) -> str:
    """Collapse Gemma-style ``\\"", \\"`` leakage back into string continuations."""
    text = _DOUBLE_ESCAPED_QUOTE_RUN_RE.sub('\\", \\"', text)
    return _LEAKED_JSON_KEY_IN_STRING_RE.sub(lambda _match: ', \\"', text)


def fix_gemma_leaked_literal_newline_before_keys(text: str) -> str:
    """Normalize Gemma ``\\n  \\"key`` leakage into valid ``"key`` openings."""
    return _GEMMA_LEAKED_LITERAL_NL_BEFORE_KEY_RE.sub(r'\1"', text)


def collapse_degenerate_literal_backslash_n_runs(text: str) -> str:
    """Remove long runs of literal ``\\n`` pairs (Gemma token-loop degeneration)."""
    return _DEGENERATE_LITERAL_BACKSLASH_N_RUN_RE.sub("", text)


def collapse_degenerate_literal_backslash_t_runs(text: str) -> str:
    """Remove long runs of literal ``\\t`` pairs."""
    return _DEGENERATE_LITERAL_BACKSLASH_T_RUN_RE.sub("", text)


def collapse_degenerate_literal_backslash_quote_runs(text: str) -> str:
    """Remove long runs of literal ``\\"`` pairs."""
    return _DEGENERATE_LITERAL_BACKSLASH_QUOTE_RUN_RE.sub("", text)


def collapse_degenerate_whitespace_runs(text: str) -> str:
    """Collapse real newline / space / tab repetition loops in prose completions."""
    text = _DEGENERATE_REAL_NEWLINE_RUN_RE.sub("\n\n", text)
    text = _DEGENERATE_REAL_SPACE_RUN_RE.sub(" ", text)
    return _DEGENERATE_REAL_TAB_RUN_RE.sub("\t", text)


def collapse_all_degenerate_llm_runs(text: str) -> str:
    """Apply every literal and whitespace degeneration collapse (JSON + prose)."""
    text = collapse_degenerate_literal_backslash_n_runs(text)
    text = collapse_degenerate_literal_backslash_t_runs(text)
    text = collapse_degenerate_literal_backslash_quote_runs(text)
    return collapse_degenerate_whitespace_runs(text)


def _resolve_prose_max_chars() -> int:
    raw = os.environ.get("MATRYCA_LLM_PROSE_COMPLETION_MAX_CHARS", "12000").strip()
    try:
        return max(2_000, min(32_000, int(raw)))
    except ValueError:
        return 12_000


def sanitize_prose_llm_completion(raw: str, *, max_chars: int | None = None) -> str:
    """Trim prose/markdown completions before they enter Ermes execution history."""
    cap = max_chars if max_chars is not None else _resolve_prose_max_chars()
    text = strip_code_fence(raw.strip())
    text = collapse_all_degenerate_llm_runs(text)
    if len(text) > cap:
        logger.warning(
            "Truncated prose LLM completion from {} to {} chars (degeneration cap)",
            len(text),
            cap,
        )
        text = text[:cap].rstrip() + "\n\n...[truncation: prose completion cap]"
    return text


def sanitize_llm_completion_text(raw: str) -> str:
    """Trim post-JSON tail and apply Gemma-specific repairs before validation."""
    text = extract_json_object(raw)
    text = collapse_degenerate_literal_backslash_n_runs(text)
    text = collapse_degenerate_literal_backslash_t_runs(text)
    text = collapse_degenerate_literal_backslash_quote_runs(text)
    text = fix_gemma_leaked_literal_newline_before_keys(text)
    text = fix_double_escaped_quote_runs(text)
    return text


def sanitize_llm_history_turn(content: str) -> str:
    """Sanitize assistant/user turns persisted in Ermes history."""
    stripped = content.strip()
    if not stripped:
        return content
    if stripped.startswith("{"):
        return sanitize_llm_completion_text(stripped)
    if stripped.startswith("["):
        return extract_json_array(stripped)
    return sanitize_prose_llm_completion(stripped)


def strip_trailing_json_garbage(text: str) -> str:
    """Remove trailing tokens such as ``} { "`` after the root object."""
    trimmed = text.rstrip()
    while True:
        match = _TRAILING_GARBAGE_RE.search(trimmed)
        if match is None:
            break
        trimmed = trimmed[: match.start()] + "}"
    return trimmed.rstrip()


def balance_json_brackets(text: str) -> str:
    """Append missing closing brackets/braces for truncated JSON payloads."""
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    suffix = "]" * max(open_brackets, 0) + "}" * max(open_braces, 0)
    return text + suffix


def repair_llm_json(raw: str) -> str:
    """Apply aggressive sanitization before ``json.loads``."""
    text = extract_json_payload_regex(raw)
    text = collapse_degenerate_literal_backslash_n_runs(text)
    text = collapse_degenerate_literal_backslash_t_runs(text)
    text = collapse_degenerate_literal_backslash_quote_runs(text)
    text = fix_gemma_leaked_literal_newline_before_keys(text)
    text = fix_double_escaped_quote_runs(text)
    text = strip_trailing_json_garbage(text)
    text = balance_json_brackets(text)
    return text


def loads_repaired_json(raw: str) -> object:
    """Parse JSON after repair, with one extra trim pass on decode failure."""
    repaired = repair_llm_json(raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        trimmed = strip_trailing_json_garbage(repaired)
        trimmed = balance_json_brackets(trimmed)
        return json.loads(trimmed)


def parse_llm_json[T: BaseModel](raw: str, model: type[T]) -> T:
    """Repair malformed LLM JSON and validate through a Pydantic model."""
    payload = loads_repaired_json(raw)
    return model.model_validate(payload)


def safe_parse_llm_json_dict(raw: str, *, context: str = "llm") -> dict[str, Any]:
    """Parse repaired LLM JSON to a dict; return ``{}`` and log on failure."""
    try:
        payload = loads_repaired_json(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("[LLM JSON FAULT] {} payload decode failed: {}", context, exc)
        return {}
    if isinstance(payload, dict):
        return payload
    logger.warning("[LLM JSON FAULT] {} payload is not a JSON object", context)
    return {}


def try_parse_llm_json[T: BaseModel](
    raw: str,
    model: type[T],
    *,
    context: str = "llm",
) -> T | None:
    """Best-effort structured parse; return ``None`` instead of raising."""
    payload = safe_parse_llm_json_dict(raw, context=context)
    if not payload:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        logger.warning("[LLM JSON FAULT] {} schema validation failed: {}", context, exc)
        return None


__all__ = [
    "balance_json_brackets",
    "collapse_all_degenerate_llm_runs",
    "collapse_degenerate_literal_backslash_n_runs",
    "collapse_degenerate_literal_backslash_quote_runs",
    "collapse_degenerate_literal_backslash_t_runs",
    "collapse_degenerate_whitespace_runs",
    "extract_json_array",
    "extract_json_object",
    "extract_json_payload_regex",
    "fix_double_escaped_quote_runs",
    "fix_gemma_leaked_literal_newline_before_keys",
    "loads_repaired_json",
    "max_consecutive_literal_backslash_n",
    "parse_llm_json",
    "repair_llm_json",
    "safe_parse_llm_json_dict",
    "sanitize_llm_completion_text",
    "sanitize_llm_history_turn",
    "sanitize_prose_llm_completion",
    "strip_code_fence",
    "strip_trailing_json_garbage",
    "try_parse_llm_json",
]

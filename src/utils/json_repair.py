"""Lenient JSON repair for malformed local LLM structured outputs."""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_JSON_PAYLOAD_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_DOUBLE_ESCAPED_QUOTE_RUN_RE = re.compile(r'\\""\s*,\s*\\"')
_LEAKED_JSON_KEY_IN_STRING_RE = re.compile(
    r'(?<=\\"),\s*\\"(?=[a-zA-Z_][a-zA-Z0-9_]*\\"\s*:)',
)
_TRAILING_GARBAGE_RE = re.compile(r"\}\s*[\{\[\"].*$", re.DOTALL)


def strip_code_fence(raw: str) -> str:
    """Remove optional markdown JSON code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = _CODE_FENCE_RE.sub("", text).strip()
    return text


def _find_balanced_object_end(text: str, start: int) -> int | None:
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def extract_json_payload_regex(raw: str) -> str:
    """Surgically extract the first JSON object or array from conversational LLM text."""
    text = strip_code_fence(raw)
    match = _JSON_PAYLOAD_RE.search(text)
    if match is not None:
        return match.group(1).strip()
    return extract_json_object(text)


def extract_json_object(raw: str) -> str:
    """Return the outermost JSON object slice from free-form LLM text."""
    text = strip_code_fence(raw)
    start = text.find("{")
    if start == -1:
        return text
    end = _find_balanced_object_end(text, start)
    if end is not None:
        return text[start : end + 1]
    return text[start:]


def fix_double_escaped_quote_runs(text: str) -> str:
    """Collapse Gemma-style ``\\"", \\"`` leakage back into string continuations."""
    text = _DOUBLE_ESCAPED_QUOTE_RUN_RE.sub('\\", \\"', text)
    return _LEAKED_JSON_KEY_IN_STRING_RE.sub(lambda _match: ', \\"', text)


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
    "extract_json_object",
    "extract_json_payload_regex",
    "fix_double_escaped_quote_runs",
    "loads_repaired_json",
    "parse_llm_json",
    "repair_llm_json",
    "safe_parse_llm_json_dict",
    "strip_code_fence",
    "strip_trailing_json_garbage",
    "try_parse_llm_json",
]

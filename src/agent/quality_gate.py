"""Pre-flight checks for outline payloads (credentials must not reach L2 via API)."""

from __future__ import annotations

import re
from typing import Any

# Payload bounds — reject oversized agent inputs before parsing / allocation.
MAX_OUTLINE_NODES = 500
MAX_OUTLINE_DEPTH = 32
MAX_BLOCK_TEXT_BYTES = 16_384
MAX_ADVANCED_QUERY_BYTES = 32_768
MAX_MARKDOWN_APPEND_BYTES = 262_144

_CRED_PROP = re.compile(
    r"(?i)\b(token::|password::|secret::|api-key::|api\.key::)\s*\S+",
)
_SK_OPENAI = re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b")


def _iter_outline_dict_texts(node: dict[str, Any]) -> list[str]:
    chunks: list[str] = [str(node.get("text", ""))]
    props = node.get("properties")
    if isinstance(props, dict):
        for key, val in props.items():
            chunks.append(f"{key} {val}")
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                chunks.extend(_iter_outline_dict_texts(child))
    return chunks


def _outline_node_count(node: dict[str, Any]) -> int:
    total = 1
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                total += _outline_node_count(child)
    return total


def _outline_max_depth(node: dict[str, Any], depth: int = 1) -> int:
    deepest = depth
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                deepest = max(deepest, _outline_max_depth(child, depth + 1))
    return deepest


def _outline_max_text_bytes(node: dict[str, Any]) -> int:
    largest = len(str(node.get("text", "")).encode("utf-8"))
    props = node.get("properties")
    if isinstance(props, dict):
        for key, val in props.items():
            largest = max(largest, len(f"{key} {val}".encode()))
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                largest = max(largest, _outline_max_text_bytes(child))
    return largest


def outline_bounds_violations(outline: dict[str, Any]) -> list[str]:
    """Return human-readable violations when outline payload exceeds size caps."""
    issues: list[str] = []
    node_count = _outline_node_count(outline)
    if node_count > MAX_OUTLINE_NODES:
        issues.append(f"outline exceeds max node count ({MAX_OUTLINE_NODES}); got {node_count}")
    depth = _outline_max_depth(outline)
    if depth > MAX_OUTLINE_DEPTH:
        issues.append(f"outline exceeds max nesting depth ({MAX_OUTLINE_DEPTH}); got {depth}")
    max_text = _outline_max_text_bytes(outline)
    if max_text > MAX_BLOCK_TEXT_BYTES:
        issues.append(
            f"outline block text/properties exceed max bytes ({MAX_BLOCK_TEXT_BYTES}); "
            f"got {max_text}",
        )
    return issues


def advanced_query_bounds_violations(query_edn: str) -> list[str]:
    """Reject advanced-query bodies that exceed the byte budget."""
    size = len(query_edn.encode("utf-8"))
    if size > MAX_ADVANCED_QUERY_BYTES:
        return [
            f"advanced query body exceeds max bytes ({MAX_ADVANCED_QUERY_BYTES}); got {size}",
        ]
    return []


def markdown_append_bounds_violations(body: str) -> list[str]:
    """Reject journal append payloads that exceed the byte budget."""
    size = len(body.encode("utf-8"))
    if size > MAX_MARKDOWN_APPEND_BYTES:
        return [
            f"markdown_body exceeds max bytes ({MAX_MARKDOWN_APPEND_BYTES}); got {size}",
        ]
    return []


def outline_security_violations(outline: dict[str, Any]) -> list[str]:
    """Return human-readable violations for unsafe outline payloads."""
    issues: list[str] = []
    blob = "\n".join(_iter_outline_dict_texts(outline))
    if _CRED_PROP.search(blob):
        issues.append("credential-like property (token/password/secret/api-key) in outline")
    if _SK_OPENAI.search(blob):
        issues.append("possible OpenAI-style API key material in outline")
    return issues


def advanced_query_security_violations(query_edn: str) -> list[str]:
    """Lightweight secret scan for raw advanced-query EDN strings."""
    issues: list[str] = list(advanced_query_bounds_violations(query_edn))
    if _CRED_PROP.search(query_edn):
        issues.append("credential-like token in advanced query body")
    if _SK_OPENAI.search(query_edn):
        issues.append("possible OpenAI-style API key material in advanced query body")
    return issues


__all__ = [
    "MAX_ADVANCED_QUERY_BYTES",
    "MAX_BLOCK_TEXT_BYTES",
    "MAX_MARKDOWN_APPEND_BYTES",
    "MAX_OUTLINE_DEPTH",
    "MAX_OUTLINE_NODES",
    "advanced_query_bounds_violations",
    "advanced_query_security_violations",
    "markdown_append_bounds_violations",
    "outline_bounds_violations",
    "outline_security_violations",
]

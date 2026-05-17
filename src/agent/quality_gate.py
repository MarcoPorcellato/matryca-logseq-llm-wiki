"""Pre-flight checks for outline payloads (credentials must not reach L2 via API)."""

from __future__ import annotations

import re
from typing import Any

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


def outline_security_violations(outline: dict[str, Any]) -> list[str]:
    """Return human-readable violations for unsafe outline payloads."""
    issues: list[str] = []
    blob = "\n".join(_iter_outline_dict_texts(outline))
    if _CRED_PROP.search(blob):
        issues.append("credential-like property (token/password/secret/api-key) in outline")
    if _SK_OPENAI.search(blob):
        issues.append("possible OpenAI-style API key material in outline")
    return issues


__all__ = ["outline_security_violations"]

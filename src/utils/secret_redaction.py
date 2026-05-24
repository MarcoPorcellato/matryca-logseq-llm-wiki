"""Detect and redact likely secrets in agent payloads and ops logs."""

from __future__ import annotations

import os
import re

_CRED_PROP = re.compile(
    r"(?i)\b(token::|password::|secret::|api-key::|api\.key::)\s*\S+",
)
_SK_OPENAI = re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

_REDACT_ENV_KEYS = frozenset(
    {
        "MATRYCA_LOG_REDACT_SECRETS",
        "MATRYCA_PLUMBER_LOG_REDACT_SECRETS",
    },
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_CRED_PROP, "[REDACTED_CREDENTIAL_PROPERTY]"),
    (_SK_OPENAI, "[REDACTED_API_KEY]"),
    (_BEARER, "Bearer [REDACTED]"),
    (_AWS_KEY, "[REDACTED_AWS_KEY]"),
    (_JWT, "[REDACTED_JWT]"),
)


def log_redaction_enabled() -> bool:
    """When true (default), scrub secrets before writing token/ops logs."""
    for key in _REDACT_ENV_KEYS:
        raw = os.environ.get(key, "").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
    return True


def redact_secrets_in_text(text: str) -> str:
    """Replace known secret shapes with stable placeholders."""
    if not text:
        return text
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_for_log(text: str) -> str:
    """Apply :func:`redact_secrets_in_text` when log redaction is enabled."""
    if not text or not log_redaction_enabled():
        return text
    return redact_secrets_in_text(text)


def secret_violations_in_text(blob: str) -> list[str]:
    """Human-readable issues when ``blob`` still contains credential-like material."""
    issues: list[str] = []
    if _CRED_PROP.search(blob):
        issues.append("credential-like property (token/password/secret/api-key) in payload")
    if _SK_OPENAI.search(blob):
        issues.append("possible OpenAI-style API key material in payload")
    if _BEARER.search(blob):
        issues.append("possible Bearer token material in payload")
    if _AWS_KEY.search(blob):
        issues.append("possible AWS access key material in payload")
    if _JWT.search(blob):
        issues.append("possible JWT material in payload")
    return issues


__all__ = [
    "log_redaction_enabled",
    "redact_for_log",
    "redact_secrets_in_text",
    "secret_violations_in_text",
]

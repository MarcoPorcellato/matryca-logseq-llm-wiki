"""Local auth token for the Matryca Plumber Sovereign UI API."""

from __future__ import annotations

import os
import secrets

_resolved_token: str | None = None


def resolve_ui_token() -> str:
    """Return the active UI bearer token (from env or generated once per process)."""
    global _resolved_token
    if _resolved_token is not None:
        return _resolved_token
    env = os.environ.get("MATRYCA_UI_TOKEN", "").strip()
    _resolved_token = env if env else secrets.token_urlsafe(32)
    return _resolved_token


def verify_ui_token(provided: str | None) -> bool:
    """Constant-time comparison against the active UI token."""
    if not provided or not provided.strip():
        return False
    return secrets.compare_digest(provided.strip(), resolve_ui_token())


def reset_ui_token_for_tests() -> None:
    """Clear cached token (tests only)."""
    global _resolved_token
    _resolved_token = None


__all__ = ["reset_ui_token_for_tests", "resolve_ui_token", "verify_ui_token"]

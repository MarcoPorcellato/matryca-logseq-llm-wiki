"""Strip terminal control sequences from user-authored graph text before TUI display."""

from __future__ import annotations

import re

# CSI / OSC / single-char ESC sequences (SGR, cursor, screen clear, etc.)
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Non-printable controls except tab, LF, CR, and ordinary space.
_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_console(text: str) -> str:
    """Remove ANSI escapes and non-printable control characters from ``text``."""
    if not text:
        return text
    cleaned = _ANSI_ESCAPE.sub("", text)
    return _CONTROL_CHAR.sub("", cleaned)


__all__ = ["sanitize_for_console"]

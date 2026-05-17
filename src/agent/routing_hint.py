"""Machine-readable L1/L2 routing hints appended to MCP tool output."""

from __future__ import annotations


def append_read_page_routing_hint(body: str) -> str:
    """Append an HTML comment agents can parse without affecting Logseq rendering."""
    stripped = body.strip()
    if not stripped:
        return body
    if stripped.startswith("LOGSEQ_GRAPH_PATH"):
        return body
    if stripped.startswith("Spatial parser is not available"):
        return body
    if stripped.startswith("Page not found"):
        return body
    if stripped.startswith("Could not read the page file"):
        return body

    lower = stripped.lower()
    if any(
        needle in lower
        for needle in (
            "password::",
            "token::",
            "secret::",
            "api-key::",
            "production",
            "must never",
            "never run",
            "ssh ",
        )
    ):
        hint = "L1_candidate"
    else:
        hint = "L2_default"
    return stripped + f"\n\n<!-- matryca_routing: hint={hint} -->\n"


def routing_hint_for_write_outline() -> str:
    """Writes through Logseq API append blocks under an existing UUID (L2)."""
    return "<!-- matryca_routing: hint=L2_graph_append -->"


__all__ = ["append_read_page_routing_hint", "routing_hint_for_write_outline"]

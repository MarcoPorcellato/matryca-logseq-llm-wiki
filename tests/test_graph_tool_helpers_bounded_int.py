"""Safe integer parsing for MCP/CLI JSON tool options."""

from __future__ import annotations

from src.agent.graph_tool_helpers import bounded_int_from_options


def test_bounded_int_from_options_clamps() -> None:
    assert (
        bounded_int_from_options(
            {"limit": "25"},
            "limit",
            default=15,
            minimum=1,
            maximum=100,
        )
        == 25
    )


def test_bounded_int_from_options_rejects_invalid() -> None:
    result = bounded_int_from_options(
        {"limit": "not-a-number"},
        "limit",
        default=15,
        minimum=1,
        maximum=100,
    )
    assert isinstance(result, str)
    assert "Invalid integer" in result

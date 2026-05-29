"""Ensure .env.example documents keys the runtime reads (minus reserved/legacy allowlist)."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Keys documented but intentionally not read by Python runtime yet.
_ALLOWLIST_UNREAD: frozenset[str] = frozenset(
    {
        "MATRYCA_LLM_PROMPT_CACHE_MODE",
        "MATRYCA_LM_INSTRUCTOR_MODE",
        "MATRYCA_LM_INSTRUCTOR_FALLBACK",
    },
)

_ENV_GET_RE = re.compile(
    r"""(?:os\.environ|env)\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""",
)
_ENV_HELPER_RE = re.compile(
    r"""_env_(?:bool|int|float|str)\(\s*["']([A-Z][A-Z0-9_]*)["']""",
)
_ENV_MAP_RE = re.compile(
    r"""_map_(?:bool|int|float|str)(?:_nonempty)?\(\s*env\s*,\s*["']([A-Z][A-Z0-9_]*)["']""",
    re.DOTALL,
)
_ENV_CONST_RE = re.compile(
    r"""^[A-Z_]+_ENV\s*=\s*["']([A-Z][A-Z0-9_]*)["']""",
    re.MULTILINE,
)
_REDACT_KEYS_RE = re.compile(
    r"""_REDACT_ENV_KEYS\s*=\s*frozenset\(\s*\{([^}]+)\}""",
    re.DOTALL,
)


def _keys_read_in_src() -> set[str]:
    keys: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        keys.update(_ENV_GET_RE.findall(text))
        keys.update(_ENV_HELPER_RE.findall(text))
        keys.update(_ENV_MAP_RE.findall(text))
        keys.update(_ENV_CONST_RE.findall(text))
        match = _REDACT_KEYS_RE.search(text)
        if match:
            block = match.group(1)
            keys.update(re.findall(r"""["']([A-Z][A-Z0-9_]*)["']""", block))
    return keys


def _keys_in_env_example() -> set[str]:
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, _ = stripped.partition("=")
        keys.add(key.strip())
    return keys


def test_env_example_keys_are_read_by_runtime_or_allowlisted() -> None:
    read_keys = _keys_read_in_src()
    example_keys = _keys_in_env_example()
    undocumented = example_keys - read_keys - _ALLOWLIST_UNREAD
    assert not undocumented, (
        f".env.example keys not read in src/ (remove or allowlist): {sorted(undocumented)}"
    )


def test_critical_runtime_keys_documented_in_env_example() -> None:
    """Core operator keys must appear in .env.example (active or commented)."""
    example_keys = _keys_in_env_example()
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    required = (
        "LOGSEQ_GRAPH_PATH",
        "MATRYCA_LM_BASE_URL",
        "MATRYCA_PLUMBER_POLL_SECONDS",
        "MATRYCA_MCP_ENABLED",
        "MATRYCA_UI_TOKEN",
        "MATRYCA_THERMAL_DELAY_BOOTSTRAP",
        "MATRYCA_PLUMBER_CONTEXT_COMPRESSION",
        "MATRYCA_LLM_MAX_COMPLETION_TOKENS",
        "MATRYCA_RAM_BUDGET_MB",
        "MATRYCA_CPU_SANDBOX",
    )
    for key in required:
        assert key in example_keys or f"{key}=" in text, f"missing from .env.example: {key}"

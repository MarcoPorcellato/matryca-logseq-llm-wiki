"""Environment-driven configuration for Matryca Plumber cognitive lint modules."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LLM_MODEL_NAME = "local-model"
DEFAULT_LLM_API_KEY = "dummy-key"

# Backward-compatible aliases for legacy imports and persisted state.
DEFAULT_LM_BASE_URL = DEFAULT_LLM_BASE_URL
DEFAULT_LM_MODEL = DEFAULT_LLM_MODEL_NAME
_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_repo_dotenv_path() -> Path | None:
    """Return the repo ``.env`` path when present (daemon may fork with an arbitrary cwd)."""
    env_path = _REPO_ROOT / ".env"
    return env_path if env_path.is_file() else None


def reload_plumber_dotenv(*, override: bool = False) -> None:
    """Refresh ``os.environ`` from the repo ``.env`` (Settings Drawer writes here).

    Defaults to ``override=False`` so explicit process env (shell, tests, subprocess
    ``LOGSEQ_GRAPH_PATH=…``) wins over the file. Long-running daemon cycles pass
    ``override=True`` to hot-reload UI and manual ``.env`` edits.
    """
    env_path = resolve_repo_dotenv_path()
    if env_path is None:
        return
    from dotenv import load_dotenv

    load_dotenv(env_path, override=override)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    """Return env value when non-empty; never override explicit env with default."""
    raw = os.environ.get(key, "").strip()
    if raw:
        return raw
    return default


def resolve_llm_model_name(*, override: str | None = None) -> str:
    """Resolve the active OpenAI-compatible model id from env or explicit override."""
    if override is not None and override.strip():
        return override.strip()
    canonical = os.environ.get("LLM_MODEL_NAME", "").strip()
    if canonical:
        return canonical
    return _env_str("MATRYCA_LM_MODEL", DEFAULT_LLM_MODEL_NAME)


def resolve_lm_model(*, override: str | None = None) -> str:
    """Backward-compatible alias for :func:`resolve_llm_model_name`."""
    return resolve_llm_model_name(override=override)


def resolve_llm_base_url(*, override: str | None = None) -> str:
    """Resolve OpenAI-compatible base URL (always ends with ``/v1``)."""
    if override is not None and override.strip():
        raw = override.strip()
    else:
        canonical = os.environ.get("LLM_BASE_URL", "").strip()
        raw = canonical if canonical else _env_str("MATRYCA_LM_BASE_URL", DEFAULT_LLM_BASE_URL)
    base = raw.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def resolve_lm_base_url(*, override: str | None = None) -> str:
    """Backward-compatible alias for :func:`resolve_llm_base_url`."""
    return resolve_llm_base_url(override=override)


def resolve_llm_api_key(*, override: str | None = None) -> str:
    """Resolve API key for OpenAI-compatible local providers (often ignored server-side)."""
    if override is not None and override.strip():
        return override.strip()
    return _env_str("LLM_API_KEY", DEFAULT_LLM_API_KEY)


@dataclass(frozen=True, slots=True)
class PlumberLintConfig:
    """Feature flags and thresholds for advanced Plumber lint modules."""

    lm_model: str = DEFAULT_LLM_MODEL_NAME
    lm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = DEFAULT_LLM_API_KEY
    marpa_framework: bool = False
    heal_dangling: bool = False
    dangling_max_words: int = 50
    entity_consolidation: bool = False
    similarity_threshold: float = 0.85
    auto_split: bool = False
    split_block_threshold: int = 15
    property_hygiene: bool = False
    infer_missing_properties: bool = True
    property_rules_path: Path | None = None
    backpropagate_links: bool = False
    semantic_routing: bool = False
    disable_semantic_corrections: bool = True
    context_compression: bool = False
    compression_trigger: int = 100_000
    compression_target: int = 30_000
    thermal_delay_bootstrap: float = 2.0
    thermal_delay_cognitive: float = 2.0
    low_priority_mode: bool = True
    mapreduce_trigger_chars: int = 25_000
    mapreduce_chunk_chars: int = 15_000

    @property
    def any_enabled(self) -> bool:
        return (
            self.marpa_framework
            or self.heal_dangling
            or self.entity_consolidation
            or self.auto_split
            or self.property_hygiene
            or self.backpropagate_links
            or self.semantic_routing
        )


def bootstrap_phase_lint_config() -> PlumberLintConfig:
    """Return a lint config with every generative/mutative module disabled (Phase 1)."""
    return PlumberLintConfig()


def _safe_thermal_seconds(seconds: float) -> float:
    """Clamp thermal pause to a non-negative float (guards bad env / UI input)."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)


def apply_thermal_pause_bootstrap(config: PlumberLintConfig | None = None) -> None:
    """GPU cool-down after one bootstrap harvest LLM turn (reads live env each call)."""
    cfg = config or load_plumber_lint_config()
    delay = _safe_thermal_seconds(cfg.thermal_delay_bootstrap)
    if delay > 0:
        time.sleep(delay)


def apply_thermal_pause_cognitive(config: PlumberLintConfig | None = None) -> None:
    """GPU cool-down after one Plumber cognitive LLM turn (reads live env each call)."""
    cfg = config or load_plumber_lint_config()
    delay = _safe_thermal_seconds(cfg.thermal_delay_cognitive)
    if delay > 0:
        time.sleep(delay)


def load_plumber_lint_config(*, reload_env: bool = False) -> PlumberLintConfig:
    """Load Plumber lint settings from environment variables."""
    if reload_env:
        reload_plumber_dotenv()
    rules_raw = os.environ.get("MATRYCA_LINT_PROPERTY_RULES", "").strip()
    rules_path = Path(rules_raw).expanduser() if rules_raw else None
    if rules_path is None:
        graph = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if graph:
            candidate = Path(graph).expanduser() / "matryca-plumber-rules.yml"
            if candidate.is_file():
                rules_path = candidate

    return PlumberLintConfig(
        lm_model=resolve_llm_model_name(),
        lm_base_url=resolve_llm_base_url(),
        llm_api_key=resolve_llm_api_key(),
        marpa_framework=_env_bool("MATRYCA_LINT_MARPA_FRAMEWORK"),
        heal_dangling=_env_bool("MATRYCA_LINT_HEAL_DANGLING"),
        dangling_max_words=_env_int("MATRYCA_LINT_DANGLING_MAX_WORDS", 50),
        entity_consolidation=_env_bool("MATRYCA_LINT_ENTITY_CONSOLIDATION"),
        similarity_threshold=_env_float("MATRYCA_LINT_SIMILARITY_THRESHOLD", 0.85),
        auto_split=_env_bool("MATRYCA_LINT_AUTO_SPLIT"),
        split_block_threshold=_env_int("MATRYCA_LINT_SPLIT_BLOCK_THRESHOLD", 15),
        property_hygiene=_env_bool("MATRYCA_LINT_PROPERTY_HYGIENE"),
        infer_missing_properties=_env_bool("MATRYCA_LINT_INFER_MISSING_PROPERTIES", True),
        property_rules_path=rules_path,
        backpropagate_links=_env_bool("MATRYCA_LINT_BACKPROPAGATE_LINKS"),
        semantic_routing=_env_bool("MATRYCA_LINT_SEMANTIC_ROUTING"),
        disable_semantic_corrections=_env_bool(
            "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS",
            True,
        ),
        context_compression=_env_bool("MATRYCA_PLUMBER_CONTEXT_COMPRESSION"),
        compression_trigger=_env_int("MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS", 100_000),
        compression_target=_env_int("MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS", 30_000),
        thermal_delay_bootstrap=_env_float("MATRYCA_THERMAL_DELAY_BOOTSTRAP", 2.0),
        thermal_delay_cognitive=_env_float("MATRYCA_THERMAL_DELAY_COGNITIVE", 2.0),
        low_priority_mode=_env_bool("MATRYCA_PLUMBER_LOW_PRIORITY_MODE", True),
        mapreduce_trigger_chars=_env_int("MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS", 25_000),
        mapreduce_chunk_chars=_env_int("MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS", 15_000),
    )


__all__ = [
    "DEFAULT_LLM_API_KEY",
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL_NAME",
    "DEFAULT_LM_BASE_URL",
    "DEFAULT_LM_MODEL",
    "PlumberLintConfig",
    "apply_thermal_pause_bootstrap",
    "apply_thermal_pause_cognitive",
    "bootstrap_phase_lint_config",
    "load_plumber_lint_config",
    "reload_plumber_dotenv",
    "resolve_llm_api_key",
    "resolve_llm_base_url",
    "resolve_llm_model_name",
    "resolve_lm_base_url",
    "resolve_lm_model",
    "resolve_repo_dotenv_path",
]

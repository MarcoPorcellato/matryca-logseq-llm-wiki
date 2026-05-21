"""Environment-driven configuration for Matryca Plumber cognitive lint modules."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LM_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LM_MODEL = "qwen2.5-coder-7b"


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


def resolve_lm_model(*, override: str | None = None) -> str:
    """Resolve the active LM Studio model id from env or explicit override."""
    if override is not None and override.strip():
        return override.strip()
    return _env_str("MATRYCA_LM_MODEL", DEFAULT_LM_MODEL)


def resolve_lm_base_url(*, override: str | None = None) -> str:
    """Resolve LM Studio OpenAI-compatible base URL (always ends with ``/v1``)."""
    raw = (
        override.strip()
        if override is not None and override.strip()
        else _env_str(
            "MATRYCA_LM_BASE_URL",
            DEFAULT_LM_BASE_URL,
        )
    )
    base = raw.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


@dataclass(frozen=True, slots=True)
class PlumberLintConfig:
    """Feature flags and thresholds for advanced Plumber lint modules."""

    lm_model: str = DEFAULT_LM_MODEL
    lm_base_url: str = DEFAULT_LM_BASE_URL
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
    context_compression: bool = False
    compression_trigger: int = 100_000
    compression_target: int = 30_000

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


def load_plumber_lint_config() -> PlumberLintConfig:
    """Load Plumber lint settings from environment variables."""
    rules_raw = os.environ.get("MATRYCA_LINT_PROPERTY_RULES", "").strip()
    rules_path = Path(rules_raw).expanduser() if rules_raw else None
    if rules_path is None:
        graph = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        if graph:
            candidate = Path(graph).expanduser() / "matryca-plumber-rules.yml"
            if candidate.is_file():
                rules_path = candidate

    return PlumberLintConfig(
        lm_model=resolve_lm_model(),
        lm_base_url=resolve_lm_base_url(),
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
        context_compression=_env_bool("MATRYCA_PLUMBER_CONTEXT_COMPRESSION"),
        compression_trigger=_env_int("MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS", 100_000),
        compression_target=_env_int("MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS", 30_000),
    )


__all__ = [
    "DEFAULT_LM_BASE_URL",
    "DEFAULT_LM_MODEL",
    "PlumberLintConfig",
    "load_plumber_lint_config",
    "resolve_lm_base_url",
    "resolve_lm_model",
]

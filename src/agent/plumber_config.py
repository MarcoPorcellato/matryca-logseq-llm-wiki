"""Environment-driven configuration for Matryca Plumber cognitive lint modules."""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LLM_MODEL_NAME = "local-model"
DEFAULT_LLM_API_KEY = "dummy-key"
DEFAULT_LLM_MAX_COMPLETION_TOKENS = 2048
DEFAULT_LLM_MAX_COMPRESSION_TOKENS = 1536
_MIN_LLM_MAX_COMPLETION_TOKENS = 256
_MAX_LLM_MAX_COMPLETION_TOKENS = 8192
_MAX_LLM_MAX_COMPRESSION_TOKENS = 4096

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


def resolve_llm_max_completion_tokens() -> int:
    """Upper bound for structured JSON completions (prevents runaway local-model loops)."""
    value = _env_int("MATRYCA_LLM_MAX_COMPLETION_TOKENS", DEFAULT_LLM_MAX_COMPLETION_TOKENS)
    return max(_MIN_LLM_MAX_COMPLETION_TOKENS, min(_MAX_LLM_MAX_COMPLETION_TOKENS, value))


def resolve_llm_max_compression_tokens() -> int:
    """Upper bound for Ermes context-compression completions (markdown, not JSON)."""
    value = _env_int("MATRYCA_LLM_MAX_COMPRESSION_TOKENS", DEFAULT_LLM_MAX_COMPRESSION_TOKENS)
    return max(_MIN_LLM_MAX_COMPLETION_TOKENS, min(_MAX_LLM_MAX_COMPRESSION_TOKENS, value))


def resolve_llm_prose_completion_max_chars() -> int:
    """Hard character cap after prose sanitization (history pollution guard)."""
    value = _env_int("MATRYCA_LLM_PROSE_COMPLETION_MAX_CHARS", 12_000)
    return max(2_000, min(32_000, value))


def resolve_cluster_focus_max_chars() -> int:
    """Cap Ermes cluster neighborhood injection (large Louvain clusters)."""
    value = _env_int("MATRYCA_CLUSTER_FOCUS_MAX_CHARS", 8000)
    return max(1_000, min(24_000, value))


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
    """Resolve OpenAI-compatible base URL (appends ``/v1`` only for bare host roots)."""
    if override is not None and override.strip():
        raw = override.strip()
    else:
        canonical = os.environ.get("LLM_BASE_URL", "").strip()
        raw = canonical if canonical else _env_str("MATRYCA_LM_BASE_URL", DEFAULT_LLM_BASE_URL)
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        return base
    parsed = urlparse(base)
    path = (parsed.path or "").rstrip("/")
    if path:
        return base
    return f"{base}/v1"


def resolve_lm_base_url(*, override: str | None = None) -> str:
    """Backward-compatible alias for :func:`resolve_llm_base_url`."""
    return resolve_llm_base_url(override=override)


def resolve_validated_llm_base_url(*, override: str | None = None) -> str:
    """Resolve and SSRF-validate the active OpenAI-compatible inference base URL."""
    from ..utils.llm_url_policy import validate_llm_proxy_url

    canonical = os.environ.get("LLM_BASE_URL", "").strip()
    raw = (
        override.strip()
        if override is not None and override.strip()
        else canonical
        if canonical
        else _env_str("MATRYCA_LM_BASE_URL", DEFAULT_LLM_BASE_URL)
    )
    configured_anchor = canonical if canonical else raw
    return validate_llm_proxy_url(raw, configured_base_url=configured_anchor)


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
            or self.context_compression
        )


def bootstrap_phase_lint_config() -> PlumberLintConfig:
    """Lint config for Phase 1: modules off, thermal/map-reduce from live ``.env``."""
    live = load_plumber_lint_config()
    return PlumberLintConfig(
        thermal_delay_bootstrap=live.thermal_delay_bootstrap,
        thermal_delay_cognitive=live.thermal_delay_cognitive,
        low_priority_mode=live.low_priority_mode,
        mapreduce_trigger_chars=live.mapreduce_trigger_chars,
        mapreduce_chunk_chars=live.mapreduce_chunk_chars,
    )


def _safe_thermal_seconds(seconds: float) -> float:
    """Clamp thermal pause to a non-negative finite float (guards bad env / UI input)."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, value)


def _safe_nonneg_int(value: int, *, default: int) -> int:
    """Clamp integer thresholds to a non-negative value (guards bad env / UI input)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _interruptible_thermal_sleep(
    seconds: float,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Sleep for thermal cool-down, waking early when ``stop_event`` is set."""
    delay = _safe_thermal_seconds(seconds)
    if delay <= 0:
        return
    if stop_event is None:
        time.sleep(delay)
        return
    stop_event.wait(timeout=delay)


def apply_thermal_pause_bootstrap(
    config: PlumberLintConfig | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """GPU cool-down after one bootstrap harvest LLM turn (reads live env each call)."""
    _ = config
    delay = _safe_thermal_seconds(load_plumber_lint_config().thermal_delay_bootstrap)
    _interruptible_thermal_sleep(delay, stop_event=stop_event)


def apply_thermal_pause_cognitive(
    config: PlumberLintConfig | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """GPU cool-down after one Plumber cognitive LLM turn (reads live env each call)."""
    _ = config
    delay = _safe_thermal_seconds(load_plumber_lint_config().thermal_delay_cognitive)
    _interruptible_thermal_sleep(delay, stop_event=stop_event)


def _map_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _map_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _map_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _map_str_nonempty(env: Mapping[str, str], key: str, default: str) -> str:
    raw = (env.get(key) or "").strip()
    return raw if raw else default


def _resolve_llm_model_name_from_env(env: Mapping[str, str]) -> str:
    canonical = (env.get("LLM_MODEL_NAME") or "").strip()
    if canonical:
        return canonical
    return _map_str_nonempty(env, "MATRYCA_LM_MODEL", DEFAULT_LLM_MODEL_NAME)


def _resolve_llm_base_url_from_env(env: Mapping[str, str]) -> str:
    from ..utils.llm_url_policy import validate_llm_proxy_url

    canonical = (env.get("LLM_BASE_URL") or "").strip()
    raw = (
        canonical
        if canonical
        else _map_str_nonempty(env, "MATRYCA_LM_BASE_URL", DEFAULT_LLM_BASE_URL)
    )
    configured_for_host = raw
    return validate_llm_proxy_url(raw, configured_base_url=configured_for_host)


def _resolve_llm_api_key_from_env(env: Mapping[str, str]) -> str:
    return _map_str_nonempty(env, "LLM_API_KEY", DEFAULT_LLM_API_KEY)


def load_plumber_lint_config_from_environ(env: Mapping[str, str]) -> PlumberLintConfig:
    """Build :class:`PlumberLintConfig` from a string environment mapping (thread-safe reads)."""
    from ..utils.config_paths import resolve_optional_path_under_allowed_roots

    rules_raw = (env.get("MATRYCA_LINT_PROPERTY_RULES") or "").strip()
    graph = (env.get("LOGSEQ_GRAPH_PATH") or "").strip()
    graph_root = Path(graph).expanduser().resolve(strict=False) if graph else None
    rules_path = (
        resolve_optional_path_under_allowed_roots(rules_raw, graph_root=graph_root)
        if rules_raw
        else None
    )
    if rules_path is None and graph_root is not None:
        candidate = graph_root / "matryca-plumber-rules.yml"
        if candidate.is_file():
            rules_path = candidate

    return PlumberLintConfig(
        lm_model=_resolve_llm_model_name_from_env(env),
        lm_base_url=_resolve_llm_base_url_from_env(env),
        llm_api_key=_resolve_llm_api_key_from_env(env),
        marpa_framework=_map_bool(env, "MATRYCA_LINT_MARPA_FRAMEWORK"),
        heal_dangling=_map_bool(env, "MATRYCA_LINT_HEAL_DANGLING"),
        dangling_max_words=_map_int(env, "MATRYCA_LINT_DANGLING_MAX_WORDS", 50),
        entity_consolidation=_map_bool(env, "MATRYCA_LINT_ENTITY_CONSOLIDATION"),
        similarity_threshold=_map_float(env, "MATRYCA_LINT_SIMILARITY_THRESHOLD", 0.85),
        auto_split=_map_bool(env, "MATRYCA_LINT_AUTO_SPLIT"),
        split_block_threshold=_map_int(env, "MATRYCA_LINT_SPLIT_BLOCK_THRESHOLD", 15),
        property_hygiene=_map_bool(env, "MATRYCA_LINT_PROPERTY_HYGIENE"),
        infer_missing_properties=_map_bool(env, "MATRYCA_LINT_INFER_MISSING_PROPERTIES", True),
        property_rules_path=rules_path,
        backpropagate_links=_map_bool(env, "MATRYCA_LINT_BACKPROPAGATE_LINKS"),
        semantic_routing=_map_bool(env, "MATRYCA_LINT_SEMANTIC_ROUTING"),
        disable_semantic_corrections=_map_bool(
            env,
            "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS",
            True,
        ),
        context_compression=_map_bool(env, "MATRYCA_PLUMBER_CONTEXT_COMPRESSION"),
        compression_trigger=_safe_nonneg_int(
            _map_int(env, "MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS", 100_000),
            default=100_000,
        ),
        compression_target=_safe_nonneg_int(
            _map_int(env, "MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS", 30_000),
            default=30_000,
        ),
        thermal_delay_bootstrap=_map_float(env, "MATRYCA_THERMAL_DELAY_BOOTSTRAP", 2.0),
        thermal_delay_cognitive=_map_float(env, "MATRYCA_THERMAL_DELAY_COGNITIVE", 2.0),
        low_priority_mode=_map_bool(env, "MATRYCA_PLUMBER_LOW_PRIORITY_MODE", True),
        mapreduce_trigger_chars=_safe_nonneg_int(
            _map_int(env, "MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS", 25_000),
            default=25_000,
        ),
        mapreduce_chunk_chars=_safe_nonneg_int(
            _map_int(env, "MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS", 15_000),
            default=15_000,
        ),
    )


def format_dotenv_value(value: str) -> str:
    """Quote ``.env`` values when needed, including ``$`` to avoid expansion surprises."""
    if re.search(r'[\s#"\'\\$]', value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def serialize_plumber_config_field_for_dotenv(field: str, value: object) -> str:
    """Serialize one Plumber UI field to a scalar string safe for ``KEY=value`` lines."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if field in {"mapreduce_trigger_chars", "mapreduce_chunk_chars"}:
        try:
            if isinstance(value, int):
                parsed = value
            elif isinstance(value, float):
                parsed = int(value)
            elif isinstance(value, str):
                parsed = int(value.strip())
            else:
                parsed = int(str(value))
            if parsed < 0:
                raise ValueError(f"{field} must be non-negative")
            return str(parsed)
        except (TypeError, ValueError) as exc:
            msg = f"Invalid integer for {field}: {value!r}"
            raise ValueError(msg) from exc
    if field in {"compression_trigger", "compression_target"}:
        try:
            if isinstance(value, int):
                parsed = value
            elif isinstance(value, float):
                parsed = int(value)
            elif isinstance(value, str):
                parsed = int(value.strip())
            else:
                parsed = int(str(value))
            if parsed < 0:
                raise ValueError(f"{field} must be non-negative")
            return str(parsed)
        except (TypeError, ValueError) as exc:
            msg = f"Invalid integer for {field}: {value!r}"
            raise ValueError(msg) from exc
    if field in {"thermal_delay_bootstrap", "thermal_delay_cognitive"}:
        try:
            if isinstance(value, (int, float)):
                thermal = float(value)
            elif isinstance(value, str):
                thermal = float(value.strip())
            else:
                thermal = float(str(value))
        except (TypeError, ValueError) as exc:
            msg = f"Invalid float for {field}: {value!r}"
            raise ValueError(msg) from exc
        if not math.isfinite(thermal):
            raise ValueError(f"{field} must be a finite number")
        if thermal < 0:
            raise ValueError(f"{field} must be non-negative")
        return str(thermal)
    if field == "logseq_graph_path":
        from ..graph.graph_path_validate import validate_logseq_graph_path_for_config

        return str(validate_logseq_graph_path_for_config(str(value)))
    return str(value).strip()


def load_plumber_lint_config(*, reload_env: bool = False) -> PlumberLintConfig:
    """Load Plumber lint settings from environment variables."""
    if reload_env:
        reload_plumber_dotenv()
    return load_plumber_lint_config_from_environ(os.environ)


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
    "format_dotenv_value",
    "load_plumber_lint_config",
    "load_plumber_lint_config_from_environ",
    "reload_plumber_dotenv",
    "resolve_llm_api_key",
    "resolve_llm_base_url",
    "resolve_validated_llm_base_url",
    "resolve_llm_max_completion_tokens",
    "resolve_llm_max_compression_tokens",
    "resolve_cluster_focus_max_chars",
    "resolve_llm_prose_completion_max_chars",
    "resolve_llm_model_name",
    "resolve_lm_base_url",
    "resolve_lm_model",
    "resolve_repo_dotenv_path",
    "serialize_plumber_config_field_for_dotenv",
]

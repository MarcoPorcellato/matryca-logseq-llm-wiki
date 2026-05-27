"""FastAPI local API server for the Matryca Plumber UI."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import dotenv_values, load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..agent.control_room_progress import resolve_control_room_progress
from ..agent.l1_memory import default_matryca_l1_directory_for_graph
from ..agent.maintenance_daemon import (
    DEFAULT_STOP_GRACE_SECONDS,
    DaemonState,
    heal_daemon_state_ledger,
    is_plumber_process,
    load_daemon_state,
    read_pid_file,
    resolve_graph_root,
    stop_daemon,
)
from ..agent.plumber_config import (
    PlumberLintConfig,
    format_dotenv_value,
    load_plumber_lint_config,
    load_plumber_lint_config_from_environ,
    reload_plumber_dotenv,
    resolve_llm_base_url,
    serialize_plumber_config_field_for_dotenv,
)
from ..config import load_matryca_wiki_config
from ..graph.graph_analytics import compute_graph_analytics
from ..graph.graph_path_validate import validate_logseq_graph_path_for_config
from ..utils.console_sanitize import sanitize_for_console
from ..utils.env_placeholders import is_template_env_path
from ..utils.llm_url_policy import UnsafeLlmProxyUrlError, validate_llm_proxy_url
from ..utils.preflight import run_preflight_checks
from ..utils.provision_l1 import provision_matryca_l1_sibling
from ..utils.runtime_bootstrap import prepare_matryca_runtime, try_prepare_matryca_runtime_from_env
from ..utils.token_logger import TokenLogger, resolve_plumber_log_path
from ..utils.updater import UpdateCheckResult, check_for_updates, update_check_to_dict
from .ui_auth import resolve_ui_token, verify_ui_token

FileStatus = Literal["processed", "skipped", "error", "pending"]
DaemonStatusValue = Literal["running", "idle", "stopped", "error"]

DEFAULT_UI_PORT = 8500

LOCAL_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    f"http://localhost:{DEFAULT_UI_PORT}",
    f"http://127.0.0.1:{DEFAULT_UI_PORT}",
    "tauri://localhost",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO_ROOT / "frontend"
_FRONTEND_DIST = _FRONTEND_ROOT / "dist"
_FRONTEND_SRC = _FRONTEND_ROOT / "src"


class FileStateResponse(BaseModel):
    """On-disk processing record for one markdown file."""

    mtime: float
    processed_at: str
    status: FileStatus
    error: str | None = None


class BootstrapRecentEntryResponse(BaseModel):
    """Recent Phase 1 catalog page for control-room pills."""

    harvest: Literal["regex", "llm", "skipped", "error"]
    processed_at: str


class GraphAnalyticsResponse(BaseModel):
    """Live graph topology and GraphRAG telemetry for the control room."""

    total_pages: int = 0
    total_journals: int = 0
    total_links: int = 0
    human_pages: int = 0
    human_links: int = 0
    ai_pages: int = 0
    ai_links: int = 0
    ai_blocks_healed: int = 0
    page_summaries: int = 0
    alias_count: int = 0
    semantic_links: int = 0
    semantic_cache_mb: float = 0.0
    context_acceleration: float = 0.0
    status: Literal["online", "offline"] = "online"


class DaemonStateResponse(BaseModel):
    """Canonical Plumber checkpoint exposed to the React UI."""

    version: int = 1
    files: dict[str, FileStateResponse] = Field(default_factory=dict)
    status: DaemonStatusValue = "idle"
    model: str
    bootstrap_complete: bool = False
    bootstrap_scanned: int = 0
    bootstrap_total: int = 0
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    current_cluster: str | None = None
    current_cluster_files_total: int = 0
    current_cluster_files_done: int = 0
    phase2_llm_turns: int = 0
    last_scan_at: str | None = None
    last_file: str | None = None
    ai_pages_created: int = 0
    ai_links_injected: int = 0
    ai_blocks_healed: int = 0
    hygiene_corrections: int = 0
    page_summaries_created: int = 0
    bootstrap_recent: dict[str, BootstrapRecentEntryResponse] = Field(default_factory=dict)
    phase2_cognitive_total: int = 0
    phase2_cognitive_done: int = 0
    phase2_cluster_file_in_flight: bool = False
    progress_mode: Literal["phase1_catalog", "phase2_cluster", "phase2_vault"] = "phase1_catalog"
    progress_title: str = "Phase 1: Cataloging Graph"
    progress_subtitle: str = "—"
    progress_done: int = 0
    progress_total: int = 0
    progress_percent: float = 0.0

    @classmethod
    def from_daemon_state(cls, state: DaemonState) -> DaemonStateResponse:
        payload = state.to_json()
        if payload.get("last_file"):
            payload["last_file"] = sanitize_for_console(str(payload["last_file"]))
        payload.update(resolve_control_room_progress(state).to_api_fields())
        return cls.model_validate(payload)


class PlumberConfigResponse(BaseModel):
    """Live Plumber configuration exposed to the React control room."""

    logseq_graph_path: str
    lm_studio_url: str
    lm_model: str
    low_priority_mode: bool
    thermal_delay_bootstrap: float
    thermal_delay_cognitive: float
    mapreduce_trigger_chars: int
    mapreduce_chunk_chars: int
    context_compression: bool
    compression_trigger: int
    compression_target: int
    semantic_routing: bool
    entity_consolidation: bool
    property_hygiene: bool
    marpa_framework: bool
    heal_dangling: bool
    backpropagate_links: bool
    enable_inline_semantic_corrections: bool
    auto_split: bool

    @classmethod
    def from_lint_config(cls, config: PlumberLintConfig) -> PlumberConfigResponse:
        graph_path = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
        return cls(
            logseq_graph_path=graph_path,
            lm_studio_url=config.lm_base_url,
            lm_model=config.lm_model,
            low_priority_mode=config.low_priority_mode,
            thermal_delay_bootstrap=config.thermal_delay_bootstrap,
            thermal_delay_cognitive=config.thermal_delay_cognitive,
            mapreduce_trigger_chars=config.mapreduce_trigger_chars,
            mapreduce_chunk_chars=config.mapreduce_chunk_chars,
            context_compression=config.context_compression,
            compression_trigger=config.compression_trigger,
            compression_target=config.compression_target,
            semantic_routing=config.semantic_routing,
            entity_consolidation=config.entity_consolidation,
            property_hygiene=config.property_hygiene,
            marpa_framework=config.marpa_framework,
            heal_dangling=config.heal_dangling,
            backpropagate_links=config.backpropagate_links,
            enable_inline_semantic_corrections=not config.disable_semantic_corrections,
            auto_split=config.auto_split,
        )


class DaemonControlResponse(BaseModel):
    """Result of a start/stop request against the maintenance daemon."""

    ok: bool
    code: str
    message: str | None = None
    pid: int | None = None


class LmModelsResponse(BaseModel):
    """OpenAI-compatible model ids exposed by the local inference server."""

    models: list[str] = Field(default_factory=list)
    error: str | None = None


class UpdateCheckResponse(BaseModel):
    """PyPI release comparison for guided in-place updates."""

    current_version: str
    latest_version: str
    update_available: bool
    pypi_url: str

    @classmethod
    def from_result(cls, result: UpdateCheckResult) -> UpdateCheckResponse:
        payload = update_check_to_dict(result)
        return cls.model_validate(payload)


class AuthSessionResponse(BaseModel):
    """Bootstrap token for the local Sovereign UI (loopback-only)."""

    token: str


PreflightStatusValue = Literal["pass", "fail", "warn"]


class PreflightCheckResponse(BaseModel):
    """One row in the Sovereign UI pre-flight checklist."""

    id: str
    title: str
    status: PreflightStatusValue
    message: str
    detail: str | None = None


class PreflightResponse(BaseModel):
    """Aggregated readiness for enabling Start Engine."""

    ready: bool
    env_created_from_example: bool = False
    checks: list[PreflightCheckResponse] = Field(default_factory=list)


class ProvisionL1Response(BaseModel):
    """Result of onboarding ``matryca-l1/`` provisioning beside the configured vault."""

    ok: bool
    l1_path: str
    created: bool
    message: str


class GraphPathUpdateRequest(BaseModel):
    """Pre-flight onboarding: update ``LOGSEQ_GRAPH_PATH`` only."""

    logseq_graph_path: str


def assert_safe_lm_proxy_url(base_url: str) -> str:
    """Validate ``base_url`` for LM Studio / OpenAI-compatible proxy use (SSRF guard).

    Use for model discovery (``GET /api/lm-models``) and for persisting ``LLM_BASE_URL``
    via ``POST /api/config`` so configuration cannot bypass the same host/DNS checks.

    Raises:
        HTTPException: When the URL is not an allowed ``http``/``https`` inference endpoint.
    """
    try:
        return validate_llm_proxy_url(
            base_url,
            configured_base_url=load_plumber_lint_config().lm_base_url,
        )
    except UnsafeLlmProxyUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_lm_models_base_url(base_url: str) -> str:
    """Restrict model discovery to local inference endpoints (SSRF guard)."""
    return assert_safe_lm_proxy_url(base_url)


def _fetch_lm_studio_models(base_url: str) -> LmModelsResponse:
    """Query ``GET /v1/models`` on an OpenAI-compatible local inference server."""
    models_url = f"{resolve_llm_base_url(override=base_url).rstrip('/')}/models"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        request = urllib.request.Request(models_url, headers={"Accept": "application/json"})
        with opener.open(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return LmModelsResponse(models=[], error=str(exc))

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return LmModelsResponse(models=[], error="Unexpected response from inference server")

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())
    return LmModelsResponse(models=sorted(set(models)))


_ENV_KEY_MAP: dict[str, str] = {
    "logseq_graph_path": "LOGSEQ_GRAPH_PATH",
    "lm_studio_url": "LLM_BASE_URL",
    "lm_model": "LLM_MODEL_NAME",
    "low_priority_mode": "MATRYCA_PLUMBER_LOW_PRIORITY_MODE",
    "thermal_delay_bootstrap": "MATRYCA_THERMAL_DELAY_BOOTSTRAP",
    "thermal_delay_cognitive": "MATRYCA_THERMAL_DELAY_COGNITIVE",
    "mapreduce_trigger_chars": "MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS",
    "mapreduce_chunk_chars": "MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS",
    "context_compression": "MATRYCA_PLUMBER_CONTEXT_COMPRESSION",
    "compression_trigger": "MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS",
    "compression_target": "MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS",
    "semantic_routing": "MATRYCA_LINT_SEMANTIC_ROUTING",
    "entity_consolidation": "MATRYCA_LINT_ENTITY_CONSOLIDATION",
    "property_hygiene": "MATRYCA_LINT_PROPERTY_HYGIENE",
    "marpa_framework": "MATRYCA_LINT_MARPA_FRAMEWORK",
    "heal_dangling": "MATRYCA_LINT_HEAL_DANGLING",
    "backpropagate_links": "MATRYCA_LINT_BACKPROPAGATE_LINKS",
    "auto_split": "MATRYCA_LINT_AUTO_SPLIT",
}


def _resolve_dotenv_path() -> Path:
    return _REPO_ROOT / ".env"


def _ui_allow_lan() -> bool:
    raw = os.environ.get("MATRYCA_UI_ALLOW_LAN", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_loopback_client_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"127.0.0.1", "localhost", "::1", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _require_loopback_client(request: Request) -> None:
    """Bearer bootstrap is loopback-only even when ``MATRYCA_UI_ALLOW_LAN`` is set."""
    client = request.client
    if client is not None and _is_loopback_client_host(client.host):
        return
    raise HTTPException(
        status_code=403,
        detail="Auth session is only available to loopback clients",
    )


def _ui_docs_enabled() -> bool:
    raw = os.environ.get("MATRYCA_UI_ENABLE_DOCS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _ui_rate_limit_authenticated_per_minute() -> int:
    raw = os.environ.get("MATRYCA_UI_RATE_LIMIT_PER_MINUTE", "120").strip()
    try:
        return max(10, int(raw))
    except ValueError:
        return 120


def _ui_rate_limit_unauthenticated_per_minute() -> int:
    raw = os.environ.get("MATRYCA_UI_RATE_LIMIT_UNAUTH_PER_MINUTE", "30").strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


class _UiRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client IP cap; authenticated callers get a higher budget than anonymous."""

    def __init__(
        self,
        app: Any,
        *,
        max_authenticated_per_minute: int,
        max_unauthenticated_per_minute: int,
    ) -> None:
        super().__init__(app)
        self._max_authenticated = max_authenticated_per_minute
        self._max_unauthenticated = max_unauthenticated_per_minute
        self._guard = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        if not path.startswith("/api/") or path in {
            "/api/health",
            "/api/auth/session",
            "/api/daemon/start",
            "/api/daemon/stop",
        }:
            return await call_next(request)
        token = request.headers.get("x-matryca-token")
        limit = self._max_authenticated if verify_ui_token(token) else self._max_unauthenticated
        client = request.client
        key = client.host if client is not None else "unknown"
        now = time.monotonic()
        with self._guard:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded; retry shortly."},
                )
            bucket.append(now)
        return await call_next(request)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    committed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        committed = True
    finally:
        if not committed:
            tmp_path.unlink(missing_ok=True)


def _require_ui_token(
    x_matryca_token: Annotated[str | None, Header(alias="X-Matryca-Token")] = None,
) -> None:
    if not verify_ui_token(x_matryca_token):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Matryca-Token")


def _redact_log_line(line: str) -> str:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return sanitize_for_console(line)
    if isinstance(payload, dict):
        for key in ("prompt", "response"):
            if key in payload:
                payload[key] = "[redacted]"
        return sanitize_for_console(json.dumps(payload, ensure_ascii=False))
    return sanitize_for_console(line)


def _apply_dotenv_updates(updates: dict[str, str], *, env_path: Path | None = None) -> Path:
    """Merge ``updates`` into the repo ``.env`` and refresh ``os.environ``."""
    target = env_path or _resolve_dotenv_path()
    if target.is_file():
        lines = target.read_text(encoding="utf-8").splitlines()
    else:
        example_path = _REPO_ROOT / ".env.example"
        lines = (
            example_path.read_text(encoding="utf-8").splitlines() if example_path.is_file() else []
        )

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in updates:
            new_lines.append(f"{key}={format_dotenv_value(updates[key])}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={format_dotenv_value(value)}")

    _atomic_write_text(target, "\n".join(new_lines).rstrip() + "\n")
    for key, value in updates.items():
        os.environ[key] = value
    load_dotenv(target, override=True)
    return target


def _update_dotenv(payload: PlumberConfigResponse) -> None:
    """Persist configuration updates to ``.env`` and the active process environment."""
    validated_lm_url = assert_safe_lm_proxy_url(payload.lm_studio_url)
    updates: dict[str, str] = {}
    for field, env_key in _ENV_KEY_MAP.items():
        raw_value = validated_lm_url if field == "lm_studio_url" else getattr(payload, field)
        updates[env_key] = serialize_plumber_config_field_for_dotenv(field, raw_value)
    updates["MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS"] = (
        "false" if payload.enable_inline_semantic_corrections else "true"
    )
    _apply_dotenv_updates(updates)


def _ensure_graph_root_env_loaded() -> None:
    """Load repo ``.env`` when ``LOGSEQ_GRAPH_PATH`` is missing from the process env."""
    if os.environ.get("LOGSEQ_GRAPH_PATH", "").strip():
        return
    from ..utils.runtime_bootstrap import ensure_repo_dotenv_from_example

    ensure_repo_dotenv_from_example()
    reload_plumber_dotenv()


@asynccontextmanager
async def _ui_app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Provision logs and graph runtime dirs before serving API traffic."""
    _ensure_graph_root_env_loaded()
    try_prepare_matryca_runtime_from_env()
    yield


app = FastAPI(
    title="Matryca Plumber API",
    lifespan=_ui_app_lifespan,
    docs_url="/docs" if _ui_docs_enabled() else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _ui_docs_enabled() else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    _UiRateLimitMiddleware,
    max_authenticated_per_minute=_ui_rate_limit_authenticated_per_minute(),
    max_unauthenticated_per_minute=_ui_rate_limit_unauthenticated_per_minute(),
)


def _resolve_graph_root_or_raise() -> Path:
    _ensure_graph_root_env_loaded()
    try:
        return resolve_graph_root()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _heal_daemon_state_in_memory(graph_root: Path, state: DaemonState) -> None:
    """Reconcile persisted AI counters against live graph totals without writing disk."""
    heal_daemon_state_ledger(graph_root, state)


def _daemon_status_for_api(graph_root: Path, state: DaemonState) -> DaemonStatusValue:
    """Expose ``stopped`` unless a live Plumber PID is present (UI Start gate)."""
    pid = read_pid_file(graph_root)
    if pid is None or not is_plumber_process(pid):
        return "stopped"
    if state.status in {"running", "idle", "error"}:
        return state.status
    return "idle"


def _safe_graph_analytics(
    graph_root: Path,
    *,
    ai_links_injected: int = 0,
    ai_blocks_healed: int = 0,
    page_summaries_created: int = 0,
) -> GraphAnalyticsResponse:
    """Compute graph telemetry without failing the daemon state endpoint."""
    try:
        return GraphAnalyticsResponse.model_validate(
            compute_graph_analytics(
                graph_root,
                ai_links_injected=ai_links_injected,
                ai_blocks_healed=ai_blocks_healed,
                page_summaries_created=page_summaries_created,
            ).to_dict()
        )
    except Exception as exc:
        logger.warning(
            "Graph analytics failed for graph_root={!r}: {}",
            graph_root,
            exc,
        )
        return GraphAnalyticsResponse(status="offline")


@app.get("/api/health")
def get_health() -> dict[str, str]:
    """Minimal liveness probe (no auth, no schema disclosure)."""
    return {"status": "ok"}


@app.get("/api/preflight", response_model=PreflightResponse)
def get_preflight(_: None = Depends(_require_ui_token)) -> PreflightResponse:
    """Validate graph, L1, and local LLM readiness before starting the daemon."""
    report = run_preflight_checks(repo_root=_REPO_ROOT)
    return PreflightResponse.model_validate(report.to_dict())


@app.post("/api/provision-l1", response_model=ProvisionL1Response)
def post_provision_l1(_: None = Depends(_require_ui_token)) -> ProvisionL1Response:
    """Create sibling ``matryca-l1/`` for the configured Logseq graph (onboarding)."""
    graph_raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if not graph_raw or is_template_env_path(graph_raw):
        raise HTTPException(
            status_code=422,
            detail="Set and save a valid Logseq graph path before provisioning L1 memory.",
        )
    try:
        graph_root = validate_logseq_graph_path_for_config(graph_raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    planned = default_matryca_l1_directory_for_graph(graph_root)
    existed_before = planned.is_dir()
    try:
        l1_dir = provision_matryca_l1_sibling(graph_root=graph_root, repo_root=_REPO_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to provision matryca-l1: {exc}",
        ) from exc

    created = not existed_before
    message = (
        f"Created matryca-l1 at {l1_dir}" if created else f"L1 memory already present at {l1_dir}"
    )
    return ProvisionL1Response(
        ok=True,
        l1_path=str(l1_dir),
        created=created,
        message=message,
    )


@app.get("/api/auth/session", response_model=AuthSessionResponse)
def get_auth_session(request: Request) -> AuthSessionResponse:
    """Return the local UI bearer token for loopback clients only."""
    _require_loopback_client(request)
    return AuthSessionResponse(token=resolve_ui_token())


def _graph_analytics_for_graph(graph_root: Path) -> GraphAnalyticsResponse:
    """Heal ledger counters and compute topology telemetry (blocking; run off the event loop)."""
    state = load_daemon_state(graph_root)
    _heal_daemon_state_in_memory(graph_root, state)
    return _safe_graph_analytics(
        graph_root,
        ai_links_injected=state.ai_links_injected,
        ai_blocks_healed=state.ai_blocks_healed,
        page_summaries_created=state.page_summaries_created,
    )


@app.get("/api/graph-analytics", response_model=GraphAnalyticsResponse)
async def get_graph_analytics(_: None = Depends(_require_ui_token)) -> GraphAnalyticsResponse:
    """Return live graph topology telemetry for the configured ``LOGSEQ_GRAPH_PATH``."""
    graph_root = _resolve_graph_root_or_raise()
    return await asyncio.to_thread(_graph_analytics_for_graph, graph_root)


@app.get("/api/state", response_model=DaemonStateResponse)
def get_state(_: None = Depends(_require_ui_token)) -> DaemonStateResponse:
    """Return the current daemon checkpoint from ``.matryca_daemon_state.json`` (no graph scan)."""
    graph_root = _resolve_graph_root_or_raise()
    state = load_daemon_state(graph_root)
    response = DaemonStateResponse.from_daemon_state(state)
    progress = resolve_control_room_progress(state)
    return response.model_copy(
        update={
            "status": _daemon_status_for_api(graph_root, state),
            **progress.to_api_fields(),
        }
    )


@app.get("/api/logs", response_model=list[str])
def get_logs(_: None = Depends(_require_ui_token)) -> list[str]:
    """Return the latest 50 non-empty operational log lines (prompt/response redacted)."""
    token_logger = TokenLogger(log_path=resolve_plumber_log_path())
    return [_redact_log_line(line) for line in token_logger.tail_lines(50)]


@app.get("/api/config", response_model=PlumberConfigResponse)
def get_config(_: None = Depends(_require_ui_token)) -> PlumberConfigResponse:
    """Return Plumber settings as persisted in the repo ``.env`` file (no global env mutation)."""
    env_path = _resolve_dotenv_path()
    if env_path.is_file():
        file_vars = dotenv_values(env_path, interpolate=True)
        merged: dict[str, str] = {
            key: value for key, value in file_vars.items() if value is not None
        }
        return PlumberConfigResponse.from_lint_config(
            load_plumber_lint_config_from_environ(merged),
        )
    return PlumberConfigResponse.from_lint_config(load_plumber_lint_config())


@app.get("/api/system/update-check", response_model=UpdateCheckResponse)
async def get_update_check(
    _: None = Depends(_require_ui_token),
    force_refresh: bool = Query(default=False),
) -> UpdateCheckResponse:
    """Compare the installed package version against the latest PyPI release."""
    result = await check_for_updates(force_refresh=force_refresh)
    return UpdateCheckResponse.from_result(result)


@app.get("/api/lm-models", response_model=LmModelsResponse)
def get_lm_models(
    _: None = Depends(_require_ui_token),
    base_url: str | None = None,
) -> LmModelsResponse:
    """Proxy local model discovery for the settings drawer (avoids browser CORS)."""
    resolved = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
    if resolved is None:
        resolved = load_plumber_lint_config().lm_base_url
    safe_base_url = _validate_lm_models_base_url(resolved)
    return _fetch_lm_studio_models(safe_base_url)


@app.post("/api/config/graph-path", response_model=PlumberConfigResponse)
@app.patch("/api/config/graph-path", response_model=PlumberConfigResponse)
def save_graph_path(
    payload: GraphPathUpdateRequest,
    _: None = Depends(_require_ui_token),
) -> PlumberConfigResponse:
    """Persist only ``LOGSEQ_GRAPH_PATH`` (pre-flight onboarding; avoids full-settings POST)."""
    try:
        validated_path = serialize_plumber_config_field_for_dotenv(
            "logseq_graph_path",
            payload.logseq_graph_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        _apply_dotenv_updates({"LOGSEQ_GRAPH_PATH": validated_path})
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write .env: {exc}") from exc
    reload_plumber_dotenv(override=True)
    graph_root = validate_logseq_graph_path_for_config(validated_path)
    prepare_matryca_runtime(graph_root=graph_root, wiki_config=load_matryca_wiki_config())
    return PlumberConfigResponse.from_lint_config(load_plumber_lint_config())


@app.post("/api/config", response_model=PlumberConfigResponse)
def post_config(
    payload: PlumberConfigResponse,
    _: None = Depends(_require_ui_token),
) -> PlumberConfigResponse:
    """Update ``.env`` and in-process settings from the control-room form."""
    try:
        _update_dotenv(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write .env: {exc}") from exc
    config = load_plumber_lint_config()
    graph_raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    graph_root = None
    if graph_raw:
        try:
            graph_root = resolve_graph_root()
        except ValueError:
            graph_root = None
    prepare_matryca_runtime(graph_root=graph_root, wiki_config=load_matryca_wiki_config())
    return PlumberConfigResponse.from_lint_config(config)


def _daemon_control_response(result: dict[str, Any]) -> DaemonControlResponse:
    return DaemonControlResponse(
        ok=bool(result.get("ok")),
        code=str(result.get("code", "unknown")),
        message=result.get("message") if isinstance(result.get("message"), str) else None,
        pid=int(result["pid"]) if isinstance(result.get("pid"), int) else None,
    )


def _spawn_plumber_daemon_cli(graph_root: Path) -> subprocess.Popen[bytes]:
    """Launch ``plumber start`` in a fresh interpreter (never fork from Uvicorn threads)."""
    env = os.environ.copy()
    env["LOGSEQ_GRAPH_PATH"] = str(graph_root)
    return subprocess.Popen(
        [sys.executable, "-m", "src.cli", "plumber", "start"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _verify_daemon_launch(
    graph_root: Path,
    proc: subprocess.Popen[bytes],
    *,
    settle_s: float = 0.35,
    verify_timeout_s: float = 5.0,
) -> tuple[bool, str | None]:
    """Wait briefly and confirm the daemon published a live PID (or failed fast)."""
    time.sleep(settle_s)
    exit_code = proc.poll()
    if exit_code is not None:
        if exit_code == 0:
            pid = read_pid_file(graph_root)
            if pid is not None and is_plumber_process(pid):
                return True, None
            return False, "Daemon launcher exited but no live PID was published"
        return (
            False,
            f"Daemon exited immediately with code {exit_code} (lock contention or startup error)",
        )
    deadline = time.monotonic() + verify_timeout_s
    while time.monotonic() < deadline:
        pid = read_pid_file(graph_root)
        if pid is not None and is_plumber_process(pid):
            return True, None
        if proc.poll() is not None:
            exit_code = proc.poll()
            if exit_code == 0:
                pid = read_pid_file(graph_root)
                if pid is not None and is_plumber_process(pid):
                    return True, None
            return (
                False,
                f"Daemon launcher exited with code {exit_code} before publishing a PID",
            )
        time.sleep(0.15)
    return False, "Daemon did not publish a live PID after launch"


@app.post("/api/daemon/start", response_model=DaemonControlResponse)
def start_daemon_endpoint(_: None = Depends(_require_ui_token)) -> DaemonControlResponse:
    """Launch the maintenance daemon without blocking the FastAPI event loop."""
    graph_root = _resolve_graph_root_or_raise()
    prepare_matryca_runtime(graph_root=graph_root, wiki_config=load_matryca_wiki_config())
    existing = read_pid_file(graph_root)
    if existing is not None and is_plumber_process(existing):
        return DaemonControlResponse(
            ok=False,
            code="already_running",
            message=f"Matryca Plumber daemon already running (pid {existing})",
            pid=existing,
        )

    proc = _spawn_plumber_daemon_cli(graph_root)
    ok, failure_message = _verify_daemon_launch(graph_root, proc)
    if not ok:
        return DaemonControlResponse(
            ok=False,
            code="start_failed",
            message=failure_message,
        )
    pid = read_pid_file(graph_root)
    return DaemonControlResponse(
        ok=True,
        code="starting",
        message="Daemon launch verified",
        pid=pid,
    )


@app.post("/api/daemon/stop", response_model=DaemonControlResponse)
def stop_daemon_endpoint(_: None = Depends(_require_ui_token)) -> DaemonControlResponse:
    """Signal the maintenance daemon to shut down gracefully."""
    graph_root = _resolve_graph_root_or_raise()
    result: dict[str, Any] = {}

    def _shutdown() -> None:
        result.update(
            stop_daemon(graph_root, grace_seconds=DEFAULT_STOP_GRACE_SECONDS + 5.0),
        )

    thread = threading.Thread(target=_shutdown, daemon=True, name="plumber-daemon-stop")
    thread.start()
    thread.join(timeout=DEFAULT_STOP_GRACE_SECONDS + 10.0)
    if not result:
        return DaemonControlResponse(
            ok=False,
            code="timeout",
            message="Stop request timed out",
        )
    return _daemon_control_response(result)


def _frontend_source_mtime() -> float:
    if not _FRONTEND_SRC.is_dir():
        return 0.0
    return max(
        (path.stat().st_mtime for path in _FRONTEND_SRC.rglob("*") if path.is_file()),
        default=0.0,
    )


def _frontend_dist_mtime() -> float:
    index_html = _FRONTEND_DIST / "index.html"
    if not index_html.is_file():
        return 0.0
    return index_html.stat().st_mtime


def _ensure_frontend_built() -> None:
    """Rebuild ``frontend/dist`` when source files are newer than the last build."""
    package_json = _FRONTEND_ROOT / "package.json"
    if not package_json.is_file():
        return
    if _frontend_source_mtime() <= _frontend_dist_mtime():
        return

    sys.stderr.write("[Matryca Plumber] Rebuilding frontend dashboard…\n")
    sys.stderr.flush()
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=_FRONTEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(
            "[Matryca Plumber] Frontend build failed; "
            "serving the previous dashboard bundle if present.\n"
        )
        if result.stderr.strip():
            sys.stderr.write(result.stderr.strip() + "\n")
        sys.stderr.flush()


def _mount_frontend_assets() -> None:
    """Serve the built React dashboard when ``frontend/dist`` is present."""
    if any(getattr(route, "name", None) == "frontend" for route in app.routes):
        return
    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
        return
    sys.stderr.write(
        f"[Matryca Plumber] frontend/dist not found at {_FRONTEND_DIST}; "
        "run `cd frontend && npm run build` to enable the web dashboard.\n"
    )


_mount_frontend_assets()


def _schedule_browser_open(
    url: str,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_UI_PORT,
    timeout_seconds: float = 15.0,
) -> None:
    """Open the dashboard after the API responds on ``/openapi.json``."""

    def _open_when_ready() -> None:
        health_url = f"http://{host}:{port}/api/health"
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=0.5) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        webbrowser.open(url)

    threading.Thread(target=_open_when_ready, daemon=True).start()


def run_ui_server(*, host: str = "127.0.0.1", port: int = DEFAULT_UI_PORT) -> None:
    """Start Uvicorn and open the Plumber control-room dashboard in the default browser."""
    import uvicorn

    if host == "0.0.0.0" and not _ui_allow_lan():
        raise ValueError(
            "Refusing to bind UI server to 0.0.0.0 without MATRYCA_UI_ALLOW_LAN=1 "
            "(LAN clients could reach authenticated API routes if they obtain a token)."
        )
    if host == "0.0.0.0":
        logger.warning(
            "WARNING: Binding to 0.0.0.0 exposes authenticated API routes on the LAN. "
            "/api/auth/session remains loopback-only; set a strong MATRYCA_UI_TOKEN."
        )

    reload_plumber_dotenv()
    _ensure_frontend_built()
    _mount_frontend_assets()

    dashboard_url = f"http://{host}:{port}/"
    sys.stdout.write(f"Matryca Plumber API listening on http://{host}:{port}\n")
    sys.stdout.write(f"Control room: {dashboard_url}\n")
    if _ui_docs_enabled():
        sys.stdout.write(f"Interactive docs: http://{host}:{port}/docs\n")
    sys.stdout.flush()
    _schedule_browser_open(dashboard_url, host=host, port=port)
    uvicorn.run("src.cli.ui_server:app", host=host, port=port, log_level="info")


__all__ = [
    "DEFAULT_UI_PORT",
    "DaemonControlResponse",
    "DaemonStateResponse",
    "FileStateResponse",
    "GraphAnalyticsResponse",
    "LmModelsResponse",
    "PlumberConfigResponse",
    "UpdateCheckResponse",
    "assert_safe_lm_proxy_url",
    "app",
    "get_config",
    "get_graph_analytics",
    "get_logs",
    "get_state",
    "post_config",
    "run_ui_server",
    "start_daemon_endpoint",
    "stop_daemon_endpoint",
]

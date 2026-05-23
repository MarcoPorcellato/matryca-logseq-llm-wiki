"""FastAPI local API server for the Matryca Plumber UI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from ..agent.maintenance_daemon import (
    DaemonState,
    heal_daemon_state_ledger,
    is_process_alive,
    load_daemon_state,
    read_pid_file,
    resolve_graph_root,
    stop_daemon,
)
from ..agent.plumber_config import (
    PlumberLintConfig,
    load_plumber_lint_config,
    reload_plumber_dotenv,
    resolve_llm_base_url,
)
from ..graph.graph_analytics import compute_graph_analytics
from ..utils.console_sanitize import sanitize_for_console
from ..utils.token_logger import TokenLogger, resolve_plumber_log_path
from ..utils.updater import UpdateCheckResult, check_for_updates, update_check_to_dict

FileStatus = Literal["processed", "skipped", "error", "pending"]
DaemonStatusValue = Literal["running", "idle", "stopped", "error"]

LOCAL_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
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
    alias_count: int = 0
    semantic_links: int = 0
    semantic_cache_mb: float = 0.0
    context_acceleration: float = 94.2
    status: Literal["online", "offline"] = "online"


class DaemonStateResponse(BaseModel):
    """Canonical Plumber checkpoint exposed to the React UI."""

    version: int = 1
    files: dict[str, FileStateResponse] = Field(default_factory=dict)
    status: DaemonStatusValue = "idle"
    model: str
    bootstrap_complete: bool = False
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
    graph_analytics: GraphAnalyticsResponse = Field(default_factory=GraphAnalyticsResponse)

    @classmethod
    def from_daemon_state(
        cls,
        state: DaemonState,
        *,
        graph_root: Path | None = None,
    ) -> DaemonStateResponse:
        if graph_root is not None:
            heal_daemon_state_ledger(graph_root, state)
        payload = state.to_json()
        if graph_root is not None:
            payload["graph_analytics"] = _safe_graph_analytics(
                graph_root,
                ai_links_injected=state.ai_links_injected,
                ai_blocks_healed=state.ai_blocks_healed,
            ).model_dump()
        if payload.get("last_file"):
            payload["last_file"] = sanitize_for_console(str(payload["last_file"]))
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


_LOCAL_LM_PROXY_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _normalize_lm_proxy_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        return normalized[1:-1]
    return normalized


def _is_safe_lm_proxy_host(host: str, *, configured_host: str) -> bool:
    normalized = _normalize_lm_proxy_host(host)
    if normalized in _LOCAL_LM_PROXY_HOSTS:
        return True
    configured = _normalize_lm_proxy_host(configured_host)
    return bool(configured) and normalized == configured


def _validate_lm_models_base_url(base_url: str) -> str:
    """Restrict model discovery to local inference endpoints (SSRF guard)."""
    parsed = urllib.parse.urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="base_url must use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="base_url must include a host")
    configured_host = urllib.parse.urlparse(load_plumber_lint_config().lm_base_url).hostname or ""
    if not _is_safe_lm_proxy_host(hostname, configured_host=configured_host):
        raise HTTPException(status_code=400, detail="base_url host is not allowed")
    return resolve_llm_base_url(override=base_url)


def _fetch_lm_studio_models(base_url: str) -> LmModelsResponse:
    """Query ``GET /v1/models`` on an OpenAI-compatible local inference server."""
    models_url = f"{resolve_llm_base_url(override=base_url).rstrip('/')}/models"
    try:
        request = urllib.request.Request(models_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5.0) as response:
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


def _format_env_value(value: str) -> str:
    if re.search(r'[\s#"\'\\]', value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _serialize_config_value(field: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if field in {"mapreduce_trigger_chars", "mapreduce_chunk_chars"}:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value))
        if isinstance(value, str):
            return str(int(value))
        return str(value)
    if field in {"thermal_delay_bootstrap", "thermal_delay_cognitive"}:
        if isinstance(value, (int, float)):
            return str(float(value))
        if isinstance(value, str):
            return str(float(value))
        return str(value)
    return str(value).strip()


def _update_dotenv(payload: PlumberConfigResponse) -> None:
    """Persist configuration updates to ``.env`` and the active process environment."""
    env_path = _resolve_dotenv_path()
    updates = {
        env_key: _serialize_config_value(field, getattr(payload, field))
        for field, env_key in _ENV_KEY_MAP.items()
    }
    updates["MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS"] = (
        "false" if payload.enable_inline_semantic_corrections else "true"
    )

    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
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
            new_lines.append(f"{key}={_format_env_value(updates[key])}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={_format_env_value(value)}")

    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    load_dotenv(env_path, override=True)


def _ensure_graph_root_env_loaded() -> None:
    """Load repo ``.env`` when ``LOGSEQ_GRAPH_PATH`` is missing from the process env."""
    if os.environ.get("LOGSEQ_GRAPH_PATH", "").strip():
        return
    reload_plumber_dotenv()


app = FastAPI(title="Matryca Plumber API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_graph_root_or_raise() -> Path:
    _ensure_graph_root_env_loaded()
    try:
        return resolve_graph_root()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _safe_graph_analytics(
    graph_root: Path,
    *,
    ai_links_injected: int = 0,
    ai_blocks_healed: int = 0,
) -> GraphAnalyticsResponse:
    """Compute graph telemetry without failing the daemon state endpoint."""
    try:
        return GraphAnalyticsResponse.model_validate(
            compute_graph_analytics(
                graph_root,
                ai_links_injected=ai_links_injected,
                ai_blocks_healed=ai_blocks_healed,
            ).to_dict()
        )
    except Exception as exc:
        logger.warning(
            "Graph analytics failed for graph_root={!r}: {}",
            graph_root,
            exc,
        )
        return GraphAnalyticsResponse(status="offline")


@app.get("/api/graph-analytics", response_model=GraphAnalyticsResponse)
def get_graph_analytics() -> GraphAnalyticsResponse:
    """Return live graph topology telemetry for the configured ``LOGSEQ_GRAPH_PATH``."""
    graph_root = _resolve_graph_root_or_raise()
    state = load_daemon_state(graph_root)
    heal_daemon_state_ledger(graph_root, state)
    return _safe_graph_analytics(
        graph_root,
        ai_links_injected=state.ai_links_injected,
        ai_blocks_healed=state.ai_blocks_healed,
    )


@app.get("/api/state", response_model=DaemonStateResponse)
def get_state() -> DaemonStateResponse:
    """Return the current daemon checkpoint from ``.matryca_daemon_state.json``."""
    graph_root = _resolve_graph_root_or_raise()
    state = load_daemon_state(graph_root)
    return DaemonStateResponse.from_daemon_state(state, graph_root=graph_root)


@app.get("/api/logs", response_model=list[str])
def get_logs() -> list[str]:
    """Return the latest 50 non-empty operational log lines."""
    token_logger = TokenLogger(log_path=resolve_plumber_log_path())
    return [sanitize_for_console(line) for line in token_logger.tail_lines(50)]


@app.get("/api/config", response_model=PlumberConfigResponse)
def get_config() -> PlumberConfigResponse:
    """Return live Plumber hardening thresholds loaded from environment."""
    return PlumberConfigResponse.from_lint_config(load_plumber_lint_config())


@app.get("/api/system/update-check", response_model=UpdateCheckResponse)
async def get_update_check() -> UpdateCheckResponse:
    """Compare the installed package version against the latest PyPI release."""
    result = await check_for_updates()
    return UpdateCheckResponse.from_result(result)


@app.get("/api/lm-models", response_model=LmModelsResponse)
def get_lm_models(base_url: str | None = None) -> LmModelsResponse:
    """Proxy local model discovery for the settings drawer (avoids browser CORS)."""
    resolved = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
    if resolved is None:
        resolved = load_plumber_lint_config().lm_base_url
    safe_base_url = _validate_lm_models_base_url(resolved)
    return _fetch_lm_studio_models(safe_base_url)


@app.post("/api/config", response_model=PlumberConfigResponse)
def post_config(payload: PlumberConfigResponse) -> PlumberConfigResponse:
    """Update ``.env`` and in-process settings from the control-room form."""
    try:
        _update_dotenv(payload)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write .env: {exc}") from exc
    return PlumberConfigResponse.from_lint_config(load_plumber_lint_config())


def _daemon_control_response(result: dict[str, Any]) -> DaemonControlResponse:
    return DaemonControlResponse(
        ok=bool(result.get("ok")),
        code=str(result.get("code", "unknown")),
        message=result.get("message") if isinstance(result.get("message"), str) else None,
        pid=int(result["pid"]) if isinstance(result.get("pid"), int) else None,
    )


def _spawn_plumber_daemon_cli(graph_root: Path) -> None:
    """Launch ``plumber start`` in a fresh interpreter (never fork from Uvicorn threads)."""
    env = os.environ.copy()
    env["LOGSEQ_GRAPH_PATH"] = str(graph_root)
    subprocess.Popen(
        [sys.executable, "-m", "src.cli", "plumber", "start"],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


@app.post("/api/daemon/start", response_model=DaemonControlResponse)
def start_daemon_endpoint() -> DaemonControlResponse:
    """Launch the maintenance daemon without blocking the FastAPI event loop."""
    graph_root = _resolve_graph_root_or_raise()
    existing = read_pid_file(graph_root)
    if existing is not None and is_process_alive(existing):
        return DaemonControlResponse(
            ok=False,
            code="already_running",
            message=f"Matryca Plumber daemon already running (pid {existing})",
            pid=existing,
        )

    _spawn_plumber_daemon_cli(graph_root)
    return DaemonControlResponse(
        ok=True,
        code="starting",
        message="Daemon launch scheduled",
    )


@app.post("/api/daemon/stop", response_model=DaemonControlResponse)
def stop_daemon_endpoint() -> DaemonControlResponse:
    """Signal the maintenance daemon to shut down gracefully."""
    graph_root = _resolve_graph_root_or_raise()
    result: dict[str, Any] = {}

    def _shutdown() -> None:
        result.update(stop_daemon(graph_root))

    thread = threading.Thread(target=_shutdown, daemon=True, name="plumber-daemon-stop")
    thread.start()
    thread.join(timeout=5.0)
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
    port: int = 8000,
    timeout_seconds: float = 15.0,
) -> None:
    """Open the dashboard after the API responds on ``/openapi.json``."""

    def _open_when_ready() -> None:
        health_url = f"http://{host}:{port}/openapi.json"
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


def run_ui_server(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start Uvicorn and open the Plumber control-room dashboard in the default browser."""
    import uvicorn

    reload_plumber_dotenv()
    _ensure_frontend_built()
    _mount_frontend_assets()

    dashboard_url = f"http://{host}:{port}/"
    docs_url = f"http://{host}:{port}/docs"
    sys.stdout.write(f"Matryca Plumber API listening on http://{host}:{port}\n")
    sys.stdout.write(f"Control room: {dashboard_url}\n")
    sys.stdout.write(f"Interactive docs: {docs_url}\n")
    sys.stdout.flush()
    _schedule_browser_open(dashboard_url, host=host, port=port)
    uvicorn.run("src.cli.ui_server:app", host=host, port=port, log_level="info")


__all__ = [
    "DaemonControlResponse",
    "DaemonStateResponse",
    "FileStateResponse",
    "GraphAnalyticsResponse",
    "LmModelsResponse",
    "PlumberConfigResponse",
    "UpdateCheckResponse",
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

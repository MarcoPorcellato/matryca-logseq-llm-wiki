"""Pre-flight readiness checks for the Sovereign UI and daemon startup."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..agent.l1_memory import (
    default_matryca_l1_directory_for_graph,
    ensure_matryca_l1_dir,
    resolve_matryca_l1_directory,
)
from ..agent.plumber_config import (
    load_plumber_lint_config,
    reload_plumber_dotenv,
    resolve_llm_base_url,
)
from ..config import load_matryca_wiki_config
from ..graph.concurrency_probe import probe_concurrency_capability
from ..graph.graph_path_validate import validate_logseq_graph_path_for_config
from .llm_url_policy import UnsafeLlmProxyUrlError, validate_llm_proxy_url
from .runtime_bootstrap import ensure_repo_dotenv_from_example, prepare_matryca_runtime

PreflightStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One row in the pre-flight checklist."""

    id: str
    title: str
    status: PreflightStatus
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Aggregated pre-flight result for ``GET /api/preflight``."""

    ready: bool
    checks: tuple[PreflightCheck, ...]
    env_created_from_example: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "env_created_from_example": self.env_created_from_example,
            "checks": [
                {
                    "id": check.id,
                    "title": check.title,
                    "status": check.status,
                    "message": check.message,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
        }


def _probe_openai_models(base_url: str, *, timeout_s: float = 3.0) -> tuple[bool, str, list[str]]:
    """Return ``(ok, message, model_ids)`` from ``GET /v1/models``."""
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
        with opener.open(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, str(exc), []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return False, "Unexpected response from inference server", []

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())
    return True, "Inference server responded", sorted(set(models))


def _check_environment(*, env_path: Path) -> PreflightCheck:
    if env_path.is_file():
        return PreflightCheck(
            id="environment",
            title="Environment file",
            status="pass",
            message="Repository .env is present",
            detail=str(env_path),
        )
    return PreflightCheck(
        id="environment",
        title="Environment file",
        status="fail",
        message="Repository .env is missing and could not be created from .env.example",
        detail=str(env_path),
    )


def _check_logseq_graph() -> tuple[PreflightCheck, Path | None]:
    raw = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if not raw:
        return (
            PreflightCheck(
                id="logseq_graph",
                title="Logseq graph path",
                status="fail",
                message="LOGSEQ_GRAPH_PATH is not set",
                detail=(
                    "Open Settings and set the absolute path to your Logseq vault root "
                    "(folder containing pages/)."
                ),
            ),
            None,
        )
    try:
        graph_root = validate_logseq_graph_path_for_config(raw)
    except ValueError as exc:
        return (
            PreflightCheck(
                id="logseq_graph",
                title="Logseq graph path",
                status="fail",
                message=str(exc),
                detail=raw,
            ),
            None,
        )
    return (
        PreflightCheck(
            id="logseq_graph",
            title="Logseq graph path",
            status="pass",
            message="Graph root is valid and accessible",
            detail=str(graph_root),
        ),
        graph_root,
    )


def _check_l1_memory(graph_root: Path | None) -> PreflightCheck:
    if graph_root is None:
        return PreflightCheck(
            id="l1_memory",
            title="L1 session memory",
            status="fail",
            message="Configure a valid Logseq graph before provisioning L1 memory",
        )

    wiki_config = load_matryca_wiki_config()
    prepare_matryca_runtime(graph_root=graph_root, wiki_config=wiki_config)
    l1_dir = ensure_matryca_l1_dir(
        logseq_graph_path=str(graph_root),
        memory_path_from_yaml=wiki_config.memory_path,
    )
    if l1_dir is not None and l1_dir.is_dir():
        return PreflightCheck(
            id="l1_memory",
            title="L1 session memory",
            status="pass",
            message="L1 directory is ready",
            detail=str(l1_dir),
        )

    resolved = resolve_matryca_l1_directory(
        logseq_graph_path=str(graph_root),
        memory_path_from_yaml=wiki_config.memory_path,
    )
    if resolved is not None and resolved.is_file() and resolved.suffix.lower() == ".md":
        return PreflightCheck(
            id="l1_memory",
            title="L1 session memory",
            status="pass",
            message="L1 memory file is configured",
            detail=str(resolved),
        )

    planned = default_matryca_l1_directory_for_graph(graph_root)
    return PreflightCheck(
        id="l1_memory",
        title="L1 session memory",
        status="fail",
        message=(
            "matryca-l1 is not ready — use “Create matryca-l1 folder” in the checklist "
            "(path must lie under your home directory or temp)"
        ),
        detail=str(resolved) if resolved is not None else str(planned),
    )


def _check_llm_endpoint() -> PreflightCheck:
    config = load_plumber_lint_config()
    base_url = config.lm_base_url.strip()
    model = config.lm_model.strip()
    if not base_url:
        return PreflightCheck(
            id="llm_endpoint",
            title="Local LLM endpoint",
            status="fail",
            message="LLM base URL is not configured",
            detail="Set LLM_BASE_URL or MATRYCA_LM_BASE_URL in Settings.",
        )
    try:
        safe_url = validate_llm_proxy_url(
            base_url,
            configured_base_url=config.lm_base_url,
        )
    except UnsafeLlmProxyUrlError as exc:
        return PreflightCheck(
            id="llm_endpoint",
            title="Local LLM endpoint",
            status="fail",
            message=str(exc),
            detail=base_url,
        )

    ok, message, models = _probe_openai_models(safe_url, timeout_s=3.0)
    if not ok:
        return PreflightCheck(
            id="llm_endpoint",
            title="Local LLM endpoint",
            status="fail",
            message=f"Inference server unreachable: {message}",
            detail=safe_url,
        )

    if model and models and model not in models:
        return PreflightCheck(
            id="llm_endpoint",
            title="Local LLM endpoint",
            status="warn",
            message=(
                f"Server is up but model {model!r} was not listed; "
                "load it in LM Studio/Ollama or pick another id"
            ),
            detail=f"Available: {', '.join(models[:8])}{'…' if len(models) > 8 else ''}",
        )

    model_hint = f" (model: {model})" if model else ""
    count_hint = f" — {len(models)} model(s) reported" if models else ""
    return PreflightCheck(
        id="llm_endpoint",
        title="Local LLM endpoint",
        status="pass",
        message=f"Inference server is reachable{model_hint}{count_hint}",
        detail=safe_url,
    )


def _check_concurrency() -> PreflightCheck:
    capability = probe_concurrency_capability()
    if capability.mode == "full":
        return PreflightCheck(
            id="concurrency",
            title="Cross-process file locks",
            status="pass",
            message=capability.message,
        )
    if capability.degradation_allowed:
        return PreflightCheck(
            id="concurrency",
            title="Cross-process file locks",
            status="warn",
            message=capability.message,
            detail=(
                "Set MATRYCA_ALLOW_FLOCK_DEGRADATION only when you accept single-writer semantics."
            ),
        )
    return PreflightCheck(
        id="concurrency",
        title="Cross-process file locks",
        status="warn",
        message=capability.message,
        detail="Enable MATRYCA_ALLOW_FLOCK_DEGRADATION or run only one writer process.",
    )


def run_preflight_checks(*, repo_root: Path | None = None) -> PreflightReport:
    """Run all pre-flight checks after ensuring ``.env`` exists and is loaded."""
    root = repo_root or Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    created = ensure_repo_dotenv_from_example(repo_root=root)
    reload_plumber_dotenv()

    checks: list[PreflightCheck] = []
    checks.append(_check_environment(env_path=env_path))
    graph_check, graph_root = _check_logseq_graph()
    checks.append(graph_check)
    checks.append(_check_l1_memory(graph_root))
    checks.append(_check_concurrency())
    checks.append(_check_llm_endpoint())

    ready = all(check.status == "pass" for check in checks)
    return PreflightReport(
        ready=ready,
        checks=tuple(checks),
        env_created_from_example=created,
    )


__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "PreflightStatus",
    "run_preflight_checks",
]

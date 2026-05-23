"""Regression tests for the Matryca Plumber FastAPI UI server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.agent.maintenance_daemon import DaemonState, FileState, save_daemon_state
from src.agent.plumber_config import reload_plumber_dotenv
from src.cli.ui_server import app


@pytest.fixture
def graph_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pages").mkdir()
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))
    return tmp_path


def test_get_state_returns_daemon_checkpoint(graph_root: Path) -> None:
    page = graph_root / "pages" / "Alpha.md"
    page.write_text("- alpha\n- link [[Beta]]\n", encoding="utf-8")
    beta = graph_root / "pages" / "Beta.md"
    beta.write_text("- beta\nalias:: B\n", encoding="utf-8")
    journal = graph_root / "journals" / "2026_05_23.md"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("- journal entry\n", encoding="utf-8")
    state = DaemonState(
        status="running",
        session_prompt_tokens=100,
        session_completion_tokens=25,
        files={
            str(page.resolve()): FileState(
                mtime=page.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    save_daemon_state(graph_root, state)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["session_prompt_tokens"] == 100
    assert payload["session_completion_tokens"] == 25
    assert len(payload["files"]) == 1
    analytics = payload["graph_analytics"]
    assert analytics["total_pages"] == 2
    assert analytics["human_pages"] == 2
    assert analytics["human_links"] == 1
    assert analytics["ai_pages"] == 0
    assert analytics["ai_links"] == 0
    assert analytics["ai_blocks_healed"] == 0
    assert analytics["total_journals"] == 1
    assert payload["ai_pages_created"] == 0
    assert payload["ai_links_injected"] == 0
    assert payload["ai_blocks_healed"] == 0
    assert analytics["alias_count"] == 1
    assert analytics["semantic_links"] == 1
    assert analytics["semantic_cache_mb"] == 0.0
    assert analytics["context_acceleration"] == 94.2


def test_get_graph_analytics_returns_topology(graph_root: Path) -> None:
    page = graph_root / "pages" / "Alpha.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("- alpha\n- link [[Beta]]\n", encoding="utf-8")
    (graph_root / "pages" / "Beta.md").write_text("- beta\nalias:: B\n", encoding="utf-8")

    with TestClient(app) as client:
        response = client.get("/api/graph-analytics")

    assert response.status_code == 200
    analytics = response.json()
    assert analytics["total_pages"] == 2
    assert analytics["alias_count"] == 1
    assert analytics["semantic_links"] == 1


def test_get_state_survives_analytics_failure(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_daemon_state(graph_root, DaemonState(status="idle"))

    def _boom(_root: Path) -> None:
        raise RuntimeError("graph scan failed")

    monkeypatch.setattr("src.cli.ui_server.compute_graph_analytics", _boom)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    analytics = response.json()["graph_analytics"]
    assert analytics["total_pages"] == 0
    assert analytics["context_acceleration"] == 94.2
    assert analytics["status"] == "offline"


def test_get_state_loads_graph_root_from_repo_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_root = tmp_path / "graph"
    (graph_root / "pages").mkdir(parents=True)
    (graph_root / "pages" / "Alpha.md").write_text("- alpha\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(f'LOGSEQ_GRAPH_PATH="{graph_root}"\n', encoding="utf-8")
    monkeypatch.setattr("src.agent.plumber_config._REPO_ROOT", tmp_path)
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    reload_plumber_dotenv()

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    analytics = response.json()["graph_analytics"]
    assert analytics["total_pages"] == 1


def test_get_state_requires_graph_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    monkeypatch.setattr("src.cli.ui_server.reload_plumber_dotenv", lambda: None)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 503


def test_get_config_returns_live_lint_settings(
    tmp_path: Path,
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                f'LOGSEQ_GRAPH_PATH="{graph_root}"',
                'LLM_BASE_URL="http://localhost:9999/v1"',
                'LLM_MODEL_NAME="gemma-4-e4b-it"',
                "MATRYCA_PLUMBER_LOW_PRIORITY_MODE=false",
                "MATRYCA_THERMAL_DELAY_BOOTSTRAP=3.5",
                "MATRYCA_THERMAL_DELAY_COGNITIVE=1.5",
                "MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS=30000",
                "MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS=12000",
                "MATRYCA_PLUMBER_CONTEXT_COMPRESSION=true",
                "MATRYCA_LINT_SEMANTIC_ROUTING=true",
                "MATRYCA_LINT_ENTITY_CONSOLIDATION=true",
                "MATRYCA_LINT_PROPERTY_HYGIENE=true",
                "MATRYCA_LINT_MARPA_FRAMEWORK=true",
                "MATRYCA_LINT_BACKPROPAGATE_LINKS=true",
                "MATRYCA_LINT_HEAL_DANGLING=true",
                "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS=false",
                "MATRYCA_LINT_AUTO_SPLIT=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.agent.plumber_config._REPO_ROOT", tmp_path)
    monkeypatch.setattr("src.cli.ui_server._REPO_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["logseq_graph_path"] == str(graph_root)
    assert payload["lm_studio_url"] == "http://localhost:9999/v1"
    assert payload["lm_model"] == "gemma-4-e4b-it"
    assert payload["low_priority_mode"] is False
    assert payload["thermal_delay_bootstrap"] == 3.5
    assert payload["thermal_delay_cognitive"] == 1.5
    assert payload["mapreduce_trigger_chars"] == 30_000
    assert payload["mapreduce_chunk_chars"] == 12_000
    assert payload["context_compression"] is True
    assert payload["semantic_routing"] is True
    assert payload["entity_consolidation"] is True
    assert payload["property_hygiene"] is True
    assert payload["marpa_framework"] is True
    assert payload["backpropagate_links"] is True
    assert payload["heal_dangling"] is True
    assert payload["enable_inline_semantic_corrections"] is True
    assert payload["auto_split"] is True


def test_post_config_updates_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('LOGSEQ_GRAPH_PATH="/old/path"\n', encoding="utf-8")
    monkeypatch.setattr("src.cli.ui_server._REPO_ROOT", tmp_path)

    body = {
        "logseq_graph_path": "/new/graph",
        "lm_studio_url": "http://localhost:1234/v1",
        "lm_model": "qwen3-8b",
        "low_priority_mode": True,
        "thermal_delay_bootstrap": 2.5,
        "thermal_delay_cognitive": 1.25,
        "mapreduce_trigger_chars": 20000,
        "mapreduce_chunk_chars": 10000,
        "context_compression": False,
        "semantic_routing": True,
        "entity_consolidation": False,
        "property_hygiene": True,
        "marpa_framework": False,
        "backpropagate_links": True,
        "heal_dangling": False,
        "enable_inline_semantic_corrections": True,
        "auto_split": False,
    }

    with TestClient(app) as client:
        response = client.post("/api/config", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["logseq_graph_path"] == "/new/graph"
    assert payload["lm_model"] == "qwen3-8b"
    assert payload["backpropagate_links"] is True
    assert payload["enable_inline_semantic_corrections"] is True
    written = env_path.read_text(encoding="utf-8")
    assert "LOGSEQ_GRAPH_PATH=/new/graph" in written
    assert "LLM_MODEL_NAME=qwen3-8b" in written
    assert "MATRYCA_LINT_BACKPROPAGATE_LINKS=true" in written
    assert "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS=false" in written
    assert "MATRYCA_LINT_PROPERTY_HYGIENE=true" in written


def test_get_lm_models_returns_openai_compatible_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "data": [
                {"id": "qwen3-8b", "object": "model"},
                {"id": "gemma-4-e4b-it", "object": "model"},
            ]
        }
    ).encode("utf-8")

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    monkeypatch.setattr(
        "src.cli.ui_server.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/lm-models",
            params={"base_url": "http://localhost:1234/v1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["models"] == ["gemma-4-e4b-it", "qwen3-8b"]


def test_get_lm_models_surfaces_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("src.cli.ui_server.urllib.request.urlopen", _fail)

    with TestClient(app) as client:
        response = client.get(
            "/api/lm-models",
            params={"base_url": "http://localhost:1234/v1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert "connection refused" in body["error"]


def test_get_lm_models_rejects_unsafe_base_url(graph_root: Path) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/lm-models",
            params={"base_url": "http://169.254.169.254/latest/meta-data/"},
        )

    assert response.status_code == 400
    assert "not allowed" in response.json()["detail"]


def test_get_lm_models_rejects_non_http_scheme() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/lm-models",
            params={"base_url": "file:///etc/passwd"},
        )

    assert response.status_code == 400
    assert "http or https" in response.json()["detail"]


def test_get_state_does_not_persist_healed_ledger(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DaemonState(status="running", ai_links_injected=999)
    save_daemon_state(graph_root, state)
    saved: list[tuple[Path, DaemonState]] = []

    def _forbidden_save(root: Path, payload: DaemonState) -> None:
        saved.append((root, payload))

    monkeypatch.setattr("src.agent.maintenance_daemon.save_daemon_state", _forbidden_save)

    with TestClient(app) as client:
        response = client.get("/api/state")
        client.get("/api/graph-analytics")

    assert response.status_code == 200
    assert saved == []


def test_get_update_check_returns_pypi_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.utils.updater import UpdateCheckResult

    async def fake_check(*, force_refresh: bool = False) -> UpdateCheckResult:
        return UpdateCheckResult(
            current_version="1.5.0",
            latest_version="1.6.0",
            update_available=True,
            pypi_url="https://pypi.org/project/matryca-logseq/",
        )

    monkeypatch.setattr("src.cli.ui_server.check_for_updates", fake_check)

    with TestClient(app) as client:
        response = client.get("/api/system/update-check")

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_version"] == "1.5.0"
    assert payload["latest_version"] == "1.6.0"
    assert payload["update_available"] is True
    assert payload["pypi_url"] == "https://pypi.org/project/matryca-logseq/"


def test_daemon_start_schedules_background_launch(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[Path] = []

    class FakeProc:
        def poll(self) -> None:
            return None

    def fake_spawn(root: Path) -> FakeProc:
        launched.append(root)
        return FakeProc()

    monkeypatch.setattr("src.cli.ui_server._spawn_plumber_daemon_cli", fake_spawn)
    monkeypatch.setattr(
        "src.cli.ui_server._verify_daemon_launch",
        lambda _graph_root, _proc: (True, None),
    )
    monkeypatch.setattr("src.cli.ui_server.read_pid_file", lambda _root: None)

    with TestClient(app) as client:
        response = client.post("/api/daemon/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["code"] == "starting"
    assert launched == [graph_root]


def test_daemon_start_reports_already_running(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.cli.ui_server.read_pid_file", lambda _root: 9001)
    monkeypatch.setattr("src.cli.ui_server.is_process_alive", lambda _pid: True)

    with TestClient(app) as client:
        response = client.post("/api/daemon/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["code"] == "already_running"
    assert payload["pid"] == 9001


def test_daemon_start_reports_launch_failure(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProc:
        def poll(self) -> int:
            return 1

    monkeypatch.setattr(
        "src.cli.ui_server._spawn_plumber_daemon_cli",
        lambda _root: FakeProc(),
    )
    monkeypatch.setattr(
        "src.cli.ui_server._verify_daemon_launch",
        lambda _graph_root, _proc: (False, "Daemon exited immediately with code 1"),
    )
    monkeypatch.setattr("src.cli.ui_server.read_pid_file", lambda _root: None)

    with TestClient(app) as client:
        response = client.post("/api/daemon/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["code"] == "start_failed"


def test_daemon_stop_invokes_shutdown(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[Path] = []

    def fake_stop(root: Path) -> dict[str, object]:
        stopped.append(root)
        return {"ok": True, "code": "signaled", "pid": 9001}

    monkeypatch.setattr("src.cli.ui_server.stop_daemon", fake_stop)

    with TestClient(app) as client:
        response = client.post("/api/daemon/stop")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["code"] == "signaled"
    assert stopped == [graph_root]


def test_get_logs_returns_latest_non_empty_lines(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = graph_root / "ops.log"
    monkeypatch.setenv("MATRYCA_PLUMBER_LOG_PATH", str(log_path))
    lines = [json.dumps({"message": f"event-{index}"}) for index in range(60)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with TestClient(app) as client:
        response = client.get("/api/logs")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 50
    assert "event-59" in payload[-1]
    assert "event-10" in payload[0]


def test_run_ui_server_opens_dashboard_and_starts_uvicorn() -> None:
    from src.cli import ui_server

    with (
        patch("src.cli.ui_server._schedule_browser_open") as mock_schedule,
        patch("uvicorn.run") as mock_run,
    ):
        ui_server.run_ui_server()

    mock_schedule.assert_called_once_with(
        "http://127.0.0.1:8000/",
        host="127.0.0.1",
        port=8000,
    )
    mock_run.assert_called_once_with(
        "src.cli.ui_server:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )


def test_ui_server_serves_frontend_index_when_dist_exists(tmp_path: Path) -> None:
    """GET / must serve the SPA shell when the frontend dist mount is active."""
    from fastapi.staticfiles import StaticFiles
    from src.cli import ui_server
    from starlette.routing import Mount

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text(
        '<!doctype html><html lang="en"><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )

    has_frontend_mount = any(
        isinstance(route, Mount) and route.name == "frontend" for route in ui_server.app.routes
    )
    if not has_frontend_mount:
        ui_server.app.mount(
            "/",
            StaticFiles(directory=str(dist_dir), html=True),
            name="frontend",
        )

    with TestClient(ui_server.app) as client:
        dashboard = client.get("/")
        config = client.get("/api/config")

    assert dashboard.status_code == 200
    assert "text/html" in dashboard.headers.get("content-type", "")
    assert 'id="root"' in dashboard.text
    assert config.status_code == 200

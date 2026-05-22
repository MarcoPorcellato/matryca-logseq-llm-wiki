"""Regression tests for the Matryca Plumber FastAPI UI server."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.agent.maintenance_daemon import DaemonState, FileState, save_daemon_state
from src.cli.ui_server import app


@pytest.fixture
def graph_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pages").mkdir()
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))
    return tmp_path


def test_get_state_returns_daemon_checkpoint(graph_root: Path) -> None:
    page = graph_root / "pages" / "Alpha.md"
    page.write_text("- alpha\n", encoding="utf-8")
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


def test_get_state_requires_graph_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 503


def test_get_config_returns_live_lint_settings(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(graph_root))
    monkeypatch.setenv("MATRYCA_LM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("MATRYCA_PLUMBER_LOW_PRIORITY_MODE", "false")
    monkeypatch.setenv("MATRYCA_THERMAL_DELAY_BOOTSTRAP", "3.5")
    monkeypatch.setenv("MATRYCA_THERMAL_DELAY_COGNITIVE", "1.5")
    monkeypatch.setenv("MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS", "30000")
    monkeypatch.setenv("MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS", "12000")
    monkeypatch.setenv("MATRYCA_PLUMBER_CONTEXT_COMPRESSION", "true")
    monkeypatch.setenv("MATRYCA_LINT_SEMANTIC_ROUTING", "true")
    monkeypatch.setenv("MATRYCA_LINT_ENTITY_CONSOLIDATION", "true")
    monkeypatch.setenv("MATRYCA_LINT_PROPERTY_HYGIENE", "true")
    monkeypatch.setenv("MATRYCA_LINT_MARPA_FRAMEWORK", "true")
    monkeypatch.setenv("MATRYCA_LINT_BACKPROPAGATE_LINKS", "true")
    monkeypatch.setenv("MATRYCA_LINT_HEAL_DANGLING", "true")
    monkeypatch.setenv("MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS", "false")
    monkeypatch.setenv("MATRYCA_LINT_AUTO_SPLIT", "true")

    with TestClient(app) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["logseq_graph_path"] == str(graph_root)
    assert payload["lm_studio_url"] == "http://localhost:9999/v1"
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
    assert payload["backpropagate_links"] is True
    assert payload["enable_inline_semantic_corrections"] is True
    written = env_path.read_text(encoding="utf-8")
    assert "LOGSEQ_GRAPH_PATH=/new/graph" in written
    assert "MATRYCA_LINT_BACKPROPAGATE_LINKS=true" in written
    assert "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS=false" in written
    assert "MATRYCA_LINT_PROPERTY_HYGIENE=true" in written


def test_daemon_start_schedules_background_launch(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[Path] = []

    def fake_start(root: Path) -> dict[str, object]:
        launched.append(root)
        return {"ok": True, "code": "started", "pid": 4242}

    monkeypatch.setattr("src.cli.ui_server.start_daemon_detached", fake_start)
    monkeypatch.setattr("src.cli.ui_server.read_pid_file", lambda _root: None)

    with TestClient(app) as client:
        response = client.post("/api/daemon/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["code"] == "starting"
    time.sleep(0.15)
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

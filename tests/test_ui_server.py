"""Regression tests for the Matryca Plumber FastAPI UI server."""

from __future__ import annotations

import json
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


def test_run_ui_server_opens_docs_and_starts_uvicorn() -> None:
    from src.cli import ui_server

    with (
        patch("webbrowser.open") as mock_open,
        patch("uvicorn.run") as mock_run,
    ):
        ui_server.run_ui_server()

    mock_open.assert_called_once_with("http://127.0.0.1:8000/docs")
    mock_run.assert_called_once_with(
        "src.cli.ui_server:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )

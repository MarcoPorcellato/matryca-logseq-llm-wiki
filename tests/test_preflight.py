"""Tests for Sovereign UI pre-flight readiness checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.cli.ui_auth import reset_ui_token_for_tests
from src.cli.ui_server import app
from src.config import MatrycaWikiConfig
from src.utils.preflight import run_preflight_checks


def _empty_wiki_config() -> MatrycaWikiConfig:
    return MatrycaWikiConfig()


@pytest.fixture(autouse=True)
def ui_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRYCA_UI_TOKEN", "test-ui-token")
    reset_ui_token_for_tests()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Matryca-Token": "test-ui-token"}


def test_run_preflight_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = tmp_path / "vault"
    (graph / "pages").mkdir(parents=True)
    env_example = tmp_path / ".env.example"
    env_example.write_text(
        f'LOGSEQ_GRAPH_PATH="{graph}"\n'
        "MATRYCA_LM_BASE_URL=http://localhost:1234/v1\n"
        "MATRYCA_LM_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.agent.plumber_config.resolve_repo_dotenv_path",
        lambda: tmp_path / ".env",
    )
    monkeypatch.setattr("src.utils.preflight.reload_plumber_dotenv", lambda **_kw: None)
    monkeypatch.setattr("src.config.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.preflight.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.runtime_bootstrap.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MATRYCA_LM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MATRYCA_LM_MODEL", "test-model")
    monkeypatch.delenv("MATRYCA_L1_PATH", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)

    with patch(
        "src.utils.preflight._probe_openai_models",
        return_value=(True, "ok", ["test-model"]),
    ):
        report = run_preflight_checks(repo_root=tmp_path)

    assert report.env_created_from_example is True
    assert (tmp_path / ".env").is_file()
    assert report.ready is True
    statuses = {check.id: check.status for check in report.checks}
    assert statuses["environment"] == "pass"
    assert statuses["logseq_graph"] == "pass"
    assert statuses["l1_memory"] == "pass"
    assert statuses["concurrency"] in {"pass", "warn"}
    assert statuses["llm_endpoint"] == "pass"
    l1_check = next(check for check in report.checks if check.id == "l1_memory")
    assert l1_check.detail is not None
    assert Path(l1_check.detail) == tmp_path / "matryca-l1"
    assert Path(l1_check.detail).is_dir()


def test_get_preflight_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
) -> None:
    graph = tmp_path / "vault"
    (graph / "pages").mkdir(parents=True)
    env_example = tmp_path / ".env.example"
    env_example.write_text(
        f'LOGSEQ_GRAPH_PATH="{graph}"\n'
        "MATRYCA_LM_BASE_URL=http://localhost:1234/v1\n"
        "MATRYCA_LM_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.cli.ui_server._REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "src.agent.plumber_config.resolve_repo_dotenv_path",
        lambda: tmp_path / ".env",
    )
    monkeypatch.setattr("src.utils.preflight.reload_plumber_dotenv", lambda **_kw: None)
    monkeypatch.setattr("src.config.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.preflight.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.runtime_bootstrap.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MATRYCA_LM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MATRYCA_LM_MODEL", "test-model")
    monkeypatch.delenv("MATRYCA_L1_PATH", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)

    with (
        patch(
            "src.utils.preflight._probe_openai_models",
            return_value=(True, "ok", ["test-model"]),
        ),
        TestClient(app) as client,
    ):
        response = client.get("/api/preflight", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert len(payload["checks"]) == 5


def test_run_preflight_l1_passes_when_matryca_l1_path_is_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template ``MATRYCA_L1_PATH`` from ``.env.example`` must not block sibling L1."""
    graph = tmp_path / "vault"
    (graph / "pages").mkdir(parents=True)
    env_path = tmp_path / ".env"
    env_path.write_text(
        f'LOGSEQ_GRAPH_PATH="{graph}"\n'
        'MATRYCA_L1_PATH="/absolute/path/to/matryca-l1"\n'
        "MATRYCA_LM_BASE_URL=http://localhost:1234/v1\n"
        "MATRYCA_LM_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.agent.plumber_config.resolve_repo_dotenv_path",
        lambda: env_path,
    )
    monkeypatch.setattr("src.utils.preflight.reload_plumber_dotenv", lambda **_kw: None)
    monkeypatch.setattr("src.config.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.preflight.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setattr("src.utils.runtime_bootstrap.load_matryca_wiki_config", _empty_wiki_config)
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(graph))
    monkeypatch.setenv("MATRYCA_L1_PATH", "/absolute/path/to/matryca-l1")
    monkeypatch.setenv("MATRYCA_LM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MATRYCA_LM_MODEL", "test-model")

    with patch(
        "src.utils.preflight._probe_openai_models",
        return_value=(True, "ok", ["test-model"]),
    ):
        report = run_preflight_checks(repo_root=tmp_path)

    l1_check = next(check for check in report.checks if check.id == "l1_memory")
    assert l1_check.status == "pass"
    assert l1_check.detail is not None
    assert Path(l1_check.detail) == tmp_path / "matryca-l1"

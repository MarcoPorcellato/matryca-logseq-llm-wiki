"""FastAPI local API server for the Matryca Plumber UI."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agent.maintenance_daemon import DaemonState, load_daemon_state, resolve_graph_root
from ..utils.token_logger import TokenLogger, resolve_plumber_log_path

FileStatus = Literal["processed", "skipped", "error", "pending"]
DaemonStatusValue = Literal["running", "idle", "stopped", "error"]

LOCAL_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
]


class FileStateResponse(BaseModel):
    """On-disk processing record for one markdown file."""

    mtime: float
    processed_at: str
    status: FileStatus
    error: str | None = None


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

    @classmethod
    def from_daemon_state(cls, state: DaemonState) -> DaemonStateResponse:
        return cls.model_validate(state.to_json())


app = FastAPI(title="Matryca Plumber API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_graph_root_or_raise() -> Path:
    try:
        return resolve_graph_root()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/state", response_model=DaemonStateResponse)
def get_state() -> DaemonStateResponse:
    """Return the current daemon checkpoint from ``.matryca_daemon_state.json``."""
    graph_root = _resolve_graph_root_or_raise()
    state = load_daemon_state(graph_root)
    return DaemonStateResponse.from_daemon_state(state)


@app.get("/api/logs", response_model=list[str])
def get_logs() -> list[str]:
    """Return the latest 50 non-empty operational log lines."""
    logger = TokenLogger(log_path=resolve_plumber_log_path())
    return logger.tail_lines(50)


def run_ui_server(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start Uvicorn and open the interactive Swagger docs in the default browser."""
    import uvicorn

    docs_url = f"http://{host}:{port}/docs"
    sys.stdout.write(f"Matryca Plumber API listening on http://{host}:{port}\n")
    sys.stdout.write(f"Interactive docs: {docs_url}\n")
    webbrowser.open(docs_url)
    uvicorn.run("src.cli.ui_server:app", host=host, port=port, log_level="info")


__all__ = [
    "DaemonStateResponse",
    "FileStateResponse",
    "app",
    "get_logs",
    "get_state",
    "run_ui_server",
]

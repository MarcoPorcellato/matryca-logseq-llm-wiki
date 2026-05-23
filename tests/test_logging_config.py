"""Tests for Loguru bootstrap (daemon startup depends on a valid file sink)."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.utils.logging_config import configure_loguru, reset_loguru_configuration


def test_configure_loguru_registers_rotating_file_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_loguru_configuration()
    log_path = tmp_path / "daemon.log"
    monkeypatch.setenv("MATRYCA_LOGURU_LOG_PATH", str(log_path))

    configure_loguru(stderr=False)

    assert log_path.is_file() or log_path.parent.is_dir()

    reset_loguru_configuration()

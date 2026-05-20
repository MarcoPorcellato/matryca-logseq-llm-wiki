"""Tests for background service install/uninstall helpers."""

from __future__ import annotations

import plistlib
import shutil
import sys
from pathlib import Path

import pytest
from src.graph.service_manager import manage_matryca_service

_FAKE_EXE = "/opt/bin/matryca-logseq-llm-wiki"


def _which_matryca(_name: str) -> str:
    return _FAKE_EXE


def test_manage_service_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    out = manage_matryca_service("install")
    assert out["ok"] is False
    assert out["code"] == "unsupported_platform"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_requires_graph_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOGSEQ_GRAPH_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", _which_matryca)
    out = manage_matryca_service("install")
    assert out["ok"] is False
    assert out["code"] == "missing_graph_path"


def test_install_macos_writes_launch_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", "/graphs/demo")
    monkeypatch.setenv("MATRYCA_L1_PATH", "/graphs/l1")
    monkeypatch.setattr(shutil, "which", _which_matryca)

    out = manage_matryca_service("install")
    assert out["ok"] is True
    plist_path = Path(out["unit_path"])
    assert plist_path.is_file()
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["Label"] == "com.matryca.logseq"
    assert payload["ProgramArguments"] == [_FAKE_EXE]
    env = payload["EnvironmentVariables"]
    assert env["LOGSEQ_GRAPH_PATH"] == "/graphs/demo"
    assert env["MATRYCA_L1_PATH"] == "/graphs/l1"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux unit layout")
def test_install_linux_writes_systemd_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", "/graphs/demo")
    monkeypatch.setattr(shutil, "which", _which_matryca)

    out = manage_matryca_service("install")
    assert out["ok"] is True
    unit_path = Path(out["unit_path"])
    text = unit_path.read_text(encoding="utf-8")
    assert f"ExecStart={_FAKE_EXE}" in text
    assert "LOGSEQ_GRAPH_PATH=/graphs/demo" in text


def test_uninstall_macos_idempotent_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    out = manage_matryca_service("uninstall")
    assert out["ok"] is True
    assert "already uninstalled" in out["message"]

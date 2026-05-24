"""Install or remove Matryca as a per-user background service (LaunchAgent / systemd)."""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

ServiceAction = Literal["install", "uninstall"]

_SERVICE_ENV_KEYS: tuple[str, ...] = (
    "LOGSEQ_GRAPH_PATH",
    "MATRYCA_L1_PATH",
    "MATRYCA_WIKI_CONFIG",
    "MATRYCA_GIT_SNAPSHOT_ON_WRITE",
)

_MACOS_LABEL = "com.matryca.logseq"
_LINUX_UNIT_NAME = "matryca-logseq.service"


def _matryca_executable() -> str | None:
    for name in ("matryca-plumber", "matryca-logseq-llm-wiki"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _service_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SERVICE_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            env[key] = value
    return env


def _log_directory() -> Path:
    return Path.home() / ".matryca" / "logs"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_LABEL}.plist"


def _linux_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _LINUX_UNIT_NAME


def _build_macos_plist(executable: str, env: dict[str, str]) -> dict[str, object]:
    log_dir = _log_directory()
    return {
        "Label": _MACOS_LABEL,
        "ProgramArguments": [executable],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "matryca.stdout.log"),
        "StandardErrorPath": str(log_dir / "matryca.stderr.log"),
        "EnvironmentVariables": env,
    }


def _build_linux_unit(executable: str, env: dict[str, str]) -> str:
    log_dir = _log_directory()
    lines = [
        "[Unit]",
        "Description=Matryca Logseq MCP headless service",
        "After=network.target",
        "",
        "[Service]",
        f"ExecStart={executable}",
        "Restart=on-failure",
        f"StandardOutput=append:{log_dir / 'matryca.stdout.log'}",
        f"StandardError=append:{log_dir / 'matryca.stderr.log'}",
    ]
    for key, value in env.items():
        safe_value = value.replace("\n", "").replace("\r", "")
        lines.append(f"Environment={key}={safe_value}")
    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=default.target",
        ],
    )
    return "\n".join(lines) + "\n"


def _install_macos() -> dict[str, Any]:
    executable = _matryca_executable()
    if executable is None:
        return {
            "ok": False,
            "platform": "darwin",
            "code": "executable_not_found",
            "error": "matryca-plumber not found on PATH; install the package first",
        }
    env = _service_environment()
    if not env.get("LOGSEQ_GRAPH_PATH"):
        return {
            "ok": False,
            "platform": "darwin",
            "code": "missing_graph_path",
            "error": "LOGSEQ_GRAPH_PATH must be set before installing the service",
        }
    plist_path = _macos_plist_path()
    log_dir = _log_directory()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with plist_path.open("wb") as handle:
            plistlib.dump(_build_macos_plist(executable, env), handle)
    except OSError as exc:
        return {
            "ok": False,
            "platform": "darwin",
            "code": "permission_denied",
            "error": str(exc),
            "unit_path": str(plist_path),
        }
    return {
        "ok": True,
        "action": "install",
        "platform": "darwin",
        "unit_path": str(plist_path),
        "executable": executable,
        "environment_keys": sorted(env),
        "message": (f"LaunchAgent installed. Load with: launchctl load {plist_path}"),
    }


def _uninstall_macos() -> dict[str, Any]:
    plist_path = _macos_plist_path()
    if not plist_path.is_file():
        return {
            "ok": True,
            "action": "uninstall",
            "platform": "darwin",
            "unit_path": str(plist_path),
            "message": "LaunchAgent plist not present (already uninstalled)",
        }
    try:
        plist_path.unlink()
    except OSError as exc:
        return {
            "ok": False,
            "platform": "darwin",
            "code": "permission_denied",
            "error": str(exc),
            "unit_path": str(plist_path),
        }
    return {
        "ok": True,
        "action": "uninstall",
        "platform": "darwin",
        "unit_path": str(plist_path),
        "message": (f"LaunchAgent removed. Unload if loaded: launchctl unload {plist_path}"),
    }


def _install_linux() -> dict[str, Any]:
    executable = _matryca_executable()
    if executable is None:
        return {
            "ok": False,
            "platform": "linux",
            "code": "executable_not_found",
            "error": "matryca-plumber not found on PATH; install the package first",
        }
    env = _service_environment()
    if not env.get("LOGSEQ_GRAPH_PATH"):
        return {
            "ok": False,
            "platform": "linux",
            "code": "missing_graph_path",
            "error": "LOGSEQ_GRAPH_PATH must be set before installing the service",
        }
    unit_path = _linux_unit_path()
    log_dir = _log_directory()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_build_linux_unit(executable, env), encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "platform": "linux",
            "code": "permission_denied",
            "error": str(exc),
            "unit_path": str(unit_path),
        }
    return {
        "ok": True,
        "action": "install",
        "platform": "linux",
        "unit_path": str(unit_path),
        "executable": executable,
        "environment_keys": sorted(env),
        "message": (
            "systemd user unit installed. Enable with: "
            "systemctl --user daemon-reload && "
            f"systemctl --user enable --now {_LINUX_UNIT_NAME}"
        ),
    }


def _uninstall_linux() -> dict[str, Any]:
    unit_path = _linux_unit_path()
    if not unit_path.is_file():
        return {
            "ok": True,
            "action": "uninstall",
            "platform": "linux",
            "unit_path": str(unit_path),
            "message": "systemd user unit not present (already uninstalled)",
        }
    try:
        unit_path.unlink()
    except OSError as exc:
        return {
            "ok": False,
            "platform": "linux",
            "code": "permission_denied",
            "error": str(exc),
            "unit_path": str(unit_path),
        }
    return {
        "ok": True,
        "action": "uninstall",
        "platform": "linux",
        "unit_path": str(unit_path),
        "message": (
            "systemd user unit removed. Disable if active: "
            f"systemctl --user disable --now {_LINUX_UNIT_NAME}"
        ),
    }


def manage_matryca_service(action: ServiceAction) -> dict[str, Any]:
    """Install or uninstall the Matryca background service for the current OS user."""
    platform = sys.platform
    if platform == "darwin":
        if action == "install":
            return _install_macos()
        return _uninstall_macos()
    if platform.startswith("linux"):
        if action == "install":
            return _install_linux()
        return _uninstall_linux()
    return {
        "ok": False,
        "action": action,
        "platform": platform,
        "code": "unsupported_platform",
        "error": f"Background service install is not supported on {platform}",
    }


__all__ = ["ServiceAction", "manage_matryca_service"]

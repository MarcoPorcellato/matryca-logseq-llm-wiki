"""Optional ``git`` snapshot of the Logseq graph before AI writes (rollback safety).

Pattern: lightweight auto-commit ideas from ``logseq/git-auto`` and MCP-git workflows.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SENSITIVE_GIT_PATH_MARKERS = (
    ".env",
    "credentials",
    "secrets",
    ".pem",
    "id_rsa",
    "private_key",
)


def _status_includes_sensitive_paths(porcelain: str) -> bool:
    for line in porcelain.splitlines():
        path_part = line[3:].strip() if len(line) > 3 else line.strip()
        lowered = path_part.lower()
        if any(marker in lowered for marker in _SENSITIVE_GIT_PATH_MARKERS):
            return True
    return False


def git_snapshot_enabled() -> bool:
    """True when ``MATRYCA_GIT_SNAPSHOT_ON_WRITE`` is a truthy flag."""
    v = os.environ.get("MATRYCA_GIT_SNAPSHOT_ON_WRITE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def snapshot_git_working_tree(
    graph_root: str | Path,
    *,
    message: str = "matryca: AI pre-edit snapshot",
) -> dict[str, object]:
    """Run ``git add -A`` + ``git commit`` under ``graph_root`` when safe and enabled.

    Never mutates git config. Failures are reported in the returned dict; callers
    decide whether to continue with Logseq writes.
    """
    if not git_snapshot_enabled():
        return {
            "enabled": False,
            "skipped": True,
            "reason": "set MATRYCA_GIT_SNAPSHOT_ON_WRITE=true to enable",
            "committed": False,
        }

    root = Path(graph_root).expanduser().resolve(strict=False)
    if not (root / ".git").exists():
        return {
            "enabled": True,
            "skipped": True,
            "reason": "no .git directory at graph root",
            "committed": False,
        }

    def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "matryca-snapshot")
        env.setdefault("GIT_AUTHOR_EMAIL", "matryca-snapshot@local.invalid")
        return subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(root),
            env=env,
        )

    chk = _run(["git", "rev-parse", "--is-inside-work-tree"], timeout=30.0)
    if chk.returncode != 0 or "true" not in chk.stdout.strip().lower():
        return {
            "enabled": True,
            "skipped": True,
            "reason": "not a git working tree",
            "committed": False,
            "stderr": (chk.stderr or "")[:800],
        }

    st = _run(["git", "status", "--porcelain"], timeout=60.0)
    if st.returncode != 0:
        return {
            "enabled": True,
            "skipped": True,
            "reason": "git status failed",
            "committed": False,
            "stderr": (st.stderr or "")[:800],
        }

    if not st.stdout.strip():
        return {
            "enabled": True,
            "skipped": True,
            "reason": "clean working tree; nothing to commit",
            "committed": False,
        }

    if _status_includes_sensitive_paths(st.stdout):
        return {
            "enabled": True,
            "skipped": True,
            "reason": "refusing snapshot: working tree includes sensitive paths (.env, keys, etc.)",
            "committed": False,
        }

    add = _run(["git", "add", "-A"], timeout=120.0)
    if add.returncode != 0:
        return {
            "enabled": True,
            "skipped": True,
            "reason": "git add failed",
            "committed": False,
            "stderr": (add.stderr or "")[:800],
        }

    commit = _run(["git", "commit", "-m", message], timeout=120.0)
    if commit.returncode != 0:
        return {
            "enabled": True,
            "skipped": False,
            "reason": "git commit failed (hooks, identity, or empty index?)",
            "committed": False,
            "stderr": (commit.stderr or "")[:800],
            "stdout": (commit.stdout or "")[:800],
        }

    return {
        "enabled": True,
        "skipped": False,
        "reason": "ok",
        "committed": True,
        "message": message,
    }


__all__ = ["git_snapshot_enabled", "snapshot_git_working_tree"]

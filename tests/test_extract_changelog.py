"""Tests for scripts/extract_changelog.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.extract_changelog import (
    extract_changelog_section,
    iter_changelog_versions,
    normalize_version,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "extract_changelog.py"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

_SAMPLE = """\
# Changelog

## [Unreleased]

### Added

- Foo

## [1.6.2] - 2026-05-27

### Added

- Bar

## [1.6.1] - 2026-05-25

No user-facing changes.
"""


def test_normalize_version_strips_v_prefix() -> None:
    assert normalize_version("v1.6.2") == "1.6.2"
    assert normalize_version("1.6.2") == "1.6.2"


def test_extract_section_includes_heading_and_body() -> None:
    out = extract_changelog_section(_SAMPLE, "1.6.2")
    assert out.startswith("## [1.6.2] - 2026-05-27\n")
    assert "### Added" in out
    assert "- Bar" in out
    assert "## [1.6.1]" not in out


def test_extract_section_accepts_v_tag() -> None:
    out = extract_changelog_section(_SAMPLE, "v1.6.2")
    assert "- Bar" in out


def test_extract_missing_version_raises_lookup() -> None:
    with pytest.raises(LookupError, match="9.9.9"):
        extract_changelog_section(_SAMPLE, "9.9.9")


def test_extract_unreleased_refused() -> None:
    with pytest.raises(ValueError, match="Unreleased"):
        extract_changelog_section(_SAMPLE, "Unreleased")


def test_iter_changelog_versions_skips_unreleased() -> None:
    assert iter_changelog_versions(_SAMPLE) == ["1.6.2", "1.6.1"]


def test_real_changelog_has_recent_releases() -> None:
    text = _CHANGELOG.read_text(encoding="utf-8")
    versions = iter_changelog_versions(text)
    assert versions, "CHANGELOG should contain at least one release section"
    latest = versions[0]
    section = extract_changelog_section(text, f"v{latest}")
    assert section.strip(), f"Latest release section v{latest} should not be empty"


def test_cli_writes_stdout(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_SAMPLE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "v1.6.2", "--changelog", str(changelog)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "- Bar" in proc.stdout


def test_cli_missing_version_exits_nonzero(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_SAMPLE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "0.0.0", "--changelog", str(changelog)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "error:" in proc.stderr

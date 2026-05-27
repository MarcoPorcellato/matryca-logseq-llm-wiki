"""LAN UI binding requires an explicit operator token."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.cli.ui_auth import require_explicit_ui_token_for_lan, reset_ui_token_for_tests


@pytest.fixture(autouse=True)
def _reset_token() -> Iterator[None]:
    reset_ui_token_for_tests()
    yield
    reset_ui_token_for_tests()


def test_require_explicit_ui_token_for_lan_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_UI_ALLOW_LAN", "1")
    monkeypatch.delenv("MATRYCA_UI_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MATRYCA_UI_TOKEN"):
        require_explicit_ui_token_for_lan()


def test_require_explicit_ui_token_for_lan_passes_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_UI_ALLOW_LAN", "1")
    monkeypatch.setenv("MATRYCA_UI_TOKEN", "operator-set-token")
    require_explicit_ui_token_for_lan()

"""Non-blocking page lock probe before expensive LLM work."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from src.graph.io_retry import PageLockUnavailableError
from src.graph.page_write_lock import clear_page_write_locks, page_rmw_lock, probe_page_rmw_lock


@pytest.fixture(autouse=True)
def _clear_lock_registry() -> Iterator[None]:
    clear_page_write_locks()
    yield
    clear_page_write_locks()


def test_probe_page_rmw_lock_succeeds_when_unlocked(tmp_path: Path) -> None:
    target = tmp_path / "pages" / "Open.md"
    target.parent.mkdir(parents=True)
    target.write_text("- open\n", encoding="utf-8")
    probe_page_rmw_lock(target)


def test_probe_page_rmw_lock_fails_when_thread_lock_held(tmp_path: Path) -> None:
    target = tmp_path / "pages" / "Busy.md"
    target.parent.mkdir(parents=True)
    target.write_text("- busy\n", encoding="utf-8")
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with page_rmw_lock(target):
            acquired.set()
            release.wait(timeout=2.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(timeout=2.0)
    try:
        with pytest.raises(PageLockUnavailableError):
            probe_page_rmw_lock(target)
    finally:
        release.set()
        thread.join(timeout=2.0)

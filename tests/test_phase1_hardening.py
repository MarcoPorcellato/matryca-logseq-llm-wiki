"""Phase 1 L5 enterprise audit hardening tests."""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path

import pytest
from src.agent.llm_context_payload import (
    MAX_PAYLOAD_CHARS,
    cap_llm_payload_chars,
)
from src.agent.maintenance_daemon import DaemonState, MaintenanceDaemon, save_daemon_state
from src.graph.graph_analytics import compute_graph_analytics, offline_graph_analytics
from src.graph.io_retry import PageLockUnavailableError
from src.graph.page_write_lock import clear_page_write_locks, page_rmw_lock


def test_cap_llm_payload_chars_truncates_oversized_content() -> None:
    huge = "x" * (MAX_PAYLOAD_CHARS + 10_000)
    capped = cap_llm_payload_chars(huge)
    assert len(capped) <= MAX_PAYLOAD_CHARS
    assert "[TRUNCATED:" in capped
    assert capped.endswith("omitted.]")


def test_cap_llm_payload_chars_preserves_small_content() -> None:
    small = "- intact bullet\n"
    assert cap_llm_payload_chars(small) == small


def test_compute_graph_analytics_offline_when_root_missing(tmp_path: Path) -> None:
    missing = tmp_path / "vanished-graph"
    metrics = compute_graph_analytics(missing)
    assert metrics.status == "offline"
    assert metrics.total_pages == 0


def test_offline_graph_analytics_preserves_ledger_fields() -> None:
    metrics = offline_graph_analytics(ai_links_injected=3, ai_blocks_healed=7)
    assert metrics.status == "offline"
    assert metrics.ai_links == 3
    assert metrics.ai_blocks_healed == 7


def test_page_rmw_lock_raises_after_retry_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_page_write_locks()
    target = tmp_path / "Locked.md"
    target.write_text("start\n", encoding="utf-8")
    lock = threading.Lock()
    lock.acquire()

    monkeypatch.setattr(
        "src.graph.page_write_lock._lock_for_key",
        lambda _key: lock,
    )
    monkeypatch.setattr("src.graph.page_write_lock.IO_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr("src.graph.page_write_lock.IO_RETRY_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr("src.graph.page_write_lock.IO_RETRY_MAX_DELAY_S", 0.02)

    with pytest.raises(PageLockUnavailableError), page_rmw_lock(target):
        pass

    lock.release()


def test_graceful_shutdown_waits_for_inflight_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_root = tmp_path / "graph"
    (graph_root / "pages").mkdir(parents=True)
    daemon = MaintenanceDaemon(graph_root, poll_seconds=0.05)
    state = DaemonState(status="running")
    save_daemon_state(graph_root, state)

    write_started = threading.Event()
    release_write = threading.Event()
    flushed = threading.Event()

    original_end = daemon._end_phase2_write

    def _slow_end() -> None:
        original_end()
        flushed.set()

    monkeypatch.setattr(daemon, "_end_phase2_write", _slow_end)

    def _simulate_active_write() -> None:
        daemon._begin_phase2_write()
        write_started.set()
        release_write.wait(timeout=5.0)
        daemon._end_phase2_write()

    worker = threading.Thread(target=_simulate_active_write, daemon=True)
    worker.start()
    assert write_started.wait(timeout=2.0)

    daemon._handle_daemon_graceful_shutdown(signal.SIGTERM, None)
    assert daemon._stop_requested is True

    finalize_thread = threading.Thread(
        target=lambda: daemon._finalize_graceful_shutdown(state),
        daemon=True,
    )
    finalize_thread.start()
    time.sleep(0.05)
    assert not flushed.is_set()

    release_write.set()
    finalize_thread.join(timeout=5.0)
    assert flushed.is_set()

    reloaded = DaemonState.from_json(
        json.loads((graph_root / ".matryca_daemon_state.json").read_text(encoding="utf-8")),
    )
    assert reloaded.status == "stopped"

"""Tests for Matryca Brain maintenance daemon, token logger, and CLI integration."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from src.agent.maintenance_daemon import (
    SEMANTIC_INDEX_HEADER,
    DaemonState,
    FileState,
    InstructorLLMClient,
    MaintenanceDaemon,
    SemanticCrossRef,
    SemanticIndexResult,
    SemanticLintCorrection,
    append_semantic_index,
    apply_semantic_corrections_to_lines,
    apply_semantic_page_result,
    compute_scan_metrics,
    list_pending_files,
    load_daemon_state,
    prune_stale_daemon_file_entries,
    read_pid_file,
    save_daemon_state,
    stop_daemon,
    write_pid_file,
)
from src.cli import build_parser
from src.cli.tui_dashboard import collect_snapshot
from src.graph.page_write_lock import clear_page_write_locks, page_rmw_lock
from src.utils.token_logger import TokenLogger

BLOCK_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class StubLLM:
    """Deterministic LLM stub for daemon tests."""

    def index_page(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        _ = (page_path, graph_root)
        corrections: list[SemanticLintCorrection] = []
        if BLOCK_UUID in content and "Redis" in content:
            corrections.append(
                SemanticLintCorrection(
                    block_uuid=BLOCK_UUID,
                    original_text="Learn about Redis caching",
                    corrected_text="Learn about [[Redis]] caching",
                    lint_type="auto_wikilink",
                    reason="Link canonical datastore concept",
                ),
            )
        return (
            SemanticIndexResult(
                summary=f"Summary for {page_title}",
                cross_references=[
                    SemanticCrossRef(
                        concept="related idea",
                        relation="see_also",
                        target="[[Other Page]]",
                    ),
                ],
                suggested_tags=["test", "brain"],
                moc_pointers=["[[MOC Test]]"],
                semantic_corrections=corrections,
            ),
            {"prompt_tokens": 42, "completion_tokens": 17},
        )


def _write_page(graph_root: Path, title: str, body: str) -> Path:
    pages = graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    path = pages / f"{title}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    clear_page_write_locks()
    return tmp_path


def test_token_logger_writes_jsonl_and_counts_session(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "matryca_brain_ops.log"
    logger = TokenLogger(log_path=log_path)
    logger.log_turn(
        target_file="pages/Demo.md",
        operation="Concept Indexing",
        prompt_tokens=10,
        completion_tokens=5,
        prompt="analyze this page",
        response='{"summary":"ok"}',
        latency_seconds=0.25,
        model="qwen-test",
    )
    assert logger.session_prompt_tokens == 10
    assert logger.session_completion_tokens == 5
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["operation"] == "Concept Indexing"
    assert payload["prompt_tokens"] == 10
    assert payload["completion_tokens"] == 5
    assert payload["total_tokens"] == 15
    assert payload["model"] == "qwen-test"
    summaries = logger.tail_summaries(1)
    assert summaries
    assert "Concept Indexing" in summaries[0]


def test_daemon_state_roundtrip(graph_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRYCA_LM_MODEL", "qwen-test")
    state = DaemonState(
        files={
            "/tmp/demo.md": FileState(
                mtime=123.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
        status="idle",
        model="qwen-test",
    )
    save_daemon_state(graph_root, state)
    loaded = load_daemon_state(graph_root)
    assert loaded.model == "qwen-test"
    assert loaded.status == "idle"
    assert "/tmp/demo.md" in loaded.files
    assert loaded.files["/tmp/demo.md"].mtime == 123.0


def test_append_semantic_index_preserves_content(graph_root: Path) -> None:
    path = _write_page(graph_root, "Demo", "- Original bullet\n  id:: abc\n")
    original = path.read_text(encoding="utf-8")
    result = SemanticIndexResult(
        summary="A demo page",
        cross_references=[
            SemanticCrossRef(concept="idea", relation="related_to", target="[[Other]]"),
        ],
        suggested_tags=["demo"],
        moc_pointers=["[[MOC Demo]]"],
    )
    append_semantic_index(graph_root, path, result)
    updated = path.read_text(encoding="utf-8")
    assert updated.startswith(original.rstrip("\n"))
    assert SEMANTIC_INDEX_HEADER in updated
    assert "- cross-references::" in updated
    assert "Original bullet" in updated


def test_append_semantic_index_is_idempotent_when_header_exists(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "Indexed",
        f"- note\n\n{SEMANTIC_INDEX_HEADER}\n- summary:: already\n",
    )
    before = path.read_text(encoding="utf-8")
    append_semantic_index(
        graph_root,
        path,
        SemanticIndexResult(summary="should not append"),
    )
    assert path.read_text(encoding="utf-8") == before


def test_maintenance_daemon_processes_pending_page(graph_root: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "brain.log"
    logger = TokenLogger(log_path=log_path)
    path = _write_page(graph_root, "Alpha", "- Alpha concept about graphs\n")
    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=StubLLM(),
        token_logger=logger,
        max_files_per_cycle=5,
    )
    state = daemon.run_cycle()
    key = str(path.resolve())
    assert key in state.files
    assert state.files[key].status == "processed"
    text = path.read_text(encoding="utf-8")
    assert SEMANTIC_INDEX_HEADER in text
    assert "Alpha concept" in text
    assert logger.session_prompt_tokens == 0  # stub does not log via Instructor


def test_pending_detection_skips_processed_mtime(graph_root: Path) -> None:
    path = _write_page(graph_root, "Beta", "- content\n")
    mtime = path.stat().st_mtime
    state = DaemonState(
        files={
            str(path.resolve()): FileState(
                mtime=mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    assert list_pending_files(graph_root, state) == []
    metrics = compute_scan_metrics(graph_root, state)
    assert metrics.total == 1
    assert metrics.processed == 1
    assert metrics.pending == 0
    assert metrics.percent_complete == 100.0


def test_pending_detection_after_file_change(graph_root: Path) -> None:
    path = _write_page(graph_root, "Gamma", "- v1\n")
    key = str(path.resolve())
    state = DaemonState(
        files={
            key: FileState(
                mtime=0.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    pending = list_pending_files(graph_root, state)
    assert path in pending


def test_list_pending_skips_hidden_markdown_paths(graph_root: Path) -> None:
    visible = _write_page(graph_root, "Visible", "- ok\n")
    hidden_dir = graph_root / "pages" / ".hidden"
    hidden_dir.mkdir(parents=True)
    hidden = hidden_dir / "Secret.md"
    hidden.write_text("- secret\n", encoding="utf-8")
    pending = list_pending_files(graph_root, DaemonState())
    assert visible in pending
    assert hidden not in pending


def test_page_rmw_lock_serializes_concurrent_append(graph_root: Path) -> None:
    path = _write_page(graph_root, "LockDemo", "- base\n")
    errors: list[str] = []

    def worker(suffix: str) -> None:
        try:
            with page_rmw_lock(path):
                time.sleep(0.05)
                text = path.read_text(encoding="utf-8")
                path.write_text(text + suffix, encoding="utf-8")
        except OSError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(f"- {i}\n",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    body = path.read_text(encoding="utf-8")
    assert body.count("- base") == 1


def test_pid_file_write_and_read(graph_root: Path) -> None:
    write_pid_file(graph_root)
    pid = read_pid_file(graph_root)
    assert pid == os.getpid()


def test_stop_daemon_removes_stale_pid(graph_root: Path) -> None:
    stale_pid = 9_999_999
    from src.agent.maintenance_daemon import pid_path

    pid_path(graph_root).write_text(f"{stale_pid}\n", encoding="utf-8")
    stop_out = stop_daemon(graph_root)
    assert stop_out["ok"] is True
    assert stop_out["code"] == "stale_pid_removed"


def test_collect_snapshot_reports_metrics(graph_root: Path) -> None:
    _write_page(graph_root, "Snap", "- snap\n")
    snap = collect_snapshot(
        graph_root=graph_root,
        token_logger=TokenLogger(log_path=graph_root / "x.log"),
    )
    assert snap.total_pages == 1
    assert snap.pending_backlog == 1


def test_parser_exposes_brain_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["brain", "start", "--foreground"])
    assert args.command == "brain"
    assert args.brain_action == "start"
    assert args.foreground is True
    args_status = parser.parse_args(["brain", "status"])
    assert args_status.brain_action == "status"
    args_stop = parser.parse_args(["brain", "stop"])
    assert args_stop.brain_action == "stop"


def test_semantic_correction_applies_wikilink(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "LintDemo",
        f"- Learn about Redis caching\n  id:: {BLOCK_UUID}\n",
    )
    result = SemanticIndexResult(
        summary="Redis notes",
        semantic_corrections=[
            SemanticLintCorrection(
                block_uuid=BLOCK_UUID,
                original_text="Learn about Redis caching",
                corrected_text="Learn about [[Redis]] caching",
                lint_type="auto_wikilink",
                reason="Canonical link",
            ),
        ],
    )
    outcome = apply_semantic_page_result(graph_root, path, "LintDemo", result)
    text = path.read_text(encoding="utf-8")
    assert outcome.applied == 1
    assert "[[Redis]]" in text
    assert SEMANTIC_INDEX_HEADER in text
    assert "- semantic-lint-applied::" in text
    assert f"id:: {BLOCK_UUID}" in text


def test_semantic_correction_skips_on_original_mismatch(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "Mismatch",
        f"- Actual bullet text\n  id:: {BLOCK_UUID}\n",
    )
    result = SemanticIndexResult(
        summary="x",
        semantic_corrections=[
            SemanticLintCorrection(
                block_uuid=BLOCK_UUID,
                original_text="Wrong original",
                corrected_text="Wrong original [[Link]]",
                lint_type="auto_wikilink",
                reason="should skip",
            ),
        ],
    )
    outcome = apply_semantic_page_result(graph_root, path, "LintDemo", result)
    after = path.read_text(encoding="utf-8")
    assert outcome.applied == 0
    assert outcome.skipped == 1
    assert "original_mismatch" in outcome.skip_reasons[0]
    assert "Actual bullet text" in after
    assert "[[Link]]" not in after


def test_semantic_correction_rejects_destructive_edit() -> None:
    body = f"- Keep this exact sentence\n  id:: {BLOCK_UUID}\n"
    lines = body.splitlines(keepends=True)
    outcome = apply_semantic_corrections_to_lines(
        lines,
        [
            SemanticLintCorrection(
                block_uuid=BLOCK_UUID,
                original_text="Keep this exact sentence",
                corrected_text="Shorter",
                lint_type="auto_wikilink",
                reason="bad",
            ),
        ],
    )
    assert outcome.applied == 0
    assert "unsafe_correction" in outcome.skip_reasons[0]
    assert lines[0] == "- Keep this exact sentence\n"


def test_anomaly_warning_does_not_modify_block(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "Warn",
        f"- Fragile note\n  id:: {BLOCK_UUID}\n",
    )
    result = SemanticIndexResult(
        summary="warn",
        semantic_corrections=[
            SemanticLintCorrection(
                block_uuid=BLOCK_UUID,
                original_text="Fragile note",
                corrected_text="Fragile note",
                lint_type="anomaly_warning",
                reason="Possible duplicate concept",
            ),
        ],
    )
    outcome = apply_semantic_page_result(graph_root, path, "LintDemo", result)
    text = path.read_text(encoding="utf-8")
    assert outcome.applied == 0
    assert "- Fragile note\n" in text
    assert "- semantic-lint-warnings::" in text
    assert "Possible duplicate concept" in text


def test_maintenance_daemon_applies_semantic_lint(graph_root: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "brain.log"
    logger = TokenLogger(log_path=log_path)
    path = _write_page(
        graph_root,
        "RedisPage",
        f"- Learn about Redis caching\n  id:: {BLOCK_UUID}\n",
    )
    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=StubLLM(),
        token_logger=logger,
        max_files_per_cycle=5,
    )
    daemon.run_cycle()
    text = path.read_text(encoding="utf-8")
    assert "[[Redis]]" in text
    assert SEMANTIC_INDEX_HEADER in text


def test_prune_stale_daemon_file_entries_removes_ghosts(graph_root: Path) -> None:
    live = _write_page(graph_root, "Live", "- ok\n")
    ghost_key = str((graph_root / "pages" / "Deleted.md").resolve())
    state = DaemonState(
        files={
            str(live.resolve()): FileState(
                mtime=live.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
            ghost_key: FileState(
                mtime=1.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="processed",
            ),
        },
    )
    removed = prune_stale_daemon_file_entries(state, graph_root)
    assert removed == 1
    assert ghost_key not in state.files
    assert str(live.resolve()) in state.files


def test_instructor_client_reset_execution_history() -> None:
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    client._append_execution_turn("prompt-a", '{"ok": true}')
    assert len(client._execution_history) == 2
    client.reset_execution_history()
    assert client._execution_history == []


def test_maintenance_daemon_resets_instructor_history_after_cycle(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    client._append_execution_turn("turn-one", '{"indexed": true}')

    def fake_index(
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        _ = (page_title, content, page_path, graph_root)
        return SemanticIndexResult(summary="ok"), {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(client, "index_page", fake_index)
    _write_page(graph_root, "HistoryReset", f"- Learn about Redis caching\n  id:: {BLOCK_UUID}\n")
    daemon = MaintenanceDaemon(graph_root, llm_client=client, max_files_per_cycle=1)
    daemon.run_cycle()
    assert client._execution_history == []


def test_load_daemon_state_overrides_stale_cached_model(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_LM_MODEL", "gemma-4-e4b-it")
    stale = DaemonState(model="qwen2.5-coder-7b")
    save_daemon_state(graph_root, stale)
    loaded = load_daemon_state(graph_root)
    assert loaded.model == "gemma-4-e4b-it"


def test_instructor_client_refresh_config_uses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_LM_MODEL", "gemma-4-e4b-it")
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    assert client.model == "gemma-4-e4b-it"
    monkeypatch.setenv("MATRYCA_LM_MODEL", "qwen3-8b")
    client.refresh_config()
    assert client.model == "qwen3-8b"


def test_instructor_client_explicit_model_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_LM_MODEL", "gemma-4-e4b-it")
    client = InstructorLLMClient(base_url="http://localhost:1234/v1", model="override-model")
    assert client.model == "override-model"
    client.refresh_config()
    assert client.model == "override-model"

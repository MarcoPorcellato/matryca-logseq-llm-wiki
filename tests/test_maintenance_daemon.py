"""Tests for Matryca Plumber maintenance daemon, token logger, and CLI integration."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from src.agent.maintenance_daemon import (
    SEMANTIC_INDEX_HEADER,
    STRUCTURAL_LINT_HEADER,
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
from src.agent.plumber_config import PlumberLintConfig
from src.agent.plumber_llm import BootstrapSummaryResult, GraphInsightsLLMResult
from src.cli import build_parser
from src.cli.tui_dashboard import MONITOR_TITLE, collect_snapshot
from src.graph.bootstrap_harvest import run_bootstrap_harvest
from src.graph.master_catalog import CatalogEntry, load_master_catalog, master_index_page_path
from src.graph.page_write_lock import clear_page_write_locks, page_rmw_lock
from src.utils.token_logger import TokenLogger, format_activity_summary, resolve_plumber_log_path

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
        alias_index: object | None = None,
        enable_semantic_routing: bool = False,
    ) -> tuple[SemanticIndexResult, dict[str, int]]:
        _ = (page_path, graph_root, alias_index, enable_semantic_routing)
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

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> BootstrapSummaryResult:
        _ = (content, page_path, graph_root)
        return BootstrapSummaryResult(summary=f"Summary for {page_title}", suggested_tags=["test"])

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult:
        _ = (metrics_json, graph_root)
        return GraphInsightsLLMResult(
            ontology_report="Stub ontology report.",
            cleanup_suggestions=["Stub cleanup suggestion."],
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
    log_path = tmp_path / "logs" / "matryca_plumber_ops.log"
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


def test_token_logger_tail_lines_reads_trailing_lines_without_full_load(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "matryca_plumber_ops.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    filler = "x" * 5000
    lines_written = [
        json.dumps({"message": f"line-{index}", "payload": filler}) for index in range(200)
    ]
    log_path.write_text("\n".join(lines_written) + "\n", encoding="utf-8")

    logger = TokenLogger(log_path=log_path)
    tail = logger.tail_lines(3)

    assert tail == lines_written[-3:]
    summaries = logger.tail_summaries(2)
    assert len(summaries) == 2
    assert "line-198" in summaries[0]
    assert "line-199" in summaries[1]


def test_load_daemon_state_self_heals_corrupt_json(graph_root: Path) -> None:
    from src.agent.maintenance_daemon import state_path

    state_path(graph_root).write_text("", encoding="utf-8")
    loaded = load_daemon_state(graph_root)
    assert loaded.files == {}
    assert loaded.status == "idle"


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


def test_save_daemon_state_persists_block_ref_error_text(graph_root: Path) -> None:
    """Error strings containing ((uuid)) patterns must not block JSON checkpoint writes."""
    bad_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    error_msg = (
        "Error: Malformed UUID detected in block ref. "
        f"Must be standard 36-char format. (example: (({bad_uuid})))."
    )
    page_key = str((graph_root / "pages" / "Broken.md").resolve())
    state = DaemonState(
        files={
            page_key: FileState(
                mtime=1.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="skipped",
                error=error_msg,
            ),
        },
        status="idle",
    )
    save_daemon_state(graph_root, state)
    loaded = load_daemon_state(graph_root)
    loaded_error = loaded.files[page_key].error
    assert loaded.files[page_key].status == "skipped"
    assert loaded_error == error_msg
    assert loaded_error is not None
    assert f"(({bad_uuid}))" in loaded_error


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


def test_pending_detection_skips_error_with_stable_mtime(graph_root: Path) -> None:
    path = _write_page(graph_root, "ErrorBackoff", "- broken\n")
    mtime = path.stat().st_mtime
    key = str(path.resolve())
    state = DaemonState(
        files={
            key: FileState(
                mtime=mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="error",
                error="LLM timeout",
            ),
        },
    )
    assert list_pending_files(graph_root, state) == []


def test_pending_detection_requeues_error_after_edit(graph_root: Path) -> None:
    path = _write_page(graph_root, "ErrorRetry", "- v1\n")
    key = str(path.resolve())
    state = DaemonState(
        files={
            key: FileState(
                mtime=0.0,
                processed_at="2026-01-01T00:00:00+00:00",
                status="error",
                error="LLM timeout",
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
    assert snap.title == MONITOR_TITLE
    assert snap.total_pages == 1
    assert snap.pending_backlog == 1
    assert snap.pid_file == ".matryca_plumber_daemon.pid"
    assert snap.bootstrap_complete is False


def test_resolve_plumber_log_path_defaults_to_plumber_ops_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATRYCA_PLUMBER_LOG_PATH", raising=False)
    assert resolve_plumber_log_path() == Path("logs") / "matryca_plumber_ops.log"


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        (
            {
                "operation": "Context Compression",
                "message": "[COMPRESSION LLM FAULT] model timeout",
            },
            "[COMPRESSION LLM FAULT]",
        ),
        (
            {
                "operation": "Health Check",
                "target_file": "pages/Broken.md",
                "message": "[STRUCTURAL LINT WARN] malformed block ref",
            },
            "[STRUCTURAL LINT WARN]",
        ),
        (
            {
                "operation": "Context Compression",
                "message": "[CONTEXT COMPRESSION EVENT] initial=100 post=40 ratio=0.4000",
            },
            "[CONTEXT COMPRESSION EVENT]",
        ),
    ],
)
def test_format_activity_summary_handles_plumber_log_tags(
    payload: dict[str, object],
    expected_fragment: str,
) -> None:
    summary = format_activity_summary(payload)
    assert expected_fragment in summary


def test_token_logger_tail_summaries_surface_structural_lint_warnings(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "matryca_plumber_ops.log"
    logger = TokenLogger(log_path=log_path)
    logger.log_structural_lint_warning(
        target_file="pages/Broken.md",
        message="malformed block ref",
        malformed_refs=["((dead-ref))"],
    )
    summaries = logger.tail_summaries(1)
    assert summaries
    assert "[STRUCTURAL LINT WARN]" in summaries[0]
    assert "Broken.md" in summaries[0]


def test_parser_exposes_plumber_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["plumber", "start", "--foreground"])
    assert args.command == "plumber"
    assert args.plumber_action == "start"
    assert args.foreground is True
    args_status = parser.parse_args(["plumber", "status"])
    assert args_status.plumber_action == "status"
    args_ui = parser.parse_args(["plumber", "ui"])
    assert args_ui.plumber_action == "ui"
    args_stop = parser.parse_args(["plumber", "stop"])
    assert args_stop.plumber_action == "stop"
    args_audit = parser.parse_args(["plumber", "audit"])
    assert args_audit.plumber_action == "audit"
    args_cluster = parser.parse_args(["plumber", "cluster"])
    assert args_cluster.plumber_action == "cluster"


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


def test_completion_with_structured_output_uses_lenient_json_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    malformed = (
        '{"ontology_report": "940 pages mapped.", '
        '"cleanup_suggestions": ["Review orphan cluster"]} { "reason": null }'
    )

    class FakeCompletions:
        def create_with_completion(self, **_kwargs: object) -> tuple[object, object]:
            raise RuntimeError("instructor parse failed")

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(
        "src.agent.maintenance_daemon.instructor.from_openai",
        lambda *_args, **_kwargs: FakeClient(),
    )
    monkeypatch.setattr(
        client,
        "_raw_json_completion",
        lambda _messages: (malformed, type("Usage", (), {"usage": None})()),
    )

    parsed, _completion = client._completion_with_structured_output(
        prompt="metrics",
        response_model=GraphInsightsLLMResult,
        system_prompt="system",
        stateless=True,
    )
    assert parsed.ontology_report == "940 pages mapped."
    assert parsed.cleanup_suggestions == ["Review orphan cluster"]


def test_bootstrap_harvest_uses_stateless_messages_when_compression_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1 must not send rolling Ermes history even if context compression is on."""
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    client._append_execution_turn("stale-user", '{"stale": true}')
    client._append_execution_turn("stale-user-2", '{"stale": 2}')
    config = PlumberLintConfig(context_compression=True)
    monkeypatch.setattr(
        "src.agent.maintenance_daemon.load_plumber_lint_config",
        lambda: config,
    )

    messages = client._completion_messages(
        system_prompt="system",
        prompt="current page",
        stateless=True,
    )
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current page"},
    ]
    assert client._execution_history == []


def test_harvest_page_summary_passes_stateless_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = InstructorLLMClient(base_url="http://localhost:1234/v1")
    captured: dict[str, bool] = {}

    def fake_completion(
        self: InstructorLLMClient,
        *,
        prompt: str,
        response_model: type[object],
        system_prompt: str,
        stateless: bool = False,
        **_kwargs: object,
    ) -> tuple[BootstrapSummaryResult, object]:
        _ = (self, prompt, response_model, system_prompt, _kwargs)
        captured["stateless"] = stateless
        return BootstrapSummaryResult(summary="ok"), object()

    monkeypatch.setattr(
        InstructorLLMClient,
        "_completion_with_structured_output",
        fake_completion,
    )
    client.harvest_page_summary("Page", "body")
    assert captured["stateless"] is True


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


def test_daemon_skips_malformed_block_ref_and_processes_healthy_page(
    graph_root: Path,
    tmp_path: Path,
) -> None:
    bad_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee"
    bad_path = _write_page(graph_root, "BrokenRef", f"- link (({bad_uuid}))\n")
    good_path = _write_page(graph_root, "Healthy", "- healthy note\n")
    log_path = tmp_path / "brain.log"
    logger = TokenLogger(log_path=log_path)

    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=StubLLM(),
        token_logger=logger,
        max_files_per_cycle=10,
    )
    state = daemon.run_cycle()

    bad_key = str(bad_path.resolve())
    good_key = str(good_path.resolve())
    assert state.files[bad_key].status == "skipped"
    assert state.files[good_key].status == "processed"
    assert list_pending_files(graph_root, state) == []

    bad_text = bad_path.read_text(encoding="utf-8")
    assert STRUCTURAL_LINT_HEADER in bad_text
    assert "Matryca Broken Reference" in bad_text
    assert SEMANTIC_INDEX_HEADER not in bad_text

    good_text = good_path.read_text(encoding="utf-8")
    assert SEMANTIC_INDEX_HEADER in good_text

    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any("[STRUCTURAL LINT WARN]" in line for line in log_lines)
    payload = json.loads(log_lines[0])
    assert payload["target_file"] == str(bad_path)
    assert bad_uuid in payload["malformed_refs"]

    persisted = load_daemon_state(graph_root)
    persisted_error = persisted.files[bad_key].error
    assert persisted.files[bad_key].status == "skipped"
    assert persisted_error is not None
    assert "Malformed UUID" in persisted_error


def test_run_cycle_prunes_deleted_pages_from_catalog(graph_root: Path) -> None:
    live = _write_page(graph_root, "LivePage", "- still here\n")
    ghost_path = graph_root / "pages" / "GhostPage.md"
    ghost_path.write_text("- gone soon\n", encoding="utf-8")
    catalog = load_master_catalog(graph_root, force_reload=True)
    catalog.upsert("LivePage", CatalogEntry(summary="live", last_mtime=1))
    catalog.upsert("GhostPage", CatalogEntry(summary="ghost", last_mtime=1))
    catalog.save()

    ghost_path.unlink()
    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=StubLLM(),
        max_files_per_cycle=1,
    )
    daemon.run_cycle()

    reloaded = load_master_catalog(graph_root, force_reload=True)
    assert "LivePage" in reloaded.pages
    assert "GhostPage" not in reloaded.pages
    _ = live


def test_bootstrap_pipeline_sets_complete_only_after_master_index(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(graph_root, "Seed", "- type:: risorsa\n- Seed body\n")
    monkeypatch.setenv("MATRYCA_LINT_SEMANTIC_ROUTING", "true")
    monkeypatch.setenv("MATRYCA_LINT_BACKPROPAGATE_LINKS", "true")
    monkeypatch.setenv("MATRYCA_LINT_HEAL_DANGLING", "true")

    daemon = MaintenanceDaemon(graph_root, llm_client=StubLLM(), max_files_per_cycle=1)
    assert daemon.bootstrap_complete is False
    daemon.run_bootstrap_pipeline()
    assert daemon.bootstrap_complete is True
    assert master_index_page_path(graph_root).is_file()

    loaded = load_daemon_state(graph_root)
    assert loaded.bootstrap_complete is True


def test_bootstrap_restart_bypasses_phase1_when_catalog_complete(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(graph_root, "Cached", "- type:: risorsa\n- Cached body\n")
    run_bootstrap_harvest(graph_root, llm=StubLLM(), incremental=False, phase1_strict=True)

    daemon = MaintenanceDaemon(graph_root, llm_client=StubLLM())
    state = load_daemon_state(graph_root)
    daemon._hydrate_bootstrap_phase(state)
    assert daemon.bootstrap_complete is True

    harvest_calls: list[bool] = []

    def _spy_harvest(*_args: object, **_kwargs: object) -> object:
        harvest_calls.append(True)
        from src.graph.bootstrap_harvest import HarvestMetrics

        return HarvestMetrics()

    monkeypatch.setattr(
        "src.agent.maintenance_daemon.run_bootstrap_harvest",
        _spy_harvest,
    )
    daemon.run_bootstrap_pipeline(state)
    assert harvest_calls == []


def test_run_cycle_disables_semantic_routing_until_bootstrap_complete(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_LINT_SEMANTIC_ROUTING", "true")
    monkeypatch.setenv("MATRYCA_LINT_BACKPROPAGATE_LINKS", "true")

    observed: dict[str, bool] = {}

    class RoutingProbeLLM(StubLLM):
        def index_page(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            alias_index: object | None = None,
            enable_semantic_routing: bool = False,
        ) -> tuple[SemanticIndexResult, dict[str, int]]:
            observed["enable_semantic_routing"] = enable_semantic_routing
            return super().index_page(
                page_title,
                content,
                page_path=page_path,
                graph_root=graph_root,
                alias_index=alias_index,
                enable_semantic_routing=enable_semantic_routing,
            )

    _write_page(graph_root, "Probe", "- Probe content\n")
    daemon = MaintenanceDaemon(graph_root, llm_client=RoutingProbeLLM(), max_files_per_cycle=1)
    daemon.bootstrap_complete = False
    daemon.run_cycle()
    assert observed.get("enable_semantic_routing") is False

    run_bootstrap_harvest(graph_root, llm=StubLLM(), incremental=False, phase1_strict=True)
    _write_page(graph_root, "ProbeTwo", "- Second phase content\n")
    daemon.bootstrap_complete = True
    observed.clear()
    daemon.run_cycle()
    assert observed.get("enable_semantic_routing") is True


def test_run_cycle_bulk_fast_track_drains_cached_pages_despite_llm_cap(
    graph_root: Path,
) -> None:
    """Instant skips must not be throttled by max_files_per_cycle or poll pacing."""
    paths: list[Path] = []
    for index in range(5):
        paths.append(
            _write_page(
                graph_root,
                f"Cached{index}",
                f"- body {index}\n\n{SEMANTIC_INDEX_HEADER}\n- summary:: cached\n",
            ),
        )

    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=StubLLM(),
        max_files_per_cycle=1,
    )
    state = daemon.run_cycle()

    for path in paths:
        key = str(path.resolve())
        assert state.files[key].status == "skipped"
    assert list_pending_files(graph_root, state) == []


def test_run_cycle_thermal_delay_only_after_llm_inference(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_THERMAL_DELAY_COGNITIVE", "2.0")
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    _write_page(
        graph_root,
        "AlreadyIndexed",
        f"- note\n\n{SEMANTIC_INDEX_HEADER}\n- summary:: done\n",
    )
    daemon = MaintenanceDaemon(graph_root, llm_client=StubLLM(), max_files_per_cycle=1)
    daemon.run_cycle()
    assert sleeps == []

    _write_page(graph_root, "NeedsLlm", "- fresh content\n")
    sleeps.clear()
    daemon.run_cycle()
    assert sleeps == [2.0]


def test_daemon_state_serializes_phase2_telemetry_fields(graph_root: Path) -> None:
    state = DaemonState(
        status="running",
        bootstrap_complete=True,
        session_prompt_tokens=120,
        session_completion_tokens=45,
        current_cluster="cluster-7",
        phase2_llm_turns=3,
    )
    save_daemon_state(graph_root, state)
    loaded = load_daemon_state(graph_root)
    assert loaded.current_cluster == "cluster-7"
    assert loaded.phase2_llm_turns == 3
    assert loaded.session_prompt_tokens == 120
    assert loaded.session_completion_tokens == 45


def test_save_cycle_checkpoint_syncs_live_token_counters(graph_root: Path) -> None:
    logger = TokenLogger(log_path=graph_root / "ops.log")
    logger.log_turn(
        target_file="pages/Live.md",
        operation="Semantic Linting",
        prompt_tokens=11,
        completion_tokens=4,
        prompt="lint",
        response="ok",
        latency_seconds=0.1,
    )
    daemon = MaintenanceDaemon(graph_root, token_logger=logger, llm_client=StubLLM())
    state = DaemonState(status="idle")
    daemon._sync_live_telemetry(state, cluster_id="cluster-a", mark_running=True)
    daemon._save_cycle_checkpoint(state)

    loaded = load_daemon_state(graph_root)
    assert loaded.session_prompt_tokens == 11
    assert loaded.session_completion_tokens == 4
    assert loaded.current_cluster == "cluster-a"
    assert loaded.status == "running"


def test_run_cycle_increments_phase2_turns_and_persists_tokens(
    graph_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATRYCA_THERMAL_DELAY_COGNITIVE", "0")

    class TokenUsageLLM(StubLLM):
        def index_page(
            self,
            page_title: str,
            content: str,
            *,
            page_path: Path | None = None,
            graph_root: Path | None = None,
            alias_index: object | None = None,
            enable_semantic_routing: bool = False,
        ) -> tuple[SemanticIndexResult, dict[str, int]]:
            result, _usage = super().index_page(
                page_title,
                content,
                page_path=page_path,
                graph_root=graph_root,
                alias_index=alias_index,
                enable_semantic_routing=enable_semantic_routing,
            )
            return result, {"prompt_tokens": 8, "completion_tokens": 2}

    run_bootstrap_harvest(graph_root, llm=StubLLM(), incremental=False, phase1_strict=True)
    _write_page(graph_root, "PhaseTwo", "- phase two body\n")
    logger = TokenLogger(log_path=graph_root / "phase2.log")
    daemon = MaintenanceDaemon(
        graph_root,
        llm_client=TokenUsageLLM(),
        token_logger=logger,
        max_files_per_cycle=1,
    )
    daemon.bootstrap_complete = True
    daemon.run_cycle()

    loaded = load_daemon_state(graph_root)
    assert loaded.phase2_llm_turns >= 1
    assert loaded.last_file is not None
    assert loaded.last_file.endswith("PhaseTwo.md")


def test_structured_completion_logs_tokens_to_shared_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = TokenLogger(log_path=Path("logs/test_structured_tokens.log"))
    client = InstructorLLMClient(
        base_url="http://localhost:1234/v1",
        token_logger=logger,
    )

    class FakeUsage:
        prompt_tokens = 120
        completion_tokens = 30

    class FakeCompletion:
        usage = FakeUsage()

    class FakeCompletions:
        def create_with_completion(self, **_kwargs: object) -> tuple[object, object]:
            from src.agent.plumber_llm import EntityOverlapResult

            return (
                EntityOverlapResult(
                    overlap_score=0.5,
                    canonical_title="Alpha",
                    alias_title="Beta",
                    should_merge_alias=False,
                    reason="stub",
                ),
                FakeCompletion(),
            )

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(
        "src.agent.maintenance_daemon.instructor.from_openai",
        lambda *_args, **_kwargs: FakeClient(),
    )
    client.assess_entity_overlap(title_a="Alpha", title_b="Beta", context="shared context")

    assert logger.session_prompt_tokens == 120
    assert logger.session_completion_tokens == 30


def test_ensure_shared_token_logger_rebinds_injected_client(graph_root: Path) -> None:
    central = TokenLogger(log_path=graph_root / "central.log")
    isolated = TokenLogger(log_path=graph_root / "isolated.log")
    llm = InstructorLLMClient(token_logger=isolated)
    _daemon = MaintenanceDaemon(graph_root, llm_client=llm, token_logger=central)

    assert llm.token_logger is central
    assert llm.token_logger is _daemon.token_logger


def test_phase2_requeues_semantic_index_skipped_pages(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "IndexedPhaseTwo",
        f"- body\n\n{SEMANTIC_INDEX_HEADER}\n- summary:: cached\n",
    )
    key = str(path.resolve())
    state = DaemonState(
        bootstrap_complete=True,
        files={
            key: FileState(
                mtime=path.stat().st_mtime,
                processed_at="2026-01-01T00:00:00+00:00",
                status="skipped",
            ),
        },
    )

    pending = list_pending_files(graph_root, state, bootstrap_complete=True)

    assert path in pending


def test_phase2_fast_track_does_not_skip_semantic_index_pages(graph_root: Path) -> None:
    path = _write_page(
        graph_root,
        "StillNeedsCognitive",
        f"- body\n\n{SEMANTIC_INDEX_HEADER}\n- summary:: cached\n",
    )
    daemon = MaintenanceDaemon(graph_root, llm_client=StubLLM())
    daemon.bootstrap_complete = True
    state = DaemonState(bootstrap_complete=True)

    assert daemon._try_fast_track_cycle_file(path, state) is False

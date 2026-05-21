"""Regression tests for the Matryca Plumber token logger."""

from __future__ import annotations

import json
from pathlib import Path

from src.utils.token_logger import TokenLogger


def test_tail_lines_returns_fresh_trailing_lines_on_each_call(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "matryca_plumber_ops.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = TokenLogger(log_path=log_path)

    first_line = json.dumps({"message": "line-0"})
    log_path.write_text(first_line + "\n", encoding="utf-8")
    assert logger.tail_lines(1) == [first_line]

    second_line = json.dumps({"message": "line-1"})
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(second_line + "\n")

    tail = logger.tail_lines(2)
    assert tail == [first_line, second_line]


def test_tail_lines_reads_trailing_lines_without_full_load(tmp_path: Path) -> None:
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


def test_tail_activity_summaries_skips_shutdown_noise(tmp_path: Path) -> None:
    log_path = tmp_path / "ops.log"
    logger = TokenLogger(log_path=log_path)
    lines = [
        json.dumps({"operation": "Daemon Lifecycle", "message": "[DAEMON SHUTDOWN RECEIVED] x"}),
        json.dumps({"operation": "Daemon Lifecycle", "message": "[DAEMON SHUTDOWN RECEIVED] y"}),
        json.dumps(
            {"operation": "Concept Indexing", "target_file": "pages/A.md", "prompt_tokens": 1},
        ),
        json.dumps(
            {"operation": "Concept Indexing", "target_file": "pages/B.md", "prompt_tokens": 2},
        ),
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summaries = logger.tail_activity_summaries(2)

    assert len(summaries) == 2
    assert all("SHUTDOWN" not in item for item in summaries)
    joined = " ".join(summaries)
    assert "A.md" in joined and "B.md" in joined


def test_tail_lines_gracefully_handles_missing_or_unreadable_log(tmp_path: Path) -> None:
    logger = TokenLogger(log_path=tmp_path / "missing.log")
    assert logger.tail_lines(5) == []


def test_session_token_totals_from_log_sums_since_last_shutdown(tmp_path: Path) -> None:
    log_path = tmp_path / "ops.log"
    logger = TokenLogger(log_path=log_path)
    lines = [
        json.dumps(
            {
                "timestamp": "2026-05-21T08:00:00+00:00",
                "operation": "Concept Indexing",
                "prompt_tokens": 100,
                "completion_tokens": 10,
            },
        ),
        json.dumps(
            {
                "timestamp": "2026-05-21T09:00:00+00:00",
                "operation": "Daemon Lifecycle",
                "message": "[DAEMON SHUTDOWN RECEIVED] stop",
            },
        ),
        json.dumps(
            {
                "timestamp": "2026-05-21T10:00:00+00:00",
                "operation": "Concept Indexing",
                "prompt_tokens": 40,
                "completion_tokens": 5,
            },
        ),
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    prompt_total, completion_total = logger.session_token_totals_from_log()

    assert prompt_total == 40
    assert completion_total == 5

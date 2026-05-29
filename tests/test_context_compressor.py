"""Tests for Matryca Plumber active context compression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.agent.context_compressor import (
    TOKEN_ESTIMATE_SAFETY_MULTIPLIER,
    ChatMessage,
    condense_messages,
    drop_oldest_messages_fraction,
    estimate_messages_tokens,
    estimate_tokens,
    extract_persisted_history,
    truncate_messages_to_budget,
)
from src.utils.token_logger import TokenLogger


def _long_text(repeat: int = 500) -> str:
    return "Processed page [[Alpha]] with MARPA domain progetto and lint corrections. " * repeat


def _build_history(turns: int) -> list[ChatMessage]:
    history: list[ChatMessage] = []
    for i in range(turns):
        history.append({"role": "user", "content": _long_text(80) + f" turn={i}"})
        history.append({"role": "assistant", "content": f'{{"turn": {i}, "status": "ok"}}'})
    return history


def test_estimate_tokens_handles_cjk_and_latin() -> None:
    latin = estimate_tokens("hello world " * 10)
    cjk = estimate_tokens("你好世界" * 10)
    assert latin > 0
    assert cjk > latin


def test_condense_messages_skips_when_under_trigger() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "Root system prompt must survive."},
        {"role": "user", "content": "short prompt"},
    ]
    compressed, event = condense_messages(
        messages,
        trigger=100_000,
        target=30_000,
        compress_fn=lambda _prompt: "should-not-run",
    )
    assert compressed == messages
    assert event is None


def test_condense_messages_sanitizes_compression_summary() -> None:
    degenerate = "Summary line.\n" + "\\n" * 200
    messages: list[ChatMessage] = [
        {"role": "system", "content": "System."},
        *_build_history(6),
        {"role": "user", "content": "Latest."},
    ]
    compressed, event = condense_messages(
        messages,
        trigger=100,
        target=30_000,
        compress_fn=lambda _prompt: degenerate,
    )
    assert event is not None
    consolidated = compressed[1]["content"]
    assert "\\n\\n\\n" not in consolidated
    assert "Summary line." in consolidated


def test_condense_messages_preserves_system_prompt() -> None:
    system_text = "Immutable Matryca Plumber root system prompt."
    messages: list[ChatMessage] = [
        {"role": "system", "content": system_text},
        *_build_history(8),
        {"role": "user", "content": "Current execution turn."},
    ]
    compressed, event = condense_messages(
        messages,
        trigger=100,
        target=30_000,
        compress_fn=lambda _prompt: "- Consolidated page Alpha\n- MARPA: progetto",
    )
    assert event is not None
    assert compressed[0]["role"] == "system"
    assert compressed[0]["content"] == system_text
    assert compressed[-1]["content"] == "Current execution turn."


def test_condense_messages_reduces_message_count_and_tokens() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "System prompt."},
        *_build_history(10),
        {"role": "user", "content": "Latest user prompt."},
    ]
    initial_tokens = estimate_messages_tokens(messages)
    compressed, event = condense_messages(
        messages,
        trigger=100,
        target=30_000,
        compress_fn=lambda _prompt: "Dense consolidated state.",
    )
    assert event is not None
    assert len(compressed) < len(messages)
    assert event.messages_after < event.messages_before
    assert event.post_tokens < event.initial_tokens
    assert event.post_tokens <= initial_tokens
    assert event.compression_ratio < 1.0
    assert any("Consolidated Epistemic State" in msg["content"] for msg in compressed)


def test_extract_persisted_history_excludes_current_user() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "current"},
    ]
    persisted = extract_persisted_history(messages)
    assert persisted == [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "done"},
    ]


def test_token_logger_writes_context_compression_event(tmp_path: Path) -> None:
    logger = TokenLogger(log_path=tmp_path / "ops.log")
    logger.log_compression_event(
        initial_tokens=120_000,
        post_tokens=28_000,
        latency_seconds=1.25,
        compression_ratio=0.2333,
        messages_before=25,
        messages_after=7,
        model="qwen2.5-coder-7b",
    )
    raw = (tmp_path / "ops.log").read_text(encoding="utf-8")
    payload = json.loads(raw.strip())
    assert payload["operation"] == "Context Compression"
    assert "[CONTEXT COMPRESSION EVENT]" in payload["message"]
    assert payload["initial_tokens"] == 120_000
    assert payload["post_tokens"] == 28_000


def test_estimate_tokens_applies_safety_buffer() -> None:
    text = "hello world " * 20
    base = max(1, int(len(text) / 3.8))
    assert estimate_tokens(text) >= int(base * TOKEN_ESTIMATE_SAFETY_MULTIPLIER)


def test_condense_messages_falls_back_to_truncate_when_split_unavailable() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": _long_text(200)},
        {"role": "assistant", "content": '{"turn": 0}'},
        {"role": "user", "content": "Latest user prompt."},
    ]
    assert estimate_messages_tokens(messages) > 100
    compressed, event = condense_messages(
        messages,
        trigger=100,
        target=500,
        preserve_tail_turns=2,
        compress_fn=lambda _prompt: "should-not-run",
    )
    assert event is None
    assert len(compressed) < len(messages)
    assert compressed[0]["role"] == "system"
    assert compressed[-1]["content"] == "Latest user prompt."


def test_condense_messages_falls_back_when_compress_fn_raises() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "System prompt."},
        *_build_history(10),
        {"role": "user", "content": "Latest user prompt."},
    ]
    compressed, event = condense_messages(
        messages,
        trigger=100,
        target=30_000,
        compress_fn=lambda _prompt: (_ for _ in ()).throw(ConnectionError("LM Studio offline")),
    )
    assert event is None
    assert len(compressed) < len(messages)
    assert compressed[0]["role"] == "system"
    assert compressed[-1]["content"] == "Latest user prompt."


def test_truncate_messages_to_budget_preserves_system_and_current_user() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "Immutable system."},
        *_build_history(8),
        {"role": "user", "content": "Current turn."},
    ]
    truncated = truncate_messages_to_budget(messages, target=500)
    assert truncated[0]["content"] == "Immutable system."
    assert truncated[-1]["content"] == "Current turn."
    assert len(truncated) < len(messages)


def test_drop_oldest_messages_fraction_removes_twenty_percent() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "current"},
    ]
    trimmed = drop_oldest_messages_fraction(messages, fraction=0.20)
    assert trimmed[0]["role"] == "system"
    assert trimmed[-1]["content"] == "current"
    assert len(trimmed) < len(messages)


def test_condense_messages_logs_compression_llm_fault(tmp_path: Path) -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "System prompt."},
        *_build_history(10),
        {"role": "user", "content": "Latest user prompt."},
    ]
    logger = TokenLogger(log_path=tmp_path / "ops.log")
    condense_messages(
        messages,
        trigger=100,
        target=30_000,
        compress_fn=lambda _prompt: (_ for _ in ()).throw(ConnectionError("VRAM exhausted")),
        warn_fn=lambda msg: logger.log_compression_warning(msg, model="qwen2.5-coder-7b"),
    )
    payload = json.loads((tmp_path / "ops.log").read_text(encoding="utf-8").strip())
    assert "[COMPRESSION LLM FAULT]" in payload["message"]


def test_token_logger_writes_compression_warning(tmp_path: Path) -> None:
    logger = TokenLogger(log_path=tmp_path / "ops.log")
    logger.log_compression_warning("[COMPRESSION LLM FAULT] timeout", model="qwen2.5-coder-7b")
    payload = json.loads((tmp_path / "ops.log").read_text(encoding="utf-8").strip())
    assert payload["fallback"] is True
    assert "[COMPRESSION LLM FAULT]" in payload["message"]


def test_load_plumber_config_compression_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATRYCA_PLUMBER_CONTEXT_COMPRESSION", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("MATRYCA_LM_MODEL", raising=False)
    monkeypatch.delenv("MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS", raising=False)
    from src.agent.plumber_config import DEFAULT_LLM_MODEL_NAME, load_plumber_lint_config

    cfg = load_plumber_lint_config()
    assert cfg.context_compression is False
    assert cfg.compression_trigger == 100_000
    assert cfg.compression_target == 30_000
    assert cfg.mapreduce_trigger_chars == 25_000
    assert cfg.mapreduce_chunk_chars == 15_000
    assert cfg.lm_model == DEFAULT_LLM_MODEL_NAME


def test_load_plumber_config_clamps_negative_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS", "-1")
    monkeypatch.setenv("MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS", "-500")
    monkeypatch.setenv("MATRYCA_PLUMBER_MAPREDUCE_TRIGGER_CHARS", "-100")
    monkeypatch.setenv("MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS", "-50")
    from src.agent.plumber_config import load_plumber_lint_config

    cfg = load_plumber_lint_config()
    assert cfg.compression_trigger == 0
    assert cfg.compression_target == 0
    assert cfg.mapreduce_trigger_chars == 0
    assert cfg.mapreduce_chunk_chars == 0


def test_any_enabled_includes_context_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRYCA_PLUMBER_CONTEXT_COMPRESSION", "true")
    from src.agent.plumber_config import load_plumber_lint_config

    cfg = load_plumber_lint_config()
    assert cfg.context_compression is True
    assert cfg.any_enabled is True


def test_resolve_llm_base_url_preserves_custom_path_suffix() -> None:
    from src.agent.plumber_config import resolve_llm_base_url

    assert (
        resolve_llm_base_url(override="http://localhost:8080/api/v2")
        == "http://localhost:8080/api/v2"
    )
    assert resolve_llm_base_url(override="http://localhost:1234/v1") == "http://localhost:1234/v1"
    assert resolve_llm_base_url(override="http://localhost:1234") == "http://localhost:1234/v1"


def test_resolve_lm_model_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.plumber_config import DEFAULT_LLM_MODEL_NAME, resolve_llm_model_name

    monkeypatch.setenv("LLM_MODEL_NAME", "gemma-4-e4b-it")
    assert resolve_llm_model_name() == "gemma-4-e4b-it"
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("MATRYCA_LM_MODEL", raising=False)
    assert resolve_llm_model_name() == DEFAULT_LLM_MODEL_NAME

    monkeypatch.setenv("MATRYCA_LM_MODEL", "legacy-model")
    assert resolve_llm_model_name() == "legacy-model"

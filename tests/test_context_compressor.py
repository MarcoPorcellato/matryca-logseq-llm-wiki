"""Tests for Matryca Plumber active context compression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.agent.context_compressor import (
    TOKEN_ESTIMATE_SAFETY_MULTIPLIER,
    ChatMessage,
    condense_messages,
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


def test_token_logger_writes_compression_warning(tmp_path: Path) -> None:
    logger = TokenLogger(log_path=tmp_path / "ops.log")
    logger.log_compression_warning("[COMPRESSION WARN] timeout", model="qwen2.5-coder-7b")
    payload = json.loads((tmp_path / "ops.log").read_text(encoding="utf-8").strip())
    assert payload["fallback"] is True
    assert "[COMPRESSION WARN]" in payload["message"]


def test_load_plumber_config_compression_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATRYCA_PLUMBER_CONTEXT_COMPRESSION", raising=False)
    monkeypatch.delenv("MATRYCA_LM_MODEL", raising=False)
    from src.agent.plumber_config import DEFAULT_LM_MODEL, load_plumber_lint_config

    cfg = load_plumber_lint_config()
    assert cfg.context_compression is False
    assert cfg.compression_trigger == 100_000
    assert cfg.compression_target == 30_000
    assert cfg.lm_model == DEFAULT_LM_MODEL


def test_resolve_lm_model_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.plumber_config import DEFAULT_LM_MODEL, resolve_lm_model

    monkeypatch.setenv("MATRYCA_LM_MODEL", "gemma-4-e4b-it")
    assert resolve_lm_model() == "gemma-4-e4b-it"
    monkeypatch.delenv("MATRYCA_LM_MODEL", raising=False)
    assert resolve_lm_model() == DEFAULT_LM_MODEL

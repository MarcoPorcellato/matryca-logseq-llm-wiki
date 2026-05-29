"""Optional NDJSON debug logging for local LLM JSON resilience investigations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .json_repair import max_consecutive_literal_backslash_n


def _debug_enabled() -> bool:
    return os.environ.get("MATRYCA_LLM_DEBUG_JSON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_log_path() -> Path:
    custom = os.environ.get("MATRYCA_LLM_DEBUG_LOG_PATH", "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parents[2] / ".cursor" / "debug-llm.jsonl"


def completion_usage_tokens(completion: object) -> dict[str, int]:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {}
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def agent_debug_log(
    *,
    location: str,
    message: str,
    data: dict[str, Any],
    hypothesis_id: str,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    if not _debug_enabled():
        return
    try:
        payload = {
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        path = _debug_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # #endregion


__all__ = [
    "agent_debug_log",
    "completion_usage_tokens",
    "max_consecutive_literal_backslash_n",
]

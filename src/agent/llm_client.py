"""Adaptive structured-output LLM client for local OpenAI-compatible servers."""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import instructor
from loguru import logger
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from ..graph.insights_engine import INSIGHTS_SYSTEM_PROMPT
from ..utils.json_repair import parse_llm_json
from ..utils.token_logger import OperationType, TokenLogger
from .context_compressor import (
    COMPRESSION_SYSTEM_PROMPT,
    MAX_EXECUTION_HISTORY_MESSAGES,
    ChatMessage,
    condense_messages,
    extract_persisted_history,
)
from .plumber_config import (
    PlumberLintConfig,
    apply_thermal_pause_bootstrap,
    apply_thermal_pause_cognitive,
    load_plumber_lint_config,
    resolve_llm_api_key,
    resolve_llm_model_name,
    resolve_validated_llm_base_url,
)
from .plumber_llm import (
    BootstrapSummaryResult,
    ContextualSeedResult,
    EntityOverlapResult,
    GraphInsightsLLMResult,
    InferredPropertiesResult,
    MarpaClassificationResult,
)
from .plumber_modules.marpa_framework import (
    build_marpa_classify_system_prompt,
    build_marpa_classify_user_prompt,
)
from .plumber_modules.semantic_cache_router import (
    cache_get,
    cache_put,
    semantic_cache_key,
)
from .prompt_constraints import finalize_system_prompt
from .prompt_layout import build_cache_aligned_prompt

_LLM_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)

ThermalProfile = Literal["none", "bootstrap", "cognitive"]

MAX_SELF_CORRECTION_RETRIES = 3

_CORRECTION_USER_TEMPLATE = (
    "Your previous output failed validation with this error:\n{error}\n"
    "Return corrected JSON only, matching the required schema."
)


class GrammarCapability(StrEnum):
    """Detected structured-output support on the local LLM backend."""

    LOGITS_JSON_SCHEMA = "logits_json_schema"
    JSON_OBJECT_ONLY = "json_object"
    INSTRUCTOR_TOOLS = "instructor_tools"
    LEGACY_TEXT = "legacy_text"


@dataclass(frozen=True, slots=True)
class LlmBackendProfile:
    """Cached capability probe result for one base_url + model pair."""

    base_url: str
    model: str
    grammar_capability: GrammarCapability
    probed_at: float


class LLMResponseError(RuntimeError):
    """Raised when the local LLM returns an empty or malformed completion."""


class StructuredOutputExhaustedError(LLMResponseError):
    """Raised after Path B self-correction retries are exhausted."""


def _extract_first_choice_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("LLM response missing choices")
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None:
        raise LLMResponseError("LLM response choice missing message")
    content = getattr(message, "content", None)
    if content is None or not str(content).strip():
        raise LLMResponseError("LLM response choice has empty content")
    return str(content).strip()


def _cluster_history_enabled() -> bool:
    raw = os.environ.get("MATRYCA_LLM_CLUSTER_HISTORY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _schema_name(model: type[BaseModel]) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", model.__name__) or "Response"


def pydantic_to_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build a strict JSON Schema dict for OpenAI ``response_format``."""
    schema = model.model_json_schema()
    schema.setdefault("additionalProperties", False)
    return schema


def append_correction_turn(messages: list[ChatMessage], *, error: str) -> list[ChatMessage]:
    """Append a user turn asking the model to fix validation failures."""
    updated = list(messages)
    updated.append(
        {
            "role": "user",
            "content": _CORRECTION_USER_TEMPLATE.format(error=error[:4000]),
        },
    )
    return updated


class _ProbeModel(BaseModel):
    ok: bool = True


class AdaptiveStructuredOutputEngine:
    """Capability probe + dual-path structured completions."""

    def __init__(self, client: InstructorLLMClient) -> None:
        self._client = client
        self._backend_profile: LlmBackendProfile | None = None

    def probe_backend(self, *, force: bool = False) -> LlmBackendProfile:
        client = self._client
        if (
            not force
            and self._backend_profile is not None
            and self._backend_profile.base_url == client.base_url
            and self._backend_profile.model == client.model
        ):
            return self._backend_profile

        capability = GrammarCapability.LEGACY_TEXT
        schema = pydantic_to_strict_json_schema(_ProbeModel)
        api_messages = cast(
            Any,
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": '{"ok": true}'},
            ],
        )
        try:
            client._raw_client.chat.completions.create(
                model=client.model,
                messages=api_messages,
                temperature=0.0,
                max_tokens=32,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "probe",
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
            capability = GrammarCapability.LOGITS_JSON_SCHEMA
        except Exception:  # noqa: BLE001 - probe is best-effort
            try:
                client._raw_client.chat.completions.create(
                    model=client.model,
                    messages=api_messages,
                    temperature=0.0,
                    max_tokens=32,
                    response_format={"type": "json_object"},
                )
                capability = GrammarCapability.JSON_OBJECT_ONLY
            except Exception:  # noqa: BLE001
                capability = GrammarCapability.LEGACY_TEXT

        profile = LlmBackendProfile(
            base_url=client.base_url,
            model=client.model,
            grammar_capability=capability,
            probed_at=time.time(),
        )
        self._backend_profile = profile
        logger.info(
            "LLM backend probe: url={} model={} capability={}",
            client.base_url,
            client.model,
            capability.value,
        )
        return profile

    def completion_structured[T: BaseModel](
        self,
        *,
        messages: list[ChatMessage],
        response_model: type[T],
        prompt: str,
        started: float,
        use_history: bool,
        stateless: bool,
        telemetry_target: str | None,
        telemetry_operation: OperationType | None,
        log_tokens: bool,
        thermal_profile: ThermalProfile,
        kv_prefix_hash: str | None = None,
    ) -> tuple[T, object]:
        profile = self.probe_backend()
        if profile.grammar_capability == GrammarCapability.LOGITS_JSON_SCHEMA:
            return self._path_a_completion(
                messages=messages,
                response_model=response_model,
                prompt=prompt,
                started=started,
                use_history=use_history,
                stateless=stateless,
                telemetry_target=telemetry_target,
                telemetry_operation=telemetry_operation,
                log_tokens=log_tokens,
                thermal_profile=thermal_profile,
                kv_prefix_hash=kv_prefix_hash,
            )
        return self._path_b_completion(
            messages=messages,
            response_model=response_model,
            profile=profile,
            prompt=prompt,
            started=started,
            use_history=use_history,
            stateless=stateless,
            telemetry_target=telemetry_target,
            telemetry_operation=telemetry_operation,
            log_tokens=log_tokens,
            thermal_profile=thermal_profile,
            kv_prefix_hash=kv_prefix_hash,
        )

    def _path_a_completion[T: BaseModel](
        self,
        *,
        messages: list[ChatMessage],
        response_model: type[T],
        prompt: str,
        started: float,
        use_history: bool,
        stateless: bool,
        telemetry_target: str | None,
        telemetry_operation: OperationType | None,
        log_tokens: bool,
        thermal_profile: ThermalProfile,
        kv_prefix_hash: str | None = None,
    ) -> tuple[T, object]:
        client = self._client
        schema = pydantic_to_strict_json_schema(response_model)
        api_messages = cast(Any, messages)
        response = client._raw_client.chat.completions.create(
            model=client.model,
            messages=api_messages,
            temperature=0.1,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(response_model),
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        raw_text = _extract_first_choice_content(response)
        parsed = response_model.model_validate_json(raw_text)
        if use_history:
            client._append_execution_turn(prompt, parsed.model_dump_json())
        if stateless:
            client.reset_execution_history()
        finalized = client._finalize_structured_completion(
            parsed=parsed,
            completion=response,
            prompt=prompt,
            started=started,
            telemetry_target=telemetry_target,
            telemetry_operation=telemetry_operation,
            log_tokens=log_tokens,
            kv_prefix_hash=kv_prefix_hash,
        )
        client._apply_thermal_after_completion(thermal_profile)
        return finalized

    def _path_b_completion[T: BaseModel](
        self,
        *,
        messages: list[ChatMessage],
        response_model: type[T],
        profile: LlmBackendProfile,
        prompt: str,
        started: float,
        use_history: bool,
        stateless: bool,
        telemetry_target: str | None,
        telemetry_operation: OperationType | None,
        log_tokens: bool,
        thermal_profile: ThermalProfile,
        kv_prefix_hash: str | None = None,
    ) -> tuple[T, object]:
        client = self._client
        if profile.grammar_capability == GrammarCapability.INSTRUCTOR_TOOLS:
            mode = instructor.Mode.TOOLS
        else:
            mode = instructor.Mode.MD_JSON
        instructor_client = instructor.from_openai(client._raw_client, mode=mode)
        last_error = ""
        working_messages = list(messages)

        for _attempt in range(MAX_SELF_CORRECTION_RETRIES):
            try:
                parsed, completion = instructor_client.chat.completions.create_with_completion(
                    model=client.model,
                    messages=working_messages,
                    response_model=response_model,
                    max_retries=1,
                )
                if use_history:
                    client._append_execution_turn(prompt, parsed.model_dump_json())
                if stateless:
                    client.reset_execution_history()
                finalized = client._finalize_structured_completion(
                    parsed=parsed,
                    completion=completion,
                    prompt=prompt,
                    started=started,
                    telemetry_target=telemetry_target,
                    telemetry_operation=telemetry_operation,
                    log_tokens=log_tokens,
                    kv_prefix_hash=kv_prefix_hash,
                )
                client._apply_thermal_after_completion(thermal_profile)
                return finalized
            except ValidationError as exc:
                last_error = str(exc)
                working_messages = append_correction_turn(working_messages, error=last_error)
            except Exception as exc:  # noqa: BLE001 - try raw JSON salvage
                last_error = str(exc)
                try:
                    raw_text, completion = client._raw_json_completion(
                        working_messages,
                        thermal_profile=thermal_profile,
                    )
                    parsed = parse_llm_json(raw_text, response_model)
                    if use_history:
                        client._append_execution_turn(prompt, parsed.model_dump_json())
                    if stateless:
                        client.reset_execution_history()
                    finalized = client._finalize_structured_completion(
                        parsed=parsed,
                        completion=completion,
                        prompt=prompt,
                        started=started,
                        telemetry_target=telemetry_target,
                        telemetry_operation=telemetry_operation,
                        log_tokens=log_tokens,
                        kv_prefix_hash=kv_prefix_hash,
                    )
                    client._apply_thermal_after_completion(thermal_profile)
                    return finalized
                except (ValidationError, Exception) as inner_exc:  # noqa: BLE001
                    last_error = str(inner_exc)
                    working_messages = append_correction_turn(
                        working_messages,
                        error=last_error,
                    )

        logger.warning(
            "Structured output exhausted after {} retries (model={}, url={}, capability={}): {}",
            MAX_SELF_CORRECTION_RETRIES,
            client.model,
            client.base_url,
            profile.grammar_capability.value,
            last_error,
        )
        msg = (
            f"structured_output_exhausted: {last_error}"
            if last_error
            else "structured_output_exhausted"
        )
        raise StructuredOutputExhaustedError(msg)


class InstructorLLMClient:
    """OpenAI-compatible local LLM client (LM Studio, Ollama, …)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        token_logger: TokenLogger | None = None,
    ) -> None:
        self.token_logger = token_logger or TokenLogger()
        self._explicit_model = model
        self._explicit_base_url = base_url
        self._explicit_api_key = api_key
        self.base_url = resolve_validated_llm_base_url(override=base_url)
        self.model = resolve_llm_model_name(override=model)
        self.api_key = resolve_llm_api_key(override=api_key)
        self._raw_client: OpenAI = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=_LLM_HTTP_TIMEOUT,
        )
        self._execution_history: list[ChatMessage] = []
        self._runtime_lint_config: PlumberLintConfig | None = None
        self.thermal_stop_event: threading.Event | None = None
        self._structured_engine = AdaptiveStructuredOutputEngine(self)

    def bind_lint_config(self, config: PlumberLintConfig | None) -> None:
        self._runtime_lint_config = config

    def _active_lint_config(self) -> PlumberLintConfig:
        return self._runtime_lint_config or load_plumber_lint_config()

    def refresh_config(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        resolved_model = model if model is not None else self._explicit_model
        resolved_base = base_url if base_url is not None else self._explicit_base_url
        resolved_api_key = api_key if api_key is not None else self._explicit_api_key
        self.base_url = resolve_validated_llm_base_url(override=resolved_base)
        self.model = resolve_llm_model_name(override=resolved_model)
        self.api_key = resolve_llm_api_key(override=resolved_api_key)
        self._raw_client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=_LLM_HTTP_TIMEOUT,
        )
        self._structured_engine._backend_profile = None

    def probe_backend(self, *, force: bool = False) -> LlmBackendProfile:
        return self._structured_engine.probe_backend(force=force)

    def reset_execution_history(self) -> None:
        self._execution_history.clear()

    def inject_cluster_focus_context(self, neighborhood_text: str) -> None:
        self.reset_execution_history()
        focus_prompt = (
            "[CLUSTER FOCUS: NEIGHBORHOOD MAP]\n"
            "Below are the summaries of all nodes in this isolated semantic cluster:\n\n"
            f"{neighborhood_text}"
        )
        self._execution_history.append({"role": "user", "content": focus_prompt})
        if _cluster_history_enabled():
            self._execution_history.append(
                {
                    "role": "assistant",
                    "content": (
                        "Acknowledged. I will process pages within this semantic neighborhood, "
                        "prioritizing dense cross-links and alias consolidation among these "
                        "related nodes."
                    ),
                },
            )
        else:
            self._execution_history.append(
                {"role": "assistant", "content": "Acknowledged."},
            )

    def _trim_execution_history(self) -> None:
        if len(self._execution_history) > MAX_EXECUTION_HISTORY_MESSAGES:
            self._execution_history = self._execution_history[-MAX_EXECUTION_HISTORY_MESSAGES:]

    def _append_execution_turn(self, prompt: str, response: str) -> None:
        self._execution_history.append({"role": "user", "content": prompt})
        self._execution_history.append({"role": "assistant", "content": response})
        self._trim_execution_history()

    def _completion_messages(
        self,
        *,
        system_prompt: str,
        prompt: str,
        stateless: bool,
    ) -> list[ChatMessage]:
        if stateless:
            self.reset_execution_history()
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        messages: list[ChatMessage] = [{"role": "system", "content": system_prompt}]
        messages.extend(self._execution_history)
        messages.append({"role": "user", "content": prompt})
        return messages

    @staticmethod
    def _extract_completion_usage(completion: object) -> dict[str, int]:
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        usage_obj = getattr(completion, "usage", None)
        if usage_obj is not None:
            usage["prompt_tokens"] = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
            usage["completion_tokens"] = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        return usage

    def _log_completion_turn(
        self,
        *,
        completion: object,
        target_file: str,
        operation: OperationType,
        prompt: str,
        response: str,
        latency_seconds: float,
        ok: bool = True,
        error: str | None = None,
        kv_prefix_hash: str | None = None,
    ) -> dict[str, int]:
        usage = self._extract_completion_usage(completion)
        self.token_logger.log_turn(
            target_file=target_file,
            operation=operation,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            prompt=prompt,
            response=response,
            latency_seconds=latency_seconds,
            model=self.model,
            ok=ok,
            error=error,
            kv_prefix_hash=kv_prefix_hash,
        )
        return usage

    def _compress_history_via_llm(self, compression_prompt: str) -> str:
        started = time.perf_counter()
        response = self._raw_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": COMPRESSION_SYSTEM_PROMPT},
                {"role": "user", "content": compression_prompt},
            ],
            temperature=0.1,
        )
        content = _extract_first_choice_content(response)
        self._log_completion_turn(
            completion=response,
            target_file="execution_history",
            operation="Context Compression",
            prompt=compression_prompt,
            response=content,
            latency_seconds=time.perf_counter() - started,
        )
        return content

    def _apply_thermal_after_completion(self, profile: ThermalProfile) -> None:
        cfg = self._active_lint_config()
        stop_event = self.thermal_stop_event
        if profile == "bootstrap":
            apply_thermal_pause_bootstrap(cfg, stop_event=stop_event)
        elif profile == "cognitive":
            apply_thermal_pause_cognitive(cfg, stop_event=stop_event)

    def _raw_json_completion(
        self,
        messages: list[ChatMessage],
        *,
        thermal_profile: ThermalProfile = "cognitive",
    ) -> tuple[str, object]:
        _ = thermal_profile
        api_messages = cast(Any, messages)
        try:
            response = self._raw_client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception:  # noqa: BLE001
            response = self._raw_client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                temperature=0.1,
            )
        content = _extract_first_choice_content(response)
        return content, response

    def _finalize_structured_completion[T: BaseModel](
        self,
        *,
        parsed: T,
        completion: object,
        prompt: str,
        started: float,
        telemetry_target: str | None,
        telemetry_operation: OperationType | None,
        log_tokens: bool,
        kv_prefix_hash: str | None = None,
    ) -> tuple[T, object]:
        if log_tokens and telemetry_target and telemetry_operation:
            self._log_completion_turn(
                completion=completion,
                target_file=telemetry_target,
                operation=telemetry_operation,
                prompt=prompt,
                response=parsed.model_dump_json(),
                latency_seconds=time.perf_counter() - started,
                kv_prefix_hash=kv_prefix_hash,
            )
        return parsed, completion

    def _completion_with_structured_output[T: BaseModel](
        self,
        *,
        prompt: str,
        response_model: type[T],
        system_prompt: str,
        stateless: bool = False,
        telemetry_target: str | None = None,
        telemetry_operation: OperationType | None = None,
        log_tokens: bool = True,
        thermal_profile: ThermalProfile = "cognitive",
        kv_prefix_hash: str | None = None,
    ) -> tuple[T, object]:
        started = time.perf_counter()
        self.refresh_config()
        config = self._active_lint_config()
        messages = self._completion_messages(
            system_prompt=system_prompt,
            prompt=prompt,
            stateless=stateless,
        )
        use_history = not stateless
        if config.context_compression and not stateless:
            messages, compression_event = condense_messages(
                messages,
                trigger=config.compression_trigger,
                target=config.compression_target,
                compress_fn=self._compress_history_via_llm,
                warn_fn=lambda msg: self.token_logger.log_compression_warning(
                    msg,
                    model=self.model,
                ),
            )
            if compression_event is not None:
                self._execution_history = extract_persisted_history(messages)
                self.token_logger.log_compression_event(
                    initial_tokens=compression_event.initial_tokens,
                    post_tokens=compression_event.post_tokens,
                    latency_seconds=compression_event.latency_seconds,
                    compression_ratio=compression_event.compression_ratio,
                    messages_before=compression_event.messages_before,
                    messages_after=compression_event.messages_after,
                    model=self.model,
                )
        return self._structured_engine.completion_structured(
            messages=messages,
            response_model=response_model,
            prompt=prompt,
            started=started,
            use_history=use_history,
            stateless=stateless,
            telemetry_target=telemetry_target,
            telemetry_operation=telemetry_operation,
            log_tokens=log_tokens,
            thermal_profile=thermal_profile,
            kv_prefix_hash=kv_prefix_hash,
        )

    def generate_contextual_seed(
        self,
        *,
        link_title: str,
        source_page: str,
        context: str,
        max_words: int,
    ) -> ContextualSeedResult:
        prompt = build_cache_aligned_prompt(
            content=context,
            task_instruction=(
                "Task: write a contextual seed definition for a dangling wikilink.\n"
                f"Target link: [[{link_title}]]\n"
                f"Source page: [[{source_page}]]\n"
                f"Max length: {max_words} words."
            ),
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=ContextualSeedResult,
            system_prompt=finalize_system_prompt(
                "You seed new Logseq pages from local context. Return JSON only. "
                "Write a neutral, concise definition without markdown headings."
            ),
            stateless=True,
            telemetry_target=source_page,
            telemetry_operation="Semantic Linting",
        )
        return result

    def assess_entity_overlap(
        self,
        *,
        title_a: str,
        title_b: str,
        context: str,
    ) -> EntityOverlapResult:
        prompt = build_cache_aligned_prompt(
            content=context[:3000],
            task_instruction=(
                "Task: decide whether two page titles refer to the same concept.\n"
                f"Title A: {title_a}\n"
                f"Title B: {title_b}"
            ),
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=EntityOverlapResult,
            system_prompt=finalize_system_prompt(
                "You are an entity consolidation linter for Logseq. Prefer one canonical title "
                "and register the other as alias. Never suggest merging file contents."
            ),
            stateless=True,
            telemetry_target=title_a,
            telemetry_operation="Semantic Linting",
        )
        return result

    def infer_tag_properties(
        self,
        *,
        tag: str,
        required_keys: list[str],
        page_title: str,
        content: str,
    ) -> InferredPropertiesResult:
        keys = ", ".join(required_keys)
        prompt = build_cache_aligned_prompt(
            content=content[:5000],
            task_instruction=(
                "Task: infer Logseq block property values from page context.\n"
                f"Page: [[{page_title}]]\n"
                f"Tag: #{tag}\n"
                f"Required property keys: {keys}"
            ),
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=InferredPropertiesResult,
            system_prompt=finalize_system_prompt(
                "Infer Logseq block properties from context. Use empty string when unknown. "
                "Return JSON with a properties object."
            ),
            stateless=True,
            telemetry_target=page_title,
            telemetry_operation="Semantic Linting",
        )
        return result

    def classify_marpa_page(
        self,
        *,
        page_title: str,
        content: str,
        namespace_hint: str | None,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> MarpaClassificationResult:
        prompt = build_marpa_classify_user_prompt(
            page_title,
            content,
            namespace_hint=namespace_hint,
        )
        config = load_plumber_lint_config()
        if config.semantic_routing and page_path is not None and graph_root is not None:
            key = semantic_cache_key(page_path, "marpa_classify")
            cached = cache_get(graph_root, "marpa", key)
            if cached is not None:
                return MarpaClassificationResult.model_validate(cached)

        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=MarpaClassificationResult,
            system_prompt=build_marpa_classify_system_prompt(),
            stateless=True,
            telemetry_target=page_title,
            telemetry_operation="Semantic Linting",
        )
        if config.semantic_routing and page_path is not None and graph_root is not None:
            key = semantic_cache_key(page_path, "marpa_classify")
            cache_put(graph_root, "marpa", key, result.model_dump())
        return result

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        task_instruction: str | None = None,
    ) -> BootstrapSummaryResult:
        task = task_instruction or (
            "Task: extract a concise one-sentence summary for catalog indexing.\n"
            "Return JSON with summary, suggested_tags (0-5 tags), and optional MARPA domain "
            "(mappa|area|risorsa|progetto|archivio) when clearly inferable.\n"
            f"Page title: {page_title}"
        )
        prompt = build_cache_aligned_prompt(
            content=content[:6000],
            task_instruction=task,
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=BootstrapSummaryResult,
            system_prompt=finalize_system_prompt(
                "You are Matryca Plumber's bootstrap harvester. Return JSON only. "
                "Write one crisp English sentence summarizing the page."
            ),
            stateless=True,
            telemetry_target=page_title,
            telemetry_operation="Concept Indexing",
            thermal_profile="bootstrap",
        )
        _ = (page_path, graph_root)
        return result

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult:
        prompt = (
            "Task: produce a panoramic ontology report and non-destructive cleanup suggestions "
            "from the structural metrics below.\n\n"
            f"Topology metrics JSON:\n{metrics_json[:12000]}"
        )
        result, _ = self._completion_with_structured_output(
            prompt=prompt,
            response_model=GraphInsightsLLMResult,
            system_prompt=INSIGHTS_SYSTEM_PROMPT,
            telemetry_target=str(graph_root),
            telemetry_operation="Concept Indexing",
        )
        return result


__all__ = [
    "AdaptiveStructuredOutputEngine",
    "GrammarCapability",
    "InstructorLLMClient",
    "LLMResponseError",
    "LlmBackendProfile",
    "StructuredOutputExhaustedError",
    "ThermalProfile",
    "append_correction_turn",
    "pydantic_to_strict_json_schema",
]

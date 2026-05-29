"""Tests for lenient LLM JSON repair utilities."""

from __future__ import annotations

import json

import pytest
from src.agent.maintenance_daemon import SemanticIndexResult
from src.agent.plumber_llm import GraphInsightsLLMResult
from src.utils.json_repair import (
    extract_json_array,
    extract_json_object,
    fix_double_escaped_quote_runs,
    fix_gemma_leaked_literal_newline_before_keys,
    loads_repaired_json,
    parse_llm_json,
    repair_llm_json,
    sanitize_llm_completion_text,
    sanitize_llm_history_turn,
    sanitize_prose_llm_completion,
    strip_trailing_json_garbage,
)


def test_fix_double_escaped_quote_runs() -> None:
    raw = (
        '{"ontology_report": "The graph remains largely unmapped.\\"\\", '
        '\\"structural_debt\\": \\"high\\"", '
        '"cleanup_suggestions": ["Link orphans"]}'
    )
    repaired = repair_llm_json(raw)
    payload = json.loads(repaired)
    assert "structural_debt" in payload["ontology_report"]
    assert payload["cleanup_suggestions"] == ["Link orphans"]


def test_strip_trailing_garbage_block() -> None:
    raw = '{"ontology_report": "Done.", "cleanup_suggestions": ["Split dense page"]} { "'
    repaired = repair_llm_json(raw)
    payload = json.loads(repaired)
    assert payload["ontology_report"] == "Done."
    assert payload["cleanup_suggestions"] == ["Split dense page"]


def test_extract_json_object_from_markdown_fence() -> None:
    raw = """```json
{"ontology_report": "ok", "cleanup_suggestions": []}
```"""
    extracted = extract_json_object(raw)
    assert json.loads(extracted) == {
        "ontology_report": "ok",
        "cleanup_suggestions": [],
    }


def test_balance_truncated_closing_brackets() -> None:
    raw = '{"ontology_report": "truncated", "cleanup_suggestions": ["a", "b"'
    repaired = repair_llm_json(raw)
    payload = json.loads(repaired)
    assert payload["cleanup_suggestions"] == ["a", "b"]


def test_parse_llm_json_validates_graph_insights_model() -> None:
    raw = (
        '{"ontology_report": "940 pages mapped.", '
        '"cleanup_suggestions": ["Review orphan cluster"]} { "reason": null }'
    )
    result = parse_llm_json(raw, GraphInsightsLLMResult)
    assert result.ontology_report == "940 pages mapped."
    assert result.cleanup_suggestions == ["Review orphan cluster"]


def test_loads_repaired_json_raises_on_unrecoverable_payload() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_repaired_json("not json at all")


def test_strip_trailing_json_garbage_preserves_valid_suffix() -> None:
    text = '{"a": 1}'
    assert strip_trailing_json_garbage(text) == '{"a": 1}'


def test_fix_double_escaped_quote_runs_helper() -> None:
    assert fix_double_escaped_quote_runs('foo\\"", \\"bar') == 'foo\\", \\"bar'


def test_repair_strips_degenerate_literal_backslash_n_tail() -> None:
    core = (
        '{"summary": "Francesco Panizzo e collaboratori.", '
        '"cross_references": [], "suggested_tags": ["#francesco"], '
        '"moc_pointers": [], "semantic_corrections": []}'
    )
    raw = core + ("\\n" * 400)
    repaired = repair_llm_json(raw)
    payload = json.loads(repaired)
    assert payload["summary"].startswith("Francesco")
    assert len(repaired) < len(raw) // 2


def test_fix_gemma_leaked_literal_newline_before_keys() -> None:
    raw = r"{\n  \"summary\": \"ok\", \"suggested_tags\": []}"
    fixed = fix_gemma_leaked_literal_newline_before_keys(raw)
    assert r"\n  \"summary" not in fixed
    payload = json.loads(fixed.replace('\\"', '"'))
    assert payload["summary"] == "ok"


def test_extract_json_object_recovers_unbalanced_degenerate_tail() -> None:
    core = '{"summary": "truncated", "tags": ["a"]'
    raw = core + ("\\n" * 300)
    extracted = extract_json_object(raw)
    assert "summary" in extracted
    assert len(extracted) < len(raw) + 10


def test_extract_json_array_ignores_degenerate_tail() -> None:
    core = '[{"concept": "a", "relation": "see_also", "target": "[[B]]"}]'
    raw = core + ("\\n" * 200)
    extracted = extract_json_array(raw)
    assert extracted == core
    assert len(extracted) < len(raw)


def test_sanitize_prose_collapses_newline_loop() -> None:
    raw = "## Consolidated Epistemic State\n\n- ok\n" + ("\n" * 80)
    cleaned = sanitize_prose_llm_completion(raw, max_chars=5000)
    assert cleaned.count("\n") < 40
    assert "Consolidated Epistemic State" in cleaned


def test_parse_json_object_repairs_agent_payload_tail() -> None:
    from src.agent.graph_tool_helpers import parse_json_object

    raw = '{"action": "write_outline", "title": "Demo"}' + ("\\n" * 60)
    parsed = parse_json_object(raw)
    assert parsed["action"] == "write_outline"
    assert parsed["title"] == "Demo"


def test_sanitize_llm_history_turn_json_vs_prose() -> None:
    json_turn = '{"summary": "x"}' + ("\\n" * 50)
    prose_turn = "## State\n\n- item\n" + ("\n" * 60)
    assert len(sanitize_llm_history_turn(json_turn)) < len(json_turn)
    assert sanitize_llm_history_turn(prose_turn).count("\n") < 40


def test_sanitize_llm_completion_text_trims_tail_and_parses_index() -> None:
    core = (
        '{"summary": "Indice pagina.", "cross_references": [], '
        '"suggested_tags": ["#demo"], "moc_pointers": [], "semantic_corrections": []}'
    )
    raw = core + ("\\n" * 80)
    sanitized = sanitize_llm_completion_text(raw)
    assert len(sanitized) == len(core)
    result = parse_llm_json(sanitized, SemanticIndexResult)
    assert result.summary == "Indice pagina."
    assert result.suggested_tags == ["#demo"]

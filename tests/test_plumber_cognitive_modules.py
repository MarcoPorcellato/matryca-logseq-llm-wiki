"""Tests for Matryca Plumber cognitive lint modules."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.agent.plumber_config import (
    PlumberLintConfig,
    apply_thermal_pause_bootstrap,
    apply_thermal_pause_cognitive,
    load_plumber_lint_config,
)
from src.agent.plumber_llm import (
    ContextualSeedResult,
    EntityOverlapResult,
    InferredPropertiesResult,
    MarpaClassificationResult,
    MarpaDomain,
)
from src.agent.plumber_modules import run_cognitive_lint_pipeline
from src.agent.plumber_modules.auto_split import run_auto_split
from src.agent.plumber_modules.dangling_healer import run_dangling_healer
from src.agent.plumber_modules.marpa_framework import run_marpa_framework
from src.agent.plumber_modules.property_hygiene import run_property_hygiene
from src.graph.page_write_lock import clear_page_write_locks


class StubPlumberLLM:
    def generate_contextual_seed(
        self,
        *,
        link_title: str,
        source_page: str,
        context: str,
        max_words: int,
    ) -> ContextualSeedResult:
        _ = (source_page, context, max_words)
        return ContextualSeedResult(definition=f"Seed definition for {link_title}")

    def assess_entity_overlap(
        self,
        *,
        title_a: str,
        title_b: str,
        context: str,
    ) -> EntityOverlapResult:
        _ = context
        return EntityOverlapResult(
            overlap_score=0.9,
            canonical_title=title_a,
            alias_title=title_b,
            should_merge_alias=True,
            reason="stub",
        )

    def infer_tag_properties(
        self,
        *,
        tag: str,
        required_keys: list[str],
        page_title: str,
        content: str,
    ) -> InferredPropertiesResult:
        _ = (tag, page_title, content)
        return InferredPropertiesResult(
            properties={key: "inferred" for key in required_keys},
        )

    def classify_marpa_page(
        self,
        *,
        page_title: str,
        content: str,
        namespace_hint: str | None,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> MarpaClassificationResult:
        _ = (page_title, namespace_hint, page_path, graph_root)
        lower = content.casefold()
        domain: MarpaDomain = (
            "progetto" if "deadline" in lower or "deliverable" in lower else "risorsa"
        )
        props: dict[str, str] = {}
        if domain == "progetto":
            props = {"deadline": "2026-06-01", "status": "active"}
        return MarpaClassificationResult(
            assigned_domain=domain,
            detected_tags=["project"],
            inferred_properties=props,
        )


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    clear_page_write_locks()
    return tmp_path


def _write_page(graph_root: Path, title: str, body: str) -> Path:
    pages = graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    path = pages / f"{title}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_apply_thermal_pause_ignores_negative_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    apply_thermal_pause_cognitive(
        PlumberLintConfig(thermal_delay_cognitive=-3.0),
    )
    assert sleeps == []
    apply_thermal_pause_bootstrap(
        PlumberLintConfig(thermal_delay_bootstrap=1.25),
    )
    assert sleeps == [1.25]


def test_load_plumber_lint_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MATRYCA_LINT_HEAL_DANGLING",
        "MATRYCA_LINT_MARPA_FRAMEWORK",
        "MATRYCA_LINT_ENTITY_CONSOLIDATION",
        "MATRYCA_LINT_AUTO_SPLIT",
        "MATRYCA_LINT_PROPERTY_HYGIENE",
        "MATRYCA_LINT_BACKPROPAGATE_LINKS",
        "MATRYCA_LINT_SEMANTIC_ROUTING",
        "MATRYCA_LINT_DISABLE_SEMANTIC_CORRECTIONS",
        "MATRYCA_THERMAL_DELAY_BOOTSTRAP",
        "MATRYCA_THERMAL_DELAY_COGNITIVE",
        "MATRYCA_PLUMBER_LOW_PRIORITY_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = load_plumber_lint_config()
    assert cfg.heal_dangling is False
    assert cfg.marpa_framework is False
    assert cfg.dangling_max_words == 50
    assert cfg.disable_semantic_corrections is True
    assert cfg.any_enabled is False
    assert cfg.thermal_delay_bootstrap == 2.0
    assert cfg.thermal_delay_cognitive == 2.0
    assert cfg.low_priority_mode is True


def test_dangling_healer_creates_seed_page(graph_root: Path) -> None:
    source = _write_page(graph_root, "Source", "- See [[Future Concept]] for details\n")
    outcome = run_dangling_healer(
        graph_root,
        source,
        "Source",
        source.read_text(encoding="utf-8"),
        llm=StubPlumberLLM(),
        max_words=50,
    )
    target = graph_root / "pages" / "Future Concept.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "Seed definition for Future Concept" in text
    assert "seeded-from:: [[Source]]" in text
    assert outcome.pages_created == ["Future Concept"]


def test_property_hygiene_appends_missing_properties(graph_root: Path) -> None:
    path = _write_page(graph_root, "Proj", "- Launch #project roadmap\n")
    outcome = run_property_hygiene(
        graph_root,
        path,
        "Proj",
        path.read_text(encoding="utf-8"),
        llm=StubPlumberLLM(),
        rules_path=None,
        infer_missing=True,
    )
    text = path.read_text(encoding="utf-8")
    assert "status:: inferred" in text
    assert "deadline:: inferred" in text
    assert outcome.pages_modified == ["Proj"]


def test_auto_split_extracts_dense_subtree(graph_root: Path) -> None:
    lines = ["- Root topic\n", "  id:: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"]
    for i in range(20):
        lines.append(f"  - child item {i}\n")
    path = _write_page(graph_root, "Dense", "".join(lines))
    outcome = run_auto_split(graph_root, path, "Dense", threshold=15)
    assert outcome.pages_created
    assert outcome.pages_modified == ["Dense"]
    updated = path.read_text(encoding="utf-8")
    assert "[[" in updated
    child = graph_root / "pages" / f"{outcome.pages_created[0]}.md"
    assert child.is_file()


def test_cognitive_pipeline_respects_disabled_modules(graph_root: Path) -> None:
    path = _write_page(graph_root, "Idle", "- plain note\n")
    outcome = run_cognitive_lint_pipeline(
        graph_root,
        path,
        "Idle",
        path.read_text(encoding="utf-8"),
        llm=StubPlumberLLM(),
        config=PlumberLintConfig(),
    )
    assert outcome.modules_run == []


def test_cognitive_pipeline_runs_enabled_modules(graph_root: Path) -> None:
    path = _write_page(graph_root, "Tagged", "- Launch #project now [[Ghost Page]]\n")
    config = PlumberLintConfig(
        heal_dangling=True,
        property_hygiene=True,
        infer_missing_properties=True,
    )
    outcome = run_cognitive_lint_pipeline(
        graph_root,
        path,
        "Tagged",
        path.read_text(encoding="utf-8"),
        llm=StubPlumberLLM(),
        config=config,
    )
    assert "heal_dangling" in outcome.modules_run
    assert "property_hygiene" in outcome.modules_run
    assert (graph_root / "pages" / "Ghost Page.md").is_file()
    assert "status:: inferred" in path.read_text(encoding="utf-8")


def test_marpa_framework_classifies_project_with_deadline(graph_root: Path) -> None:
    body = "- Launch MVP #project\n  Deliverable due 2026-06-01 with hard deadline\n"
    path = _write_page(graph_root, "Progetti___Lancio", body)
    outcome = run_marpa_framework(
        graph_root,
        path,
        "Progetti___Lancio",
        body,
        llm=StubPlumberLLM(),
    )
    text = path.read_text(encoding="utf-8")
    assert "type:: progetto" in text
    assert "deadline:: 2026-06-01" in text
    assert "### Matryca MARPA Validation" in text
    assert outcome.pages_modified == ["Progetti___Lancio"]


def test_marpa_disabled_by_default_leaves_file_untouched(graph_root: Path) -> None:
    original = "- plain note without MARPA metadata\n"
    path = _write_page(graph_root, "Idle", original)
    outcome = run_cognitive_lint_pipeline(
        graph_root,
        path,
        "Idle",
        original,
        llm=StubPlumberLLM(),
        config=PlumberLintConfig(),
    )
    assert "marpa_framework" not in outcome.modules_run
    assert path.read_text(encoding="utf-8") == original


def test_marpa_validation_section_is_replaced_on_rerun(graph_root: Path) -> None:
    body = "- Launch MVP\n  Deliverable due deadline\n"
    path = _write_page(graph_root, "Rerun", body)
    run_marpa_framework(
        graph_root,
        path,
        "Rerun",
        body,
        llm=StubPlumberLLM(),
    )
    first = path.read_text(encoding="utf-8")
    assert first.count("### Matryca MARPA Validation") == 1

    edited = first.replace("Launch MVP", "Launch MVP v2")
    path.write_text(edited, encoding="utf-8")
    outcome = run_marpa_framework(
        graph_root,
        path,
        "Rerun",
        edited,
        llm=StubPlumberLLM(),
    )
    second = path.read_text(encoding="utf-8")
    assert second.count("### Matryca MARPA Validation") == 1
    assert "Launch MVP v2" in second
    assert outcome.pages_modified == ["Rerun"]

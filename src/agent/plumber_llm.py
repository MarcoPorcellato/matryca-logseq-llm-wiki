"""Structured LLM payloads and protocol for Plumber cognitive modules."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

MarpaDomain = Literal["mappa", "area", "risorsa", "progetto", "archivio"]


class MarpaClassificationResult(BaseModel):
    """LLM classification payload for the MARPA taxonomy framework."""

    assigned_domain: MarpaDomain = Field(description="MARPA domain assignment")
    detected_tags: list[str] = Field(default_factory=list)
    inferred_properties: dict[str, str] = Field(default_factory=dict)
    violates_ssot_duplication: bool = Field(default=False)


class ContextualSeedResult(BaseModel):
    """Micro-definition for a newly seeded dangling-link page."""

    definition: str = Field(description="Short contextual definition for the new page")


class EntityOverlapResult(BaseModel):
    """Semantic overlap assessment between two page concepts."""

    overlap_score: float = Field(ge=0.0, le=1.0, description="0-1 semantic similarity")
    canonical_title: str = Field(description="Preferred canonical page title")
    alias_title: str = Field(description="Secondary title to register as alias")
    should_merge_alias: bool = Field(
        description="True when overlap_score exceeds threshold and alias should be added",
    )
    reason: str = Field(default="")


class InferredPropertiesResult(BaseModel):
    """Properties inferred for a tagged page."""

    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Logseq property keys (without ::) mapped to values",
    )


class BootstrapSummaryResult(BaseModel):
    """Minimal structured payload for bootstrap catalog harvesting."""

    summary: str = Field(description="One-sentence page summary")
    suggested_tags: list[str] = Field(default_factory=list)
    domain: str = Field(default="", description="MARPA domain when inferable")


class GraphInsightsLLMResult(BaseModel):
    """Structured LLM payload for the graph insights dashboard."""

    ontology_report: str = Field(
        description="Panoramic review of hidden conceptual clusters (markdown prose)",
    )
    cleanup_suggestions: list[str] = Field(
        default_factory=list,
        description="Non-destructive cleanup recommendations as short bullet texts",
    )


class HarvestLLM(Protocol):
    """LLM surface for bootstrap catalog harvesting."""

    def harvest_page_summary(
        self,
        page_title: str,
        content: str,
        *,
        page_path: Path | None = None,
        graph_root: Path | None = None,
        task_instruction: str | None = None,
    ) -> BootstrapSummaryResult: ...


class InsightsLLM(Protocol):
    """LLM surface for graph insights generation."""

    def generate_graph_insights(
        self,
        *,
        metrics_json: str,
        graph_root: Path,
    ) -> GraphInsightsLLMResult: ...


class PlumberModuleLLM(Protocol):
    """LLM surface used by cognitive lint modules."""

    def generate_contextual_seed(
        self,
        *,
        link_title: str,
        source_page: str,
        context: str,
        max_words: int,
    ) -> ContextualSeedResult: ...

    def assess_entity_overlap(
        self,
        *,
        title_a: str,
        title_b: str,
        context: str,
    ) -> EntityOverlapResult: ...

    def infer_tag_properties(
        self,
        *,
        tag: str,
        required_keys: list[str],
        page_title: str,
        content: str,
    ) -> InferredPropertiesResult: ...

    def classify_marpa_page(
        self,
        *,
        page_title: str,
        content: str,
        namespace_hint: str | None,
        page_path: Path | None = None,
        graph_root: Path | None = None,
    ) -> MarpaClassificationResult: ...


__all__ = [
    "BootstrapSummaryResult",
    "GraphInsightsLLMResult",
    "HarvestLLM",
    "InsightsLLM",
    "PlumberModuleLLM",
    "ContextualSeedResult",
    "EntityOverlapResult",
    "InferredPropertiesResult",
    "MarpaClassificationResult",
    "MarpaDomain",
]

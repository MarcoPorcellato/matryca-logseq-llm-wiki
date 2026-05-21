"""Structured LLM payloads and protocol for Brain cognitive modules."""

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


class BrainModuleLLM(Protocol):
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
    "BrainModuleLLM",
    "ContextualSeedResult",
    "EntityOverlapResult",
    "InferredPropertiesResult",
    "MarpaClassificationResult",
    "MarpaDomain",
]

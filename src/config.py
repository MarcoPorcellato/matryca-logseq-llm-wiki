"""Optional ``matryca-wiki.yml`` orchestration (namespaces, L1 path, lint prefix)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator


class MatrycaWikiConfig(BaseModel):
    """Validated subset of llm-wiki-style YAML (no markdown parsing here)."""

    namespaces: list[str] = Field(
        default_factory=list,
        description="Top-level wiki namespaces for planning / hub tools.",
    )
    memory_path: str | None = Field(
        default=None,
        description="L1 Markdown directory or file (used when MATRYCA_L1_PATH is unset).",
    )
    max_depth: int = Field(default=3, ge=1, le=10)
    dashboard_page_title: str = Field(default="Matryca Dashboard")
    wiki_file_prefix: str = Field(default="Matryca___")

    @field_validator("namespaces", mode="before")
    @classmethod
    def _strip_namespace_strings(cls, value: Any) -> list[str]:  # noqa: ANN401
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out


def resolve_matryca_wiki_config_path() -> Path | None:
    """Pick ``matryca-wiki.yml`` path: ``MATRYCA_WIKI_CONFIG`` else graph root file."""
    explicit = os.environ.get("MATRYCA_WIKI_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    graph = os.environ.get("LOGSEQ_GRAPH_PATH", "").strip()
    if graph:
        candidate = Path(graph).expanduser().resolve(strict=False) / "matryca-wiki.yml"
        return candidate
    return None


def load_matryca_wiki_config() -> MatrycaWikiConfig:
    """Load YAML or return defaults when missing / invalid."""
    path = resolve_matryca_wiki_config_path()
    if path is None or not path.is_file():
        return MatrycaWikiConfig()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.bind(path=str(path)).warning("Could not read matryca-wiki config: {}", exc)
        return MatrycaWikiConfig()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.bind(path=str(path)).warning("Invalid YAML in matryca-wiki config: {}", exc)
        return MatrycaWikiConfig()

    if data is None:
        return MatrycaWikiConfig()
    if not isinstance(data, dict):
        logger.bind(path=str(path)).warning("matryca-wiki.yml must be a mapping at the root")
        return MatrycaWikiConfig()

    try:
        cfg = MatrycaWikiConfig.model_validate(data)
    except ValueError as exc:
        logger.bind(path=str(path)).warning("matryca-wiki.yml validation failed: {}", exc)
        return MatrycaWikiConfig()

    logger.bind(path=str(path), namespaces=len(cfg.namespaces)).info("Loaded matryca-wiki.yml")
    return cfg


__all__ = ["MatrycaWikiConfig", "load_matryca_wiki_config", "resolve_matryca_wiki_config_path"]

"""Infer and append missing structured properties for tagged pages (Tana-style)."""

from __future__ import annotations

from pathlib import Path

import yaml

from ...graph.markdown_blocks import atomic_write_bytes
from ...graph.page_write_lock import page_rmw_lock
from ._shared import ModuleOutcome, extract_inline_tags, page_property_keys

_DEFAULT_RULES: dict[str, list[str]] = {
    "project": ["status", "deadline"],
    "person": ["role", "email"],
    "meeting": ["date", "attendees"],
}


def _load_tag_rules(rules_path: Path | None) -> dict[str, list[str]]:
    if rules_path is None or not rules_path.is_file():
        return dict(_DEFAULT_RULES)
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return dict(_DEFAULT_RULES)
    if not isinstance(raw, dict):
        return dict(_DEFAULT_RULES)
    tag_rules = raw.get("tag_rules", raw)
    if not isinstance(tag_rules, dict):
        return dict(_DEFAULT_RULES)
    out: dict[str, list[str]] = {}
    for tag, spec in tag_rules.items():
        if isinstance(spec, dict):
            req = spec.get("required_properties", spec.get("properties", []))
        elif isinstance(spec, list):
            req = spec
        else:
            continue
        if isinstance(req, list):
            keys = [str(k).strip().lower() for k in req if str(k).strip()]
            if keys:
                out[str(tag).lstrip("#").casefold()] = keys
    return out or dict(_DEFAULT_RULES)


def run_property_hygiene(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    content: str,
    *,
    llm: object,
    rules_path: Path | None,
    infer_missing: bool,
) -> ModuleOutcome:
    """Append missing ``key:: value`` property lines inferred from page context."""
    outcome = ModuleOutcome()
    rules = _load_tag_rules(rules_path)
    tags = extract_inline_tags(content)
    if not tags:
        return outcome

    existing = page_property_keys(content)
    missing_by_tag: dict[str, list[str]] = {}
    for tag in tags:
        required = rules.get(tag, [])
        missing = [key for key in required if key not in existing or not existing[key].strip()]
        if missing:
            missing_by_tag[tag] = missing

    if not missing_by_tag:
        return outcome

    inferred: dict[str, str] = {}
    if infer_missing and hasattr(llm, "infer_tag_properties"):
        for tag, keys in missing_by_tag.items():
            result = llm.infer_tag_properties(
                tag=tag,
                required_keys=keys,
                page_title=page_title,
                content=content[:6000],
            )
            for key, value in result.properties.items():
                norm_key = key.strip().lower()
                if norm_key in keys and value.strip():
                    inferred[norm_key] = value.strip()
    else:
        for _tag, keys in missing_by_tag.items():
            for key in keys:
                inferred.setdefault(key, "unknown")

    if not inferred:
        return outcome

    with page_rmw_lock(page_path):
        text = page_path.read_text(encoding="utf-8", errors="replace")
        additions = "".join(f"{key}:: {value}\n" for key, value in sorted(inferred.items()))
        if additions.strip() in text:
            return outcome
        new_text = text.rstrip("\n") + "\n" + additions
        atomic_write_bytes(page_path, new_text.encode("utf-8"), graph_root=graph_root)

    outcome.pages_modified.append(page_title)
    outcome.details.append(f"properties:{','.join(sorted(inferred))}")
    return outcome


__all__ = ["run_property_hygiene"]

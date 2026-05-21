"""MARPA cognitive framework (Mappa, Aree, Risorse, Progetti, Archivio)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from ...graph.generational_cache import patch_generational_caches_for_paths
from ...graph.markdown_blocks import atomic_write_bytes
from ...graph.page_write_lock import page_rmw_lock
from ..brain_llm import MarpaClassificationResult
from ._shared import ModuleOutcome

MarpaDomain = Literal["mappa", "area", "risorsa", "progetto", "archivio"]

_TYPE_LINE = re.compile(r"^\s*type::\s*(\S+)\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MIN_DUP_CHARS = 80
_MARPA_HEADER = "### Matryca MARPA Validation"


def detect_marpa_namespace(page_title: str) -> str | None:
    """Return namespace segment from ``Area/Topic`` or ``Area___Topic`` titles."""
    if "/" in page_title:
        return page_title.split("/", 1)[0].casefold()
    if "___" in page_title:
        return page_title.split("___", 1)[0].casefold()
    return None


def _has_top_level_type(content: str) -> bool:
    return any(_TYPE_LINE.match(line) for line in content.splitlines()[:12])


def _scan_ssot_duplication(graph_root: Path, page_path: Path, content: str) -> list[str]:
    """Flag large verbatim blocks that appear to duplicate another page (SSoT guard)."""
    flags: list[str] = []
    blocks: list[str] = []
    for line in content.splitlines():
        match = _BULLET.match(line.rstrip("\n"))
        if match and len(match.group(2)) >= _MIN_DUP_CHARS:
            blocks.append(match.group(2).strip())
    if not blocks:
        return flags

    try:
        self_text = page_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        self_text = content
    pages_dir = graph_root / "pages"
    if not pages_dir.is_dir():
        return flags

    for other in pages_dir.rglob("*.md"):
        if other.resolve() == page_path.resolve():
            continue
        try:
            other_text = other.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in blocks:
            if block in other_text and block not in self_text.replace(block, "", 1):
                rel = other.relative_to(graph_root).as_posix()
                flags.append(f"ssot_duplicate:{rel}")
                break
    return flags


def _inject_type_and_properties(
    lines: list[str],
    domain: MarpaDomain,
    inferred: dict[str, str],
) -> None:
    """Insert ``type::`` and inferred properties under the first list bullet."""
    for idx, line in enumerate(lines):
        if not _BULLET.match(line.rstrip("\n")):
            continue
        window = lines[idx : idx + 8]
        if any(_TYPE_LINE.match(w.rstrip("\n")) for w in window):
            return
        indent = "  "
        insert: list[str] = [f"{indent}type:: {domain}\n"]
        for key, value in inferred.items():
            if key.casefold() == "type":
                continue
            if value.strip():
                insert.append(f"{indent}{key}:: {value.strip()}\n")
        lines[idx + 1 : idx + 1] = insert
        return

    lines.insert(0, f"- MARPA classified page\n  type:: {domain}\n")


def _strip_marpa_validation_section(text: str) -> str:
    """Remove a trailing MARPA validation section so it can be upserted idempotently."""
    idx = text.rfind(_MARPA_HEADER)
    if idx == -1:
        return text
    prefix = text[:idx].rstrip("\n")
    return f"{prefix}\n" if prefix else ""


def _upsert_validation_section(
    lines: list[str],
    classification: MarpaClassificationResult,
    ssot_flags: list[str],
) -> None:
    """Replace any existing MARPA validation block, then append a fresh report."""
    body = _strip_marpa_validation_section("".join(lines))
    lines.clear()
    if body:
        lines.extend(body.splitlines(keepends=True))
    section_lines = [
        "",
        _MARPA_HEADER,
        f"- assigned-domain:: {classification.assigned_domain}",
    ]
    if classification.detected_tags:
        section_lines.append(
            "- detected-tags:: "
            + " ".join(f"#{t.lstrip('#')}" for t in classification.detected_tags),
        )
    if ssot_flags or classification.violates_ssot_duplication:
        section_lines.append("- ssot-warnings::")
        for flag in ssot_flags:
            section_lines.append(f"  - {flag} (prefer ((block-uuid)) transclusion)")
    section_lines.append("")
    lines.append("\n".join(section_lines))


def run_marpa_framework(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    content: str,
    *,
    llm: object,
) -> ModuleOutcome:
    """Classify page into MARPA domain and inject ``type::`` metadata safely."""
    outcome = ModuleOutcome()
    if not hasattr(llm, "classify_marpa_page"):
        return outcome

    namespace = detect_marpa_namespace(page_title)
    ssot_flags = _scan_ssot_duplication(graph_root, page_path, content)
    has_type = _has_top_level_type(content)

    if has_type:
        domain: MarpaDomain = "risorsa"
        for line in content.splitlines()[:12]:
            match = _TYPE_LINE.match(line)
            if match:
                raw = match.group(1).casefold()
                if raw in {"mappa", "area", "risorsa", "progetto", "archivio"}:
                    domain = raw  # type: ignore[assignment]
                break
        classification = MarpaClassificationResult(assigned_domain=domain)
    else:
        classification = llm.classify_marpa_page(
            page_title=page_title,
            content=content[:8000],
            namespace_hint=namespace,
            page_path=page_path,
            graph_root=graph_root,
        )
    if ssot_flags:
        classification.violates_ssot_duplication = True

    with page_rmw_lock(page_path):
        if page_path.is_file():
            text = page_path.read_text(encoding="utf-8", errors="replace")
        else:
            text = content
        lines = text.splitlines(keepends=True)
        if not has_type:
            _inject_type_and_properties(
                lines,
                classification.assigned_domain,
                classification.inferred_properties,
            )
        _upsert_validation_section(lines, classification, ssot_flags)
        new_text = "".join(lines)
        if new_text == text:
            return outcome
        atomic_write_bytes(page_path, new_text.encode("utf-8"), graph_root=graph_root)

    outcome.pages_modified.append(page_title)
    outcome.details.append(f"marpa:{classification.assigned_domain}")
    if classification.violates_ssot_duplication:
        outcome.details.append("marpa:ssot_flagged")
    patch_generational_caches_for_paths(graph_root, [page_path])
    return outcome


__all__ = [
    "MarpaDomain",
    "detect_marpa_namespace",
    "run_marpa_framework",
]

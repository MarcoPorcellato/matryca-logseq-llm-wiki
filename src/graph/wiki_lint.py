"""Wiki-style lint for prefixed Logseq pages (conventions, stale, credentials)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import MatrycaWikiConfig

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TYPE_RE = re.compile(r"(?im)^\s*type::\s*(\S+)\s*$")
_UPDATED_RE = re.compile(r"(?im)^\s*updated::\s*(\d{4}-\d{2}-\d{2})\s*$")
_CONFIDENCE_RE = re.compile(r"(?im)^\s*confidence::\s*(\S+)\s*$")
_CRED_PROP_RE = re.compile(
    r"(?i)\b(token::|password::|secret::|api-key::|api\.key::)\s*\S+",
)
_B64_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


@dataclass(frozen=True, slots=True)
class WikiLintFinding:
    """Single lint finding for a page."""

    path: str
    rule: str
    severity: str
    detail: str


def _parse_iso_date(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def lint_wiki_prefixed_pages(
    graph_root: str | Path,
    wiki_config: MatrycaWikiConfig,
) -> list[WikiLintFinding]:
    """Run lightweight rules on ``pages/{prefix}*.md`` (Logseq OG flat pages)."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return [
            WikiLintFinding(
                path=str(pages),
                rule="pages_dir",
                severity="error",
                detail="Graph has no pages/ directory",
            ),
        ]

    prefix = wiki_config.wiki_file_prefix
    findings: list[WikiLintFinding] = []
    today = datetime.now(tz=UTC).date()
    stale_cutoff = today - timedelta(days=90)

    for path in sorted(pages.glob("*.md")):
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(
                WikiLintFinding(
                    path=rel,
                    rule="read_error",
                    severity="warning",
                    detail=str(exc),
                ),
            )
            continue

        if _CRED_PROP_RE.search(text):
            findings.append(
                WikiLintFinding(
                    path=rel,
                    rule="credential_property",
                    severity="critical",
                    detail="Possible credential property (token/password/secret/api-key)",
                ),
            )

        for match in _B64_RE.finditer(text):
            token = match.group(0)
            if len(token) >= 48 and token.endswith("="):
                findings.append(
                    WikiLintFinding(
                        path=rel,
                        rule="long_base64",
                        severity="warning",
                        detail="Long base64-like token (possible secret material)",
                    ),
                )
                break

        if not _TYPE_RE.search(text):
            findings.append(
                WikiLintFinding(
                    path=rel,
                    rule="missing_type",
                    severity="warning",
                    detail="No `type::` property line detected",
                ),
            )

        type_m = _TYPE_RE.search(text)
        conf_m = _CONFIDENCE_RE.search(text)
        upd_m = _UPDATED_RE.search(text)
        if (
            type_m
            and type_m.group(1).strip().lower() == "knowledge"
            and conf_m
            and conf_m.group(1).strip().lower() == "high"
            and upd_m
        ):
            parsed = _parse_iso_date(upd_m.group(1))
            if parsed and parsed.date() < stale_cutoff:
                findings.append(
                    WikiLintFinding(
                        path=rel,
                        rule="stale_high_confidence",
                        severity="warning",
                        detail=(
                            f"updated:: {upd_m.group(1)} older than 90 days with confidence:: high"
                        ),
                    ),
                )

        if not _WIKILINK_RE.search(text):
            findings.append(
                WikiLintFinding(
                    path=rel,
                    rule="no_wikilinks",
                    severity="info",
                    detail="No [[...]] wikilinks found (isolated page risk)",
                ),
            )

    return findings


def format_wiki_lint_report(findings: list[WikiLintFinding], *, prefix: str) -> str:
    """Markdown summary for MCP."""
    lines = [
        "# Wiki convention lint",
        "",
        f"- **Filename prefix:** `{prefix}`",
        f"- **Findings:** {len(findings)}",
        "",
    ]
    if not findings:
        lines.append("No issues for scanned prefixed pages.")
        return "\n".join(lines)

    lines.append("## Results")
    lines.append("")
    for f in findings:
        lines.append(f"- `{f.path}` — **{f.rule}** ({f.severity}) — {f.detail}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "WikiLintFinding",
    "format_wiki_lint_report",
    "lint_wiki_prefixed_pages",
]

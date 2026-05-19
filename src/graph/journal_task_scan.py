"""Scan Logseq ``journals/`` files for open task markers (string / indent scan only)."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .markdown_blocks import atomic_write_bytes
from .page_write_lock import page_rmw_lock

_BULLET = re.compile(r"^(\s*)[-*+]\s+")
# Logseq markers (uppercase) after bullet
_MARKER_LINE = re.compile(
    r"^(\s*)[-*+]\s+(TODO|LATER|WAITING)\s+(.*)$",
    re.IGNORECASE,
)
_SCHEDULED = re.compile(r"SCHEDULED:\s*<[^>\n]+>", re.IGNORECASE)
_DEADLINE = re.compile(r"DEADLINE:\s*<[^>\n]+>", re.IGNORECASE)


def normalize_marker(marker: str) -> str:
    return marker.strip().upper()


def journal_file_path(graph_root: Path, day: date) -> Path:
    """Default Logseq daily journal path: ``journals/YYYY_MM_DD.md``."""
    name = day.strftime("%Y_%m_%d")
    return graph_root / "journals" / f"{name}.md"


def iter_journal_paths(
    graph_root: str | Path,
    *,
    days: int = 7,
    as_of: date | None = None,
) -> list[tuple[date, Path]]:
    """Return ``(date, path)`` pairs for the last ``days`` days ending at ``as_of`` (inclusive)."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    end = as_of or date.today()
    out: list[tuple[date, Path]] = []
    for i in range(days):
        d = end - timedelta(days=i)
        p = journal_file_path(root, d)
        out.append((d, p))
    return out


@dataclass(frozen=True, slots=True)
class JournalTaskItem:
    """One unresolved task line cluster from a journal file."""

    source_iso_date: str
    source_relpath: str
    marker: str
    headline: str
    scheduled: str | None
    deadline: str | None
    block_text: str


@dataclass
class JournalTaskScanReport:
    """Aggregate scan result."""

    graph_root: str
    days_scanned: int
    files_scanned: int
    items: list[JournalTaskItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _bullet_indent(line: str) -> int | None:
    m = _BULLET.match(line)
    if not m:
        return None
    return len(m.group(1))


def _collect_block_lines(
    lines: list[str],
    start_idx: int,
    base_indent: int,
) -> tuple[list[str], int]:
    """Return lines belonging to the bullet at ``start_idx`` until a sibling/parent bullet."""
    block = [lines[start_idx]]
    j = start_idx + 1
    while j < len(lines):
        raw = lines[j]
        if not raw.strip():
            block.append(raw)
            j += 1
            continue
        ind = _bullet_indent(raw)
        if ind is not None and ind <= base_indent:
            break
        block.append(raw)
        j += 1
    return block, j


def _extract_sched_dead(block_lines: list[str]) -> tuple[str | None, str | None]:
    scheduled: str | None = None
    deadline: str | None = None
    blob = "\n".join(block_lines)
    sm = _SCHEDULED.search(blob)
    dm = _DEADLINE.search(blob)
    if sm:
        scheduled = sm.group(0).strip()
    if dm:
        deadline = dm.group(0).strip()
    return scheduled, deadline


def scan_journal_tasks(
    graph_root: str | Path,
    *,
    days: int = 7,
    as_of: date | None = None,
) -> JournalTaskScanReport:
    """Scan journal files for ``TODO`` / ``LATER`` / ``WAITING`` bullets."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    report = JournalTaskScanReport(
        graph_root=str(root),
        days_scanned=max(1, min(days, 366)),
        files_scanned=0,
    )
    pairs = iter_journal_paths(root, days=report.days_scanned, as_of=as_of)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        report.notes.append("No `journals/` directory under graph root.")
        return report

    for d, path in pairs:
        if not path.is_file():
            continue
        report.files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report.notes.append(f"Could not read `{path}`: {exc}")
            continue
        lines = text.splitlines()
        i = 0
        iso = d.isoformat()
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
        while i < len(lines):
            line = lines[i]
            mm = _MARKER_LINE.match(line)
            if not mm:
                i += 1
                continue
            base_indent = len(mm.group(1))
            marker = normalize_marker(mm.group(2))
            headline = mm.group(3).strip()
            block_lines, next_i = _collect_block_lines(lines, i, base_indent)
            sched, deadl = _extract_sched_dead(block_lines)
            report.items.append(
                JournalTaskItem(
                    source_iso_date=iso,
                    source_relpath=rel,
                    marker=marker,
                    headline=headline,
                    scheduled=sched,
                    deadline=deadl,
                    block_text="\n".join(block_lines).strip(),
                ),
            )
            i = next_i
    return report


def format_journal_task_review_markdown(report: JournalTaskScanReport) -> str:
    """Markdown snippet suitable for pasting into today's journal."""
    lines = [
        f"### Task review (last {report.days_scanned} days)",
        "",
        f"- **Files scanned:** {report.files_scanned}",
        f"- **Open items:** {len(report.items)}",
        "",
    ]
    for n in report.notes[:5]:
        lines.append(f"_Note: {n}_")
    if report.notes:
        lines.append("")
    if not report.items:
        lines.append("_No unresolved TODO / LATER / WAITING lines in scanned journals._")
        return "\n".join(lines)

    by_day: dict[str, list[JournalTaskItem]] = {}
    for it in report.items:
        by_day.setdefault(it.source_iso_date, []).append(it)

    for day in sorted(by_day.keys(), reverse=True):
        lines.append(f"#### {day}")
        lines.append("")
        for it in by_day[day]:
            meta: list[str] = [f"`{it.marker}`"]
            if it.scheduled:
                meta.append(it.scheduled)
            if it.deadline:
                meta.append(it.deadline)
            lines.append(f"- {' · '.join(meta)} {it.headline}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def append_journal_markdown_section(
    graph_root: str | Path,
    markdown_body: str,
    *,
    as_of: date | None = None,
    dry_run: bool = True,
) -> dict[str, object]:
    """Append a markdown section to today's ``journals/YYYY_MM_DD.md`` (creates file if absent).

    Writes UTF-8 with a trailing newline; uses atomic replace and ``.bak`` like property edits.
    """
    root = Path(graph_root).expanduser().resolve(strict=False)
    day = as_of or date.today()
    path = journal_file_path(root, day)
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
    try:
        root_resolved = root.resolve()
        path.resolve().relative_to(root_resolved)
    except ValueError:
        return {
            "ok": False,
            "code": "path_forbidden",
            "hint": "Journal path would escape graph root.",
            "dry_run": dry_run,
            "relative_path": rel,
        }

    section = markdown_body.rstrip() + "\n"

    with page_rmw_lock(path):
        prev = ""
        if path.is_file():
            prev = path.read_text(encoding="utf-8", errors="replace")
        new_text = prev.rstrip("\n") + ("\n\n" if prev.strip() else "") + section

        if dry_run:
            return {
                "ok": True,
                "code": "dry_run_ok",
                "hint": "Re-run with dry_run=false to append to the journal file on disk.",
                "dry_run": True,
                "relative_path": rel,
                "bytes_before": len(prev.encode("utf-8")),
                "bytes_after": len(new_text.encode("utf-8")),
                "preview_tail": new_text[-400:],
            }

        path.parent.mkdir(parents=True, exist_ok=True)

        if path.is_file():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        atomic_write_bytes(path, new_text.encode("utf-8"))

        return {
            "ok": True,
            "code": "applied",
            "hint": f"Journal updated at `{rel}` (backup alongside if file existed).",
            "dry_run": False,
            "relative_path": rel,
            "bytes_before": len(prev.encode("utf-8")),
            "bytes_after": path.stat().st_size,
        }


__all__ = [
    "JournalTaskItem",
    "JournalTaskScanReport",
    "append_journal_markdown_section",
    "format_journal_task_review_markdown",
    "iter_journal_paths",
    "journal_file_path",
    "scan_journal_tasks",
]

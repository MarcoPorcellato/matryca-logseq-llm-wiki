"""Surgical regex edits on Logseq ``key::`` lines inside a single block (anchored at ``id::``).

Inspired by cyanheads ``obsidian_replace_in_note``: capture groups, dry-run, typed hints.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .alias_index import normalize_concept_key
from .global_fence_scanner import compute_page_protected_line_indices
from .markdown_blocks import (
    atomic_write_bytes_if_unchanged,
    block_bullet_index,
    block_subtree_end,
    canonical_line_suffix,
    file_mtime_drifted,
    find_id_line_index,
    graph_safe_page_path,
    occ_snapshot,
    read_file_mtime,
    strip_line_endings,
    strip_lines_for_match,
)
from .mldoc_properties import is_logseq_block_property_line, split_logseq_property_list_values
from .page_properties import inject_page_property
from .page_write_lock import page_rmw_lock
from ..utils.regex_policy import validate_regex_pattern


def _property_line_indices(lines: list[str], start: int, end: int) -> list[int]:
    """Indices in ``[start, end)`` for Logseq ``key::`` block property lines (mldoc-aligned)."""
    out: list[int] = []
    for i in range(start, end):
        if is_logseq_block_property_line(lines[i]):
            out.append(i)
    return out


def _translate_js_style_replacement(repl: str) -> str:
    """Map ``$&`` / ``$1``-style replacements to Python :func:`re.sub` backrefs."""
    out = repl.replace("$&", r"\g<0>")
    return re.sub(r"\$(\d+)", lambda m: f"\\g<{m.group(1)}>", out)


def _apply_pattern(
    text: str,
    search: str,
    replacement: str,
    *,
    use_regex: bool,
    replace_all: bool,
    case_sensitive: bool,
) -> tuple[str, int]:
    """Return (new_text, match_count)."""
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        repl_py = _translate_js_style_replacement(replacement)
        try:
            rx = validate_regex_pattern(search, flags)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if replace_all:
            new_text, n = rx.subn(repl_py, text)
            return new_text, n
        m = rx.search(text)
        if not m:
            return text, 0
        chunk = text[m.start() : m.end()]
        replaced = rx.sub(repl_py, chunk, count=1)
        new_text = text[: m.start()] + replaced + text[m.end() :]
        return new_text, 1
    if case_sensitive:
        if replace_all:
            n = text.count(search)
            return text.replace(search, replacement), n
        idx = text.find(search)
        if idx < 0:
            return text, 0
        return text[:idx] + replacement + text[idx + len(search) :], 1
    low = text.lower()
    s = search.lower()
    if replace_all:
        out = []
        i = 0
        n = 0
        while True:
            j = low.find(s, i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j] + replacement)
            n += 1
            i = j + len(search)
        return "".join(out), n
    j = low.find(s)
    if j < 0:
        return text, 0
    return text[:j] + replacement + text[j + len(search) :], 1


@dataclass(frozen=True, slots=True)
class PropertyLineEditOutcome:
    """Structured result for MCP (JSON-serializable as dict)."""

    ok: bool
    code: str
    hint: str
    dry_run: bool
    match_count: int
    previews: list[str]
    previous_size_bytes: int
    current_size_bytes: int
    lines_changed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "match_count": self.match_count,
            "previews": self.previews,
            "previous_size_bytes": self.previous_size_bytes,
            "current_size_bytes": self.current_size_bytes,
            "lines_changed": self.lines_changed,
        }


def edit_block_property_lines(
    graph_root: str | Path,
    page_ref: str,
    block_uuid: str,
    search: str,
    replacement: str,
    *,
    dry_run: bool = True,
    use_regex: bool = False,
    replace_all: bool = False,
    case_sensitive: bool = True,
    baseline_mtime: float | None = None,
) -> PropertyLineEditOutcome:
    """Edit only ``key::`` property lines for the block identified by ``id::``."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    try:
        path = graph_safe_page_path(root, page_ref)
    except ValueError:
        return PropertyLineEditOutcome(
            ok=False,
            code="path_forbidden",
            hint="Resolved path would escape LOGSEQ_GRAPH_PATH.",
            dry_run=dry_run,
            match_count=0,
            previews=[],
            previous_size_bytes=0,
            current_size_bytes=0,
            lines_changed=0,
        )
    if not path.is_file():
        return PropertyLineEditOutcome(
            ok=False,
            code="page_not_found",
            hint=f"No file at `{path}` under graph root.",
            dry_run=dry_run,
            match_count=0,
            previews=[],
            previous_size_bytes=0,
            current_size_bytes=0,
            lines_changed=0,
        )

    if baseline_mtime is None:
        baseline_mtime = occ_snapshot(path)
    if baseline_mtime is not None and file_mtime_drifted(path, baseline_mtime):
        return PropertyLineEditOutcome(
            ok=False,
            code="occ_conflict",
            hint="File modified since the mutation was requested; write aborted.",
            dry_run=dry_run,
            match_count=0,
            previews=[],
            previous_size_bytes=0,
            current_size_bytes=0,
            lines_changed=0,
        )

    with page_rmw_lock(path):
        raw = path.read_bytes()
        previous_size = len(raw)

        if use_regex:
            try:
                re.compile(search, 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                return PropertyLineEditOutcome(
                    ok=False,
                    code="invalid_regex",
                    hint=str(exc),
                    dry_run=dry_run,
                    match_count=0,
                    previews=[],
                    previous_size_bytes=previous_size,
                    current_size_bytes=previous_size,
                    lines_changed=0,
                )

        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if not lines:
            lines = [""]

        protected = compute_page_protected_line_indices(text)

        stripped = strip_lines_for_match(lines)
        id_idx = find_id_line_index(stripped, block_uuid)
        if id_idx is None:
            return PropertyLineEditOutcome(
                ok=False,
                code="uuid_not_found",
                hint="No `id:: <uuid>` line matches this block_uuid in the page file.",
                dry_run=dry_run,
                match_count=0,
                previews=[],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        bullet_idx = block_bullet_index(stripped, id_idx)
        if bullet_idx is None:
            return PropertyLineEditOutcome(
                ok=False,
                code="malformed_block",
                hint="Could not find a list bullet (`-` / `*` / `+`) above the `id::` line.",
                dry_run=dry_run,
                match_count=0,
                previews=[],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        end = block_subtree_end(stripped, id_idx, bullet_idx)
        prop_indices = _property_line_indices(stripped, bullet_idx + 1, end)
        if not prop_indices:
            return PropertyLineEditOutcome(
                ok=False,
                code="no_property_lines",
                hint="No property lines found in the resolved block span.",
                dry_run=dry_run,
                match_count=0,
                previews=[],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        blocked = [li for li in prop_indices if li in protected]
        if blocked:
            first = blocked[0] + 1
            return PropertyLineEditOutcome(
                ok=False,
                code="protected_fence",
                hint=(
                    "A property line in this block sits inside a global Markdown / query / HTML "
                    f"dead zone (first hit: line {first})."
                ),
                dry_run=dry_run,
                match_count=0,
                previews=[],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        previews: list[str] = []
        total_matches = 0
        new_lines = list(lines)
        lines_changed = 0

        for li in prop_indices:
            original = new_lines[li]
            core = strip_line_endings(original)
            new_core, mc = _apply_pattern(
                core,
                search,
                replacement,
                use_regex=use_regex,
                replace_all=replace_all,
                case_sensitive=case_sensitive,
            )
            total_matches += mc
            if mc and new_core != core:
                lines_changed += 1
                previews.append(f"L{li + 1}: `{core[:120]}` → `{new_core[:120]}`")
            new_lines[li] = new_core + canonical_line_suffix(original)

        if total_matches == 0:
            return PropertyLineEditOutcome(
                ok=False,
                code="no_match",
                hint="Search pattern did not match any allowed property line in the block span.",
                dry_run=dry_run,
                match_count=0,
                previews=[],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        if not replace_all and total_matches > 1:
            return PropertyLineEditOutcome(
                ok=False,
                code="ambiguous_match",
                hint=(
                    "Multiple matches but replace_all=false; "
                    "narrow the pattern or enable replace_all."
                ),
                dry_run=dry_run,
                match_count=total_matches,
                previews=previews[:10],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        new_text = "".join(strip_line_endings(ln) + canonical_line_suffix(ln) for ln in new_lines)
        new_bytes = new_text.encode("utf-8")

        if dry_run:
            return PropertyLineEditOutcome(
                ok=True,
                code="dry_run_ok",
                hint="Re-run with dry_run=false to apply (writes atomically with .bak).",
                dry_run=True,
                match_count=total_matches,
                previews=previews[:20],
                previous_size_bytes=previous_size,
                current_size_bytes=len(new_bytes),
                lines_changed=lines_changed,
            )

        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        write_mtime = baseline_mtime if baseline_mtime is not None else read_file_mtime(path)
        if write_mtime is None or not atomic_write_bytes_if_unchanged(
            path,
            new_bytes,
            graph_root=root,
            baseline_mtime=write_mtime,
        ):
            return PropertyLineEditOutcome(
                ok=False,
                code="occ_conflict",
                hint="File modified since the mutation was requested; write aborted.",
                dry_run=False,
                match_count=total_matches,
                previews=previews[:20],
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
                lines_changed=0,
            )

        final_size = path.stat().st_size
        return PropertyLineEditOutcome(
            ok=True,
            code="applied",
            hint=f"Backup at `{bak.name}` next to the page.",
            dry_run=False,
            match_count=total_matches,
            previews=previews[:20],
            previous_size_bytes=previous_size,
            current_size_bytes=final_size,
            lines_changed=lines_changed,
        )


_ALIAS_LINE_ANY = re.compile(r"(?im)^(\s*)alias::(\s*)(.+)\s*$")


def _split_alias_csv(raw: str) -> list[str]:
    return split_logseq_property_list_values(raw)


def _alias_token_compare_key(token: str) -> str:
    t = token.strip()
    if t.startswith("[[") and t.endswith("]]"):
        t = t[2:-2]
    return normalize_concept_key(t)


def _format_alias_token(display: str, use_wikilink: bool) -> str:
    inner = display.strip().strip("[]").strip()
    if use_wikilink:
        return f"[[{inner}]]"
    return inner


@dataclass(frozen=True, slots=True)
class PageAliasAppendResult:
    """Result of appending to a page-level ``alias::`` line (or creating one)."""

    ok: bool
    code: str
    hint: str
    dry_run: bool
    added: bool
    line_before: str | None
    line_after: str | None
    previous_size_bytes: int
    current_size_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "added": self.added,
            "line_before": self.line_before,
            "line_after": self.line_after,
            "previous_size_bytes": self.previous_size_bytes,
            "current_size_bytes": self.current_size_bytes,
        }


def append_page_alias_line(
    graph_root: str | Path,
    page_ref: str,
    alias: str,
    *,
    dry_run: bool = True,
    baseline_mtime: float | None = None,
) -> PageAliasAppendResult:
    """Append ``alias`` to the first ``alias::`` line, or add a new line at EOF (idempotent).

    Matching ignores case and wikilink brackets when checking duplicates.
    """
    root = Path(graph_root).expanduser().resolve(strict=False)
    try:
        path = graph_safe_page_path(root, page_ref)
    except ValueError:
        return PageAliasAppendResult(
            ok=False,
            code="path_forbidden",
            hint="Resolved path would escape LOGSEQ_GRAPH_PATH.",
            dry_run=dry_run,
            added=False,
            line_before=None,
            line_after=None,
            previous_size_bytes=0,
            current_size_bytes=0,
        )
    if not path.is_file():
        return PageAliasAppendResult(
            ok=False,
            code="page_not_found",
            hint=f"No file at `{path}` under graph root.",
            dry_run=dry_run,
            added=False,
            line_before=None,
            line_after=None,
            previous_size_bytes=0,
            current_size_bytes=0,
        )

    raw = path.read_bytes()
    previous_size = len(raw)
    if baseline_mtime is None:
        baseline_mtime = occ_snapshot(path)
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [""]

    stripped = strip_lines_for_match(lines)
    protected = compute_page_protected_line_indices(text)
    target_idx: int | None = None
    for i, line in enumerate(stripped):
        if _ALIAS_LINE_ANY.match(line):
            target_idx = i
            break

    new_key = _alias_token_compare_key(alias)
    if not new_key:
        return PageAliasAppendResult(
            ok=False,
            code="empty_alias",
            hint="Alias value is empty after normalization.",
            dry_run=dry_run,
            added=False,
            line_before=None,
            line_after=None,
            previous_size_bytes=previous_size,
            current_size_bytes=previous_size,
        )

    if target_idx is not None:
        line = stripped[target_idx]
        if target_idx in protected:
            return PageAliasAppendResult(
                ok=False,
                code="protected_fence",
                hint="The existing `alias::` line sits inside a fenced / query / HTML dead zone.",
                dry_run=dry_run,
                added=False,
                line_before=None,
                line_after=None,
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
            )
        m = _ALIAS_LINE_ANY.match(line)
        if not m:
            return PageAliasAppendResult(
                ok=False,
                code="internal",
                hint="Lost alias line match.",
                dry_run=dry_run,
                added=False,
                line_before=None,
                line_after=None,
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
            )
        indent, mid_space, payload = m.group(1), m.group(2), m.group(3)
        tokens = _split_alias_csv(payload)
        existing_keys = {_alias_token_compare_key(t) for t in tokens}
        use_wiki = any("[[" in t for t in tokens) or " " in alias.strip()
        if new_key in existing_keys:
            return PageAliasAppendResult(
                ok=True,
                code="noop_duplicate",
                hint="Alias already present on the page (case/bracket insensitive).",
                dry_run=dry_run,
                added=False,
                line_before=line,
                line_after=line,
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
            )
        new_tok = _format_alias_token(alias, use_wikilink=use_wiki)
        new_payload = f"{payload.rstrip()}, {new_tok}"
        new_core = f"{indent}alias::{mid_space}{new_payload}"
        new_line = new_core + canonical_line_suffix(lines[target_idx])
        new_lines = list(lines)
        new_lines[target_idx] = new_line
    else:
        use_wiki = " " in alias.strip()
        new_tok = _format_alias_token(alias, use_wikilink=use_wiki)
        new_text_body = inject_page_property(text, "alias", new_tok)
        new_bytes_preview = new_text_body.encode("utf-8")
        if dry_run:
            return PageAliasAppendResult(
                ok=True,
                code="dry_run_ok",
                hint="No `alias::` line found; would inject page-level `alias::` frontmatter.",
                dry_run=True,
                added=True,
                line_before=None,
                line_after=f"alias:: {new_tok}",
                previous_size_bytes=previous_size,
                current_size_bytes=len(new_bytes_preview),
            )
        with page_rmw_lock(path):
            if baseline_mtime is not None and file_mtime_drifted(path, baseline_mtime):
                return PageAliasAppendResult(
                    ok=False,
                    code="occ_conflict",
                    hint="File modified since the mutation was requested; write aborted.",
                    dry_run=False,
                    added=False,
                    line_before=None,
                    line_after=None,
                    previous_size_bytes=previous_size,
                    current_size_bytes=previous_size,
                )
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)
            write_mtime = baseline_mtime if baseline_mtime is not None else read_file_mtime(path)
            if write_mtime is None or not atomic_write_bytes_if_unchanged(
                path,
                new_bytes_preview,
                graph_root=root,
                baseline_mtime=write_mtime,
            ):
                return PageAliasAppendResult(
                    ok=False,
                    code="occ_conflict",
                    hint="File modified since the mutation was requested; write aborted.",
                    dry_run=False,
                    added=False,
                    line_before=None,
                    line_after=None,
                    previous_size_bytes=previous_size,
                    current_size_bytes=previous_size,
                )
        final = path.stat().st_size
        return PageAliasAppendResult(
            ok=True,
            code="applied",
            hint=f"Page-level alias injected as frontmatter; backup `{bak.name}`.",
            dry_run=False,
            added=True,
            line_before=None,
            line_after=f"alias:: {new_tok}",
            previous_size_bytes=previous_size,
            current_size_bytes=final,
        )

    new_text = "".join(strip_line_endings(ln) + canonical_line_suffix(ln) for ln in new_lines)
    new_bytes = new_text.encode("utf-8")
    if dry_run:
        return PageAliasAppendResult(
            ok=True,
            code="dry_run_ok",
            hint="Re-run with dry_run=false to apply (writes atomically with .bak).",
            dry_run=True,
            added=True,
            line_before=stripped[target_idx],
            line_after=new_core,
            previous_size_bytes=previous_size,
            current_size_bytes=len(new_bytes),
        )

    bak = path.with_suffix(path.suffix + ".bak")
    with page_rmw_lock(path):
        if baseline_mtime is not None and file_mtime_drifted(path, baseline_mtime):
            return PageAliasAppendResult(
                ok=False,
                code="occ_conflict",
                hint="File modified since the mutation was requested; write aborted.",
                dry_run=False,
                added=False,
                line_before=stripped[target_idx],
                line_after=new_core,
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
            )
        shutil.copy2(path, bak)
        write_mtime = baseline_mtime if baseline_mtime is not None else read_file_mtime(path)
        if write_mtime is None or not atomic_write_bytes_if_unchanged(
            path,
            new_bytes,
            graph_root=root,
            baseline_mtime=write_mtime,
        ):
            return PageAliasAppendResult(
                ok=False,
                code="occ_conflict",
                hint="File modified since the mutation was requested; write aborted.",
                dry_run=False,
                added=False,
                line_before=stripped[target_idx],
                line_after=new_core,
                previous_size_bytes=previous_size,
                current_size_bytes=previous_size,
            )

    final_size = path.stat().st_size
    return PageAliasAppendResult(
        ok=True,
        code="applied",
        hint=f"Backup at `{bak.name}` next to the page.",
        dry_run=False,
        added=True,
        line_before=stripped[target_idx],
        line_after=new_core,
        previous_size_bytes=previous_size,
        current_size_bytes=final_size,
    )


__all__ = [
    "PageAliasAppendResult",
    "PropertyLineEditOutcome",
    "append_page_alias_line",
    "edit_block_property_lines",
]

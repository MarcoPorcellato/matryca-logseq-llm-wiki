# Phase 4: Logseq Native Superpowers

Actionable roadmap to evolve the MCP agent into a **proactive knowledge manager** using Logseq-native syntax only: **Advanced Queries**, **journal/task conventions**, and **`alias::` resolution**. All work stays in the **pure local Markdown / outliner** paradigm: **no new databases**, **no new Markdown parsers** beyond the existing **`logseq-matryca-parser`** integration; graph logic uses **string scanning**, **regex / line-based helpers**, **FastMCP** tools, and existing bridges (`LogseqClient`, surgical property editing).

---

## 1. Agentic Datalog Queries (Advanced Query blocks)

Inspired by the idea of programmatic query construction (e.g. `cldwalker/logseq-query`): expose tools that let the agent **author valid `#+BEGIN_QUERY` … `#+END_QUERY`** blocks and **insert them into the graph** (as block content under a chosen parent), so dashboards can refresh inside Logseq instead of static agent-generated lists.

- [ ] **Design query payload contract** (Pydantic): optional title, `:query` vector tree as JSON-friendly structure, `:inputs` / `:collapsed?` / `:breadcrumb?` etc., with validation rules aligned with Logseq Advanced Query syntax (string-escape and bracket balancing checks only—no custom Datalog engine).
- [ ] **Implement query block assembler** in new module `src/graph/advanced_query_block.py`: build canonical `#+BEGIN_QUERY` / `#+END_QUERY` text from validated input; reject malformed trees with actionable errors for the agent.
- [ ] **Optional template library** in `src/graph/templates.py` (extend existing): named presets (e.g. “open todos this week”, “pages tagged X”) returning query vectors as data, consumed by `advanced_query_block.py`.
- [ ] **MCP tool** in `src/agent/mcp_server.py`: e.g. `inject_logseq_advanced_query` (name TBD) — parameters: parent block UUID (or page + insertion policy consistent with existing tools), assembled query body, dry-run flag returning rendered markdown without write.
- [ ] **Wire `LogseqClient.append_block` / existing outline writer** so injected content is a **single block** (or parent + children) matching how Logseq stores query blocks; reuse patterns from `write_logseq_outline` / property merge validators.
- [ ] **Agent guidance**: tool descriptions + optional routing hints so the model prefers **live query blocks** over static bullet lists where appropriate.
- [ ] **Tests** in `tests/test_advanced_query_block.py` (and MCP integration slice in `tests/test_mcp_server.py`): golden examples for rendered fences, escaping edge cases, validation failures.

**Files (create / modify):**

| Action | Path |
|--------|------|
| Create | `src/graph/advanced_query_block.py` |
| Modify | `src/graph/templates.py` (optional presets) |
| Modify | `src/graph/__init__.py` (exports if needed) |
| Modify | `src/agent/mcp_server.py` (new tool + validation) |
| Create | `tests/test_advanced_query_block.py` |
| Modify | `tests/test_mcp_server.py` |

---

## 2. Journal Rollover & Task Manager

Tooling to **scan `journals/` for the last 7 days**, collect unresolved **`TODO` / `LATER` / `WAITING`** items (Logseq marker conventions), respect **`SCHEDULED:`** and **`DEADLINE:`** when present, and let the agent write a consolidated **“Task Review”** into **today’s** journal page using native syntax.

- [ ] **Discover journal paths** from `MatrycaWikiConfig` / `LOGSEQ_GRAPH_PATH` (same pattern as other graph-wide tools); enumerate daily journal files for rolling window **N = 7** (configurable constant or tool param, default 7).
- [ ] **Implement scanner** in new module `src/graph/journal_task_scan.py`: line- and indent-aware **string pass** over each journal file; detect marker keywords (`TODO`, `LATER`, `WAITING`, `DOING` optional policy); capture block text, optional `SCHEDULED:` / `DEADLINE:` lines (regex), and source date from filename.
- [ ] **Structured report** (dataclasses / TypedDict) consumed by a formatter for MCP JSON + optional markdown summary for the agent.
- [ ] **MCP tool** in `src/agent/mcp_server.py`: e.g. `analyze_journal_tasks` — returns structured unresolved tasks + suggested consolidation snippet (markdown) **without** auto-writing unless a separate explicit write tool is called (keeps user in control; agent “proactive” via orchestration).
- [ ] **Optional companion tool** (same phase or follow-up): append consolidated review to today’s journal via **`LogseqClient`** or existing file-aware path if journal page UUID resolution is standardized—document chosen approach in tool description.
- [ ] **Tests** in `tests/test_journal_task_scan.py`: fixture journal markdown covering markers, indent nesting, scheduled/deadline lines, and empty windows.

**Files (create / modify):**

| Action | Path |
|--------|------|
| Create | `src/graph/journal_task_scan.py` |
| Modify | `src/agent/mcp_server.py` (`analyze_journal_tasks` + optional write helper) |
| Modify | `src/config.py` only if a single knob for journal folder name / path is required (prefer reusing existing graph root) |
| Create | `tests/test_journal_task_scan.py` |
| Modify | `tests/test_mcp_server.py` |

---

## 3. Alias Manager & Entity Resolution

Inspired by graph validation / deduping concepts (e.g. `logseq/graph-validator`): before creating pages, the agent **indexes `alias::` properties** across **`pages/**/*.md`** (and optionally journals if aliases appear there), resolves **candidate titles** against existing canonical pages and alias sets, and **appends new aliases surgically** instead of spawning duplicate entity pages.

- [ ] **Full-graph alias index** in new module `src/graph/alias_index.py`: scan property lines for `alias::` (and Logseq multi-value conventions if used: comma-separated / quoted); build map: normalized alias → canonical page title / file path; handle collisions with deterministic warnings.
- [ ] **Entity resolution API**: `resolve_page_title(candidate: str) -> ResolvedEntity` (canonical title, file, matched alias, confidence); fuzzy matching policy: **normalization only** (casefold, trim, collapse whitespace) unless you explicitly add a tiny Levenshtein—**default: no extra dependencies**; document tradeoff in module docstring.
- [ ] **MCP tool** in `src/agent/mcp_server.py`: e.g. `resolve_logseq_entity` — input candidate string(s), return canonical page + existing aliases + “safe to create” boolean.
- [ ] **Surgical append** reuse: extend or wrap `src/graph/property_line_edit.py` (modify) with **`append_alias_to_page`**: idempotent append of a new alias value to the first `alias::` line or create property block per project conventions; **string-based** file edit consistent with existing surgical editor guarantees.
- [ ] **Pre-flight hook pattern** (documentation + optional helper): suggest calling `resolve_logseq_entity` before `write_logseq_outline` when `page_type` is entity; optionally add routing hint in `src/agent/routing_hint.py` if that file owns similar hints.
- [ ] **Tests** in `tests/test_alias_index.py` and `tests/test_property_line_edit.py` (extend for alias append) + MCP tests for the new tool.

**Files (create / modify):**

| Action | Path |
|--------|------|
| Create | `src/graph/alias_index.py` |
| Modify | `src/graph/property_line_edit.py` (alias append / merge helpers) |
| Modify | `src/graph/__init__.py` (exports if needed) |
| Modify | `src/agent/mcp_server.py` (`resolve_logseq_entity` + optional `append_page_alias`) |
| Modify | `src/agent/routing_hint.py` (optional alias preflight hint) |
| Create | `tests/test_alias_index.py` |
| Modify | `tests/test_property_line_edit.py` |
| Modify | `tests/test_mcp_server.py` |

---

## Cross-cutting constraints (all sections)

- [ ] **No new databases** (SQLite, KV, etc.); indexes are **in-memory per tool call** or small cached structures inside the MCP process if needed later—Phase 4 starts stateless per request.
- [ ] **No new Markdown parsers**; optional enrichment: use existing **`get_page_spatial_context`** / parser only where it already adds value; default paths remain **regex / line-based** for speed and predictability on full-graph scans.
- [ ] **FastMCP-only** transport; tools return **structured JSON + markdown snippets** suitable for agent reasoning.
- [ ] **Docs touch** (only when implementing): brief mentions in `docs/ARCHITECTURE.md` for new tools—deferred until coding phase per repo convention.

---

Please review the Logseq Superpowers Roadmap. Say **Proceed** to begin implementation.

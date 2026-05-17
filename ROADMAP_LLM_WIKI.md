# Roadmap: LLM-Wiki patterns → Matryca (FastMCP / Logseq OG)

Actionable checklist derived from [MehmetGoekce/llm-wiki](https://github.com/MehmetGoekce/llm-wiki) (L1/L2 architecture, openspec ingest/lint, schema templates, dashboard hub) adapted to **this** repo: **FastMCP**, **uv**, **loguru**, **Pydantic**, **async/await**, external **`logseq-matryca-parser`**, **no SQLite**. Prefer MCP tools + on-disk graph reads over Claude Code slash commands.

---

## Baseline (required concepts)

- [x] **L1/L2 cache MCP tool (`read_l1_memory`)** — Expose a fast-context read: one or more small Markdown files (session rules, identity, gotchas) from a path such as `MATRYCA_L1_PATH` or `LOGSEQ_GRAPH_PATH/../matryca-l1/` so the agent loads “CPU L1” without scanning the whole vault. Return trimmed text + file list; document routing (dangerous/embarrassing → L1 vs deep wiki → L2) in `SYSTEM_PROMPT.md`. **Create:** optional `src/agent/l1_memory.py` (path resolution, size limits). **Modify:** `src/agent/mcp_server.py` (register tool), `src/main.py` if lifespan needs extra config, `README.md` / `.env.example` for env vars, `tests/test_mcp_server.py`.

- [x] **Graph lint MCP tool (broken `((uuid))` block refs)** — Walk `LOGSEQ_GRAPH_PATH` (at least `pages/**/*.md`), find Logseq block-reference patterns `((uuid))`, validate UUID v4 shape, and flag references to UUIDs that never appear as `id:: <uuid>` (or parser-exposed UUIDs) in the graph. Optionally reuse parser APIs where they expose all block UUIDs; otherwise a two-pass scan (collect ids, then validate refs). **Create:** `src/graph/block_ref_lint.py` (pure filesystem + regex; delegate UUID index to parser if available). **Modify:** `src/agent/mcp_server.py` (register `lint_logseq_block_refs` or similar), `docs/ARCHITECTURE.md` (lint section), `tests/test_block_ref_lint.py`.

- [x] **Namespace & schemas on `OutlineNode`** — Extend `OutlineNode` with optional, validated fields aligned with llm-wiki schema ideas: e.g. `page_type` (entity | project | knowledge | hub | feedback), `domain` (tech | business | content | ops | …), `entity_type` when `page_type=entity`, plus required `properties` keys enforced via Pydantic `model_validator` for certain types. Keeps agent JSON explicit and matches Logseq `type::` / `domain::` conventions. **Modify:** `src/agent/mcp_server.py` (`OutlineNode`, validators, tool docstrings), `SYSTEM_PROMPT.md` (property conventions), `tests/test_mcp_server.py`.

- [x] **3-phase ingestion workflow in `SYSTEM_PROMPT.md`** — Replace/augment the linear “Execution Rules” with explicit **Search → Scan → Update** phases mirroring llm-wiki’s ingest spec: (1) *Search* — identify sources, entities, L1 vs L2 routing; (2) *Scan* — `read_logseq_page` / spatial context, resolve targets under `LOGSEQ_GRAPH_PATH`, plan creates vs appends; (3) *Update* — `write_logseq_outline` via API, non-destructive appends, `updated::` / cross-refs. Add quality gate bullets (no credentials in L2 pages, max children, etc.). **Modify:** `SYSTEM_PROMPT.md`; optionally `docs/ARCHITECTURE.md` cross-link.

- [x] **Metrics dashboard MCP tool** — Implement a tool that scans the graph (page counts, blocks with `id::`, broken ref count from lint module, last-modified hints) and returns **Logseq-ready outline Markdown** (or writes under a fixed page name via API if you add a “dashboard parent UUID” env) so the user can paste or target a **[[Matryca Dashboard]]** page. Inspired by `templates/logseq/Dashboard.md` + `/wiki status`. **Create:** `src/graph/dashboard.py` (aggregation only; no DB). **Modify:** `src/agent/mcp_server.py`, `tests/test_dashboard.py`, `README.md`.

---

## Additional concepts from llm-wiki (high value)

- [x] **Optional `matryca-wiki.yml` (or reuse env)** — Mirror `config.example.yml`: namespaces list, L1 path, max depth, dashboard page title. Parser stays authoritative for markdown shape; YAML only for orchestration. **Create:** `matryca-wiki.example.yml`. **Modify:** `src/main.py` / small `src/config.py` loader, `README.md`.

- [x] **Wiki-style lint pack (non-UUID)** — Port a *subset* of openspec `lint.md`: orphan/stale heuristics for pages under a configurable prefix (e.g. `Matryca___` or `Wiki___`), missing `type::` / dates, credential-pattern scan (`token::`, long base64). Keep scope Logseq-OG-safe (outliner lines). **Create:** `src/graph/wiki_lint.py`. **Modify:** `src/agent/mcp_server.py`, `tests/test_wiki_lint.py`.

- [x] **Ingest quality gate helper** — Shared function used before/after writes: block credential patterns in outline payloads, enforce minimum cross-link properties when `page_type` demands it. **Create:** `src/agent/quality_gate.py`. **Modify:** `src/agent/mcp_server.py`, tests.

- [x] **L1/L2 routing hint in tool outputs** — When `read_logseq_page` or ingestion tools return summaries, append a short machine-readable “routing” hint (L1 candidate vs L2) using the llm-wiki routing question. **Modify:** `src/rag/matryca_hooks.py` or `mcp_server.py`, `SYSTEM_PROMPT.md`.

- [x] **Hub / namespace index generator** — Tool or dashboard section that lists pages per namespace folder (Logseq `pages/` flat names) for navigation, inspired by `templates/logseq/Hub.md`. **Modify:** `src/graph/dashboard.py` or new `src/graph/hubs.py`, `mcp_server.py`, tests.

- [x] **Openspec-style internal docs** — Add `docs/openspec/` with trimmed specs (ingest phases, lint rules, L1/L2 routing) pointing to this roadmap for traceability. **Create:** `docs/openspec/*.md` (markdown only).

- [x] **Query / RAG bridge (lightweight)** — Optional MCP tool: given a keyword, rank pages by simple token frequency or parser-backed block text scan (no SQLite; in-memory index built per call). Bridges toward llm-wiki `/wiki query` without a vector DB. **Create:** `src/rag/local_query.py`. **Modify:** `src/agent/mcp_server.py`, tests.

---

## Done criteria (global)

- [x] All new tools registered on the shared `FastMCP` instance in `src/main.py` path (`register_mcp_tools`).
- [x] `make check` (ruff, mypy, pytest) passes after each implemented item.
- [x] No new SQLite or custom full-file Logseq regex parsers for block structure — use **`logseq_matryca_parser`** for spatial tasks; plain scans only where the external parser does not cover the concern (e.g. raw `((uuid))` grep across files).

---

When implementation starts, work top-to-bottom, one checkbox per PR-sized slice, and mark `[x]` only after `make check` is green.

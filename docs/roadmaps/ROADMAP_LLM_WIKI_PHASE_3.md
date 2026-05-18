# Phase 3 roadmap — High-safety PKM + deep outliner tools (no Postgres)

Actionable checklist aligned with **pure Logseq OG**: on-disk Markdown, **`logseq_matryca_parser`** for spatial truth where structure matters, **FastMCP** tools, and **no PostgreSQL**, **no embeddings**, **no PyMuPDF/VLM ingestion**.

**Inspirations (patterns to study, not vendor):**

- [drewburchfield/obsidian-graph](https://github.com/drewburchfield/obsidian-graph) — multi-hop BFS, connection discovery, hub/orphan thinking (we keep **structural** hops only).
- [cyanheads/obsidian-mcp-server](https://github.com/cyanheads/obsidian-mcp-server) — surgical regex replace, capture groups, typed errors, size / blast-radius hints.
- [BakeRolls/mcp-git](https://github.com/BakeRolls/mcp-git) & Logseq **`logseq/git-auto`** idea — lightweight **git** snapshots before risky writes so humans can roll back.
- [SilentVoid13/Templater](https://github.com/SilentVoid13/Templater) — discover **templates/** so the agent hydrates from the user’s own property/tag/block conventions.
- [scambier/obsidian-omnisearch](https://github.com/scambier/obsidian-omnisearch) — **BM25 / TF-IDF**-style relevance over note text (we stay pure-Python, in-memory, no DB).

---

## Semantic hop traversal (BFS) — inspired by obsidian-graph

- [x] **Explicit-link and tag hop graph (BFS / bounded DFS)** — Build a pure on-disk adjacency over `pages/**/*.md`: `[[Page]]` / `[[Page|alias]]` wikilinks, `#tags` and `tags::` lines, plus shared `type::` / `domain::` edges. MCP tool returns hop levels, cycle-skipped visited set, and per-edge reason (link vs tag vs schema). **Create:** `src/graph/link_tag_hop.py`. **Modify:** `src/agent/mcp_server.py`, `tests/test_link_tag_hop.py`, `docs/ARCHITECTURE.md`.

- [x] **Hub / orphan signals on the structural graph** — Rank pages by undirected neighbor count (wikilinks + tags + schema edges from the hop builder); surface top hubs and low-degree “orphans” (disk-only, no vectors). **Modify:** `src/graph/link_tag_hop.py` (or small `src/graph/structural_hubs.py`), `src/agent/mcp_server.py`, `tests/test_link_tag_hop.py`, optional one-line in `src/graph/dashboard.md` / dashboard if we fold a summary later.

---

## Surgical metadata editor — inspired by cyanheads/obsidian-mcp-server

- [x] **Scoped property-line regex tool** — MCP tool: `page` path or title, `block_uuid`, `search` / `replacement`, `use_regex`, `replace_all`, case flag. Matches **only** Logseq `key::` property lines in the target block’s span (line-based region anchored at `id:: <uuid>`); support `$1` / `$&` when `use_regex`. **Create:** `src/graph/property_line_edit.py`. **Modify:** `src/agent/mcp_server.py`, `tests/test_property_line_edit.py`.

- [x] **Pre-flight validation and typed errors** — Dry-run returns match count + previews; errors for 0 matches, ambiguous single replace, path outside graph, UUID not anchored; stable `code` + `hint` fields in tool result. **Modify:** `src/graph/property_line_edit.py`, `src/agent/mcp_server.py`, `tests/test_property_line_edit.py`.

- [x] **Atomic disk write with backup** — On apply, write via temp swap + optional `.bak` of the original page once; document Logseq API limitation if we do not add `updateBlock` RPC. **Modify:** `src/graph/property_line_edit.py`, `docs/ARCHITECTURE.md`.

---

## Core PKM refinements & safety

- [x] **Agentic Git snapshots (inspired by `BakeRolls/mcp-git` & `logseq/git-auto`)** — Opt-in (`MATRYCA_GIT_SNAPSHOT_ON_WRITE`): before `write_logseq_outline`, run `git add -A` + `git commit` with a fixed message (e.g. `matryca: AI pre-edit snapshot`) under `LOGSEQ_GRAPH_PATH` when `.git` exists; no-op with reason when disabled or not a repo. Optional MCP tool for a manual snapshot. **Create:** `src/agent/git_snapshot.py`. **Modify:** `src/agent/mcp_server.py` (`MatrycaMCPServer.write_logseq_outline`), `.env.example`, `tests/test_git_snapshot.py`.

- [x] **Template hydration (inspired by `SilentVoid13/Templater`)** — MCP tool `read_logseq_template` (and optional `list_logseq_templates`) reading `templates/*.md` under the graph so the agent can mirror user property/tag/block patterns. **Create:** `src/graph/templates.py`. **Modify:** `src/agent/mcp_server.py`, `tests/test_templates.py`.

- [x] **In-memory BM25 / TF-IDF ranking (inspired by `scambier/obsidian-omnisearch`)** — Upgrade `query_logseq_pages_local`: pure-Python BM25 (or TF-IDF) over per-page documents from `pages/**/*.md`; keep optional legacy substring mode. No external databases. **Modify:** `src/rag/local_query.py`, `src/agent/mcp_server.py`, `tests/test_local_query.py`.

---

## Cross-cutting quality & docs

- [x] **MCP tool result contracts** — Mutating tools return `previous_size_bytes` / `current_size_bytes` and/or structured `git_snapshot` / `edit` metadata where applicable (cyanheads-style visibility). **Modify:** affected modules + `src/agent/mcp_server.py`.

- [x] **Architecture and openspec** — Phase 3 section: parser boundary, no vectors/Postgres, git snapshot opt-in. **Modify:** `docs/ARCHITECTURE.md`, `docs/openspec/README.md`.

- [x] **Config knobs** — Env / `matryca-wiki.yml` for hop max depth, BM25 top-k, template directory override, snapshot toggle. **Modify:** `src/config.py`, `matryca-wiki.example.yml`, `.env.example`.

---

## Done criteria (Phase 3)

- [x] Each new tool registered in `register_mcp_tools` (`src/agent/mcp_server.py`) without breaking existing defaults.
- [x] `make check` green after each merged slice.
- [x] No PostgreSQL, no Redis/SQLite vector store, no embedding models, no PDF/VLM pipeline.

Phase 3 items above are implemented; extend this file when planning Phase 4.

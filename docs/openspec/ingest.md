# Ingest (Matryca MCP)

**Source of truth for the living roadmap:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md)

## Phases (Logseq OG)

1. **Search** — Identify sources, extract entities/facts, classify, apply L1 vs L2 routing (`read_l1_memory`, `SYSTEM_PROMPT.md`).
2. **Scan** — `read_logseq_page`, `query_logseq_pages_local`, `lint_logseq_block_refs`, `lint_matryca_wiki_pages`, `render_logseq_dashboard`, `list_logseq_namespace_index`.
3. **Update** — `write_logseq_outline` (Logseq JSON-RPC). Payloads pass `quality_gate` credential scan before Pydantic `OutlineNode` validation.

## Tooling

- Optional orchestration: `matryca-wiki.yml` (see `matryca-wiki.example.yml`) via `src/config.py`.
- Spatial structure: external `logseq-matryca-parser` only (no custom outline regex in this repo).

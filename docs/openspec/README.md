# Openspec-style notes (Matryca)

Trimmed behavioral specs aligned with [MehmetGoekce/llm-wiki](https://github.com/MehmetGoekce/llm-wiki) openspec, mapped to this repository.

**Canonical checklist:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md) in the repo root.

| Document | Scope |
|----------|--------|
| [`ingest.md`](ingest.md) | Search → Scan → Update ingest phases (MCP + Logseq OG). |
| [`lint.md`](lint.md) | On-disk lint: block refs + wiki convention pack. |
| [`l1-l2-routing.md`](l1-l2-routing.md) | L1 memory vs L2 graph routing and MCP hints. |

Implementation entry points: `src/main.py`, `src/agent/mcp_server.py`, `src/config.py`, `src/graph/`, `src/rag/`.

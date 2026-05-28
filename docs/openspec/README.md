# Openspec-style notes (Matryca Plumber)

**Matryca Plumber** — developed by Marco Porcellato · [Matryca.ai](https://matryca.ai). Naming rules: [`../BRANDING.md`](../BRANDING.md).

Trimmed behavioral specs aligned with [MehmetGoekce/llm-wiki](https://github.com/MehmetGoekce/llm-wiki) openspec, mapped to this repository.

**Canonical checklists:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md), [`ROADMAP_LLM_WIKI_PHASE_3.md`](../../ROADMAP_LLM_WIKI_PHASE_3.md) in the repo root.

| Document | Scope |
|----------|--------|
| [`ingest.md`](ingest.md) | Search → Scan → Update ingest phases (MCP + Logseq OG). |
| [`lint.md`](lint.md) | On-disk lint: block refs + wiki convention pack. |
| [`l1-l2-routing.md`](l1-l2-routing.md) | L1 memory vs L2 graph routing and MCP hints. |
| [`runtime-bootstrap.md`](runtime-bootstrap.md) | Startup directory/config provisioning (logs, L1, cache, wiki YAML). |
| [`llm-performance.md`](llm-performance.md) | v1.8 KV-cache layout, RAM caps, cooperative bootstrap I/O. |
| [`../v1.8-SOFTWARE-EDGE-PLAN.md`](../v1.8-SOFTWARE-EDGE-PLAN.md) | CPU sandbox, frozen KV prefix, adaptive LLM, mmap reads. |
| [`../v1.8-OPTIMIZATION-PLAN.md`](../v1.8-OPTIMIZATION-PLAN.md) | v1.8 operator env vars, verification matrix, load testing. |

Implementation entry points: `src/main.py`, `src/utils/runtime_bootstrap.py`, `src/agent/mcp_server.py`, `src/config.py`, `src/graph/`, `src/rag/`.

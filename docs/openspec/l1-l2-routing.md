# L1 / L2 routing (Matryca)

**Roadmap:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md)

## Layers

- **L1** — `MATRYCA_L1_PATH`, `matryca-wiki.yml` `memory_path`, or `<graph-parent>/matryca-l1/*.md`. Loaded via MCP `read_l1_memory`.
- **L2** — Logseq graph on disk under `LOGSEQ_GRAPH_PATH` and API writes.

## Hints in tool output

Successful reads may end with `<!-- matryca_routing: hint=L1_candidate|L2_default -->`.  
`write_logseq_outline` returns JSON with `routing_hint` for L2 persistence.

See `SYSTEM_PROMPT.md` and `src/agent/routing_hint.py`.

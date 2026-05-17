# Lint (Matryca on-disk)

**Roadmap:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md)

## Tools

| MCP tool | Module | Role |
|----------|--------|------|
| `lint_logseq_block_refs` | `src/graph/block_ref_lint.py` | `((uuid))` vs graph-wide `id::` (regex pass). |
| `lint_matryca_wiki_pages` | `src/graph/wiki_lint.py` | Prefixed pages: `type::`, stale knowledge, credentials, wikilinks. |

Block structure for spatial semantics remains in **`logseq-matryca-parser`**; these lints are text/heuristic passes only.

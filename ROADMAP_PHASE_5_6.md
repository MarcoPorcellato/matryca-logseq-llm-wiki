# Phase 5 & 6 — Graph Gardener + Synthesis Engine

Actionable checklist for overnight implementation. **Constraints:** pure Markdown on disk, FastMCP tools, Logseq OG (no external DB). Reference repos inform behavior only (not vendored).

## Reference repositories (patterns only)

| Item | Inspiration |
|------|-------------|
| Native spaced repetition | [st3v3nmw/obsidian-spaced-repetition](https://github.com/st3v3nmw/obsidian-spaced-repetition) (`::` Q/A, card boundaries) |
| Tag / ontology lint | [platers/obsidian-linter](https://github.com/platers/obsidian-linter), Logseq tags-manager style normalization |
| Semantic outline refactor | [vslinko/obsidian-outliner](https://github.com/vslinko/obsidian-outliner) (reparent / indent tree) |
| Unlinked mentions | [brian-sun/logseq-plugin-unlinked-references](https://github.com/brian-sun/logseq-plugin-unlinked-references) |
| MOC / index pages | [zoottel/obsidian-zoottelkeeper](https://github.com/zoottel/obsidian-zoottelkeeper) |
| Atomic block split | [FeralFlora/obsidian-text-segmenter](https://github.com/FeralFlora/obsidian-text-segmenter) |

---

## Phase A — Graph Gardener

- [x] **1. `generate_logseq_flashcards`** — Read a dense block span on a page; extract Q/A (incl. `question :: answer` SRS-style where safe); append child bullets as Logseq native cards (`#card` on the question line); assign new `id::` per generated child.
- [x] **2. `lint_unify_logseq_tags`** — Scan `pages/` + `journals/` for `#tag` tokens; cluster case/variant forms; pick canonical by highest frequency; preview or apply global unification with URL / wikilink / code-fence guards (atomic per-file writes + `.bak`).
- [x] **3. `refactor_logseq_blocks`** — Same-page reparent: new category bullets, nest listed block UUIDs under them (indent-only moves, preserve `id::` and body); call `snapshot_logseq_graph_git` semantics via `snapshot_git_working_tree` when `dry_run=false`.

## Phase B — Synthesis Engine

- [x] **4. `resolve_unlinked_mentions`** — Disk scan: plain-text occurrences of existing page titles → locations + suggested `[[Title]]` (skip URLs, `[[...]]`, `` `...` ``, block refs).
- [x] **5. `generate_moc_page`** — Input namespace / domain string; list related pages; return hierarchical MOC Markdown (wikilink index) for paste or optional atomic page write.
- [x] **6. `refactor_large_blocks`** — Find overlong bullets; split into parent + child bullets; keep original `id::` on parent; nest remainder as children; snapshot when applying.

---

## Verification

After each implementation slice: `make check` (ruff, mypy, pytest).

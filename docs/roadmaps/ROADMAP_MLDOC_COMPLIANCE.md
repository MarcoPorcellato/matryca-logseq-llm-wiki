# Roadmap: mldoc-aligned graph tools (Phase 7)

Pure-Python structural rules inspired by the official Logseq Markdown/Org compiler (`logseq/mldoc`): no Rust/Clojure runtime, no external binary parser.

## Done (this phase)

- [x] **`src/graph/mldoc_properties.py`** — `key:: value` line parsing (first `::` only), bullet/`#`-heading exclusions, Unicode casefold keys, comma splitting with **double quotes** and **`[[wikilinks]]`** awareness, quoted spans for downstream guards; normalized key **`id` excluded** so `id:: <uuid>` is never treated as editable metadata by property hygiene or MCP property-line tools.
- [x] **`src/graph/mldoc_guards.py`** — Skip naive bullet surgery when the first line or pre-`id::` span suggests **fenced code**, **drawers** (`:NAME:`), or **`{{` macros**.
- [x] **`property_line_edit.py`** — Property line detection uses `mldoc_properties`; alias CSV uses `split_logseq_property_list_values`.
- [x] **`tag_unify.py`** — Tag unify matches the scanner: **no `#tag` rewrites inside quoted property values**; line-based apply preserves guards.
- [x] **`split_large_blocks.py`** — Respects mldoc guards before splitting long bullets.
- [x] **`unlinked_mentions.py`** — Excludes **`{{...}}`** macro spans from plain-title hits.
- [x] **`flashcards.py`** — Single source of truth: `is_logseq_block_property_line` from `mldoc_properties`.
- [x] **Tests** — `tests/test_mldoc_phase7.py` (+ full suite green).

## Possible follow-ups

- [ ] Finer **multi-line fence** state (odd/even backticks across the whole page) for tools that rewrite outside a single block.
- [ ] **Org `#+KEY:`** and journal-specific property shapes if you add Org ingestion.
- [ ] Optional **AST export** behind a feature flag if you later allow a native `mldoc` subprocess (out of scope for the current pure-Python policy).

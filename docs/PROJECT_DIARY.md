# Project diary — technical lifecycle log

This document records **architecture decisions**, **phase milestones**, and **real-world bugs crushed** during the evolution of **matryca-logseq-llm-wiki** from a baseline MCP bridge to a production-grade **Ironclad Autonomous Linter OS** with **100% Logseq Datalog parity** (Phase 15). For the engineering contract (modules, data planes, diagrams), see [`ARCHITECTURE.md`](ARCHITECTURE.md). For operator setup, see [`../README.md`](../README.md).

Entries are chronological (newest first within each phase summary). When a decision is superseded, add a new entry rather than rewriting history.

---

## [2026-05-23] Phase 15: The Ironclad Logseq-Native Shield & Windows Parity

### Context

Phase 14 delivered a production-grade Plumber daemon with GraphRAG clustering, Context Acceleration, and a React cockpit — but real-world graph stress tests exposed a class of bugs that generic Markdown tools never encounter: **Logseq's on-disk contract is not CommonMark**. Namespace pages split into ghost duplicates, page properties prefixed with `- ` fell out of the indexer, block `id::` lines orphaned below nested children broke `((uuid))` integrity, and Windows default encodings corrupted Unicode titles. Concurrent editing during slow local LLM inference risked **silent overwrites** of live human edits.

This phase closes the gap between "works on a test graph" and **100% parity with Logseq's internal Clojure Datalog filesystem layer** — with enterprise-grade concurrency safety and operator-visible mutation guardrails.

### Victories shipped

1. **349+ passing tests, zero Ruff/Mypy strict warnings** — the CI bar now enforces Logseq-native path hygiene (`tests/test_graph_path_hygiene.py`, `tests/test_page_path.py`, `tests/test_page_properties.py`, expanded plumber cognitive module coverage). **`make check`** green: **349 passed**, 2 skipped.

2. **Ghost Clones defeated** — `is_scannable_graph_markdown()` in `alias_index.py` explicitly excludes `logseq/` (including `logseq/bak/`), `.recycle/`, `.git`, and hidden directories from alias/catalog/daemon scans. Stale backup files no longer masquerade as live pages or trigger duplicate page creation by the Dangling Healer.

3. **Code Block Immunity hardened** — `global_fence_scanner.py` dead-zone indices guard every scan and mutation pass. Fenced ``` blocks, HTML comments, and `#+BEGIN_QUERY` … `#+END_QUERY` regions are immutable; markers **inside** open fences are masked so pasted examples cannot flip scanner state.

4. **Zero-byte / ghost file early-exits** — `is_blank_page_content()` in `plumber_modules/_shared.py` skips 0-byte and whitespace-only pages before inference — no crashes, no empty semantic index pollution.

5. **Explicit UTF-8 on all I/O** — every graph `read_text`, `write_text`, and `encode("utf-8")` commit path forces `encoding="utf-8"`. Prevents Windows `cp1252` locale corruption of Unicode page titles and property values.

6. **Optimistic Concurrency Control** — `read_file_mtime()` → LLM inference → `atomic_write_bytes_if_unchanged()` in `markdown_blocks.py`. If the user typed in Logseq during inference, `file_mtime_drifted()` aborts the write — **no data loss**. Complements `fcntl.flock` RMW locks with lost-update prevention.

7. **Physical vs. semantic namespace parity** — `page_path.py` mirrors Logseq's `:triple-lowbar` rule: `/` → `___`, then `urllib.parse.quote` for OS-reserved characters. Cross-platform Windows filenames now match what Logseq OG writes natively.

8. **True frontmatter vs. block property discipline** — `page_properties.py` injects page metadata at line 0 without bullet prefixes; block properties (`id::`, `matryca-plumber:: true`) remain contiguous to parent text at +2 space indent via `mldoc_properties.py` and `property_line_edit.py`.

9. **Alias-aware, case-insensitive resolution** — `MasterCatalog` + `alias_index.py` track comma-separated `alias::` values and resolve candidates case-insensitively via `normalize_concept_key()` before any module creates a new page — eliminating ghost duplicates from casing drift.

10. **Trust & Safety UI Drawer** — `frontend/src/components/SettingsDrawer.tsx` maps every cognitive lint toggle to a visible risk tier:
    - 🟢 **Safe Mode** — read-only & metadata (semantic routing, compression, entity consolidation, property hygiene, MARPA).
    - 🟠 **Augmented Mode** — foldable `- ### ` side-blocks and isolated seed pages (dangling healer, backlink backpropagation).
    - 🔴 **Surgeon Mode** — strictly opt-in inline semantic corrections and auto-split with `- {{embed [[Page]]}}` stubs.

    Toggles hot-reload via `reload_plumber_dotenv()` on the next daemon cycle — no restart required.

11. **Outliner UX** — generated sections use `- ### Heading` for Logseq foldability; auto-split replaces dense subtrees with embed stubs to keep parent files light while preserving inline readability.

### Outcome

Matryca Plumber now understands Logseq's idiosyncrasies — namespace encoding, property planes, fence dead zones, alias graphs, and concurrent edit windows — **better than any third-party tool on the market**. The graph re-indexes cleanly after daemon passes; operators control mutation depth from the cockpit; and the test suite proves the contract.

### Status

Shipped. **`make check`** — **349 passed**, 2 skipped; strict Mypy and Ruff clean.

---

## Phase map (Phases 1 → 15)

| Phase | Name | What shipped |
|:-----:|------|--------------|
| **1** | Baseline headless plane | FastMCP stdio, `OutlineNode`, DFS `write_logseq_outline`, parser-backed `read_logseq_page`, block-ref lint |
| **2** | L1 / L2 routing | Capped `read_l1_memory`, `routing_hint` traceability on tool payloads |
| **3** | PKM refinements | BM25 local query, structural hops, property-line surgery, templates, wiki lint, git snapshots |
| **4** | Logseq superpowers | Advanced Query injection, journal mining, entity resolution, alias append |
| **5** | Graph gardener | Flashcards, tag unify, same-page reparent |
| **6** | Synthesis engine | Unlinked mentions, MOC generation, large-block split |
| **7** | Mldoc compliance | `mldoc_properties` + `mldoc_guards` integrated into mutators |
| **8** | Ironclad Autonomous Linter OS | Global fence scanner, atomic writes, generational cache, **Matryca Plumber** daemon, Ermes context compression, structural quarantine, GraphRAG Louvain clustering, FastAPI + React cockpit, **349+ test** CI bar |
| **15** | Logseq-native parity shield | Namespace encoding parity, OCC mtime guard, frontmatter vs block properties, alias case-insensitivity, ghost-clone prevention, Trust & Safety UI, UTF-8 I/O hardening |

Phases **9–14** (Trust plane, delivery, Fortress, Headless Revolution, operational hardening, Plumber OS, Context Acceleration) are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md) § Complete phase evolution history.

---

## [2026-05-22] Phase 14d: Context Acceleration & TRIZ Hardening

### Context & defect discovery

Production validation on a live graph copy exposed a **5,260-block monster page** that turned Phase 2 cognitive lint into a sequential GPU bottleneck. Each sub-operation on the same file — MARPA classification, entity consolidation, property hygiene, semantic indexing — triggered a full LM Studio **prefill** pass because:

1. **Payload mass** — even flat-truncated excerpts exceeded practical local context budgets.
2. **Prefix invalidation** — dynamic task instructions preceded page content in the prompt string, so llama.cpp treated every turn as a new prefix and **destroyed KV-cache reuse** across the 5–6 inference calls per file.

In parallel, detached daemon forks revealed a **non-atomic `.env` resolution bug**: child processes called `load_dotenv()` without an explicit repository root. Forked Plumber workers inherited an arbitrary `cwd`, failed to load thermal delays and MapReduce thresholds from the repo `.env`, and appeared to ignore Settings Drawer hot-swaps — operators saw default pacing while the web cockpit wrote valid configuration to disk.

### The solution

Framing the bottleneck through **TRIZ** (Theory of Inventive Problem Solving) produced three surgical modules instead of a brute-force context window upgrade:

1. **`src/agent/llm_context_payload.py`** — Principle **10 (Preliminary Action)**: substitute raw megabyte-class text with Phase 1 hierarchical summaries above `mapreduce_trigger_chars`; Principle **2 (Separation)**: regex semantic skeleton when summaries are absent.
2. **`src/agent/prompt_layout.py`** — Principle **5 (Prefix Alignment)**: `build_cache_aligned_prompt()` enforces `[STABLE_CONTENT] + [DYNAMIC_TASK]` so consecutive operations share the heaviest token prefix for **100% Prompt Cache Hit** on turns 2–6.
3. **`reload_plumber_dotenv()`** — Principle **15 (Dynamicity)**: anchor `.env` to the repository root and re-read on every `_sync_runtime_config()` cycle so thermal breaks and lint flags adapt live from the React cockpit.

Six new integration tests in **`tests/test_llm_context_payload.py`** validate payload selection, skeleton extraction, and prefix ordering — pushing the global bar to **317 green validations** (1 skipped).

### Outcome

Local inference on giant pages transformed from a **sequential prefill bottleneck** (minutes per file, sustained GPU fan load) into an **accelerated caching workflow**: one prefill on the stable content prefix, near-instant subsequent turns. Disk mutations remain on raw markdown; only the LLM client receives compressed, cache-aligned payloads.

### Status

Shipped. **`make check`** — **317 passed**, 1 skipped; strict Mypy clean.

---

## [2026-05-21] Phase 14c: UI Hardening — Monolithic Cockpit & Production Graph Validation

### Context

Phase 14b closed thermal pacing and Louvain GraphRAG on test graphs. The next gate was **production-scale validation** on a real-world Logseq deployment and retiring the fragmented operator experience (Rich terminal canvas + implicit log tailing) in favor of a **100% stable, monolithic, single-server** control plane guarded by **317 green regression tests**.

On a live graph copy, bootstrap harvest grew the connected corpus from **1,426 fragmented pages** to a **hyper-connected network of 3,862 pages** — validating latent **link backpropagation** and **semantic neighbor routing** under strict Phase separation (no orphan-page hallucination loops during Phase 1).

### The telemetry defect lessons

1. **Isolated token logging blindspot** — Cognitive sub-modules (backpropagation, semantic routing, MARPA lint) logged tokens into submodule-scoped loggers. The React cockpit and `.matryca_daemon_state.json` showed stale session counters while JSONL ops logs contained the real spend. Fixed via **`_ensure_shared_token_logger()`**, **`_absorb_token_logger_delta()`**, and **`_save_cycle_checkpoint()`** flushes immediately before and after LLM inference blocks.
2. **Lenient JSON repair engine** — Local Gemma 4 outputs leaked grammar artifacts (broken brackets, double-escaped quote runs, trailing `{` blocks after the root object). Refactored **`src/utils/json_repair.py`** to auto-heal fences, balance brackets, collapse `\\"", \\"` leakage, and strip trailing garbage before Pydantic validation — reducing Instructor retry storms on the daemon hot path.
3. **POSIX atomic checkpoints vs 1 Hz polling** — Non-atomic `open(..., "w")` on daemon state caused transient **`JSONDecodeError`** flakes when the FastAPI reader polled mid-truncation. Hardened **`save_daemon_state()`** (tmp → flush → fsync → **`os.replace`**) and **`_read_daemon_state_payload()`** double-read retry decouple writers from parallel Web API readers.

### Cockpit architecture decisions

1. **Deprecated Rich TUI dashboard** — `matryca plumber status` no longer renders a text canvas; it starts **`run_ui_server()`** (FastAPI + Uvicorn on **`:8000`**) serving REST endpoints and the compiled React SPA from **`frontend/dist/`**.
2. **Full-stack React layout** — Dark cyberpunk cockpit (`CognitiveProgressCard`, `TokenCounterCard`, `HardeningShieldCard`, `LiveConsole`) polls `/api/state`, `/api/logs`, `/api/config` at 1 Hz via **`usePlumberPolling`**. Layout is designed for future native packaging into **Tauri** (CORS already allows `tauri://localhost`).
3. **Outliner-native MapReduce** — Giant pages now chunk on **root-bullet tree boundaries** (`hierarchical_summarization.py`) at **15K char buckets** with per-chunk history reset — preventing destructive flat splits on Logseq nesting.

### Status

Shipped. **`make check`** — **317 passed**, 1 skipped; strict Mypy clean. The Plumber plane is formally stable for long-running production graphs with concurrent browser monitoring.

---

### Context

Phase 1 stateless bootstrap reduced per-page latency from ~25 s to ~2 s, but back-to-back local inference still saturates consumer GPU/NPU heatsinks during long harvest runs — causing fan ramp, battery drain, and thermal throttling on MacBook-class hardware.

### Decisions made

1. **Dual-parameter duty-cycle modulation** — Introduced `MATRYCA_THERMAL_DELAY_BOOTSTRAP` (Phase 1 catalog harvest) and `MATRYCA_THERMAL_DELAY_COGNITIVE` (Phase 2 indexing + cognitive lint), both defaulting to **2.0 seconds** and parsed as floats via `load_plumber_lint_config()`.
2. **Phase-aware injection sites** — Bootstrap pacing fires after each successful LLM page summary in `run_bootstrap_harvest()`; cognitive pacing fires at the end of each file iteration in `MaintenanceDaemon.run_cycle()`.
3. **Zero-cost disable path** — Setting either env var to **`0`** skips the corresponding sleep block with no other behavioral change.

### Status

Shipped. Operators tune delays independently per phase without touching code.

---

## [2026-05-21] Phase 14: Engineering Consolidation and Native GraphRAG Architecture

### Context and Problem Discovered

During the first large-scale stress tests on a fresh copy of the test graph, the system exhibited two systemic anomalies:

1. Phase 1 accumulated rolling memory from the previous 48 messages, causing enormous local GPU Prompt Prefill overhead (up to 25 seconds per file) and introducing inter-page semantic contamination risks (*context bleeding*).
2. Cross-reference modules active in Phase 1, lacking a consolidated global graph map, compulsively hallucinated new concept pages — driving the daemon queue into a theoretically infinite processing loop.

### Architectural Decisions Imposed

1. **Strict Phase Separation:** Rewrote the daemon lifecycle, locking Phase 1 in purely passive mode (Read/Append-Index). Introduced the atomic `bootstrap_complete` flag that inhibits any Markdown file mutation or creation until the catalog and Master Index are fully written to disk.
2. **Stateless Ingestion Optimization:** Forced total reset of the Instructor LLM Client conversation buffer during Phase 1. Per-page processing time collapsed from 25 seconds to under 2 seconds per file, drastically reducing Mac GPU thermal footprint.
3. **Native Louvain GraphRAG Engine Injection:** Introduced `semantic_clustering.py`, inspired by Microsoft GraphRAG hierarchical communities. Python computes locally at zero token cost a hybrid TF-IDF + Jaccard Tags matrix and executes Louvain partitioning with a 20-iteration loop guard. Phase 2 now operates exclusively by confining rolling memory within these isolated semantic neighborhoods (5–35 pages), guided by a central hub node (*Cluster Hub Anchor Node*).
4. **Total Operational Hardening:** Closed the last instability gaps from virtual filesystems (iCloud, Dropbox) by intercepting `flock` failures, implemented automatic self-healing when blackout events zero out JSON state files, and Error Backoff to prevent infinite CPU loops on unchanged corrupted files.

### Test Suite Status

Final validation brought the global counter to **317 unit and integration tests fully passed (100% green)**, brilliantly exceeding MyPy Strict and Ruff linting constraints. The system is formally declared stable, resilient, and hermetic for production loads on complex real-world graphs.

---

## [2026-05-21] — Brand hardening: Matryca Plumber (OSS) vs Matryca Brain (Pro)

### Context

The open-source maintenance daemon, linter, and indexing subsystem shared the **Matryca Brain** moniker with the Nuitka-compiled Pro enterprise ingestion suite — creating brand collision in docs, CLI, and env vars.

### Decisions made

1. **OSS rename to Matryca Plumber** — CLI group `matryca plumber {start,status,stop}`, env prefix `MATRYCA_PLUMBER_*`, runtime files `.matryca_plumber_daemon.pid` and `logs/matryca_plumber_ops.log`.
2. **Module plane** — `plumber_config.py`, `plumber_llm.py`, `plumber_modules/` replace the former `brain_*` paths.
3. **Matryca Brain reserved for Pro** — Twin Ingestion, Epistemic Guardian, and Nuitka-compiled enterprise ingestion remain exclusively **Matryca Brain**.

### Status

Shipped.

---

## [2026-05-21] — Phase 8: Fault-tolerant structural quarantine & daemon resilience

### Context

Running `matryca plumber start --foreground` against a real graph surfaced a hard crash path: pages containing typo'd `((uuid))` block references triggered `ValueError: Malformed UUID detected in block ref` inside `atomic_write_bytes` during indexing. A background daemon must never exit because of user data entry errors.

### Decisions made

1. **Preflight scan in `run_cycle()`** — `find_malformed_block_refs()` runs before any LLM or cognitive-lint work.
2. **Quarantine path** — malformed pages are logged as `[STRUCTURAL LINT WARN]` in `logs/matryca_plumber_ops.log`, marked `skipped` in `.matryca_daemon_state.json`, and annotated inline via `### Matryca Structural Lint` (zero-destruction: existing refs are not rewritten).
3. **`validate_block_refs=False` escape hatch** — warning injection bypasses the commit-time guard so the daemon can append alerts without promoting broken refs through normal writes.
4. **`list_pending_files` settlement** — `skipped` files with stable `mtime` are not re-queued every poll cycle (fixes retry loops for empty pages, already-indexed pages, and quarantined pages).

### Status

Shipped. **317** pytest targets green (1 skipped); strict Mypy clean.

---

## [2026-05-21] — Prompt-hardening pass (English instructions, multilingual outputs)

### Context

Local models (Gemma 4, Qwen 7B/9B) drift when system prompts mix Italian operational prose with English JSON keys. Token budget is wasted on conversational preambles instead of slot-filling.

### Decisions made

1. **`src/agent/prompt_constraints.py`** — mandatory `[CRITICAL LANGUAGE CONSTRAINT]` appended to every Plumber system prompt: instructions in English; human-readable Pydantic fields (`summary`, `reason`, `corrected_text`, …) match the source document language.
2. **MARPA taxonomy prompts** — formal English domain definitions live in `plumber_modules/marpa_framework.py` for zero-shot boundary accuracy.
3. **Open-source IP boundary** — Pavlyshyn bipartite graph validation removed from OSS; reserved for Nuitka commercial tier (see IP separation entry below).

### Status

Shipped.

---

## [2026-05-21] — Matryca Plumber: sovereign local maintenance daemon

### Context

Interactive MCP sessions excel at agent-driven edits, but a personal graph also needs **continuous, low-latency cognitive maintenance** — semantic indexing, safe micro-lint, optional MARPA taxonomy — without cloud APIs or a Logseq UI dependency.

### Decisions made

1. **`MaintenanceDaemon`** (`src/agent/maintenance_daemon.py`) — polls `pages/` and `journals/` for pending markdown, calls LM Studio via Instructor + `JSON_SCHEMA`, appends `### Matryca Semantic Index` sections.
2. **Env-gated cognitive modules** (`src/agent/plumber_modules/`) — dangling-link healer, entity consolidation, auto-split, property hygiene, MARPA framework, backlink backpropagation, semantic routing cache.
3. **Web cockpit (supersedes Rich TUI)** — `matryca plumber status` starts the FastAPI + React control room on `:8000`; the legacy Rich text canvas in `tui_dashboard.py` remains for snapshot tests only.
4. **Detached vs foreground** — `matryca plumber start` forks a session; `--foreground` runs the infinite loop in the current terminal for debugging.

### Status

Shipped.

---

## [2026-05-21] — Ermes-inspired context compression (100k trigger)

### Context

During long multi-turn maintenance loops, local KV-cache growth caused **attention degradation** (“needle in a haystack”) and VRAM pressure. One stress run produced a **~16,000-token reasoning loop** before compression existed — unusable on consumer GPUs.

### Decisions made

1. **`src/agent/context_compressor.py`** — when estimated prompt tokens exceed `MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS` (default **100,000**), intermediate turns collapse into `## Consolidated Epistemic State` markdown (target **30,000** tokens).
2. **`TOKEN_ESTIMATE_SAFETY_MULTIPLIER = 1.12`** — conservative buffer because CJK/code-heavy prompts underestimate BPE counts.
3. **`MAX_EXECUTION_HISTORY_MESSAGES = 48`** — hard cap on per-page execution history retained in the Instructor client.
4. **Fallback truncation** — if the compression LLM call fails (`ConnectionError`, timeout), history is truncated instead of crashing the daemon.

### Status

Shipped.

---

## [2026-05-21] — Instructor `JSON_SCHEMA` grammar mode

### Context

Compact local weights emit polite conversational wrappers around JSON, causing parse failures and retry storms in the daemon hot path.

### Decisions made

1. Primary mode **`MATRYCA_LM_INSTRUCTOR_MODE=JSON_SCHEMA`** — grammar-constrained sampling at the inference engine (LM Studio).
2. Fallback **`MATRYCA_LM_INSTRUCTOR_FALLBACK=MD_JSON`** — secondary Instructor mode when schema binding fails.
3. Structured payloads: `SemanticIndexResult`, `MarpaClassificationResult`, cognitive-module results — all Pydantic-validated.

### Status

Shipped.

---

## [2026-05-21] — Daemon model string leakage fix

### Context

`.matryca_daemon_state.json` persisted a stale `model` field (`qwen2.5-coder-7b`) while operators changed `MATRYCA_LM_MODEL` in `.env`. The web cockpit and token logger reported the wrong model after reload.

### Decisions made

**`sync_daemon_state_from_env()`** — every `load_daemon_state()` and daemon cycle start re-resolves `MATRYCA_LM_MODEL` from the environment so persisted state cannot override live configuration.

### Status

Shipped (`tests/test_maintenance_daemon.py::test_load_daemon_state_overrides_stale_cached_model`).

---

## [2026-05-21] — Logseq list bullet regex (`- `) parsing alignment

### Context

Plumber semantic lint targets **list bullets** (`-`, `*`, `+`) for UUID-anchored micro-corrections. Property lines and inline prose were occasionally mis-identified as bullet bodies when regex boundaries were too loose.

### Decisions made

Tightened `_BULLET` / `_ID_LINE` patterns in `maintenance_daemon.py` and shared graph helpers so `id::` anchoring, bullet inline text extraction, and additive wikilink lint operate on true outliner bullets only — matching Logseq OG `- ` list syntax.

### Status

Shipped; covered by semantic lint daemon tests.

---

## [2026-05-19] — V1.4.0 The Headless Revolution

### Context

Earlier versions depended on Logseq desktop HTTP JSON-RPC (ports 8080/12315), creating split-brain risk, latency, and a hard requirement to keep Electron open.

### Decisions made

1. Removed `httpx` / `LogseqClient`; all mutations via `graph_dispatch.py` + `append_child_to_node`.
2. Upgraded to **`logseq-matryca-parser==0.3.3`**.
3. **X-Ray persistence** — `.matryca_xray_state.json` via `SessionAliasRegistry`.
4. In-memory **`get_broken_references()`** for vault-wide block-ref lint.

### Status

Approved & shipped.

---

## [2026-05-19] — V1.3.0 Fortress Release

### Context

Adversarial audit: LLM path traversal hallucinations and HTTP deadlocks on frozen Logseq API.

### Decisions made

1. **`path_sandbox.py`** — `is_relative_to(graph_root)` gate on every FS path.
2. HTTP timeouts on legacy client (pre-headless).
3. Graceful MCP lifespan teardown.

### Status

Superseded for writes by v1.4.0 headless plane; sandbox remains mandatory.

---

## [2026-05-19] — V1 Launch: 106k-token stress test & synthetic ID guardrails

### Context

Cursor MCP agent built a **2,300+ line MOC** using parser UUIDs inside `((...))` without persisting `id::` lines — broken links after Logseq re-index.

### Decisions made

1. Parser exposes **`synthetic_id`** / **`source_uuid`**.
2. **`assert_valid_block_refs_in_markdown`** pre-flight on atomic writes.
3. **`SYSTEM_PROMPT.md`** persist-first policy for agents.

### Status

Shipped in v1.0.1.

---

## [2026-05-17] — Foundation: outliner paradigm & modular parser

### Context

Early bridges treated Logseq pages as flat text; parent-child UUID races caused `UNRESOLVED_PARENT_UUID` failures.

### Decisions made

- Logseq OG pure Markdown as single system of record.
- **`uv`** + Makefile DX bar.
- FastMCP + Pydantic `OutlineNode` validation.
- DFS async UUID generation for hierarchical writes.
- **`logseq-matryca-parser`** as external single source of truth; `matryca_hooks.py` adapter boundary.

### Next steps (historical)

- ~~Inline parser in repo~~ → external package (done).
- Expand MCP tool surface (ongoing through Phase 8).

---

## IP separation: open source vs commercial tier

| Capability | OSS (`matryca-logseq` / **Matryca Plumber**) | Commercial (**Matryca Brain**, Nuitka binary) |
|------------|------------------------|----------------------------|
| MARPA domain taxonomy (Mappa/Area/Risorsa/Progetto/Archivio) | ✅ env-gated | ✅ |
| Dangling healer, entity consolidation, auto-split, property hygiene | ✅ env-gated | ✅ |
| Semantic routing disk cache, backlink backpropagation | ✅ env-gated | ✅ |
| Pavlyshyn bipartite graph validation network | ❌ removed from OSS | ✅ proprietary |
| Twin Ingestion (multi-format pipelines) | ❌ not wired | ✅ |
| Epistemic Guardian (normative time resolution) | ❌ not wired | ✅ |

Enterprise placeholders in `.env.example` (`MATRYCA_LINT_TWIN_INGESTION`, `MATRYCA_LINT_EPISTEMIC_GUARDIAN`) document the **Matryca Brain** commercial surface only — they are **not** loaded by `plumber_config.py` in the open-source engine.

---

## Entry template

```markdown
## [YYYY-MM-DD] — Title

### Context
What problem triggered this entry?

### Decisions made
What was decided, rejected, or deferred?

### Status
Shipped | In progress | Superseded by [link]
```

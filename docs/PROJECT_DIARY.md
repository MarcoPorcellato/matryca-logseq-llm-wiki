# Project diary — technical lifecycle log

This document records **architecture decisions**, **phase milestones**, and **real-world bugs crushed** during the evolution of **matryca-logseq-llm-wiki** from a baseline MCP bridge to a production-grade **Ironclad Autonomous Linter OS** (Phase 8). For the engineering contract (modules, data planes, diagrams), see [`ARCHITECTURE.md`](ARCHITECTURE.md). For operator setup, see [`../README.md`](../README.md).

Entries are chronological (newest first within each phase summary). When a decision is superseded, add a new entry rather than rewriting history.

---

## Phase map (Phases 1 → 8)

| Phase | Name | What shipped |
|:-----:|------|--------------|
| **1** | Baseline headless plane | FastMCP stdio, `OutlineNode`, DFS `write_logseq_outline`, parser-backed `read_logseq_page`, block-ref lint |
| **2** | L1 / L2 routing | Capped `read_l1_memory`, `routing_hint` traceability on tool payloads |
| **3** | PKM refinements | BM25 local query, structural hops, property-line surgery, templates, wiki lint, git snapshots |
| **4** | Logseq superpowers | Advanced Query injection, journal mining, entity resolution, alias append |
| **5** | Graph gardener | Flashcards, tag unify, same-page reparent |
| **6** | Synthesis engine | Unlinked mentions, MOC generation, large-block split |
| **7** | Mldoc compliance | `mldoc_properties` + `mldoc_guards` integrated into mutators |
| **8** | Ironclad Autonomous Linter OS | Global fence scanner, atomic writes, generational cache, **Matryca Plumber** daemon, Ermes context compression, structural quarantine, GraphRAG Louvain clustering, 262-test CI bar |

Phases **9–13** (Trust plane, delivery, Fortress, Headless Revolution, operational hardening) are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md) § Complete phase evolution history.

---

## [2026-05-21] Phase 14: Consolidamento Ingegneristico e Architettura GraphRAG Locale

### Contesto e Problema Rilevato

Durante i primi stress test su larga scala con una copia fresca del grafo di test, il sistema ha sperimentato due anomalie sistemiche:

1. La Fase 1 accumulava la memoria rolling dei 48 messaggi precedenti, causando un enorme overhead di Prompt Prefill sulla GPU locale (fino a 25 secondi per file) e introducendo rischi di contaminazione semantica inter-pagina (*context bleeding*).
2. I moduli di cross-reference attivi in Fase 1, non avendo ancora una mappa globale consolidata del grafo, allucinavano la creazione compulsiva di nuove pagine di concetti, portando la coda del demone in un loop di elaborazione teoricamente infinito.

### Decisioni Architetturali Imposte

1. **Separazione Rigorosa delle Fasi (Strict Phase Separation):** Riscritto il ciclo vitale del demone blindando la Phase 1 in modalità puramente passiva (Read/Append-Index). Introdotta la flag atomica `bootstrap_complete` che inibisce qualsiasi mutazione o creazione di file markdown fino alla completa stesura del catalogo e del Master Index.
2. **Ottimizzazione Stateless di Ingestione:** Forzato l'azzeramento totale del buffer di conversazione dell'Instructor LLM Client durante la Phase 1. Il tempo di elaborazione per singola pagina è crollato verticalmente da 25 secondi a meno di 2 secondi per file, riducendo drasticamente l'impronta termica della GPU Mac.
3. **Iniezione del Motore GraphRAG Louvain-Nativo:** Introdotto il modulo `semantic_clustering.py` ispirato alle comunità gerarchiche di Microsoft GraphRAG. Python calcola localmente a costo token zero una matrice ibrida TF-IDF + Jaccard Tags ed esegue il partizionamento di Louvain con un loop guard di 20 iterazioni massime. La Phase 2 ora opera esclusivamente confinando la memoria rolling all'interno di questi isolati "quartieri semantici" (5-35 pagine), guidata da un nodo hub centrale (*Cluster Hub Anchor Node*).
4. **Hardening Totale Operativo:** Chiusi gli ultimi varchi di instabilità legati ai file system virtuali (iCloud, Dropbox) intercettando i fallimenti di `flock`, implementato il self-healing automatico in caso di file JSON di stato azzerati da blackout, ed ottimizzata la TUI tramite un lettore di log streaming inverso a blocchi costanti da 8KB.

### Stato della Suite di Test

La validazione finale ha portato il contatore globale a **262 test unitari e di integrazione completamente superati (100% verdi)**, superando brillantemente i vincoli di MyPy Strict e Ruff linting. Il sistema è formalmente dichiarato stabile, resiliente ed ermetico per carichi di produzione su grafi reali complessi.

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

Shipped. **262** pytest targets green; strict Mypy clean.

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
3. **Rich TUI** — `matryca plumber status` renders scan progress, token session totals, and ops-log tail summaries.
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

`.matryca_daemon_state.json` persisted a stale `model` field (`qwen2.5-coder-7b`) while operators changed `MATRYCA_LM_MODEL` in `.env`. The TUI and token logger reported the wrong model after reload.

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

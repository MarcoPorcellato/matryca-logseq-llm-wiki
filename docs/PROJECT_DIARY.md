# Project diary — technical lifecycle log

This document records **architecture decisions**, **phase milestones**, and **real-world defects crushed** during the evolution of **Matryca Plumber** (`matryca-plumber` on PyPI; current line **v1.8.0**).

The project began as an MCP-first bridge so external LLM hosts could mutate Logseq Markdown safely. Phases **12–16** completed the pivot to a **fully autonomous background agent** — `MaintenanceDaemon`, Sovereign UI, native AST I/O, OCC, and Zero-Trust cockpit APIs — where **FastMCP is an optional auxiliary surface**, not the product’s center of gravity.

For the engineering contract (modules, diagrams, concurrency), see [`ARCHITECTURE.md`](ARCHITECTURE.md). For operator setup, see [`../README.md`](../README.md).

Entries are chronological (**newest first** within each major release block). When a decision is superseded, add a new entry rather than rewriting history.

---

## [2026-05-29] v1.8 pre-release — Round 4 read-only audit (operational hardening)

### Context

After TRIZ rounds 1–3 on local JSON degeneration (Gemma tail of death, compression walls, poisoned semantic cache), a final **read-only** audit flagged five items that could still harm vault integrity or host responsiveness on real graphs. These are **not** new cognitive features — they tighten contracts already documented under OCC and LLM resilience.

### Milestones shipped

1. **Stateless graph insights** — `generate_graph_insights` uses `stateless=True` so panoramic ontology reports do not append to Ermes execution history.

2. **Compression persist hygiene** — `_compress_history_via_llm` and `condense_messages` run `sanitize_prose_llm_completion()` on summaries **before** they are injected into consolidated history.

3. **Semantic index block catalog cap** — `_enumerate_blocks_for_prompt` stops at **8000 characters** (same order of magnitude as the indexed page body cap) with an explicit truncation note so the model does not target uncatalogued UUIDs.

4. **Phase 2 lock scope** — `_process_llm_cycle_file` no longer wraps cognitive lint + `index_page` in `page_rmw_lock`; the lock is acquired only in `apply_semantic_page_result` for the atomic write. Multi-minute LLM work must not block Logseq saves.

5. **`id::` identity protection** — `parse_logseq_property_line` excludes normalized key `id` so property hygiene and MCP property-line tools never treat UUID lines as editable metadata.

### Architectural outcome

OCC documentation in [`ARCHITECTURE.md`](ARCHITECTURE.md) now matches runtime: snapshot → read → infer **without** page lock → verify → lock → commit. Resilience and mldoc docs cross-link the new guards. See [`CHANGELOG.md`](../CHANGELOG.md) `[Unreleased]` and [`resilience-llm-json-triz.md`](resilience-llm-json-triz.md) §5.

### Status

**Shipped in `[Unreleased]`** — pending semver tag with the rest of v1.8.x.

---

## [2026-05-27] v1.8 — Edge computing & performance (16 GB / 10k pages)

### Context

Matryca Plumber’s stated operator profile is a **CPU-only 16 GB laptop** with a **local** LLM (LM Studio, Ollama, llama.cpp) and vaults approaching **10,000** Markdown pages. Phase 14d fixed megabyte-page **token** cost via summaries and introduced content-before-task ordering for Phase 2 indexing — but production traces still showed:

1. **Bootstrap harvest** sending `Task` before `Content`, destroying KV-cache reuse on every MapReduce chunk and reduce pass.
2. **Per-page alias maps in the system prompt**, preventing any cross-page system-string reuse on local servers.
3. **RAM creep** — full BM25 token bags, unbounded semantic-cache `_memory`, and a full-graph backlink rebuild at every Phase 1 start.
4. **Host freezes** — synchronous 10k-file loops, daemon checkpoints every five cataloged pages, and `purge_expired_semantic_cache` accidentally deleting `master_catalog.json` alongside inference cache files.

v1.8 is explicitly **performance-only**: zero new semantic or graph-manipulation features.

### Milestones shipped

1. **`PagePromptSession`** (`page_prompt_session.py`) — One stable page block per file per cycle; cognitive modules and semantic index share it; AliasIndex moves to a **capped user footer** (`MATRYCA_ALIAS_PROMPT_MAX_CHARS`).

2. **`build_semantic_lint_system_prompt()`** (`semantic_lint_prompts.py`) — Stable system instructions extracted from `maintenance_daemon.py` to break circular imports and keep the system string constant.

3. **Cache-aligned bootstrap** — `harvest_page_summary` and MapReduce reduce use `build_cache_aligned_prompt`; `stateless=True` on per-page inference paths.

4. **Memory plane** — BM25 postings-lite (`doc_term_freqs`), `release_phase1_memory()` after Phase 1, semantic cache LRU, `unload_master_catalog()`, reserved JSON files during TTL purge.

5. **I/O plane** — `cooperative_yield.yield_host()`, persisted `backlink_counts.json`, `MATRYCA_BOOTSTRAP_CHECKPOINT_EVERY`, `apply_plumber_priority()` (`nice` + optional psutil ionice). Micro-yields (2 ms batch pauses) are distinct from **thermal** sleeps (≥ 1 s, post-LLM only); thermal tests filter `time.sleep` with `s >= 1.0`.

6. **Docs & tests** — [`v1.8-OPTIMIZATION-PLAN.md`](v1.8-OPTIMIZATION-PLAN.md), [`openspec/llm-performance.md`](openspec/llm-performance.md), `scripts/gen_synthetic_graph.py`, `make perf` / `@pytest.mark.slow`.

7. **Software edge** — [`llm_client.py`](../src/agent/llm_client.py) probe-driven Path A/B structured output; `FrozenPromptPrefix` + `kv_prefix_hash`; [`markdown_io.py`](../src/graph/markdown_io.py) mmap Phase 1 reads; [`process_priority.py`](../src/agent/process_priority.py) CPU sandbox (`MATRYCA_CPU_SANDBOX`, `[edge]` extra). Spec: [`v1.8-SOFTWARE-EDGE-PLAN.md`](v1.8-SOFTWARE-EDGE-PLAN.md).

### Architectural outcome

The product can be described as **edge-ready**: the daemon is designed to run **silently for days** on laptop-class hardware when operators enable the v1.8 `.env` profile. The Context Acceleration Shield (Phase 14d) and the v1.8 Zero-Prefill stack are complementary — summaries shrink tokens; stable prefixes maximize **reuse** of whatever tokens remain.

### Status

**Shipped in 1.8.0** — see [`CHANGELOG.md`](../CHANGELOG.md) `[1.8.0]`.

---

## [2026-05-25] Security depth pass (post-audit hardening)

### Context

A full-repository security review identified gaps where **SSRF policy applied only to the Sovereign UI** (not the maintenance daemon’s `InstructorLLMClient`), where **`LOGSEQ_GRAPH_PATH` could be repointed** to any directory with a `pages/` folder via authenticated UI saves, and where **MCP stdio started unconditionally** for any host spawning bare `matryca-plumber`.

### Milestones shipped

1. **`src/utils/llm_url_policy.py`** — Single `validate_llm_proxy_url()` used by `ui_server.assert_safe_lm_proxy_url`, `plumber_config` env load, and `resolve_validated_llm_base_url()` in the daemon LLM client.

2. **Graph config allowlist** — `validate_logseq_graph_path_for_config()` + `graph_config_allowed_roots()` (home, repo, temp, current graph, `MATRYCA_ALLOWED_GRAPH_ROOTS`).

3. **UI rate-limit tiers** — Authenticated vs unauthenticated per-IP budgets; `/api/health` and loopback `/api/auth/session` exempt.

4. **`MATRYCA_MCP_ENABLED`** — Default **off**; `plumber_entry` refuses MCP stdio until explicitly enabled (documented in `.env.example`, `CONTRIBUTING.md`, `SECURITY.md`).

5. **MCP tool error sanitization** — Filesystem paths and raw runtime text hidden from MCP clients unless `MATRYCA_DEBUG=true`.

6. **Test bar** — **453** pytest targets; new coverage in `tests/test_llm_url_policy.py` and security remediation cases.

### Status

**Shipped in 1.7.5**. Operators enabling Claude Desktop should set `MATRYCA_MCP_ENABLED=true` in `.env`. See [`CHANGELOG.md`](../CHANGELOG.md) `[1.7.5]`.

---

## [2026-05-24] v1.5.15 — The Ironclad consolidation sprint (1.5.x era)

### Context

By mid-1.5.x, Matryca Plumber had already shipped Logseq-native parity (Phase 15), enterprise UI security (Phase 16), and the Context Acceleration Shield. Production use on real graphs exposed a **second wave of integration defects**: flaky async tests around Loguru’s queued sink, false-negative daemon launch detection in the Sovereign UI, OCC ordering gaps when cognitive lint performed self-writes mid-request, and stdout pollution when MCP hosts spawned the same wheel as the CLI.

This sprint hardens the **operational glue** between the three runtime surfaces (daemon, UI, MCP) so the system behaves as a single product at **`uvx`** install time — not only when run from a git checkout.

### Milestones shipped

1. **Product identity: Matryca Plumber** — The open-source maintenance daemon, linter, and indexing engine is **Matryca Plumber**. **Matryca Brain** remains reserved for the Nuitka-compiled Pro enterprise tier (Twin Ingestion, Epistemic Guardian). CLI: `matryca plumber {start,status,stop,ui}`; env prefix `MATRYCA_PLUMBER_*`; ops log `logs/matryca_plumber_ops.log`.

2. **`uvx` zero-install workflow** — Operators run `uvx --from matryca-plumber matryca-plumber status` without polluting global site-packages. Documented in README; service installers must still target a **stable** `uv tool install` binary (not ephemeral `uvx` cache paths).

3. **`plumber_entry.py` — CLI vs MCP stdio routing** — The `matryca-plumber` console script normalizes shorthand (`start`, `status`, …) to `matryca plumber` and **lazy-imports** `main.main()` only when argv is not CLI-shaped. FastMCP no longer loads at import time for operator commands — eliminating stdio/stream corruption when the same entrypoint serves both roles.

4. **pytest-asyncio + Loguru MCP telemetry** — Replaced flaky `asyncio.sleep` polling in `tests/test_mcp_telemetry.py` with **`await logger.complete()`** so enqueued sink records drain deterministically. The bridge stores **`id(ctx)`** in `record["extra"]` and maps live `Context` handles in `_mcp_sessions` — fixing **multiprocessing pickling errors** when Loguru’s worker thread forwarded records containing non-picklable MCP objects.

5. **Sovereign UI daemon launch false negative** — `_verify_daemon_launch()` in `ui_server.py` previously treated **exit code 0** from the detached launcher as failure. The worker intentionally exits after spawning the foreground daemon; success is confirmed by a **live PID** in `.matryca_plumber_daemon.pid` (`is_plumber_process`), not by the launcher process staying alive.

6. **OCC ordering refinement** — `occ_snapshot()` is captured **before** page reads and LLM work; `occ_verify_before_write()` runs **before** `page_rmw_lock`; mtime is re-checked **inside** the lock; `atomic_write_bytes_if_unchanged()` guards the final commit. Cognitive lint paths re-baseline after Plumber’s own intermediate writes to avoid false conflicts on multi-step applies.

7. **Atomic `.env` persistence** — Settings drawer saves use `_atomic_write_text` (temp + `fsync` + `os.replace`) so partial writes cannot tear Plumber configuration during 1 Hz operator edits.

8. **Page-lock registry LRU** — At 4096 entries, evict **unlocked** locks LRU-style instead of clearing the entire registry — stable hot-path locking on large vaults.

9. **Codebase hardening bar** — **453** pytest targets passing (was 437 at v1.5.15); **Mypy strict** clean on `src` and `tests`; Ruff lint/format via `make check`.

### Architectural outcome

Matryca Plumber is fairly described as an **enterprise-grade, local-first background daemon** with a Sovereign UI and optional MCP sidecar — not “a Claude Desktop plugin.” The 1.5.x **Ironclad** era closes the gap between architectural intent (documented in Phase 14–16) and day-to-day reliability when installed via **`uvx`**, monitored from the browser cockpit, and stressed under concurrent human editing.

### Status

**Shipped** — v1.5.15. See also [`CHANGELOG.md`](../CHANGELOG.md) `[Unreleased]` for the same defect class.

---

## [2026-05-23] Phase 16: Enterprise security and concurrency (Ironclad)

### Context

Phase 15 delivered Logseq filesystem parity. Phase 16 layered **Zero-Trust** authentication on the Sovereign UI (`X-Matryca-Token`), cross-platform **`subprocess.Popen`** daemon launch (replacing UNIX-only `fork()`), exclusive **`.matryca_plumber_daemon.lock`**, SSRF-hardened LM discovery, paranoia-level ledger commits, and **`MATRYCA_ALLOW_FLOCK_DEGRADATION`** for cloud-sync vaults.

### Outcome

The autonomous daemon graduated from power-user script to **production operator surface** — secure loopback cockpit, Windows-first background ops, ledger survival on power loss.

### Status

Shipped. Superseded for operational polish by **v1.5.15** entries above.

---

## [2026-05-23] Phase 15: Logseq-native parity shield and Windows I/O

### Context

Real graphs exposed bugs generic Markdown tools never see: ghost duplicate pages from namespace drift, page properties prefixed with `- ` falling out of the indexer, orphaned `id::` lines breaking `((uuid))` integrity, `cp1252` corruption on Windows, and **silent overwrites** when users typed during slow local inference.

### Victories shipped

- **OCC** — `occ_snapshot` → inference → `atomic_write_bytes_if_unchanged` with `file_mtime_drifted` aborts.
- **`page_path.py`** — `/` → `___` + percent-encode reserved characters.
- **`page_properties.py`** — true line-0 frontmatter vs +2-indent block properties.
- **Alias index** — case-insensitive resolution; exclude `logseq/bak/`, `.recycle/`, `.git`.
- **Trust & Safety drawer** — Safe / Augmented / Surgeon tiers in `SettingsDrawer.tsx`.
- **UTF-8 / CRLF** — explicit encoding and line-ending normalization on all graph I/O.

### Status

Shipped. Test bar at phase close: **349+** passing (later superseded by 437).

---

## Phase map (condensed)

| Phase | Name | What shipped |
|:-----:|------|--------------|
| **1** | Baseline headless plane | `graph_dispatch`, optional FastMCP, `OutlineNode`, DFS writes |
| **2** | L1 / L2 routing | `read_l1_memory`, `routing_hint` |
| **3** | PKM refinements | BM25 query, property surgery, git snapshots |
| **4–6** | Logseq superpowers + gardener | Queries, journals, flashcards, MOC, split blocks |
| **7–8** | Mldoc + Ironclad Shield | Fence scanner, atomic writes, generational cache |
| **9** | Trust plane | `quality_gate`, synthetic `id::` policy |
| **10** | Delivery | GitHub Actions CI, Dependabot, release workflow |
| **11** | Fortress (v1.3) | `path_sandbox`, `mcp_tool_guard` |
| **12** | Headless Revolution (v1.4) | Removed HTTP client; X-Ray state file |
| **13** | Operational hardening (v1.4.1) | `chdir` sandbox root, MCP telemetry sanitizer, `service install` |
| **14** | Plumber OS | `MaintenanceDaemon`, Louvain GraphRAG, FastAPI + React cockpit |
| **14d** | Context Acceleration | TRIZ payload + prompt prefix alignment + `reload_plumber_dotenv` |
| **1.8** | Edge computing & performance | PagePromptSession, backlink index, BM25 slimming, cooperative harvest, memory teardown |
| **15** | Logseq-native parity | OCC, namespaces, frontmatter, Trust UI |
| **16** | Enterprise Ironclad | Zero-Trust UI, subprocess daemon, SSRF, cross-platform lock |
| **1.5.15** | Ironclad consolidation | `plumber_entry`, MCP log bridge, UI launch fix, OCC ordering, atomic `.env`, LRU locks |
| **1.5.17** | Security depth pass | `llm_url_policy`, graph path allowlist, `MATRYCA_MCP_ENABLED`, UI rate tiers, **453** tests |

Phases **9–14** narrative detail remains in [`ARCHITECTURE.md`](ARCHITECTURE.md) § Phase evolution.

---

## [2026-05-22] Phase 14d: Context Acceleration and TRIZ hardening

### Context

A **5,260-block** production page turned Phase 2 into a sequential GPU prefill bottleneck. Dynamic task instructions preceded page content, destroying llama.cpp KV-cache reuse. Detached daemon children missed repo `.env` when `cwd` was arbitrary.

### Solution

- `llm_context_payload.py` — Phase 1 summary substitution + semantic skeleton fallback.
- `prompt_layout.py` — `[SYSTEM] + [STABLE_PAGE] + [DYNAMIC_TASK]` ordering.
- `reload_plumber_dotenv()` — anchor `.env` to package repo root, re-read each sync cycle.

### Status

Shipped. **`tests/test_llm_context_payload.py`** added; suite grew to **317+** green at the time.

**Evolution (v1.8):** Phase 14d fixed Phase 2 indexing and token volume; v1.8 extends the same `[STABLE_PAGE] + [DYNAMIC_TASK]` contract to **bootstrap harvest**, **MapReduce**, and **multi-module cognitive pipelines** via `PagePromptSession` — see the [2026-05-27 v1.8 entry](#2026-05-27-v18--edge-computing--performance-16-gb--10k-pages) above.

---

## [2026-05-21] Phase 14c: Monolithic Sovereign UI

### Context

Retire fragmented Rich TUI; validate on a graph growing **1,426 → 3,862** connected pages under Phase 1 bootstrap.

### Defects crushed

- **Isolated token logging** — submodule loggers vs shared `TokenLogger` / `_save_cycle_checkpoint`.
- **JSON repair** — `json_repair.py` for local model grammar leakage.
- **Resilient structured output (TRIZ)** — [`resilience-llm-json-triz.md`](resilience-llm-json-triz.md): Gemma “tail of death” (`\n` loops), `MATRYCA_LLM_MAX_COMPLETION_TOKENS`, balanced-brace extraction, sanitization pipeline integrated with Path A/B in `llm_client.py`.
- **Non-atomic daemon state** — `save_daemon_state` tmp + `fsync` + `os.replace`; double-read on load.

### Status

Shipped. `matryca plumber status` → Uvicorn `:8500` + `frontend/dist/`.

---

## [2026-05-21] Phase 14: Engineering consolidation and native GraphRAG

### Context

Phase 1 rolling memory caused **~25 s** prefill per page and orphan-page hallucination loops when generative modules ran before catalog completion.

### Decisions

- **Strict phase separation** — `bootstrap_complete` wall between census and cognitive mutation.
- **Stateless Phase 1** — reset Instructor history per page (~2 s per file).
- **`semantic_clustering.py`** — Louvain communities (5–35 pages) with TF-IDF + Jaccard tags hybrid.

### Status

Shipped. Suite at **317** green (historical).

---

## [2026-05-21] Brand hardening: Matryca Plumber vs Matryca Brain

OSS rename to **Matryca Plumber**; `plumber_config.py`, `plumber_modules/` module plane; Brain reserved for Pro Nuitka binary.

**Status:** Shipped.

---

## [2026-05-21] — Phase 8: Structural quarantine

Malformed `((uuid))` on user pages crashed the daemon. Preflight `find_malformed_block_refs`, quarantine with `### Matryca Structural Lint`, `validate_block_refs=False` for warning-only appends.

**Status:** Shipped.

---

## [2026-05-21] — Matryca Plumber sovereign maintenance daemon

`MaintenanceDaemon`, env-gated `plumber_modules/`, Instructor `JSON_SCHEMA`, Ermes compression at 100k tokens.

**Status:** Shipped.

---

## [2026-05-19] — V1.4.0 Headless Revolution

Removed `LogseqClient` / `httpx`; all writes via `graph_dispatch` + `append_child_to_node`; `.matryca_xray_state.json`.

**Status:** Shipped.

---

## [2026-05-19] — V1.3.0 Fortress

`path_sandbox.py`, MCP lifespan teardown. Sandbox remains mandatory post-headless.

**Status:** Shipped (HTTP client superseded).

---

## [2026-05-19] — V1.0.1: 106k-token MCP stress test

MOC built with **synthetic parser UUIDs** in `((refs))` without persisted `id::`. Led to `synthetic_id` exposure, `assert_valid_block_refs_in_markdown`, and persist-first policy in `SYSTEM_PROMPT.md`.

**Status:** Shipped.

---

## [2026-05-17] — Foundation

Logseq OG as single system of record; FastMCP + Pydantic `OutlineNode`; external `logseq-matryca-parser`; `uv` + Makefile DX.

**Status:** Shipped.

---

## IP separation: open source vs commercial

| Capability | OSS (**Matryca Plumber**) | Commercial (**Matryca Brain**) |
|------------|---------------------------|--------------------------------|
| MARPA taxonomy, dangling healer, property hygiene, semantic routing | ✅ env-gated | ✅ |
| Twin Ingestion, Epistemic Guardian | ❌ | ✅ proprietary |

Enterprise placeholders in `.env.example` for Brain-only features are **not** loaded by `plumber_config.py` in OSS.

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

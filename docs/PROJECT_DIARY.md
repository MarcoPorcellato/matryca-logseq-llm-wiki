# Project diary — technical lifecycle log

This document records **architecture decisions**, **phase milestones**, and **real-world defects crushed** during the evolution of **Matryca Plumber** (`matryca-plumber` on PyPI, **v1.5.15**).

The project began as an MCP-first bridge so external LLM hosts could mutate Logseq Markdown safely. Phases **12–16** completed the pivot to a **fully autonomous background agent** — `MaintenanceDaemon`, Sovereign UI, native AST I/O, OCC, and Zero-Trust cockpit APIs — where **FastMCP is an optional auxiliary surface**, not the product’s center of gravity.

For the engineering contract (modules, diagrams, concurrency), see [`ARCHITECTURE.md`](ARCHITECTURE.md). For operator setup, see [`../README.md`](../README.md).

Entries are chronological (**newest first** within each major release block). When a decision is superseded, add a new entry rather than rewriting history.

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

9. **Codebase hardening bar** — **437** pytest targets passing; **Mypy strict** clean on `src` and `tests`; Ruff lint/format via `make check`. CI badge and README aligned to this count.

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
| **15** | Logseq-native parity | OCC, namespaces, frontmatter, Trust UI |
| **16** | Enterprise Ironclad | Zero-Trust UI, subprocess daemon, SSRF, cross-platform lock |
| **1.5.15** | Ironclad consolidation | `plumber_entry`, MCP log bridge, UI launch fix, OCC ordering, atomic `.env`, LRU locks, **437** tests |

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

---

## [2026-05-21] Phase 14c: Monolithic Sovereign UI

### Context

Retire fragmented Rich TUI; validate on a graph growing **1,426 → 3,862** connected pages under Phase 1 bootstrap.

### Defects crushed

- **Isolated token logging** — submodule loggers vs shared `TokenLogger` / `_save_cycle_checkpoint`.
- **JSON repair** — `json_repair.py` for local model grammar leakage.
- **Non-atomic daemon state** — `save_daemon_state` tmp + `fsync` + `os.replace`; double-read on load.

### Status

Shipped. `matryca plumber status` → Uvicorn `:8000` + `frontend/dist/`.

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

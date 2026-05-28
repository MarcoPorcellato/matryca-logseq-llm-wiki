# LLM performance & edge computing (v1.8)

**Roadmap:** [`../v1.8-OPTIMIZATION-PLAN.md`](../v1.8-OPTIMIZATION-PLAN.md)  
**Related:** Phase 14d Context Acceleration in [`../PROJECT_DIARY.md`](../PROJECT_DIARY.md)

Matryca Plumber targets **CPU-only laptops with 16 GB RAM** running a local OpenAI-compatible server (LM Studio, Ollama, llama.cpp). The vault may contain **up to ~10,000** Markdown pages. v1.8 adds **no new semantic features** — only memory governance, KV-cache-friendly prompts, and cooperative I/O so the host OS stays responsive during long harvests.

---

## Design goals

| Goal | Mechanism |
|------|-----------|
| **Zero-Prefill** on repeated tasks per page | Stable user prefix (`Page content:` first, task last); constant system prompt |
| **Bounded RAM** across multi-day daemon uptime | Postings-lite BM25, semantic cache LRU, Phase 1 teardown, optional catalog unload |
| **Host survival** during bootstrap | `yield_host()`, persisted backlink index, thermal pauses, `os.nice` / optional ionice |

---

## Prompt layout contract

Every local LLM call that reads page text should follow:

```text
[System — stable compiler rules, no per-page alias map]
[User — Page content: <stable payload> + optional AliasIndex footer + Task: <dynamic>]
```

### Modules

| Module | Role |
|--------|------|
| [`prompt_layout.py`](../../src/agent/prompt_layout.py) | `build_cache_aligned_prompt(content, task_instruction)` |
| [`semantic_lint_prompts.py`](../../src/agent/semantic_lint_prompts.py) | `build_semantic_lint_system_prompt()` — shared across index + cognitive pipeline |
| [`page_prompt_session.py`](../../src/agent/page_prompt_session.py) | One `PagePromptSession` per file per daemon cycle |
| [`llm_context_payload.py`](../../src/agent/llm_context_payload.py) | Shrinks giant pages to Phase 1 summary / skeleton before they enter the stable block |

### Per-page session lifecycle

1. `prepare_llm_context_payload()` produces the reduced body (raw, summary, skeleton, or truncated).
2. Optional **AliasIndex** footer is appended to the stable block (capped by `MATRYCA_ALIAS_PROMPT_MAX_CHARS`, default 2048) — **not** injected into the system prompt (v1.7 and earlier hurt KV reuse).
3. Cognitive modules (MARPA, dangling healer, entity consolidation, property hygiene) reuse the **same** `stable_page_block`.
4. `semantic_index_page` uses `stateless=True` so Ermes rolling history does not grow the prefix between unrelated operations.

### Bootstrap & MapReduce

- **`harvest_page_summary`** uses `build_cache_aligned_prompt` (fixes the pre-v1.8 anti-pattern that placed `Content:` after the task).
- **MapReduce** (`hierarchical_summarization.py`): chunk passes send page trees; the reduce pass sends **section summaries** as stable content and the consolidation instruction as the task tail.

### Cluster mode

When semantic clustering is active, `inject_cluster_focus_context()` seeds neighborhood summaries once per cluster. Set `MATRYCA_LLM_CLUSTER_HISTORY=false` (default) for a minimal assistant ack — better prefix stability than a long synthetic JSON turn in history.

---

## Memory plane

| Component | v1.8 behavior |
|-----------|----------------|
| **BM25** (`generational_cache.py`) | `doc_term_freqs` per page (postings-lite); `MATRYCA_BM25_MODE=resident\|ondemand`; `release_bm25_corpus()` on Phase 1 teardown |
| **Semantic cache** (`semantic_cache_router.py`) | Disk TTL + in-process LRU (`MATRYCA_SEMANTIC_CACHE_MEMORY_ENTRIES`); purge skips reserved JSON (`master_catalog.json`, `backlink_counts.json`, `semantic_clusters.json`) |
| **Master catalog** (`master_catalog.py`) | `unload_master_catalog()` after bootstrap teardown |
| **Memory budget** (`memory_budget.py`) | `release_phase1_memory()`, `snapshot()`, `maybe_release_after_cycle()` |

After successful Phase 1 bootstrap, the daemon calls `release_phase1_memory()` (clear generational caches, release BM25, trim semantic RAM, unload catalog, `gc.collect()`), precomputes semantic clusters, then enters Phase 2 polling.

---

## I/O & CPU plane

| Component | Role |
|-----------|------|
| [`cooperative_yield.py`](../../src/agent/cooperative_yield.py) | `yield_host()` every `MATRYCA_BOOTSTRAP_YIELD_EVERY` files (default 25) |
| [`backlink_index.py`](../../src/graph/backlink_index.py) | Persists `.matryca_semantic_cache/backlink_counts.json`; invalidates on `patch_generational_caches_for_paths` |
| [`process_priority.py`](../../src/agent/process_priority.py) | `apply_cpu_sandbox()` — CPU affinity + `nice(19)` + ionice when `MATRYCA_CPU_SANDBOX=true` (`pip install matryca-plumber[edge]` for `psutil`) |
| [`markdown_io.py`](../../src/graph/markdown_io.py) | `mmap_graph_page()` / `read_graph_page_text()` when `MATRYCA_GRAPH_READ_MMAP=true` |
| **Thermal pauses** (`plumber_config.py`) | `MATRYCA_THERMAL_DELAY_BOOTSTRAP` / `MATRYCA_THERMAL_DELAY_COGNITIVE` after each LLM turn |

---

## Adaptive structured output

| Path | When | Behavior |
|------|------|----------|
| **A** | Probe finds `json_schema` strict support | Single completion via OpenAI `response_format`; no Python JSON repair |
| **B** | Legacy / probe miss | Up to 3 self-correction turns with `ValidationError` feedback; `parse_llm_json` once per attempt |
| **Exhausted** | Path B fails | `StructuredOutputExhaustedError` → page `status=error` (skip until mtime change or restart) |

Module: [`llm_client.py`](../../src/agent/llm_client.py). Foreground daemon calls `probe_backend()` once at start.

---

## Frozen KV prefix

- `FrozenPromptPrefix` in [`page_prompt_session.py`](../../src/agent/page_prompt_session.py): SHA-256 over canonical system + stable user bytes.
- `verify_unchanged()` before each LLM call; ops log field `kv_prefix_hash`.
- Task tail uses fixed delimiter `CANONICAL_TASK_HEADER` in [`prompt_layout.py`](../../src/agent/prompt_layout.py) (no `.strip()` on stable body).

Non-LLM bootstrap paths (regex catalog hits) may sleep `MATRYCA_BOOTSTRAP_IO_BATCH_PAUSE_MS` (default 2 ms) between files to spare slow disks.

Daemon checkpoint frequency during Phase 1 is controlled by `MATRYCA_BOOTSTRAP_CHECKPOINT_EVERY` (default 50), not a hard-coded interval.

---

## Operator checklist (large vaults)

1. Copy the **v1.8 Edge computing & performance** block from `.env.example`.
2. Keep `MATRYCA_PLUMBER_CONTEXT_COMPRESSION=false` unless you accept extra LLM calls that rewrite history.
3. On HDD or busy laptops: set `MATRYCA_YIELD_SLEEP_MS=1`–`5`, increase thermal delays modestly.
4. If RAM is tight: `MATRYCA_BM25_MODE=ondemand`, lower `MATRYCA_SEMANTIC_CACHE_MEMORY_ENTRIES`, set `MATRYCA_RAM_BUDGET_MB` and watch `logs/matryca_plumber_ops.log` for budget warnings.
5. Load-test locally: `uv run python scripts/gen_synthetic_graph.py /path/to/graph --count 1000` then run bootstrap; soak: `make perf`.

---

## Verification

| Test area | Location |
|-----------|----------|
| Prompt layout / harvest | `tests/test_llm_context_payload.py` |
| PagePromptSession | `tests/test_page_prompt_session.py` |
| Semantic cache LRU | `tests/test_semantic_cache_router.py` |
| Memory teardown | `tests/test_memory_budget.py` |
| Backlink index | `tests/test_backlink_index.py` |
| Bootstrap yield | `tests/test_bootstrap_yield.py` |
| Adaptive LLM | `tests/test_llm_client_adaptive.py` |
| CPU sandbox | `tests/test_process_priority.py` |
| Mmap reads | `tests/test_markdown_io.py` |
| Slow soak | `tests/slow/test_daemon_memory_soak.py` (`pytest -m slow`) |

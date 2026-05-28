# Runtime bootstrap (startup provisioning)

**Roadmap:** [`ROADMAP_LLM_WIKI.md`](../../ROADMAP_LLM_WIKI.md) (L1/L2, `matryca-wiki.yml`)

Matryca Plumber provisions **directories and optional config files** before harvest, lint, or daemon duty cycles touch the vault. Provisioning is **idempotent**: existing files are never overwritten.

**Implementation:** `src/utils/runtime_bootstrap.py` (`prepare_matryca_runtime`, `try_prepare_matryca_runtime_from_env`).

---

## When bootstrap runs

| Entry surface | Trigger |
|---------------|---------|
| **Maintenance daemon** | `start_daemon_foreground` and `MaintenanceDaemon.run_forever` (before bootstrap harvest) |
| **MCP stdio** | `app_lifespan` in `src/main.py` (after `load_dotenv`, before graph hygiene sweeps) |
| **CLI** | `cli.main` immediately after `reload_plumber_dotenv()` |
| **Sovereign UI** | FastAPI lifespan on server start; `POST /api/config`; `POST /api/daemon/start`; `GET /api/preflight` |

If `LOGSEQ_GRAPH_PATH` is unset or invalid, only **log directories** are ensured.

On first startup, if repo **`.env`** is missing and **`.env.example`** exists, Matryca Plumber copies the example to `.env` (logged at INFO) before loading environment variables.

---

## What is provisioned

### 1. Log sinks (always)

| Artifact | Env override | Default |
|----------|--------------|---------|
| Ops JSONL (token-accurate LLM trace) | `MATRYCA_PLUMBER_LOG_PATH` | `logs/matryca_plumber_ops.log` (under repo root) |
| Rotating application log (Loguru) | `MATRYCA_LOGURU_LOG_PATH` | `logs/matryca_plumber.log` |

**Rationale:** Parent directories are created with `mkdir(parents=True, exist_ok=True)` so the Sovereign UI activity feed and Loguru rotation never fail on first write. Paths must resolve under allowed roots (`$HOME`, repo, temp) — see `src/utils/config_paths.py`.

`configure_loguru()` delegates to the same helper so file sinks and bootstrap stay aligned.

### 2. L1 memory (when a valid graph is configured)

| Artifact | Default location | Overrides |
|----------|------------------|-----------|
| L1 directory | `<parent-of-LOGSEQ_GRAPH_PATH>/matryca-l1/` | `MATRYCA_L1_PATH`, or `memory_path` in `matryca-wiki.yml` |
| `README.md` | Inside L1 dir | Created only if missing — **not** loaded into LLM context |
| `session-rules.md` | Inside L1 dir | Created only when no other content `*.md` exists (excluding `README.md`) |

**Why L1 is a sibling of the vault (default):**

1. **L1 ≠ L2** — Session rules, deploy notes, and identity stubs are not part of the wiki corpus indexed as Logseq pages.
2. **Scan isolation** — The daemon harvests `pages/` and `journals/` only; a sibling folder is never ingested as graph content.
3. **Multi-vault sharing** — Several vaults under the same parent directory can share one L1 folder.
4. **Operational separation** — Sync/backup of the vault does not have to include agent-local rules unless you choose to.

To keep L1 **inside** the graph root instead, set:

```env
MATRYCA_L1_PATH=/absolute/path/to/your/vault/matryca-l1
```

or `memory_path` in `matryca-wiki.yml`. Bootstrap will create that path and seed `matryca-wiki.yml` accordingly.

**L1 read safety:** `collect_l1_markdown_paths` only reads under `$HOME` or the system temp directory. `README.md` is excluded from agent context by filename (documentation only).

### 3. Graph-local working directories (when a valid graph is configured)

| Directory / file | Purpose |
|------------------|---------|
| `.matryca_semantic_cache/` | Working cache root — excluded from alias/catalog page scans |
| `.matryca_semantic_cache/master_catalog.json` | Phase 1 catalog rows (summaries, tags, mtimes) |
| `.matryca_semantic_cache/backlink_counts.json` | Persisted incoming wikilink counts (v1.8 — avoids full-graph rescans) |
| `.matryca_semantic_cache/semantic_clusters.json` | Louvain neighborhoods for Phase 2 cluster cycles |
| `.matryca_semantic_cache/*.json` (hash names) | Per-operation semantic inference cache (TTL); **not** deleted when reserved files above are present |
| `templates/` (or `templates_subdir` from wiki YAML) | Template Markdown for `read_logseq_template` |

**Rationale:** Cache and templates must exist before the first catalog save or template read; creating them at startup avoids racey `mkdir` scattered through writers.

### 4. Wiki orchestration file (when missing)

| File | Behavior |
|------|----------|
| `<graph-root>/matryca-wiki.yml` | Copied from `matryca-wiki.example.yml` in the repo if absent; `memory_path` is patched to the resolved L1 directory |

**Rationale:** Namespaces, `wiki_file_prefix`, and structural-hop limits are orchestration metadata — not Logseq page content. Seeding gives new graphs a sane default without manual copy-paste.

---

## What is *not* provisioned at bootstrap

| Artifact | Reason |
|----------|--------|
| Repo **`.env`** (pre-existing) | Never overwritten; auto-created from `.env.example` only when `.env` is absent |
| **`pages/` / `journals/`** | A valid Logseq graph must already exist; Matryca Plumber does not fabricate vault structure |
| **`.matryca_daemon_state.json`**, **`.matryca_xray_state.json`** | Runtime ledgers — created on first checkpoint / X-Ray session |
| **Daemon PID / lock files** | Created when the daemon process starts |
| **`master_catalog.json` body** | Populated by bootstrap harvest, not empty placeholders |

This follows *create on first meaningful write* for stateful JSON so checkpoints stay honest.

---

## Related specs

- [`l1-l2-routing.md`](l1-l2-routing.md) — How L1 content is loaded into agent context vs L2 graph reads.
- [`ingest.md`](ingest.md) — Search → Scan → Update after bootstrap has prepared the filesystem.
- [`llm-performance.md`](llm-performance.md) — v1.8 KV-cache layout, memory teardown, cooperative harvest.

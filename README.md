# Matryca Logseq LLM Wiki

> **Agentic knowledge management for Logseq OG** — an MCP server that treats your vault as a **block graph**, not a flat document store. **Local-first**, **database-free**, and engineered for humans and agents co-editing the same Markdown outliner files.

[![CI](https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki/actions/workflows/ci.yml/badge.svg)](https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-92%20passing-brightgreen)](https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[Logseq](https://logseq.com/) materializes a folder of Markdown into a **typed block graph**. [Model Context Protocol](https://modelcontextprotocol.io/) connects LLMs to tools; **matryca-logseq-llm-wiki** is the bridge that speaks Logseq’s native semantics: nested bullets, `id::` UUIDs, `[[wikilinks]]`, `((block references))`, journals, **Advanced Query** (Datalog) blocks, and property lines shaped like `key:: value`.

The design follows an **L1 / L2** mental model (session rules versus vault depth), similar in spirit to Karpathy’s “LLM Wiki” lineage and [llm-wiki](https://github.com/MehmetGoekce/llm-wiki)-style routing — but **rebuilt for Logseq OG**: one graph on disk, **FastMCP** and **Pydantic** at the tool boundary, and **no** Postgres, Redis, SQLite, or hosted vector index as system-of-record.

---

## Why Matryca

| Typical “AI + notes” | Matryca |
|----------------------|---------|
| Treats each file as prose | Treats the vault as a **tree of blocks** with stable `id::` anchors |
| Static lists that go stale | **`#+BEGIN_QUERY` … `#+END_QUERY`** blocks that **re-evaluate inside Logseq** |
| Ignores journals and tasks | **`analyze_journal_tasks`** and **`append_logseq_journal_markdown`** for `TODO` / `LATER` / `WAITING` plus `SCHEDULED:` / `DEADLINE:` |
| Rewrites whole pages blindly | **Depth-first** **`write_logseq_outline`** via Logseq’s API; **scoped** **`patch_logseq_block_property_lines`** for `key::` lines only |
| No rollback story | Optional **`MATRYCA_GIT_SNAPSHOT_ON_WRITE`**: **git commit** on the graph repo before selected writes |

### Core engineering pillars

**Strict localism and zero database footprint**  
Every durable artifact lives under **`LOGSEQ_GRAPH_PATH`**. Lexical discovery uses an **in-process Okapi BM25** corpus over `pages/**/*.md`; structural navigation uses **line- and regex-bounded graph scans**; alias resolution builds an **in-memory index** with generational invalidation. Nothing maintains a competing queryable store: rankings and traversals are **ephemeral views** computed for the request and discarded, which eliminates sync, migration, and “two truths” failure modes while keeping latency predictable on large vaults.

**Zero-corruption transactional file engine**  
Disk mutators do not shrink files in place. Payloads flow through **`atomic_write_bytes`** in **`src/graph/markdown_blocks.py`**: write to a **hidden sibling temp** (`.<basename>.XXXXXX.tmp` in the **same directory** as the target so `os.replace` never crosses filesystems), **`flush`** the handle, **`os.fsync`** to push bytes toward durable media, then a **single atomic `os.replace`** onto the live `.md` path. On failure before rename, the temp file is removed and the original inode remains intact — the same **write-ahead, commit-on-rename** family of guarantees as SQLite’s page journal or Git’s object store updates. Selected tools also write a **`.bak`** copy (e.g. property-line surgery) before swap for operator-visible rollback.

**Compiler-grade AST and fence awareness**  
Spatial reads delegate block boundaries and indentation to **[logseq-matryca-parser](https://github.com/MarcoPorcellato/logseq-matryca-parser)**. For mutators, **`mldoc_properties`** and **`mldoc_guards`** encode **Logseq / mldoc-aligned** rules: first `::` pair on property lines, wikilink-depth-aware CSV splitting, double-quoted spans, drawers (`:LOGBOOK:` … `:END:`), fenced code, `#+BEGIN_QUERY` regions, and `{{` macro opens. **`compute_page_protected_line_indices`** in **`global_fence_scanner.py`** performs a **streaming global fence pass** so code blocks, HTML comments, and query blocks become **dead zones** — mutators **fail closed** when a span would intersect protected lines.

**Agentic git safety snapshots**  
When **`MATRYCA_GIT_SNAPSHOT_ON_WRITE=true`** and the graph root is a **git** checkout, **`snapshot_git_working_tree`** runs **`git add -A`** and **`git commit`** before **`write_logseq_outline`** and selected disk mutators (`refactor_logseq_blocks`, `refactor_large_blocks`, MOC writes). That yields a **reversible checkpoint** (`git revert` / `git log`) without any hosted snapshot product — entirely **local** and opt-in.

---

## Architecture stack

```mermaid
flowchart TB
  subgraph host["Host machine"]
    IDE["MCP client\n(Claude Desktop, Cursor, …)"]
    LQ["Logseq\nHTTP JSON-RPC"]
  end
  subgraph proc["matryca-logseq-llm-wiki process"]
    MCP["FastMCP stdio"]
    SRV["MatrycaMCPServer\n+ Pydantic validators"]
    QG["quality_gate\n(credential / key scans)"]
    MCP --> SRV
    SRV --> QG
  end
  subgraph data["Data plane"]
    FS[("LOGSEQ_GRAPH_PATH\npages/ · journals/ · templates/")]
    PAR["logseq-matryca-parser\n(spatial read)"]
    GFS["global_fence_scanner\n(dead-zone line index)"]
    ATW["atomic_write_bytes\n(tmp + fsync + replace)"]
    GC["generational_cache\n(mtime_ns signatures)"]
  end
  subgraph delivery["Delivery gates (repo)"]
    CI["GitHub Actions\nRuff · Mypy · Pytest"]
    DB["Dependabot\npip + Actions"]
    REL["tag v* → uv build\ngh release create"]
  end
  IDE <-->|JSON-RPC MCP| MCP
  SRV <-->|Bearer JSON-RPC| LQ
  SRV <-->|read / mutate| FS
  SRV --> PAR
  SRV --> GFS
  SRV --> ATW
  SRV --> GC
  LQ --> FS
  CI -.->|enforces| proc
  DB -.->|updates| delivery
  REL -.->|artifacts| delivery
```

### L1 versus L2 (two-layer context)

```mermaid
flowchart TB
  subgraph L1["L1 — session rules (small, capped)"]
    M1["MATRYCA_L1_PATH\nor matryca-l1/*.md\nor matryca-wiki.yml memory_path"]
  end
  subgraph L2["L2 — the graph"]
    M2["pages/ · journals/ · templates/"]
  end
  A[Agent] --> R{High-stakes\nbefore vault?}
  R -->|yes| T[read_l1_memory]
  R -->|no| Q[query_logseq_pages_local\nBM25 + generational cache]
  T --> M1
  Q --> M2
  M1 --> M2
```

### Runtime agent loop (search → gate)

```mermaid
flowchart LR
  S[Search\nBM25 / hops] --> C[Scan\nread_logseq_page · lint]
  C --> U[Refactor\nAPI + disk mutators]
  U --> G[Garden\nMOC · tags · split]
  G --> Q[Quality gate\nsecrets · fan-out · routing_hint]
  subgraph iron["Ironclad sub-loop"]
    F[fence scanner] --> A2[atomic swap]
    A2 --> I[incremental cache]
  end
  U -.-> iron
  G -.-> iron
```

---

## Feature matrix: ten architectural phases

Each phase adds capabilities; later phases **harden** earlier tools without necessarily renaming the MCP surface. **Phase** is the product narrative; **modules** are what you grep in `src/`.

| Phase | Core capabilities | MCP tools (exposed names) |
|:-----:|-------------------|---------------------------|
| **1 — Baseline bridge** | FastMCP server, async Logseq JSON-RPC client, **`OutlineNode`** validation, DFS **`write_logseq_outline`**, spatial **`read_logseq_page`**, block-ref integrity scan, dashboard aggregation | `read_logseq_page`, `write_logseq_outline`, `lint_logseq_block_refs`, `render_logseq_dashboard` |
| **2 — L1 / L2 routing** | Capped **`read_l1_memory`** from configurable paths; **routing hints** on read/write responses for traceability (`routing_hint.py`) | `read_l1_memory` *(hints on other tools’ payloads)* |
| **3 — PKM refinements** | BM25 and substring local query; structural BFS hops and hub/orphan reports; surgical **`key::`** property edits; templates; wiki-prefix lint; namespace index; optional **git snapshot** on outline and heavy mutators | `query_logseq_pages_local`, `traverse_logseq_structural_hops`, `report_structural_hubs_orphans`, `patch_logseq_block_property_lines`, `list_logseq_templates`, `read_logseq_template`, `lint_matryca_wiki_pages`, `list_logseq_namespace_index`, `snapshot_logseq_graph_git` |
| **4 — Logseq superpowers** | Advanced Query injection with preset or raw EDN; journal task mining; entity resolution via alias index; page alias append | `inject_logseq_advanced_query`, `analyze_journal_tasks`, `append_logseq_journal_markdown`, `resolve_logseq_entity`, `append_logseq_page_alias` |
| **5 — Graph gardener** | SRS-style flashcards from `::` pairs; vault-wide tag unify; same-page reparent refactor | `generate_logseq_flashcards`, `lint_unify_logseq_tags`, `refactor_logseq_blocks` |
| **6 — Synthesis engine** | Unlinked mention discovery; MOC generation; long-bullet split; manual git snapshot | `resolve_unlinked_mentions`, `generate_moc_page`, `refactor_large_blocks`, `snapshot_logseq_graph_git` |
| **7 — Mldoc guards** | **`mldoc_properties`** (property grammar, CSV / wikilink / quote semantics); **`mldoc_guards`** (drawers, fences, macros) wired into property edit, tag unify, alias append, large-block split | *Same tool names as phases 3–6; strengthened internals* |
| **8 — Ironclad data plane** | **`compute_page_protected_line_indices`** (global fence lexer); **`atomic_write_bytes`** on mutators; **`generational_cache`** (`st_mtime_ns` keyed alias + BM25 corpus reuse, Salsa-style invalidation) | *Same tool names; dead-zone and cache behavior upgraded* |
| **9 — Trust and policy plane** | **`quality_gate`**: blocks credential-like outline properties and raw query EDN; **`lint_matryca_wiki_pages`** for configurable wiki discipline; structured dry-run / apply responses | Enforcement inside `write_logseq_outline`, `inject_logseq_advanced_query`; governance tools listed in phase 3 |
| **10 — Delivery and community** | **GitHub Actions** (`ci.yml`): `uv sync --locked`, Ruff lint + format check, **Mypy** on `src` and `tests`, Pytest; **Dependabot** (pip + Actions); **`release.yml`** on `v*` tags (`uv build`, `gh release create`); **`SECURITY.md`**; **Contributor Covenant** (`CODE_OF_CONDUCT.md`); issue and PR templates | *Repository operations — no additional MCP tools* |

**Roadmaps and design history:** [`docs/roadmaps/`](docs/roadmaps/) (LLM Wiki, Phase 3, Logseq superpowers, Phase 5–6, mldoc compliance, Ironclad Shield).

---

## Zero-install execution (`uvx`)

You do **not** need to clone this repository to run the published console script. **[uv](https://docs.astral.sh/uv/)** can materialize an ephemeral environment from the Git VCS URL (the package depends on **`logseq-matryca-parser`** via `git+https` in `pyproject.toml`, so the practical install path remains VCS-backed).

```bash
uvx --from git+https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki.git matryca-logseq-llm-wiki
```

Configure **`LOGSEQ_API_TOKEN`**, **`LOGSEQ_GRAPH_PATH`**, and related variables in your MCP host’s server definition alongside this command. For **responsible vulnerability disclosure**, see [`SECURITY.md`](SECURITY.md).

---

## ⚠️ CRITICAL SAFETY WARNING: The Sandbox Rule

Matryca uses **two different paths** to reach your vault:

| Path | Mechanism | What it controls |
|------|-----------|------------------|
| **Reads** (search, spatial reads, disk mutators) | Direct filesystem access via **`LOGSEQ_GRAPH_PATH`** | Whatever folder you point at in `.env` |
| **Writes** (outline insert, block API, query injection) | Logseq’s **local HTTP API** | **Only the graph currently open in the Logseq app** |

This creates a **Split-Brain** hazard. If you set `LOGSEQ_GRAPH_PATH` to a test folder but leave your **real** vault open in Logseq, the AI will **read** from the sandbox and **write** destructive changes to your production graph. There is no server-side guard: Logseq’s API always targets the active UI graph, not the path in your `.env`.

**Before dogfooding, testing, or pointing Matryca at any non-production folder, follow all four steps below. Skipping step 2 is the most common way to corrupt real data.**

### Safe test environment (4 steps)

1. **Clone the graph.** Copy your real Logseq folder to a dedicated location (for example `Logseq_Matryca_Test`). Use a full copy, not a symlink, so reads and writes stay isolated.

2. **Isolate the Logseq UI (CRITICAL).** Open the Logseq app. Click your **current graph name** in the sidebar, choose **Add new graph**, and open the **`Logseq_Matryca_Test`** folder. ⚠️ **The Logseq window MUST be focused on the test folder** — not your daily driver vault. If the title bar or graph picker still shows your real graph, **stop** and switch graphs before running any MCP tools. Writes always land on whatever graph Logseq has open, regardless of `LOGSEQ_GRAPH_PATH`.

3. **Enable API and get token.** In Logseq: **Settings → Advanced → Enable HTTP APIs server**. Copy the **Authorization** token shown there.

4. **Align the `.env`.** Ensure your project `.env` (or MCP host `env` block) matches the **same** test folder and token:

   ```env
   LOGSEQ_GRAPH_PATH=/absolute/path/to/Logseq_Matryca_Test
   LOGSEQ_API_TOKEN=your_token_here
   MATRYCA_GIT_SNAPSHOT_ON_WRITE=true
   ```

   Use an **absolute** path. With `MATRYCA_GIT_SNAPSHOT_ON_WRITE=true`, selected writes also create a local **git commit** on the test graph so you can revert experiments.

<details>
<summary><b>🛠️ Using Logseq Sync? Click here if Logseq refuses to open the test folder.</b></summary>
<br>
If you use Logseq Sync, the app tracks graphs by hidden UUIDs, not folder paths. A direct copy-paste will cause a UUID clash. Do a <b>Clean Transplant</b> instead:
<ol>
  <li>Create a brand new, empty folder (e.g., <code>Logseq_Matryca_Test</code>).</li>
  <li>Open Logseq, click "Add new graph", and select this empty folder to initialize a clean state.</li>
  <li>Close Logseq completely.</li>
  <li>Copy <b>ONLY</b> the <code>pages/</code>, <code>journals/</code>, and <code>assets/</code> folders from your real graph into the test folder. (Do NOT copy the <code>logseq/</code> folder or hidden git files).</li>
  <li>Reopen Logseq and click <b>Re-index</b>.</li>
</ol>
</details>

**Sanity check:** After setup, change one harmless block via the API (or a single MCP write) and confirm the edit appears **only** under `Logseq_Matryca_Test` on disk — not in your production vault.

---

## Quickstart (clone and develop)

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)**
- **Logseq** with the **HTTP API** enabled and a **Bearer token**

### Install

```bash
git clone https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki.git
cd matryca-logseq-llm-wiki
make install
```

### Environment

Copy **`.env.example`** to **`.env`** and set at minimum:

| Variable | Role |
|----------|------|
| `LOGSEQ_API_TOKEN` | **Required.** Bearer token for Logseq’s HTTP API |
| `LOGSEQ_API_URL` | Default `http://localhost:12315` |
| `LOGSEQ_GRAPH_PATH` | **Required for disk tools.** Absolute graph root (directory containing `pages/`) |
| `MATRYCA_L1_PATH` | Optional: file or directory of small Markdown “L1” session rules |
| `MATRYCA_WIKI_CONFIG` | Optional: path to `matryca-wiki.yml` (else `$LOGSEQ_GRAPH_PATH/matryca-wiki.yml`) |
| `MATRYCA_GIT_SNAPSHOT_ON_WRITE` | `true` or `false` — opt-in automatic **`git add -A` + `git commit`** before selected writes when the graph is a git checkout |

Optional graph orchestration: copy [`matryca-wiki.example.yml`](matryca-wiki.example.yml) to your graph as **`matryca-wiki.yml`** for namespaces, template subdirectory, wiki lint prefix, and dashboard title.

### Verify

```bash
make check
```

Runs Ruff (format + lint), **strict Mypy** on `src/` and `tests/`, and **pytest** (92 tests). The same bar is enforced on **`main`** in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### Claude Desktop (stdio MCP)

Example fragment for **`claude_desktop_config.json`**:

```json
{
  "mcpServers": {
    "matryca-logseq-llm-wiki": {
      "command": "uv",
      "args": ["run", "python", "-m", "src.main"],
      "cwd": "/absolute/path/to/matryca-logseq-llm-wiki",
      "env": {
        "LOGSEQ_API_TOKEN": "your-token",
        "LOGSEQ_API_URL": "http://localhost:12315",
        "LOGSEQ_GRAPH_PATH": "/absolute/path/to/your/Logseq/graph",
        "MATRYCA_GIT_SNAPSHOT_ON_WRITE": "false"
      }
    }
  }
}
```

Restart the MCP host after edits. Keep **Logseq running** when tools call live **`insertBlock`** or query injection.

---

## Documentation map

| Document | Audience |
|----------|----------|
| [`SYSTEM_PROMPT.md`](SYSTEM_PROMPT.md) | Agent operators — outline discipline, Search → Scan → Update, dry-run-first mutators |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Engineers — bounded-work parsing, Ironclad data plane, phase history |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contributors — `uv`, `make check`, MCP testing notes |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Community standards (Contributor Covenant 2.1) |
| [`SECURITY.md`](SECURITY.md) | Private reporting via GitHub Security Advisories |

---

## License

Apache-2.0 — see [LICENSE](LICENSE).

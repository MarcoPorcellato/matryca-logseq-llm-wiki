# Architecture

**matryca-plumber** connects local LLM cognition and operator automation to **Logseq OG** (pure local Markdown) through a **standalone Python asynchronous daemon**, the **`matryca` CLI**, and a **100% headless** CRUD & mutation plane powered by **`logseq-matryca-parser==0.3.3`**. **FastMCP** provides an **optional auxiliary stdio API** for external MCP hosts; it is **not** the primary execution model — the **MaintenanceDaemon** runs duty-cycle graph scans, OCC-guarded writes, and ledger persistence with **no MCP client attached**. **Pydantic** validates structured payloads at every boundary. This document is the engineering contract: **bounded-work parsing**, **where spatial truth lives**, **how the Headless CRUD & Mutation Plane commits structural edits**, **how X-Ray state persists across stateless invocations**, and **how the repository’s delivery gates stay aligned with runtime behavior**.

---

## Core philosophy

### Single system of record (no auxiliary database)

The only durable source of truth is the tree under **`LOGSEQ_GRAPH_PATH`**: `pages/`, `journals/`, `templates/`, and the rest of the Logseq graph on disk.

The project deliberately does **not** introduce Postgres, SQLite, Redis, embedding indices, or document stores that could fork truth away from Logseq’s files. There is also **no** Logseq HTTP JSON-RPC client: the Logseq desktop application is **not** a runtime dependency.

1. **One artifact for humans and agents** — Diffs, `git blame`, ripgrep, and backup tools observe exactly what Logseq observes.
2. **No invalidation labyrinth** — A secondary index would require correctness proofs every time a human edits outside the agent.
3. **Forces block-shaped thinking** — Tools steer toward AST-guided appends and scoped file surgery instead of destructive whole-file rewrites.

When the codebase needs **ranking** (Okapi BM25), **adjacency** (wikilink and tag BFS), or **aggregates** (dashboard counts), it computes them **inside the running Matryca process** — daemon poll cycle, CLI invocation, or MCP tool handler — without any auxiliary database. **Generational caches** (`st_mtime_ns` signatures) reuse in-memory structures across calls when the underlying files are quiescent — that is **memoization**, not a competing database.

---

## The bounded-work paradigm (parser versus streaming passes)

### When delegation to `logseq-matryca-parser` is mandatory

**[logseq-matryca-parser](https://github.com/MarcoPorcellato/logseq-matryca-parser)** (`==0.3.3`) owns **spatial truth**: indentation, block boundaries, parent/child relationships, and a faithful view of a page consistent with Logseq’s outliner model. The adapter **`src/rag/matryca_hooks.py`** feeds **`read_logseq_page`** so agents receive the same block-tree semantics the editor uses.

Use the parser whenever the question is **“what is a block on this page?”** — hierarchy, `id::` placement, or evidence gathering before proposing edits.

### When targeted line-by-line streaming passes execute

This repository **does not** reimplement a second full-file Markdown AST for vault-wide chores. Instead it runs **narrow, auditable** passes on raw text — regex-bounded, directory-scoped, and often **dry-run first** — where the parser is the wrong abstraction or graph-wide coverage is required:

| Class of work | Modules / tools | Mechanism |
|---------------|-------------------|-----------|
| **Scoped `key::` surgery** | `patch_logseq_block_property_lines` — `property_line_edit.py` | Lines inside a subtree anchored at `id:: <uuid>`; only true property lines per **`is_logseq_block_property_line`**; intersection with **`compute_page_protected_line_indices`** |
| **Graph-wide ref integrity** | `lint_logseq_block_refs` — `block_ref_lint.py` | **`LogseqGraph.load_directory`** + **`get_broken_references()`** — in-memory index over all `((uuid))` refs |
| **Lexical discovery** | `query_logseq_pages_local` — `local_query.py` | Token bags + Okapi BM25 in memory; corpus memoized in **`generational_cache`** keyed by page `st_mtime_ns` sets |
| **Structural hops** | `traverse_logseq_structural_hops`, hub/orphan reports — `link_tag_hop.py` | Wikilinks, `#tags` / `tags::`, light `type::` / `domain::` edges over `pages/**/*.md` |
| **Hashtag normalization** | `lint_unify_logseq_tags` — `tag_unify.py` | Token-level `#tag` detection with **mldoc quoted-span** awareness and **global fence** guards |
| **Journals and aliases** | `journal_task_scan.py`, `alias_index.py` | Line- and property-oriented scans; alias index rebuilt through **`cached_build_alias_index`** |

**Design principle:** parser for **hierarchy and identity**; streaming passes for **surgical edits**, **graph-wide invariants**, and **diff-friendly** transformations that must remain reviewable in Python.

---

## 5. Headless Mutation & State Plane (`v1.4.0`)

Starting with **v1.4.0**, Matryca fully abandons the local HTTP client-server model (Logseq JSON-RPC on ports 8080/12315) and becomes a **100% Headless** daemon engine. The transition from a **stateful, network-bounded** architecture (Electron app + API tokens) to a **local-first, zero-dependency** model is the milestone of this release: reads and writes converge on a single path — `LOGSEQ_GRAPH_PATH` — with no split-brain risk.

### 5.1 AST File Splicing Engine

The mutation flow no longer goes through Logseq APIs; it manipulates the abstract syntax tree (AST) of local Markdown files via **`logseq_matryca_parser.agent_writer`**.

Mutations (`write_logseq_outline`, `patch_logseq_block_property_lines`, `inject_logseq_advanced_query`, and the `mutate_graph_data` mega-tool) load the graph in memory (`LogseqGraph.load_directory`), compute spatial block hierarchy, and insert new bullets at the correct indentation (`indent_level + 1` × `tab_size`). The canonical operation is **`append_child_to_node`**, orchestrated by **`src/agent/graph_dispatch.py`**: mathematical insertion at line `_deepest_line_end(parent)`, atomic commit via `tmp` + `os.replace`. Every write is protected by the concurrent context manager **`page_rmw_lock(path)`** (`src/graph/page_write_lock.py`).

```mermaid
flowchart TD
  TOOL["Daemon / CLI / optional MCP\nwrite_logseq_outline · mutate_graph_data"] --> GD["graph_dispatch.py"]
  GD --> LOAD["LogseqGraph.load_directory(graph_root)"]
  LOAD --> RESOLVE["Resolve parent UUID\n(registry + id:: embed ref)"]
  RESOLVE --> LOCK["page_rmw_lock(source_path)"]
  LOCK --> AW["append_child_to_node\n(agent_writer)"]
  AW --> SPLICE["Mathematical line insert\nat _deepest_line_end(parent)"]
  SPLICE --> SWAP["tmp file + os.replace\n(per-page atomic commit)"]
  SWAP --> RELOAD["Return new block UUID"]
```

**`_headless_write_outline`** walks validated **`OutlineNode`** trees depth-first (DFS): each append waits for a real on-disk UUID before children, preserving ordering invariants without network round-trips.

`graph_dispatch.py` is the sole async dispatch layer for FastMCP and the **`matryca`** CLI. Mutators in `src/graph/` (property edit, tag unify, reparent, MOC, journal) use **`atomic_write_bytes`** and acquire **`page_rmw_lock`** where per-page RMW is required.

| Removed | Replaced by |
|---------|-------------|
| `src/bridge/logseq_client.py` | Deleted — no HTTP transport |
| `httpx` | Removed from `pyproject.toml` |
| `LOGSEQ_API_TOKEN` / `LOGSEQ_API_URL` | Obsolete — not read at runtime |
| Split-brain read/write risk | Single system of record: **`LOGSEQ_GRAPH_PATH`** |

### 5.2 Printing Press State Persistence (X-Ray)

To maximize LLM token efficiency, the system adopts **X-Ray** mode (Printing Press Mode). When an agent runs a dense scan (`read_graph_data` with `target_type="xray_page"`), 36-character UUIDs are replaced by short aliases like `[0]`, `[1]` — typically up to **~35×** less overhead than repeatedly echoing UUIDs in context.

Because the CLI and MCP server are **stateless** between invocations, the translation map is saved natively in the hidden file **`.matryca_xray_state.json`** at the graph root via **`SessionAliasRegistry`** (`src/agent/alias_state.py`). Subsequent mutation calls intercept aliases (`resolve_target`, `resolve_pipe_target`), read state from disk, resolve the real UUID, and perform atomic file I/O writes.

```mermaid
flowchart LR
  READ["read_graph_data\ntarget_type=xray_page"] --> OUT["[n] outline\n(properties stripped)"]
  OUT --> SAVE["SessionAliasRegistry.save_to_disk"]
  SAVE --> FILE[".matryca_xray_state.json\n(graph root)"]
  MUT["mutate_graph_data\ntarget=[n]"] --> LOAD["SessionAliasRegistry.load_from_disk"]
  LOAD --> RESOLVE["resolve_target → UUID"]
  RESOLVE --> GD2["graph_dispatch headless write"]
```

---

## FastMCP, Pydantic, and the trust boundary

`src/main.py` constructs **FastMCP** with a lifespan that wires **`MatrycaWikiConfig`** (no `LogseqClient`). **`register_mcp_tools`** in **`src/agent/mcp_server.py`** registers every `@mcp.tool()` handler.

Incoming outline payloads validate as **`OutlineNode`** (`page_type`, `domain`, `entity_type` invariants, normalized `children`) before any disk mutation. **`outline_security_violations`** and **`advanced_query_security_violations`** in **`src/agent/quality_gate.py`** reject credential-shaped property names and OpenAI-style key material from reaching the graph via **`write_logseq_outline`** or **`inject_logseq_advanced_query`**.

**`routing_hint.py`** attaches machine-readable hints to high-signal responses so orchestrators can preserve **L1/L2** traceability without re-parsing natural-language tool output.

### Git snapshots as opt-in rollback

**`MATRYCA_GIT_SNAPSHOT_ON_WRITE`** (`src/agent/git_snapshot.py`) optionally runs **`git add -A`** + **`git commit`** on **`LOGSEQ_GRAPH_PATH`** when it is a git checkout. **`snapshot_logseq_graph_git`** exposes the same primitive for manual checkpoints.

```mermaid
flowchart TD
  E["MATRYCA_GIT_SNAPSHOT_ON_WRITE"] -->|false| SKIP["No auto commit"]
  E -->|true| G{"Graph path is a git repo?"}
  G -->|no| SKIP2["Skipped; reason logged"]
  G -->|yes| C["git add -A && git commit"]
  C --> W["Outline write or disk mutator"]
```

### Choosing read versus write paths

```mermaid
flowchart TD
  START["What do you need?"] --> Q1{"Spatial tree\nid:: · indent?"}
  Q1 -->|yes| R1["read_logseq_page\nparser adapter"]
  Q1 -->|no| Q2{"Append bullets under\na known parent UUID?"}
  Q2 -->|yes| W1["write_logseq_outline\ngraph_dispatch · append_child_to_node"]
  Q2 -->|no| Q3{"Only key:: lines\nin one block?"}
  Q3 -->|yes| W2["patch_logseq_block_property_lines\ndry_run first"]
  Q3 -->|no| Q4{"Vault-wide scan\nBM25 · lint · hops?"}
  Q4 -->|yes| D1["FS scan or in-memory index\nno second DB"]
```

---

## End-to-end data flow

On the **autonomous primary path**, **`MaintenanceDaemon`** and the **`matryca plumber`** / **`matryca`** CLI drive **`graph_dispatch`** directly: **structural writes** splice Markdown via **`append_child_to_node`** under **`page_rmw_lock`**; **spatial reads** use the parser adapter; **other disk mutators** use **`atomic_write_bytes`** (and often a **`.bak`** copy immediately before swap for property-style edits). **File I/O, OCC locks, and graph parsing are native to this process** — no MCP attachment is required for the daemon to run.

When an **MCP host** spawns the same binary on **stdio**, tool calls flow through FastMCP into **`MatrycaMCPServer`** and the **identical** **`graph_dispatch`** layer:

```mermaid
sequenceDiagram
    autonumber
    participant LLM as LLM Agent
    participant MCP as FastMCP (stdio)
    participant Bridge as MatrycaMCPServer
    participant GD as graph_dispatch
    participant QG as quality_gate
    participant AW as agent_writer
    participant FS as Markdown on disk
    participant GFS as global_fence_scanner

    LLM->>MCP: Tool call
    MCP->>Bridge: Validated args / context
    alt High-risk write
        Bridge->>QG: outline / query EDN scan
        QG-->>Bridge: pass or ValueError / error dict
    end
    alt Write path (headless outline)
        Bridge->>GD: _headless_write_outline
        GD->>AW: append_child_to_node
        AW->>FS: AST splice under page_rmw_lock
        AW-->>GD: new block uuid
    else Read path (spatial)
        Bridge->>FS: read page via parser adapter
    else Disk mutator
        Bridge->>GFS: protected line indices
        GFS-->>Bridge: dead-zone set
        Bridge->>Bridge: pre-flight ((uuid)) shape check
        Bridge->>FS: tmp + fsync + os.replace (+ optional git snapshot)
    end
    Bridge-->>MCP: Structured result + routing_hint
    MCP-->>LLM: Tool result
```

### Outline write ordering (depth-first)

`write_logseq_outline` walks **`OutlineNode`** depth-first: each headless append returns a **real** UUID before children are created, so the graph never receives unresolved parent placeholders.

```mermaid
flowchart TD
  P["parent_block_uuid"] --> N1["append node → uuid A"]
  N1 --> C1["each child"]
  C1 --> N2["append under A → uuid B"]
  N2 --> C2["recurse"]
  C2 --> DONE["DFS-ordered UUIDs returned"]
```

---

## The agentic pipeline

Operational prompting lives in **`SYSTEM_PROMPT.md`**. At a high level:

1. **Search** — Prefer **`query_logseq_pages_local`** with **`mode=bm25`**. Optionally **`read_l1_memory`** when mistakes would be costly before touching L2.
2. **Scan** — **`read_logseq_page`** for ground truth; **`traverse_logseq_structural_hops`** / **`report_structural_hubs_orphans`**; **`lint_logseq_block_refs`** when editing many `((uuid))` refs.
3. **Refactor / update** — **`write_logseq_outline`**; **`patch_logseq_block_property_lines`** (including **persist-first** `id::` when the parser reports **`synthetic_id: true`**); **`inject_logseq_advanced_query`**; journal and alias tools as needed.
4. **Garden** — Flashcards, tag unify, reparent, unlinked mentions, MOC generation, large-block split — almost always **`dry_run=true`** first.
5. **Quality gate** — Re-lint refs after large edits; respect **`protected_fence`** when a mutation would cross fenced code, HTML comments, Advanced Query blocks, or drawer regions.

```mermaid
flowchart TD
  subgraph search["1 Search"]
    BM25["query_logseq_pages_local\n+ generational_cache"]
    L1["read_l1_memory optional"]
  end
  subgraph scan["2 Scan"]
    READ["read_logseq_page"]
    HOP["traverse · hubs/orphans"]
    LINT["lint_logseq_block_refs"]
  end
  subgraph update["3 Refactor / Update"]
    OUT["write_logseq_outline\n+ quality_gate"]
    PATCH["patch_logseq_block_property_lines\n+ fence scanner"]
    AQ["inject_logseq_advanced_query\n+ quality_gate"]
    JR["journal analyze/append"]
    AL["entity resolve/alias"]
  end
  subgraph garden["4 Garden"]
    G1["flashcards · tags · reparent\n+ mldoc + fences"]
    G2["unlinked · MOC · split blocks"]
  end
  subgraph gate["5 Quality gate"]
    QG["routing hints · wiki lint"]
  end
  L1 --> BM25
  BM25 --> READ
  READ --> HOP
  HOP --> LINT
  LINT --> OUT
  OUT --> PATCH
  PATCH --> AQ
  AQ --> JR
  JR --> AL
  AL --> G1
  G1 --> G2
  G2 --> QG
```

---

## Ironclad data plane (Phases 7 and 8)

Phases **7** and **8** are **engineering pillars** under the existing MCP tool names: **compiler-aligned** parsing, **whole-page dead zones**, **transactional durability**, and **incremental** vault indexes.

### Global fence lexers (dead zones across full files)

**`src/graph/global_fence_scanner.py`** exports **`compute_page_protected_line_indices(file_content) -> set[int]`**: one **O(n)** streaming pass over lines, returning **0-based** indices of every line that must be treated as **immutable** for mutators and certain lexical tools.

Mechanics:

1. **Markdown fenced code** — Opening fences (up to three leading spaces, run of ≥ three backticks); closing fence must match tick count. **Every line inside** an open fence is protected, including the opener (including empty fences).
2. **HTML block comments** — `<!--` / `-->` with **`in_comment`** state carried across lines.
3. **Advanced Query blocks** — `#+BEGIN_QUERY` / `#+END_QUERY` after leading whitespace.

**Critical invariant:** While **inside a Markdown fence**, HTML and Advanced Query detectors are **masked**. A line containing `#+BEGIN_QUERY` or `<!--` **inside a code block** does **not** flip global query or HTML state — preventing pasted examples from corrupting the scanner.

**Consumers:** `property_line_edit`, `tag_unify`, `reparent_blocks`, `split_large_blocks`, and `unlinked_mentions` intersect edit or match spans with the protected set and **fail closed** (`protected_fence` or skip) when a mutation would cross a dead zone. **`tests/test_ironclad_phase8.py`** covers nested fences, masked markers, and multiline HTML.

### ACID-inspired file swaps

**`atomic_write_bytes`** / **`atomic_write_file`** in **`src/graph/markdown_blocks.py`** implement a **commit** discipline for each `.md` artifact:

1. **`tempfile.mkstemp`** in the **target directory** with prefix `.<basename>.` and suffix `.tmp` — guarantees `os.replace` stays on one filesystem volume.
2. **Write full payload**, **`flush`**, then **`os.fsync(fileno)`** — pushes data through the kernel toward durable media before any live path references the new bytes.
3. **`os.replace(tmp, final)`** — POSIX **atomic** rename over the destination.
4. **Parent-directory `fsync`** — reopens the containing directory read-only and fsyncs directory metadata so the rename survives sudden power loss on journaling-challenged volumes.

On **any** exception before `replace` completes, the temp file is **unlinked** and the original path is unchanged. There is **no in-place truncation** window. Disk mutators across **`property_line_edit`**, **`tag_unify`**, **`reparent_blocks`**, **`split_large_blocks`**, **`journal_task_scan`**, **`flashcards`**, and **`moc_page`** route through this helper. Property-line apply additionally uses **`shutil.copy2`** to a **`.bak`** sibling before swap for a second, human-visible rollback lever.

### UUID lifecycle and synthetic ID guardrails (`v1.0.1`)

The **Trust plane** (Phase 9) and **Ironclad Shield** (Phase 8) converge on a single invariant: **ephemeral AST identity must never be mistaken for durable Logseq database identity**.

```mermaid
flowchart LR
  subgraph read["Spatial read"]
    P["logseq-matryca-parser"]
    A["matryca_hooks.py"]
    P -->|"uuid · synthetic_id · source_uuid"| A
    A -->|"read_logseq_page summary"| LLM["Agent"]
  end
  subgraph persist["Persist-first (agent policy)"]
    LLM -->|"synthetic_id true, no source_uuid"| PATCH["patch_logseq_block_property_lines\nid:: injection"]
    PATCH --> DISK["Source .md on disk"]
  end
  subgraph write["Disk commit"]
    MOC["MOC / mutator payload"]
    MOC --> GUARD["atomic_write_bytes\npre-flight ((uuid)) scan"]
    GUARD -->|"valid"| SWAP["tmp + fsync + os.replace"]
    GUARD -->|"malformed"| FAIL["ValueError — no replace"]
  end
  DISK --> MOC
```

#### Parser contract: `synthetic_id` and `source_uuid`

**[logseq-matryca-parser](https://github.com/MarcoPorcellato/logseq-matryca-parser)** annotates each block with:

| Field | Meaning |
|-------|---------|
| **`uuid`** | Effective identifier for the block in the current parse (may be read from disk or synthesized). |
| **`source_uuid`** | When present, the UUID taken from a persisted **`id::`** line on disk. |
| **`synthetic_id`** | **`true`** when the effective UUID was **generated in memory** (e.g. deterministic UUIDv5 for blocks lacking `id::`); **`false`** when identity is anchored to on-disk `id::`. |

The adapter **`src/rag/matryca_hooks.py`** surfaces these fields in every **`read_logseq_page`** spatial summary—e.g. `` `synthetic_id` true ``, `` `source_uuid` … (persisted `id::` on disk) ``, or an explicit **not on disk — persist `id::` before `((uuid))`** warning when the agent must not reference the block yet.

**Design rule:** A `((uuid))` in new Markdown is only safe when that UUID is already backed by an **`id::`** line in the **source** file Logseq will index. Parser output alone does not create graph edges.

#### Pre-flight Block Reference Guard

Before **`os.replace`**, **`atomic_write_bytes`** decodes UTF-8 payloads that contain `((` and calls **`assert_valid_block_refs_in_markdown`** in **`src/graph/logseq_uuid.py`**. The guard:

- Scans all **`((...))`** tokens with a bounded regex.
- Requires each inner token to be a **canonical 36-character hyphenated UUID** that parses under **`uuid.UUID`**.
- Raises **`ValueError`** with an actionable message on the first malformed ref—**no temp file is promoted** to the live path.

This complements **`lint_logseq_block_refs`**: the linter queries **`LogseqGraph.get_broken_references()`** for referential integrity vault-wide; the pre-flight guard catches **syntactically impossible** refs at commit time (common when models truncate or mistype hex). Together they enforce **separation of concerns** between *shape validity* (Ironclad) and *referential integrity* (Trust plane + gardening tools).

#### Agent policy (`SYSTEM_PROMPT.md`)

Operational prompting requires a **persist-first** sequence when building MOCs or any disk artifact with block refs:

1. **Read** the source page and inspect **`synthetic_id` / `source_uuid` / `uuid`**.
2. If **`synthetic_id: true`** and there is no on-disk `id::`, **`patch_logseq_block_property_lines`** (`dry_run=true` then `false`) injects **`id:: <uuid>`** into the original file.
3. Only then may tools such as **`generate_moc_page`** or other **`atomic_write_bytes`** paths emit **`((that-uuid))`**.

Release **`v1.0.1`** hardened this pipeline after a **~106k-token** Cursor MCP stress test that built a large MOC from **2,300+** lines of nested Markdown—the parser hierarchy was correct, but ephemeral UUIDs in refs broke after Logseq re-indexed.

### Incremental computation caches (Salsa-style invalidation)

**`src/graph/generational_cache.py`** provides **process-lifetime**, **thread-safe** (`threading.Lock`) memoization keyed by **`frozenset[(relative_path, st_mtime_ns)]`** over participating files — the same **“inputs changed?”** contract as **Salsa**-style incremental compilers (famously **rust-analyzer**): if no participating file’s nanosecond mtime moved, the prior in-memory artifact is **reused**.

- **`cached_build_alias_index`** — Wraps **`build_alias_index`**: **`resolve_logseq_entity`** avoids rebuilding the vault-wide alias map on every call when `pages/**/*.md` mtimes are stable.
- **BM25 corpus** — Pre-tokenized document bags and collection statistics; **`local_query.py`** consumes the cache so ranking over large graphs avoids re-reading and re-tokenizing the world per query.

**`clear_generational_caches()`** exists for tests and hot reload. **`tests/test_ironclad_phase8.py`** asserts alias cache **invalidation** after an mtime bump and BM25 cache identity plus scoring.

### Path Traversal Sandbox Layer (`v1.3.0` — Fortress Release)

**`src/graph/path_sandbox.py`** is the **deterministic gate** for every graph-relative filesystem path before a read, resolve, or atomic write proceeds. No disk mutator or spatial read adapter may trust raw LLM-supplied page names or paths without passing through this layer.

Mechanics:

1. **`resolved_graph_root(graph_root)`** — Canonicalizes `LOGSEQ_GRAPH_PATH` (or an explicit graph root passed to a tool) via `Path.expanduser().resolve(strict=False)`.
2. **`assert_path_within_graph(path, graph_root)`** — Resolves the candidate path and requires `resolved.is_relative_to(root)`. On failure it raises **`ValueError("Security Violation: Path traversal attempt blocked.")`** — an unbypassable error that **`guard_mcp_tool`** maps to LLM-safe tool output without leaking stack traces.
3. **`graph_safe_page_path(graph_root, page_ref)`** — Resolves `pages/Foo.md` or bare page titles under `pages/` only when the resolved absolute path remains inside the graph root (blocks `../../../etc/passwd`, `pages/../../outside.md`, and symlink escape attempts after `resolve()`).

**Consumers:**

| Operation | Module | Sandbox hook |
|-----------|--------|--------------|
| Page resolution | `markdown_blocks.graph_safe_page_path`, `property_line_edit` | Delegates to `path_sandbox` |
| Spatial reads | `matryca_hooks.resolve_logseq_page_md` | Validates every candidate before `is_file()` |
| Atomic commits | `markdown_blocks.atomic_write_bytes` | **Required** `graph_root=`; calls `assert_path_within_graph` before `mkstemp` |
| Template reads | `templates.py` | `_safe_templates_dir` + `assert_path_within_graph` |
| Tmp hygiene | `sweep_dangling_atomic_tmp_files` | Skips files that fail sandbox (defense in depth under `pages/` / `journals/`) |

**Invariant:** The sandbox runs **before** any `read_text`, `open`, or `atomic_write_bytes` temp-file creation. Prompt-injected relative paths cannot escape the designated Logseq graph directory even when an agent hallucinates `pages/` prefixes or `..` segments. **`resolve_graph_relative_key`** rejects `..` path components before resolution; **`Path.resolve()`** follows symlinks so a `pages/evil` symlink pointing outside the graph root still fails **`is_relative_to`** — closing the classic `../` and symlink-escape attack surface.

**L1 memory isolation (`v1.5 Ironclad`):** **`src/agent/l1_memory.py`** restricts session-critical Markdown reads to paths under the operator's **`$HOME`** or the system temp directory. Any attempt to load L1 rules from outside those boundaries returns an empty set — agents cannot exfiltrate arbitrary filesystem paths via `MATRYCA_L1_PATH` injection.

### MCP tool guard and lifespan teardown

**`guard_mcp_tool`** in **`src/agent/mcp_tool_guard.py`** catches domain errors (`ValueError`, `RuntimeError`, **`PathTraversalSecurityError`**, …) and returns concise tool failure strings to the agent — the MCP stdio session stays alive instead of surfacing raw stack traces. Path sandbox violations surface as **`Security Violation: Path traversal attempt blocked.`** without stack leakage.

**Lifespan setup (`v1.4.1`):** When **`LOGSEQ_GRAPH_PATH`** is set, **`app_lifespan`** in **`src/main.py`** canonicalizes the graph root with **`resolved_graph_root`**, then **`os.chdir(str(resolved_root))`** so the process working directory stays inside the sandbox (avoids **`EPERM`** / **`uv_cwd`** failures when MCP hosts or daemons start from arbitrary directories). Startup still sweeps dangling atomic-write temps under that root.

**Lifespan teardown:** **`app_lifespan`** `finally` calls **`clear_page_write_locks()`**, and the loguru→MCP bridge in **`mcp_telemetry.py`** registers **`add_done_callback`** on fire-and-forget tasks so shutdown-time notification failures do not surface as unhandled exceptions.

### Privacy-safe MCP telemetry (`v1.4.1`)

**`src/agent/mcp_telemetry.py`** forwards INFO+ loguru records to the active MCP client via **`Context.info`** during tool calls. Unless **`MATRYCA_DEBUG=true`** (case-insensitive), **`sanitize_log_message`** masks standard UUIDs as **`[CENSORED_UUID]`** and redacts values after **`payload:`**, **`content:`**, **`snippet:`**, and **`query:`** markers as **`[REDACTED_CONTENT]`**. With debug enabled, the bridge preserves full text for operator troubleshooting.

### Background service installer (`v1.4.1`)

**`src/graph/service_manager.py`** implements **`manage_matryca_service(install | uninstall)`**:

| Platform | Unit file | Executable |
|----------|-----------|------------|
| **macOS** | `~/Library/LaunchAgents/com.matryca.logseq.plist` | `matryca-plumber` on `PATH` |
| **Linux** | `~/.config/systemd/user/matryca-logseq.service` | same |

Stdio is appended under **`~/.matryca/logs/`**. Install captures **`LOGSEQ_GRAPH_PATH`** and optional Matryca env vars from the **installing shell**. The CLI exposes this as **`matryca service install|uninstall`** (JSON summary on stdout).

**UX invariant:** The service unit must point at a **stable** binary (for example after **`uv tool install matryca-plumber`** → `~/.local/bin/matryca-plumber`). **`uvx`** uses an ephemeral cache path; a LaunchAgent or systemd unit written from a **`uvx`** invocation will break after cache eviction or reboot.

---

### Layered property grammar and structural guards (Phase 7)

**`src/graph/mldoc_properties.py`** encodes **line-level invariants** aligned with Logseq’s Markdown outliner (and the upstream **`logseq/mldoc`** grammar family), without embedding the Clojure/Rust compiler:

- **`parse_logseq_property_line`** — True **`key:: value`** property lines: rejects list bullets, rejects `#`-first heading noise, requires a colon-free key segment, uses the **first `::` pair only** so values may contain `::` inside `[[wikilinks]]`.
- **`split_logseq_property_list_values`** — Splits comma-separated lists **without** splitting inside **double-quoted** spans (with `\` escapes) or **nested `[[` … `]]`** wikilink depth.
- **`double_quoted_spans_in_value`** — Absolute character intervals for quoted runs so downstream tools (e.g. tag unify) **skip** hashtag normalization inside string literals.

**`src/graph/mldoc_guards.py`** adds **structural predicates** for bullet surgery: fenced code markers, **Org-style drawers** (`:LOGBOOK:`, `:END:`, general `:Name:` tokens), and **`{{` macro** opens. Helpers such as **`pre_id_block_lines_protected`** and **`block_span_has_code_fence_or_drawer`** gate sentence-level splits so indivisible subtrees are never torn apart.

**Integration:** `property_line_edit`, `tag_unify`, `alias_index` flows, and **`split_large_blocks`** consult these modules. **`tests/test_mldoc_phase7.py`** locks wikilink and quote edge cases.

---

## Logseq-native parity layer (Phase 15 — the *WHY*)

Matryca Plumber now mirrors Logseq OG's internal Clojure/Datalog filesystem contract end-to-end. Third-party tools routinely corrupt graphs by treating Markdown as flat text; these mechanisms exist because **Logseq's on-disk grammar is not generic CommonMark** — it is a spatial outliner with encoding rules, property placement invariants, and concurrency assumptions baked into the desktop app's indexer.

### Physical vs. semantic namespaces

Logseq stores **semantic page titles** in memory and wikilinks using `/` as a namespace separator — e.g. `[[Domain/Subdomain/Topic]]`. On disk, Logseq's `:file/name-format :triple-lowbar` rule translates `/` to `___` and then percent-encodes OS-reserved characters via `urllib.parse.quote` so filenames remain valid on **Windows** (where `?`, `#`, `%`, `<`, `>`, `|`, `\`, `"`, `:`, and `*` are forbidden in paths).

**Module:** `src/graph/page_path.py`

| Layer | Example |
|-------|---------|
| **Semantic (in-graph)** | `Domain/Subdomain/Topic` |
| **Physical (on disk)** | `pages/Domain___Subdomain___Topic.md` |
| **Reserved char** | `Topic?` → `Topic%3F.md` |

Every Plumber module that resolves, creates, or scans pages — alias index, dangling healer, auto-split, MasterCatalog — routes through `page_title_to_filename()` / `filename_to_page_title()` instead of naive string concatenation. **Ghost duplicate pages** from path-encoding drift (the classic `Domain/Topic` vs `Domain___Topic` split) are eliminated at the source.

### True frontmatter vs. block properties

Logseq distinguishes two property planes with **different line-level grammar**. Conflating them breaks indexing silently.

#### Page properties (absolute frontmatter)

Page-level metadata (`tags::`, `alias::`, `title::`, …) must appear as **raw `key:: value` lines at the top of the file (line 0 region)** — **without** a leading bullet dash (`- `). A blank line separates frontmatter from the first outliner bullet. Logseq's indexer reads this region as page metadata; prefixing `- ` promotes the line to a block and **drops it from the property index**.

**Module:** `src/graph/page_properties.py` — `inject_page_property()`, `_frontmatter_span()`

#### Block properties (contiguous to parent text)

Block-scoped properties (`id::`, `matryca-plumber:: true`, `collapsed:: true`, …) must sit **immediately after the parent bullet's text**, indented **exactly +2 spaces** relative to the bullet, **before any child bullets**. Orphaning a property below nested children, or deleting an existing `id::`, breaks Logseq's block identity graph and produces dangling `((uuid))` references.

**Enforcement:** `src/graph/mldoc_properties.py` (`is_logseq_block_property_line`), `src/graph/property_line_edit.py` (subtree-scoped surgery), cursor rules in `.cursor/rules/01-core-paradigm.mdc`.

```text
tags:: project, idea          ← page frontmatter (no bullet)

- Parent bullet text
  id:: aaaa-bbbb-…            ← block property (+2 spaces, before children)
  matryca-plumber:: true
  - Child bullet              ← children come after properties
```

### Optimistic concurrency control (OCC)

Local LLM inference is **slow** (seconds to minutes per page). Humans continue typing in Logseq during that window. A naive read-modify-write cycle would **overwrite live edits** with stale LLM output.

**Mechanism:** `read_file_mtime()` → inference → `atomic_write_bytes_if_unchanged()` in `src/graph/markdown_blocks.py`.

1. Before reading page content for inference, record `st_mtime` as `baseline_mtime`.
2. Run LLM inference and assemble the mutation payload.
3. Immediately before `atomic_write_bytes`, call `file_mtime_drifted()` — if `st_mtime` changed, **abort the write** and return `False` (no data loss).
4. If mtime still matches, commit via temp file → `fsync` → `os.replace` under `page_rmw_lock`.

This complements **`fcntl.flock`** serialization (which prevents torn writes between concurrent writers) with **lost-update prevention** (which protects the human editor's in-flight changes). Cognitive modules (`property_hygiene`, `auto_split`, semantic index append) all pass `baseline_mtime` through the OCC gate.

```mermaid
sequenceDiagram
    participant D as Plumber daemon
    participant FS as Page .md on disk
    participant LLM as Local LLM

    D->>FS: read_file_mtime() → baseline
    D->>FS: read_text(encoding=utf-8)
    D->>LLM: structured inference (seconds…)
    Note over FS: User edits in Logseq
    D->>FS: file_mtime_drifted()?
    alt mtime changed
        D-->>D: abort write — no data loss
    else mtime stable
        D->>FS: atomic_write_bytes_if_unchanged()
    end
```

### Alias-aware resolution and case-insensitivity

The **Dangling Healer** and entity consolidation modules must not spawn **ghost duplicate pages** when a canonical page already exists under a different casing or alias spelling.

**Module:** `src/graph/alias_index.py` + `src/graph/master_catalog.py`

1. **`build_alias_index()`** scans all scannable `pages/**/*.md`, parses comma-separated `alias::` / `aliases::` frontmatter, and builds `alias_to_page` + `page_to_aliases` maps.
2. **`normalize_concept_key()`** case-folds candidates before lookup — `Topic`, `topic`, and `TOPIC` resolve to the same canonical title.
3. **`resolve_existing_page_title()`** in `page_path.py` checks on-disk filenames **and** the alias index before any module creates a new page file.
4. **`MasterCatalog`** tracks canonical titles for Phase 1 bootstrap and Phase 2 cognitive routing — alias collisions surface as `collision_notes` instead of silent overwrites.

Combined with **`EXCLUDED_GRAPH_DIR_NAMES`** (`logseq`, `.recycle`, `.git`), backup trees under `logseq/bak/` never enter the alias scan — preventing **Ghost Clones** where stale backup files masquerade as live pages.

### Outliner UX — foldable headings and embed stubs

Logseq's outliner UI treats `- ### Heading` as a **foldable section heading** (bulleted heading), not a flat Markdown H3 inside document body text. Plumber-generated sections use this form so operators can collapse daemon output without leaving the outliner paradigm:

- `- ### Matryca Semantic Index`
- `- ### Matryca Backlink Context`
- `- ### Matryca MARPA Validation`
- `- ### Matryca Structural Lint`

**Auto-split** (`src/agent/plumber_modules/auto_split.py`) extracts dense subtrees (above `MATRYCA_LINT_SPLIT_BLOCK_THRESHOLD` child bullets) into dedicated child pages, then replaces the original subtree with a lightweight stub:

```markdown
- {{embed [[Child Page Title]]}}
```

Embeds preserve **inline readability** on the parent page while keeping files small — the full subtree lives on the child page, transcluded on demand in Logseq. This is strictly **Surgeon Mode** (opt-in via the Trust & Safety drawer) because it restructures block hierarchy.

### Code-block immunity and graph hygiene guards

**Module:** `src/graph/global_fence_scanner.py`

Every scan and mutation pass intersects with **protected line indices**: Markdown fenced code (```), HTML comments (`<!-- … -->`), and Logseq Advanced Query blocks (`#+BEGIN_QUERY` … `#+END_QUERY`). Lines inside an open fence **mask** inner query/comment markers — pasted examples cannot corrupt scanner state.

Additional early-exit guards in `src/agent/plumber_modules/_shared.py`:

- **`is_blank_page_content()`** — 0-byte files and whitespace-only ghost pages skip inference (no crash, no empty index pollution).
- **`is_scannable_graph_markdown()`** — excludes hidden dirs, `logseq/bak/`, `.recycle/`, `.git`, and internal `.matryca_*` caches from alias/catalog/daemon scans.

### UTF-8 I/O hardening (Windows parity)

All graph read/write paths use **`encoding="utf-8"`** explicitly. On Windows, Python's locale-default encoding (`cp1252`) silently mangles Unicode page titles and property values — producing **graph corruption** invisible until Logseq re-index fails. Matryca forces UTF-8 on every `read_text`, `write_text`, and `encode("utf-8")` commit path; decode fallbacks use `errors="replace"` only on **read** paths where corrupted third-party bytes must not crash the daemon.

**Windows CRLF immunity:** fence scanners (`global_fence_scanner.py`), property-line surgery (`property_line_edit.py`), and tag normalization (`tag_unify.py`) normalize line endings via `rstrip("\r\n")` and explicit `\r\n|\n|\r` splitting — mixed or Windows-native files never corrupt protected-line detection or block-span math.

### Multiline block bodies (Shift+Enter continuity)

Logseq allows a single logical bullet to span multiple physical lines (Shift+Enter in the outliner). Continuation lines must be padded to **`parent_bullet_indent + 2 spaces`** so the Datalog indexer keeps them inside the parent block — not as orphan bullets or stray prose.

**Rule:** only the first line carries the `- ` bullet marker; continuation lines are plain text at the deeper indent. Block properties (`id::`, `matryca-plumber::`, …) still sit **immediately after the first bullet line**, at **+2 spaces** relative to the bullet, **before** any continuation body lines or child bullets. Violating this ordering breaks block identity and produces ghost nodes in the graph index.

**Module contract:** `logseq-matryca-parser` (`>=0.3.3`) owns spatial tree reconstruction; Matryca's write path (`append_child_to_node` in `graph_dispatch.py`) preserves indentation invariants on every splice.

### Versioned authorship stamping (`made-by::`)

Every page spawned by Plumber cognitive modules (dangling healer seed pages, auto-split child pages, backlink contexts) is permanently stamped at creation time:

```text
made-by:: matryca plumber v1.5.1
```

**Module:** `src/graph/page_properties.py`

| Mechanism | Detail |
|-----------|--------|
| **`stamp_plumber_authored_page()`** | Idempotently injects `made-by:: matryca plumber v{version}` as true line-0 frontmatter |
| **`get_plumber_version()`** | Resolves the installed package version via `@functools.lru_cache(maxsize=1)` + `importlib.metadata` — zero-latency I/O on hot write paths |
| **`is_plumber_authored_page()`** | Detects versioned stamps (`made-by:: matryca plumber v*`) and legacy `created-by:: plumber` for telemetry |
| **Bilingual authorship** | Page prose may be Italian, English, or mixed; the stamp key is always ASCII `made-by::` per Logseq property grammar |

This stamp is the **on-disk provenance anchor** for dynamic telemetry: the Sovereign UI counts AI-spawned pages by scanning frontmatter, not by guessing from content tone.

---

## Sovereign UI & dynamic graph telemetry (v1.5 Ironclad)

The **Sovereign UI** (`frontend/` → `frontend/dist/`, served by `src/cli/ui_server.py` on `:8000`) is the operator-facing control room. It polls `/api/state` at **1 Hz** via `usePlumberPolling`, rendering daemon progress, token economics, Trust & Safety toggles, and the **`GraphInsightsCard`** Human-vs-AI split.

### The dynamic subtraction invariant (no history database)

Matryca deliberately avoids Postgres, SQLite, or any auxiliary telemetry store. Instead it maintains a **mathematical invariant** reconciling live graph scans with a portable incremental ledger:

$$\text{Human Metrics} = \max\bigl(0,\; \text{Absolute Scanned Metrics} - \text{AI Impact}\bigr)$$

**Module:** `src/graph/graph_analytics.py` → `compute_graph_analytics()`

| Metric | Absolute scan | AI impact source | Human result |
|--------|---------------|------------------|--------------|
| **Pages** | Count scannable `pages/**/*.md` | Pages with `made-by:: matryca plumber v*` (or legacy `created-by:: plumber`) frontmatter | `human_pages = total_pages - ai_pages_on_disk` |
| **Links** | Regex count of `[[WikiLinks]]` across scannable files | `ai_links_injected` from daemon ledger | `human_links = total_links - ai_links_injected` |
| **Blocks healed** | — | `ai_blocks_healed` from daemon ledger | surfaced directly as agent metric |

Link subtraction uses the **historical ledger** because individual wikilinks cannot be tagged at rest. Page subtraction uses **live frontmatter stamps** because each AI-spawned page carries an immutable `made-by::` signature. Together they give operators a real-time **Organic Human Mind vs Plumber Agent Cognition** dashboard without forking truth away from Logseq files.

```mermaid
flowchart LR
  SCAN["Live graph scan\npages + wikilinks"] --> SUB["Dynamic subtraction"]
  LEDGER[".matryca_daemon_state.json\nai_links_injected · ai_blocks_healed"] --> SUB
  STAMP["made-by:: frontmatter\non-disk AI pages"] --> SUB
  SUB --> UI["GraphInsightsCard\nOrganic vs Agent columns"]
  API["GET /api/state"] --> UI
```

A **2-second in-process TTL cache** keyed by `(graph_root, page_count, mtime_ns_revision, ledger_counters)` prevents CPU thrash under 1 Hz polling while still busting when topology changes.

### The local incremental ledger (`.matryca_daemon_state.json`)

The decentralized ledger lives at the **root of each Logseq graph** — it travels with the vault when you copy `pages/` and `journals/`, or when you repoint `LOGSEQ_GRAPH_PATH` to a different graph.

**Module:** `src/agent/maintenance_daemon.py` → `DaemonState`, `save_daemon_state()`, `record_daemon_impact()`

| Field | Role |
|-------|------|
| **`ai_pages_created`** | Cumulative count of pages created by cognitive modules (dangling healer, auto-split, …) |
| **`ai_links_injected`** | Cumulative wikilinks backpropagated or reconnected by Plumber |
| **`ai_blocks_healed`** | Cumulative structured edits (property hygiene, auto-split extractions, semantic lint applies) |
| **`files`** | Per-markdown processing checkpoint (`mtime`, `status`, `error`) — idempotent daemon resume |
| **`session_*_tokens`** | Live token economics mirrored to the UI mid-inference |

Persistence follows the same Ironclad contract as page writes: **`.matryca_daemon_state.json.tmp` → `fsync` → `os.replace` → `.matryca_daemon_state.json.bak`**, with `load_daemon_state()` double-read retry for 1 Hz API readers. See **Atomic filesystem integrity (paranoia level)** below for the full pipeline.

**Zero-config multi-graph:** change `LOGSEQ_GRAPH_PATH` (or open a different graph in `.env`) and restart the daemon — each graph maintains its own ledger file. No migration scripts, no cloud sync of telemetry metadata.

### Related hidden artifacts (graph root)

| File | Role |
|------|------|
| **`.matryca_daemon_state.json`** | Daemon checkpoint + AI incremental ledger |
| **`.matryca_xray_state.json`** | X-Ray alias map (`[n]` → UUID) for MCP/CLI sessions |
| **`.matryca_semantic_cache/`** | Filesystem-backed semantic routing cache + GraphRAG artifacts |
| **`.matryca_plumber_daemon.pid`** | Published PID for operator visibility |
| **`.matryca_plumber_daemon.lock`** | Cross-process exclusive lock — prevents dual-daemon race conditions (POSIX `flock` or Windows `O_EXCL` + `msvcrt.locking`) |

---

## Complete phase evolution history

Use this table as a **mental map** for `src/` and `.github/` — phases are narrative; modules and workflows are what you grep.

| Phase | What shipped | Core architectural reason |
|:-----:|--------------|---------------------------|
| **1 — Baseline** | Headless **`graph_dispatch`** + parser adapter (`FastMCP` **optional** stdio registration, `register_mcp_tools`), **`OutlineNode`**, **`write_logseq_outline`** (DFS headless append), **`read_logseq_page`**, **`lint_logseq_block_refs`**, **`render_logseq_dashboard`** | Prove agents **read spatially** and **write block-by-block** with validation before touching production graphs; MCP is one ingress, not the sole runtime |
| **2 — L1 / L2** | **`read_l1_memory`**, **`routing_hint`** attachments on tool outputs | Session-critical rules and traceability without loading the entire vault into context |
| **3 — PKM refinements** | BM25 and substring **`query_logseq_pages_local`**, structural hops + hubs/orphans, **`patch_logseq_block_property_lines`**, templates list/read, **`lint_matryca_wiki_pages`**, namespace index, **`MATRYCA_GIT_SNAPSHOT_ON_WRITE`** integration | Discovery, **surgical** disk edits, house-style templates, and optional **reversible** checkpoints |
| **4 — Logseq superpowers** | **`inject_logseq_advanced_query`**, journal task scan + append, **`resolve_logseq_entity`**, **`append_logseq_page_alias`** | First-class Logseq primitives (Datalog, journals, entity graph) as disk-native Markdown |
| **5 — Graph gardener** | **`generate_logseq_flashcards`**, **`lint_unify_logseq_tags`**, **`refactor_logseq_blocks`** | Hygiene at scale: SRS cards, tag normalization, structural reparenting |
| **6 — Synthesis engine** | **`resolve_unlinked_mentions`**, **`generate_moc_page`**, **`refactor_large_blocks`**, **`snapshot_logseq_graph_git`** | Graph “thickening,” long-bullet repair, and **manual** operator checkpoints |
| **7 — Mldoc compliance** | **`mldoc_properties.py`**, **`mldoc_guards.py`** integrated into property, tag, alias, and split flows | Eliminate **false positives** on `key::` lines and **destructive edits** inside fragile bodies — auditable Python mirroring **mldoc** intent |
| **8 — Ironclad Shield** | **`global_fence_scanner.py`**, **`atomic_write_bytes`** (+ **`logseq_uuid`** pre-flight on `((uuid))`), **`generational_cache.py`** | **Global** awareness of code, comments, and queries; **no torn writes**; **commit-time** rejection of malformed block refs; **sub-millisecond** hot paths when signatures are stable |
| **9 — Trust plane** | **`quality_gate.py`** (outline and Advanced Query EDN secret scans), wiki lint under **`MatrycaWikiConfig`**, **`synthetic_id` / persist-first** rules in **`SYSTEM_PROMPT.md`**, structured dry-run / apply payloads | Prevent **credential leakage into L2**; enforce **policy** and **UUID referential discipline** without a second datastore |
| **10 — Delivery and community** | **`.github/workflows/ci.yml`** (`uv sync --locked`, Ruff lint + format check, Mypy on `src` + `tests`, Pytest), **`.github/dependabot.yml`** (weekly pip and GitHub Actions bumps), **`.github/workflows/release.yml`** (push tag `v*` → `uv build` → `gh release create`), **`SECURITY.md`**, **`CODE_OF_CONDUCT.md`**, issue and PR templates | **Reproducible quality bar**, **supply-chain hygiene**, **tag-driven artifacts**, and **clear vulnerability and community process** |
| **11 — Fortress (`v1.3.0`)** | **`path_sandbox.py`** (`is_relative_to` graph root), **`mcp_tool_guard`**, lifespan lock/tmp-task teardown | **Adversarial hardening**: block LLM path traversal, graceful MCP shutdown |
| **12 — Headless Revolution (`v1.4.0`)** | Removed **`httpx`** / **`LogseqClient`** / `src/bridge/`; **`graph_dispatch.py`** + **`append_child_to_node`**; **`.matryca_xray_state.json`**; **`get_broken_references()`** lint | **Zero UI dependency**: server-safe automation with a single read/write path on disk |
| **13 — Operational hardening (`v1.4.1`)** | Lifespan **`os.chdir`** to sandbox root; **`mcp_telemetry` privacy sanitizer** (`MATRYCA_DEBUG`); **`service_manager.py`** + CLI **`matryca service`**; **162** strict tests | **Daemon-safe cwd**, **production-safe MCP logs**, **LaunchAgent / systemd** background integration |
| **14 — Ironclad Autonomous Linter OS (`v1.5.x`)** | **`MaintenanceDaemon`**, Instructor **`JSON_SCHEMA`**, Ermes **context compression**, cognitive lint modules, **structural quarantine**, **`semantic_clustering.py`** (Louvain GraphRAG), strict phase separation, **`hierarchical_summarization.py`** (outliner MapReduce), **Context Acceleration Shield** (`llm_context_payload.py`, `prompt_layout.py`), **`ui_server.py`** + **Sovereign UI** React SPA, **`graph_analytics.py`** dynamic Human-vs-AI telemetry, intra-turn telemetry sync, POSIX atomic daemon checkpoints, **`json_repair.py`**, **`prompt_constraints`**, **`reload_plumber_dotenv()`** hot-reload, **`patch_generational_caches_for_paths`** on Plumber writes | **Continuous local graph maintenance** without cloud APIs; monolithic single-server operator UX; fault-tolerant background processing |
| **15 — Logseq-native parity shield (`v1.5.x`)** | **`page_path.py`** namespace encoding (`___` + percent-encode), **`page_properties.py`** true frontmatter vs block property placement + **`made-by::`** versioned stamping, **`atomic_write_bytes_if_unchanged`** optimistic concurrency (mtime guard), **`alias_index.py`** case-insensitive alias resolution + `logseq/bak/` / `.recycle/` exclusion, **`global_fence_scanner.py`** code-block immunity, UTF-8 / CRLF I/O on all graph paths, **`SettingsDrawer.tsx`** Trust & Safety UI (Safe / Augmented / Surgeon tiers), **`auto_split.py`** embed stubs, **`GraphInsightsCard.tsx`** Organic vs Agent telemetry | **100% Datalog filesystem parity** with Logseq OG; zero ghost-clone pages; no data loss during concurrent human + daemon edits; operator-visible mutation guardrails |
| **16 — Enterprise security & concurrency (`v1.5 Ironclad`)** | **Zero-Trust UI API** (`ui_auth.py`, `X-Matryca-Token`), SSRF guards on LM proxy discovery (`ui_server.py`), cross-platform **`subprocess`** daemon launch (`start_daemon_detached`), **`.matryca_plumber_daemon.lock`** exclusivity, paranoia-level ledger pipeline + **`.bak`** recovery, **`PageLockUnavailableError`** lock-skip protocol, **`MATRYCA_ALLOW_FLOCK_DEGRADATION`**, L1 **`$HOME`** path isolation, **`graph_path_validate.py`**, **`json_flock.py`** cross-process JSON checkpoints, **417** strict tests | **Indestructible by default** for r/LocalLLaMA power-users: secure loopback cockpit, Windows-first daemon ops, zero ledger corruption on power loss, no path escape |

**Cross-cutting:** **`src/config.py`**, **`matryca-wiki.yml`**, **`docs/openspec/`**, **`docs/PROJECT_DIARY.md`**, roadmap documents under **`docs/roadmaps/`**.

---

## Component layers

```mermaid
flowchart TB
  subgraph agent["Agent tier"]
    LLM["LLM / orchestrator"]
  end
  subgraph mcp["MCP tier"]
    FM["FastMCP stdio"]
    REG["register_mcp_tools"]
    FM --> REG
  end
  subgraph dispatch["Dispatch tier"]
    MS["MatrycaMCPServer"]
    GD["graph_dispatch.py"]
    AS["alias_state.py\n.matryca_xray_state.json"]
    GS["git_snapshot"]
    QG["quality_gate"]
    MS --> GD
    MS --> AS
    MS --> GS
    MS --> QG
  end
  subgraph data["Data tier (headless)"]
    PAR["logseq_matryca_parser\nread + agent_writer"]
    LOCK["page_rmw_lock"]
    DISK["Graph passes: path_sandbox · fence · mldoc · atomic_write · logseq_uuid · generational_cache"]
  end
  subgraph ops["Phase 10 — delivery"]
    CI["GitHub Actions"]
    DB["Dependabot"]
    REL["Release workflow"]
  end
  LLM <--> FM
  REG --> MS
  GD --> PAR
  GD --> LOCK
  MS --> DISK
  CI -.->|gates| mcp
  DB -.->|updates deps| ops
  REL -.->|wheels/sdists| ops
```

---

## Key entry points

| Path | Role |
|------|------|
| `src/main.py` | FastMCP app, lifespan (`chdir` to graph root), `register_mcp_tools` |
| `src/cli.py` | Agent-native CLI (`read`, `search`, `mutate`, `refactor`, `lint`, `service`) |
| `src/agent/mcp_telemetry.py` | Loguru→MCP bridge with optional privacy sanitization |
| `src/graph/service_manager.py` | LaunchAgent / systemd user-unit install and uninstall |
| `src/agent/mcp_server.py` | All `@mcp.tool()` handlers, `OutlineNode`, `MatrycaMCPServer` |
| `src/agent/graph_dispatch.py` | Headless CRUD dispatch: `_headless_write_outline`, mega-tool routing |
| `src/agent/alias_state.py` | X-Ray `SessionAliasRegistry` load/save at `.matryca_xray_state.json` |
| `src/graph/page_write_lock.py` | Per-page `page_rmw_lock` for serialized AST splices |
| `src/graph/path_sandbox.py` | Graph-root path sandbox (`is_relative_to`); blocks traversal before any FS read/write |
| `src/agent/mcp_tool_guard.py` | Outer MCP tool error boundary (LLM-safe failures) |
| `src/agent/git_snapshot.py` | Optional commits on graph root |
| `src/agent/quality_gate.py` | Outline and Advanced Query EDN pre-flight scans |
| `src/rag/matryca_hooks.py` | Parser adapter for spatial reads |
| `src/graph/page_path.py` | Logseq semantic title ↔ on-disk filename translation (`/` → `___`, percent-encoding) |
| `src/graph/page_properties.py` | True page frontmatter injection (line 0, no bullet prefix); **`stamp_plumber_authored_page()`** versioned `made-by::` |
| `src/graph/markdown_blocks.py` | `atomic_write_bytes` / `atomic_write_bytes_if_unchanged` (OCC mtime guard), block line helpers, `graph_safe_page_path` |
| `src/graph/logseq_uuid.py` | UUID v4/v5 shape checks; `assert_valid_block_refs_in_markdown` for atomic writes |
| `src/graph/block_ref_lint.py` | Vault-wide `((uuid))` ↔ `id::` integrity via `LogseqGraph.get_broken_references()` |
| `src/graph/global_fence_scanner.py` | Whole-page dead-zone line index |
| `src/graph/mldoc_properties.py` / `mldoc_guards.py` | Phase 7 property grammar + structural shields |
| `src/graph/generational_cache.py` | Phase 8 mtime-keyed alias + BM25 corpus memoization |
| `src/rag/local_query.py` | BM25 query path (cached corpus when graph root is set) |
| `.github/workflows/ci.yml` | Ruff, Mypy, Pytest on `main` |
| `.github/workflows/release.yml` | Tag-driven build and GitHub Release |
| `.github/dependabot.yml` | Automated dependency PRs |
| `src/agent/maintenance_daemon.py` | Matryca Plumber daemon cycle, semantic index append, structural quarantine |
| `src/agent/plumber_config.py` | Env-driven cognitive lint flags (`load_plumber_lint_config`) |
| `src/agent/plumber_modules/` | Env-gated cognitive plugins (MARPA, dangling healer, property hygiene, …) |
| `src/agent/context_compressor.py` | Ermes-inspired epistemic condensation (100k trigger) |
| `src/agent/prompt_constraints.py` | Cross-lingual output constraint for Plumber LLM prompts |
| `src/agent/prompt_layout.py` | Cache-aligned prompt builder (`build_cache_aligned_prompt`) for KV prefix reuse |
| `src/agent/llm_context_payload.py` | Phase 1 summary substitution + semantic skeleton for giant-page LLM payloads |
| `src/cli/ui_server.py` | FastAPI + Uvicorn monolith for `matryca plumber status` — REST API + **Sovereign UI** React SPA on `:8000`; Zero-Trust auth + SSRF guards |
| `src/cli/ui_auth.py` | UI Bearer token resolution (`MATRYCA_UI_TOKEN` or runtime generation) and constant-time verification |
| `src/graph/graph_analytics.py` | Live graph telemetry — dynamic Human-vs-AI subtraction for `GraphInsightsCard` |
| `frontend/` | **Sovereign UI** React cockpit (Vite build → `frontend/dist/`); Light/Dark theme; Tauri-ready layout |
| `src/graph/hierarchical_summarization.py` | Outliner-native MapReduce chunking for giant pages (Phase 1 bootstrap) |
| `src/utils/json_repair.py` | Lenient JSON repair for local LLM grammar leakages |

---

## Matryca Plumber — autonomous maintenance daemon (Phase 14)

**Matryca Plumber** is a **local-first background structure & cognitive maintenance daemon** that progressively indexes Logseq markdown with a **local LLM** (LM Studio OpenAI-compatible API). It is the **primary autonomous runtime**: duty-cycle scans, cognitive lint, semantic index append, and ledger checkpoints do **not** depend on an MCP host. Each cycle may repair broken block references (when enabled), flush context loops via Ermes compression, append semantic metadata, run optional cognitive lint modules, and stream structured telemetry to **`logs/matryca_plumber_ops.log`**. On-demand **CLI** invocations and **optional FastMCP** sessions share the same **`graph_dispatch`** mutation plane for interactive edits.

> **Brand separation:** **Matryca Brain** is reserved exclusively for the Nuitka-compiled Pro enterprise ingestion suite. The open-source maintenance daemon described here is **Matryca Plumber**.

State checkpoint: **`.matryca_daemon_state.json`** at the graph root (per-file `mtime`, `processed` / `skipped` / `error`). Process exclusivity: **`.matryca_plumber_daemon.lock`** + **`.matryca_plumber_daemon.pid`**.

### Cross-platform process management (`v1.5 Ironclad`)

Early Plumber builds detached the daemon with UNIX-only **`os.fork()`** — a pattern that **cannot run on Windows** and complicates signal handling on modern macOS sandboxes. **v1.5 Ironclad** replaces this with a robust **`subprocess.Popen`** launcher in **`start_daemon_detached()`** (`src/agent/maintenance_daemon.py`):

```python
subprocess.Popen(
    [sys.executable, "-m", "src.cli", "plumber", "start", "--foreground"],
    cwd=str(_REPO_ROOT),
    env=env,  # LOGSEQ_GRAPH_PATH injected
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,  # detach from terminal session
    close_fds=True,
)
```

The parent polls **`read_pid_file()`** for up to 15 seconds; the foreground worker acquires the exclusive daemon lock and publishes its PID. This yields **identical semantics on Windows, macOS, and Linux** without fork-specific edge cases.

#### Exclusive `.lock` file — dual-daemon prevention

A stale PID file alone cannot guarantee single-instance safety (crashed processes leave orphan PIDs). Matryca therefore acquires **`.matryca_plumber_daemon.lock`** at daemon bootstrap:

| Platform | Mechanism | Module |
|----------|-----------|--------|
| **POSIX** | `fcntl.flock(LOCK_EX \| LOCK_NB)` on lock sidecar | `_try_acquire_daemon_process_lock()` |
| **Windows** | `O_CREAT \| O_EXCL` create + `msvcrt.locking(LK_NBLCK)` | `_try_acquire_daemon_process_lock_windows()` |

If the lock is held by a live Plumber process, **`start_daemon_detached()`** returns **`already_running`**; if held by a dead PID, the lock file is reclaimed. Graceful shutdown (`SIGTERM`/`SIGINT`) releases flock descriptors and unlinks the lock sidecar before exit.

```mermaid
flowchart TD
  START["matryca plumber start"] --> SUB["subprocess.Popen\n--foreground worker"]
  SUB --> LOCK{"Acquire\n.matryca_plumber_daemon.lock"}
  LOCK -->|held by live PID| FAIL["exit: already running"]
  LOCK -->|acquired| RUN["MaintenanceDaemon.run_forever()"]
  RUN --> PUB["write .matryca_plumber_daemon.pid"]
```

### Sovereign UI — monolithic FastAPI + React SPA

The legacy Rich terminal canvas is **deprecated**. **`matryca plumber status`** and **`matryca plumber ui`** invoke **`run_ui_server()`** in **`src/cli/ui_server.py`**, which binds **Uvicorn** on **`127.0.0.1:8000`** and serves the **Sovereign UI**:

| Surface | Path | Role |
|---------|------|------|
| **Daemon checkpoint + telemetry** | `GET /api/state` | Reads `.matryca_daemon_state.json` via `load_daemon_state()`; attaches live `graph_analytics` from `compute_graph_analytics()` |
| **Ops log tail** | `GET /api/logs` | Last 50 JSONL lines from `TokenLogger` |
| **Hardening thresholds** | `GET /api/config` | Live `PlumberLintConfig` (thermal delays, MapReduce char buckets) |
| **React SPA** | `/` | Static assets from `frontend/dist/` when built (`npm run build`); Light/Dark theme via `next-themes` |

The React hook **`usePlumberPolling`** polls `/api/state`, `/api/logs`, and `/api/config` at **1 Hz**. **`GraphInsightsCard`** renders the Organic Human Mind vs Plumber Agent Cognition columns. CORS allows local Vite dev (`:5173`) and future **Tauri** packaging (`tauri://localhost`). One process, one port — no split between “API server” and “dashboard server.”

#### Zero-Trust local API authentication (`v1.5 Ironclad`)

Even on loopback, Matryca treats the Sovereign UI as an **untrusted client surface** — any other local process must not drive daemon lifecycle or read telemetry without proof of possession.

| Component | Module | Contract |
|-----------|--------|----------|
| **Token resolution** | `src/cli/ui_auth.py` → `resolve_ui_token()` | Reads **`MATRYCA_UI_TOKEN`** from env, or generates **`secrets.token_urlsafe(32)`** once per Uvicorn process |
| **Session bootstrap** | `GET /api/auth/session` | Returns `{ "token": "…" }` — the React SPA caches this and attaches it to every subsequent request |
| **Request gate** | `_require_ui_token()` dependency | Validates **`X-Matryca-Token`** header via constant-time compare; missing/invalid → **401** |
| **Protected routes** | `/api/state`, `/api/logs`, `/api/config`, daemon control | All require Bearer token except `/api/auth/session` and static SPA assets |

**`frontend/src/hooks/usePlumberPolling.ts`** resolves the token on first poll, then sends **`X-Matryca-Token`** on every 1 Hz fetch — the daemon inference loop never blocks on auth, but the control plane is cryptographically gated.

```mermaid
sequenceDiagram
    participant SPA as React SPA
    participant API as FastAPI :8000
    participant DAEMON as MaintenanceDaemon

    SPA->>API: GET /api/auth/session
    API-->>SPA: { token }
    loop 1 Hz poll
        SPA->>API: GET /api/state + X-Matryca-Token
        API->>API: verify_ui_token()
        API->>DAEMON: load_daemon_state (read-only)
        API-->>SPA: checkpoint + graph_analytics
    end
```

#### SSRF guards on LM proxy discovery

The Settings drawer allows operators to point model discovery at alternate LM Studio endpoints. Because the UI server performs server-side HTTP fetches, **`src/cli/ui_server.py`** implements a **defense-in-depth SSRF filter** before any outbound request:

1. **Hostname blocklist** — hard-deny **`169.254.169.254`**, **`metadata.google.internal`**, and other cloud metadata hosts.
2. **IPv4-mapped normalization** — **`::ffff:169.254.169.254`** is canonicalized to IPv4 before classification, closing the IPv6-mapped bypass.
3. **DNS resolution audit** — `socket.getaddrinfo()` resolves every A/AAAA record; any answer in link-local, multicast, reserved, unspecified, or non-loopback private ranges is rejected unless the hostname is explicitly **`localhost` / `127.0.0.1` / `::1`** or matches the configured **`MATRYCA_LM_BASE_URL`** host.
4. **Allowlist default** — only loopback hosts and the operator-configured LM base URL pass **`_validate_lm_models_base_url()`**.

This prevents a compromised browser tab or malicious local process from using the Matryca UI as a springboard to scrape AWS/GCP instance metadata or scan the LAN — even though inference itself remains local-first.

```mermaid
flowchart TD
  REQ["GET /api/lm/models?base_url=…"] --> PARSE["urlparse host"]
  PARSE --> BL{"Blocked host?\n169.254.169.254 · metadata.*"}
  BL -->|yes| DENY["400 host not allowed"]
  BL -->|no| DNS["getaddrinfo → normalize IPv4-mapped"]
  DNS --> IP{"Any blocked IP class?"}
  IP -->|yes| DENY
  IP -->|no| ALLOW["resolve_llm_base_url → fetch models"]
```

```mermaid
flowchart LR
  CLI["matryca plumber status"] --> UV["Uvicorn :8000\nui_server.py"]
  UV --> API["/api/state · /api/logs · /api/config"]
  UV --> SPA["React SPA\nfrontend/dist"]
  DAEMON["MaintenanceDaemon\nbackground"] --> SAVE["save_daemon_state\nPOSIX os.replace"]
  SAVE --> JSON[".matryca_daemon_state.json"]
  API --> LOAD["load_daemon_state\ndouble-read retry"]
  LOAD --> JSON
  SPA -->|1 Hz poll| API
```

### Structural hardening matrix (Phase 14 shields)

Three engineering shields close the last production gaps between “works in foreground debug” and “runs for weeks on a 3,000+ page graph while a browser polls state at 1 Hz.”

#### Outliner-native hierarchical MapReduce chunking

**Problem:** Naive character- or token-based text splitting is **destructive** for Logseq outliners. Fixed-width cuts shatter parent-child nesting trees, tear `key:: value` property syntax, and bisect Markdown code fences — producing summaries that hallucinate structure the parser never saw.

**Solution:** **`src/graph/hierarchical_summarization.py`** implements a deterministic **root-level indentation tree compiler**:

1. **`_split_root_trees`** — partitions page bytes on **root bullet boundaries** (`^[-*+]\s+` at column 0); child lines stay attached to their parent subtree.
2. **`chunk_outliner_content`** — packs whole subtrees into bounded buckets (default **`MATRYCA_PLUMBER_MAPREDUCE_CHUNK_CHARS=15_000`**) without splitting a tree mid-block.
3. **`mapreduce_harvest_page_summary`** — when `len(content) > mapreduce_trigger_chars` (default **25_000**), runs a **Map → Reduce** flow: each chunk gets an isolated `harvest_page_summary` call with **`reset_execution_history()`** between chunks, then a final reduce pass merges partial summaries and tag sets.

This isolates Gemma 4’s attention window per chunk, prevents cross-chunk KV-cache contamination, and keeps local prefill latency bounded on megabyte-class outliner pages.

```mermaid
flowchart TD
  PAGE["Giant Logseq page\n(root bullet forest)"] --> SPLIT["_split_root_trees\natomic subtrees"]
  SPLIT --> PACK["chunk_outliner_content\n15K char buckets"]
  PACK --> MAP["Map: harvest_page_summary\nper chunk + history reset"]
  MAP --> REDUCE["Reduce: consolidate partials\nsingle page summary"]
  REDUCE --> INDEX["BootstrapSummaryResult\n→ Master Index"]
```

#### Intra-turn telemetry sync engine

**Problem:** Cognitive sub-modules (link backpropagation, semantic routing, MARPA lint) instantiated their own **`TokenLogger`** instances. Usage accumulated in submodule memory but **never reached** `.matryca_daemon_state.json` — the React cockpit showed stale zero counters while JSONL logs contained the real spend (“isolated token logging” blindspot).

**Solution:** **`MaintenanceDaemon._ensure_shared_token_logger()`** rebinds the central logger onto **`InstructorLLMClient`** at construction and every **`_sync_runtime_config()`** cycle. **`_absorb_token_logger_delta()`** merges prompt/completion deltas from submodule loggers after **`run_cognitive_lint_pipeline()`**. **`_sync_live_telemetry()`** mirrors in-memory totals onto **`DaemonState`**, and **`_save_cycle_checkpoint()`** flushes to disk **immediately before and after** active LLM inference blocks via **`save_daemon_state()`** — so 1 Hz FastAPI readers observe monotonic session counters even mid-file.

```mermaid
sequenceDiagram
    participant D as MaintenanceDaemon
    participant TL as Central TokenLogger
    participant SUB as Cognitive submodule
    participant DISK as .matryca_daemon_state.json

    D->>TL: _ensure_shared_token_logger()
    D->>DISK: _save_cycle_checkpoint (pre-inference)
    D->>SUB: run_cognitive_lint_pipeline()
    SUB->>TL: isolated usage (via shared client)
    D->>D: _absorb_token_logger_delta()
    D->>DISK: _save_cycle_checkpoint (post-lint)
    D->>D: index_page LLM call
    D->>DISK: _save_cycle_checkpoint (post-inference)
```

#### POSIX-atomic write-and-replace daemon checkpoints (paranoia level)

**Problem:** Standard non-atomic truncation (`open(path, "w")`) on `.matryca_daemon_state.json` exposes a torn-write window. Concurrent **1 Hz** frontend pollers calling **`json.loads`** during truncation raised transient **`JSONDecodeError`** flakes — decoupling disk persistence from parallel Web API readers. A sudden power loss mid-write could corrupt the incremental AI ledger.

**Solution:** **`save_daemon_state()`** implements the full **paranoia-level** commit pipeline shared with page writes, plus a hot backup tier:

1. **`tempfile.mkstemp`** in the graph root with prefix **`.matryca_daemon_state.json.`** and suffix **`.tmp`** — guarantees **`os.replace`** stays on one filesystem volume.
2. **Write full JSON payload**, **`flush()`**, then **`os.fsync(fileno)`** — data reaches durable media before any live inode references the new bytes.
3. **`os.replace(tmp, final)`** — POSIX **atomic** inode swap; concurrent readers observe either the previous complete checkpoint or the next one — never a torn JSON document.
4. **`shutil.copy2(final, .matryca_daemon_state.json.bak)`** — human-visible recovery sibling; **`load_daemon_state()`** auto-restores from **`.bak`** when the primary file is corrupt or empty.
5. **`cross_process_json_flock()`** (`src/graph/json_flock.py`) — exclusive **`.matryca_daemon_state.json.flock`** sidecar serializes concurrent writers (daemon checkpoint + UI control actions) on POSIX; Windows skips flock but retains atomic replace.

Markdown page writes use the same core discipline in **`atomic_write_bytes`** (`src/graph/markdown_blocks.py`), with an additional **parent-directory `fsync`** after replace (directory metadata durability) and optional **`.bak`** siblings on property-style mutators.

```mermaid
flowchart LR
  PAYLOAD["DaemonState JSON"] --> MK["mkstemp in graph root"]
  MK --> W["write + flush + fsync"]
  W --> R["os.replace → live checkpoint"]
  R --> BAK["copy2 → .bak sibling"]
  R --> READERS["1 Hz API pollers\nalways valid JSON"]
```

**`load_daemon_state()`** pairs this with a **defensive double-read fallback**: **`_read_daemon_state_payload()`** retries once on empty or malformed reads (catching the microsecond race if a reader opens mid-replace). Corrupt primary + corrupt backup self-heal to a fresh **`DaemonState()`** with a logged **`[METADATA CORRUPTION DETECTED]`** warning.

Markdown page writes continue to use **`atomic_write_bytes`** in **`markdown_blocks.py`**; daemon state uses the same **write → fsync → replace → backup** contract without sharing the page-level `((uuid))` pre-flight guard.

### Why these mechanisms exist

#### Atomic RMW with OS-level locking (`fcntl.flock`)

Matryca Plumber runs **concurrently** with human edits in Logseq OG and with MCP tool sessions on the same files. Without serialization, read-modify-write cycles (read page → LLM lint → append index → `os.replace`) would interleave and produce torn writes or lost bullets.

**`page_rmw_lock(path)`** (`src/graph/page_write_lock.py`) acquires an exclusive **`fcntl.flock`** on a sidecar lock file beside each page. The Plumber daemon, MCP mutators, and cognitive modules all acquire this lock before mutating a page — **transactional immutability** at the file grain.

**Cloud-sync degradation:** iCloud, Dropbox, and OneDrive often reject or noop **`flock`**. By default Matryca **fails closed** — raising **`PageLockUnavailableError`** so the daemon skips the file rather than writing without cross-process safety. Operators on synced vaults may set **`MATRYCA_ALLOW_FLOCK_DEGRADATION=true`** (at-your-own-risk) to fall back to in-process thread locking only.

**Strict lock-skip protocol:** when **`PageLockUnavailableError`** fires (e.g., Logseq holds the file open, or flock retries exhaust), **`MaintenanceDaemon._process_one_file()`** logs a structural warning, **does not mutate the page**, **does not update the per-file ledger entry**, and leaves the file **pending for the next poll cycle** — zero torn writes, zero false "processed" states.

#### Incremental cache patching (`patch_generational_caches_for_paths`)

Rebuilding the full BM25 token corpus and alias index after every single-block edit would **CPU-thrash** large graphs. After each Plumber write, **`patch_generational_caches_for_paths`** surgically updates only the touched relative paths inside the in-memory generational caches — preserving Salsa-style `st_mtime_ns` invalidation without vault-wide rescans.

#### Native grammar-based sampling (`JSON_SCHEMA`)

Compact local weights (Gemma 4, Qwen 7B/9B) waste tokens on conversational JSON preambles. **`InstructorLLMClient`** defaults to **`MATRYCA_LM_INSTRUCTOR_MODE=JSON_SCHEMA`**, binding LM Studio grammar sampling so structured fields (`SemanticIndexResult`, `MarpaClassificationResult`, …) fill immediately. **`MATRYCA_LM_INSTRUCTOR_FALLBACK=MD_JSON`** provides a secondary extraction mode when schema binding fails — parsing errors drop to near zero in production graphs.

#### Ermes-inspired context compressor

Long multi-turn maintenance loops inflate the local **KV cache** and degrade attention (“needle in a haystack”). When estimated history tokens exceed **`MATRYCA_PLUMBER_COMPRESSION_TRIGGER_TOKENS`** (default **100,000**), **`condense_messages`** collapses intermediate turns into a dense **`## Consolidated Epistemic State`** summary targeting **`MATRYCA_PLUMBER_COMPRESSION_TARGET_TOKENS`** (default **30,000**). The system prompt and trailing user turn are preserved; compression LLM failures fall back to truncation — the daemon never exits.

#### Thermal pacing & hardware protection shield

Continuous local inference on consumer laptops saturates GPU/NPU heatsinks and triggers thermal throttling. **Thermal Pacing** (duty-cycle modulation) inserts configurable cooling delays after each LLM inference event:

| Phase | Env var | Default | Injection site |
|-------|---------|---------|----------------|
| **Phase 1 — bootstrap harvest** | `MATRYCA_THERMAL_DELAY_BOOTSTRAP` | **2.0 s** | After each atomic `harvest_page_summary` turn in `mapreduce_harvest_page_summary()` (per MapReduce chunk + reduce) |
| **Phase 2 — cognitive cycle** | `MATRYCA_THERMAL_DELAY_COGNITIVE` | **2.0 s** | After each atomic completion in `InstructorLLMClient` (indexing, lint modules, context compression) |

Set either variable to **`0`** to disable pacing for that phase. Defaults preserve battery longevity and zero-throttling execution on Apple Silicon and discrete-GPU workstations without sacrificing data quality.

#### Low-Impact Antivirus Mode (Sympathetic Background Scheduling)

Consumer-edge deployments must not compete with the user’s interactive session. **Low-Impact Antivirus Mode** delegates CPU scheduling to the OS: at detached daemon bootstrap (`start_daemon_detached`), when **`MATRYCA_PLUMBER_LOW_PRIORITY_MODE=true`** (default), the grandchild process calls **`os.nice(19)`** — maximum POSIX courtesy tier.

| Mechanism | Detail |
|-----------|--------|
| **Trigger** | Once, before `MaintenanceDaemon.run_forever()` in the detached fork |
| **Env var** | `MATRYCA_PLUMBER_LOW_PRIORITY_MODE` (default **`true`**) |
| **Effect** | Kernel deprioritizes Plumber threads while foreground apps hold the run queue; idle periods reclaim full CPU for backlog drain (e.g. Phase 1 bootstrap over thousands of pages) |
| **Disable** | Set env to **`false`** on headless inference nodes where normal priority is desired |

Unlike application-level “is the user typing?” heuristics, this path adds **zero** polling overhead — Darwin/BSD schedulers already arbitrate between niceness classes. Combined with **Thermal Pacing**, the Plumber behaves like a modern antivirus or Spotlight indexer: present, persistent, and imperceptible during active use.

#### Fault-tolerant block reference quarantine

Human typo'd `((uuid))` tokens must not crash the daemon. **`run_cycle()`** preflights each page with **`find_malformed_block_refs`**. On failure:

1. Log **`[STRUCTURAL LINT WARN]`** to the ops log.
2. Mark the file **`skipped`** (stable `mtime` — no retry loop).
3. Append **`### Matryca Structural Lint`** with `#todo [[Matryca Broken Reference]]` (write uses **`validate_block_refs=False`** — zero-destruction of existing prose).
4. Continue the poll loop — **100% daemon uptime**.

#### IP protection (OSS vs Pro)

Open source exposes **MARPA domain taxonomy**, dangling-link healing, property hygiene, semantic routing cache, and core semantic lint via **Matryca Plumber**. **Pavlyshyn bipartite graph validation**, **Twin Ingestion**, and **Epistemic Guardian** are structurally omitted from OSS — reserved for **Matryca Brain**, the Nuitka-compiled commercial tier (see **`docs/PROJECT_DIARY.md`**).

### Plumber lifecycle (data plane)

```mermaid
flowchart TD
  SCAN["Scan directory\niter_alias_source_paths"] --> PENDING["list_pending_files\n(settled: processed + skipped)"]
  PENDING --> READ["Read page UTF-8"]
  READ --> PRE{"Preflight\nfind_malformed_block_refs"}
  PRE -->|malformed| QUAR["Quarantine\nlog STRUCTURAL LINT WARN\nappend Structural Lint section\nstatus=skipped"]
  PRE -->|ok| COG{"Cognitive modules\nenv-gated pipeline"}
  COG --> MARPA["MARPA taxonomy\n(optional)"]
  MARPA --> LLM["Instructor structured inference\nJSON_SCHEMA → MD_JSON fallback"]
  LLM --> COOL["thermal_delay_cognitive\napply_thermal_pause_cognitive"]
  COOL --> ERMES{"Token estimate\n> compression trigger?"}
  ERMES -->|yes| COMP["Ermes condensation\nConsolidated Epistemic State"]
  ERMES -->|no| APPLY
  COMP --> APPLY["apply_semantic_page_result\npage_rmw_lock + atomic_write_bytes"]
  APPLY --> PATCH["patch_generational_caches_for_paths"]
  PATCH --> STATE["Update .matryca_daemon_state.json"]
  QUAR --> STATE
  STATE --> SLEEP["sleep MATRYCA_PLUMBER_POLL_SECONDS"]
  SLEEP --> SCAN
```

### Hardening mechanisms (latest optimization pass)

| Vector | Implementation | Rationale |
|--------|----------------|-----------|
| **Outliner MapReduce** | `hierarchical_summarization.py` — root-tree chunking at 15K char buckets, history reset per chunk | Preserve Logseq nesting; bound Gemma 4 attention / KV-cache on giant pages |
| **Intra-turn telemetry** | `_ensure_shared_token_logger`, `_absorb_token_logger_delta`, `_save_cycle_checkpoint` before/after LLM | Unify token counters for cockpit + checkpoint plane |
| **Atomic daemon checkpoints** | `save_daemon_state` tmp + fsync + `os.replace`; `load_daemon_state` double-read retry | Eliminate JSONDecodeError flakes under 1 Hz API polling |
| **Lenient JSON repair** | `json_repair.py` — fence strip, bracket balance, double-escape heal, trailing garbage trim | Recover structured output from local LLM grammar leakages |
| **Memory history cap** | `MAX_EXECUTION_HISTORY_MESSAGES = 48` in `context_compressor.py`; Instructor client trims `_execution_history` after each page | Constant memory across infinite daemon uptime |
| **HTTP / compression fallbacks** | `condense_messages` try/except → truncation; compression warnings logged as `[COMPRESSION WARN]` | LM Studio offline must not kill the child process |
| **Idempotent backlink deduplication** | `run_backlink_backpropagator` + semantic lint skip reasons (`no_change`, `uuid_not_found`) | Prevents duplicate wikilink injection on re-runs |
| **Token estimate safety buffer** | `TOKEN_ESTIMATE_SAFETY_MULTIPLIER = 1.12` on Qwen/Gemma heuristic | Triggers compression before VRAM cliff on code/CJK-heavy pages |
| **Thermal pacing shield** | `apply_thermal_pause_*` in `plumber_config.py`; bootstrap pauses in MapReduce map/reduce loops; cognitive pauses in `InstructorLLMClient._completion_with_structured_output` | Breaks intra-file micro-cycle heat spikes (chunk lint, entity routing, compression) |
| **Low-Impact Antivirus Mode** | `MATRYCA_PLUMBER_LOW_PRIORITY_MODE` (default **`true`**); `os.nice(19)` at detached bootstrap | Kernel-level CPU courtesy — zero polling; full throughput during idle |
| **Context Acceleration Shield** | `llm_context_payload.py` + `prompt_layout.py` — Phase 1 summary substitution, semantic skeleton fallback, cache-aligned prompt prefix | Bound prefill on 5,000+ block pages; maximize llama.cpp KV-cache hits across consecutive Phase 2 operations |

---

## TRIZ-Driven Inventive Architecture for Context Acceleration

Matryca Plumber Phase 2 executes multiple consecutive LLM operations per page — MARPA classification, dangling-link seeding, entity consolidation, property hygiene, and semantic indexing. On production graphs containing **5,000+ block outliner pages**, each operation previously forced LM Studio (llama.cpp) to recompute a massive prompt prefill, invalidating the GPU **KV-cache** and stalling inference for minutes per file.

### The technical contradiction

| Improve (Parameter A) | Worsen (Parameter B) |
|-----------------------|----------------------|
| **Deep semantic contextual processing** — Phase 2 linting and routing require rich page context for accurate entity topology, alias consolidation, and MARPA taxonomy | **Local inference throughput** — massive graph notes exceed practical context windows, invalidate KV-cache prefixes, and explode GPU prefill latency |

Classic engineering would trade quality for speed (truncate blindly) or speed for quality (send full raw text every turn). The **Context Acceleration Shield** resolves this contradiction without sacrificing semantic resolution by applying four **TRIZ Inventive Principles** in combination.

### TRIZ Principle 10 — Preliminary Action (Pre-Action)

**Module:** `src/agent/llm_context_payload.py` → `prepare_llm_context_payload()`

Phase 1 bootstrap already distills giant pages via outliner-native MapReduce (`hierarchical_summarization.py`) and persists dense abstracts in `master_catalog.json` and each page's `### Matryca Semantic Index` block. Principle 10 applies that work **before** Phase 2 demands it:

- When `len(content) > mapreduce_trigger_chars` (default **25,000**), Phase 2 cognitive modules receive the **pre-computed hierarchical summary** instead of raw or flat-truncated text.
- Token mass drops by up to **~90%** while high-level entity topology (domain, tags, summary prose) remains intact.
- Disk mutations (property injection, alias lines, seed page creation) still operate on **raw on-disk content** — only the LLM inference payload is compressed.

### TRIZ Principle 5 — Merging / Combination (Prefix Alignment)

**Module:** `src/agent/prompt_layout.py` → `build_cache_aligned_prompt()`

llama.cpp **Prompt Caching** reuses KV tensors when consecutive requests share an identical token prefix. Phase 2 previously placed dynamic task instructions **before** page content, mutating the prefix on every sub-operation and forcing full prefill recomputation.

Principle 5 enforces an **invariant prefix layout** across all Phase 2 LLM calls:

```text
[SYSTEM_PROMPT]  +  [STABLE_PAGE_CONTENT_OR_SUMMARY]  +  [DYNAMIC_TASK_INSTRUCTION]
```

Implementation rules:

1. **System prompt** — role, safety rules, cross-lingual constraint (`finalize_system_prompt`).
2. **User message body** — stable page payload first (`Page content:\n…`), derived once per file via `prepare_llm_context_payload()`.
3. **Dynamic tail** — task-specific fields (MARPA domain catalog, tag keys, entity pair titles, seed link targets) appended **last**.

Consecutive operations on the same file therefore share the heaviest token prefix. LM Studio logs show **`[Prompt Cache Hit]`** on turns 2–6; sequential prefill latency drops to **~0 s** after the first operation.

```mermaid
flowchart LR
  subgraph prefix["Shared KV-cache prefix"]
    SYS["System prompt"]
    BODY["Page content / Phase 1 summary"]
  end
  subgraph tail["Per-operation suffix"]
    T1["MARPA classify task"]
    T2["Entity overlap task"]
    T3["Property infer task"]
  end
  SYS --> BODY
  BODY --> T1
  BODY --> T2
  BODY --> T3
```

**Semantic indexing nuance:** `_build_index_prompt()` passes the compressed `llm_body` for reading while `_enumerate_blocks_for_prompt()` still derives the block UUID catalog from **raw content** — preserving per-block `semantic_corrections` accuracy.

### TRIZ Principle 2 — Taking Out / Separation

**Module:** `src/agent/llm_context_payload.py` → `extract_semantic_skeleton()`

When a giant page lacks a complete Phase 1 summary (stale catalog row, interrupted bootstrap), Principle 2 **separates signal from noise** at zero LLM cost:

- A lightweight regex pass retains only lines carrying active information: `[[WikiLinks]]`, inline `#tags`, and `key:: value` property lines.
- Conversational prose, filler bullets, and non-structural narrative are stripped before inference.
- Payload size typically shrinks **~85%** while link/tag/property topology — the inputs entity consolidation and property hygiene actually need — survives intact.

Fallback order: **raw** (under threshold) → **summary** (catalog or on-page index) → **skeleton** → **truncated** raw cap.

### TRIZ Principle 15 — Dynamicity

**Module:** `src/agent/plumber_config.py` → `reload_plumber_dotenv()`

Detached daemon forks inherit an arbitrary working directory. A blind `load_dotenv()` without an explicit repository root caused the child process to **miss thermal delays, MapReduce thresholds, and lint flags** written to the repo `.env` by the React Settings Drawer — configuration appeared frozen at startup defaults.

Principle 15 makes runtime parameters **dynamic within live background threads**:

- `resolve_repo_dotenv_path()` anchors `.env` to the package repository root (`Path(__file__).resolve().parents[2]`), not process `cwd`.
- `reload_plumber_dotenv()` re-reads that file with `override=True` on every `_sync_runtime_config()` cycle and at daemon entry points.
- Operators hot-swap thermal breaks, `mapreduce_trigger_chars`, and cognitive lint toggles from the web cockpit without restarting the Plumber process.

### Integration surface

| Component | Role |
|-----------|------|
| `run_cognitive_lint_pipeline()` | Computes `llm_context` once per page; passes to MARPA, entity consolidation, property hygiene |
| `InstructorLLMClient` | All structured Phase 2 methods use `build_cache_aligned_prompt()` |
| `index_page()` | Auto-invokes `prepare_llm_context_payload()` when `graph_root` is available |
| `tests/test_llm_context_payload.py` | Six integration tests validating payload selection, skeleton extraction, and prefix order |

| **417** pytest targets green (2 skipped); strict Mypy and Ruff clean via `make check`.

---

### Plumber index artifacts (on-disk)

**Semantic index** (appended once per page after successful inference):

```markdown
### Matryca Semantic Index
- indexed-at:: 2026-05-21 14:30 UTC
- summary:: …
- suggested-tags:: #tag1 #tag2
- moc-pointers::
  - [[MOC Example]]
- cross-references::
  - concept (relation) → [[Target Page]]
- semantic-lint-applied::
  - auto_wikilink:uuid…:reason
```

**Structural lint** (quarantine annotation — user-facing alert):

```markdown
### Matryca Structural Lint
- malformed-block-refs::
  - ((aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee))
- todo:: #todo [[Matryca Broken Reference]] — fix ((uuid)) typos in Logseq
```

**MARPA validation** (when `MATRYCA_LINT_MARPA_FRAMEWORK=true`):

```markdown
### Matryca MARPA Validation
- assigned-domain:: progetto
- detected-tags:: #project
- ssot-warnings::
  - ssot_duplicate:pages/Other.md (prefer ((block-uuid)) transclusion)
```

---

## Lifecycle Architecture and Fault Tolerance

### 1. Strict Phase Separation Pipeline

To prevent infinite conceptual hallucination loops (where the LLM creates orphan pages during initial census that recursively extend the scan queue), Matryca Plumber enforces an impassable wall between data collection and logical graph evolution.

```
[ Logseq Graph ] ──► PHASE 1 (Stateless Ingestion) ──► Generate Master Index & Catalog
│
▼
[ LOUVAIN MODULE ] ──► Group into Semantic Neighborhoods (5–35 pages)
│
▼
PHASE 2 (Cognitive Loop) ──► Alias-First Semantics & Link Backpropagation
```

#### Phase 1: Pure Bootstrap Harvesting (Read/Append Only)

During this phase, all mutative and generative modules (`Semantic Routing`, `Link Backpropagation`) are forcibly disabled in code.

- Execution is **Stateless**: the Instructor LLM Client history is reset after every single page write.
- The flat `master_catalog.json` map is populated and the structured `pages/Matryca Master Index.md` file is compiled, setting the native Logseq `collapsed:: true` property on MARPA macro-sections to preserve visual DOM performance.
- No additional Markdown file creation is permitted.

#### Phase 2: Neighborhood-Contextualized Cognitive Engine

The `bootstrap_complete` flag flips to `True` only when the pending backlog reaches zero and the master index is on disk.

- When file modifications arrive, the daemon activates cognitive analysis.
- Instead of scanning pages in flat order, the system processes them cluster-by-cluster based on membership.
- On cluster entry, `reset_execution_history()` runs and a neighborhood context map is injected, forcing Ermes rolling memory (48 messages) to remain confined within the same conceptual perimeter.

### 2. Deterministic Semantic Clustering Engine

Implemented in pure Python to exclude heavy C dependencies or external vector databases, the `semantic_clustering.py` module performs hierarchical community partitioning inspired by the Microsoft GraphRAG paradigm.

1. **Tokenization and Stopword Filtering:** Summary text is cleaned via a static set of Italian and English stopwords, preventing structural prose from polluting similarity calculations.
2. **Hybrid Similarity Matrix:** A weighted network is built where proximity between two nodes $A$ and $B$ is defined by the linear combination:
   $$\text{Similarity}(A, B) = (0.55 \times \text{CosineSimilarity}_{\text{TF-IDF}}) + (0.45 \times \text{JaccardIndex}_{\text{Tags}})$$
3. **Balanced Louvain Algorithm with Loop Guard:** In-RAM modularity optimization is protected by `LOUVAIN_MAX_ITERATIONS = 20` to prevent infinite oscillation on degenerate graphs. Resulting clusters are rigidly balanced between a minimum of 5 and a maximum of 35 pages to preserve local model context limits.
4. **Cluster Hub Anchor:** For each neighborhood, the node with maximum weighted degree (the structural epicenter) is computed mathematically. This node is explicitly marked in the prompt as the cognitive anchor (`[CLUSTER FOCUS ANCHOR NODE]`), dramatically improving associative retrieval precision.

### 3. Operating System Hardening Mechanisms

- **Universal Unicode Resilience:** Every decode/encode I/O operation applies `errors="replace"` to digest corrupted web fragments without ever raising `UnicodeDecodeError`.
- **Graceful Signal Evacuation (`SIGTERM`/`SIGINT`):** On shutdown signal interception, an atomic hook writes current state, persists the in-RAM catalog, releases all process lock descriptors (`*.matryca.lock`, **`.matryca_plumber_daemon.lock`**), and deallocates the PID file before `sys.exit(0)`.
- **Live cockpit telemetry:** The React control room polls `/api/state` and `/api/logs` at 1 Hz; ops log tailing uses bounded line reads — constant RAM regardless of multi-megabyte JSONL history.
- **Error Backoff:** Pages that fail indexing due to external causes (e.g. LM Studio VRAM timeout) are marked `"error"`. The daemon excludes them from subsequent scan cycles until their on-disk `st_mtime` receives a physical modification from the user, zeroing wasted CPU cycles.

---

## Related reading

- **[`SYSTEM_PROMPT.md`](../SYSTEM_PROMPT.md)** — agent rules (outlines, dry-runs, L1/L2)
- **[`docs/roadmaps/`](../docs/roadmaps/)** — phased delivery checklists
- **[`docs/PROJECT_DIARY.md`](PROJECT_DIARY.md)** — phase lifecycle log, ADRs, crushed bottlenecks
- **[`docs/openspec/README.md`](openspec/README.md)** — trimmed internal specifications
- **[`SECURITY.md`](../SECURITY.md)** — private vulnerability reporting
- **[`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)** — community standards

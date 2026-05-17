# Architecture

**matryca-logseq-llm-wiki** connects an LLM agent to **Logseq OG** (pure local Markdown) through **FastMCP**, **Pydantic**, and a thin async **Logseq HTTP JSON-RPC** client. This document is the engineering contract: *why* there is no second database, *where* spatial parsing lives, and *how* the agentic pipeline is supposed to run.

---

## Core philosophy

### No external database (defended)

The **only** durable system of record is the tree under **`LOGSEQ_GRAPH_PATH`**: `pages/`, `journals/`, `templates/`, and the rest of your Logseq graph on disk.

We deliberately **do not** add Postgres, SQLite, Redis, an embedding index, or a document store that could fork truth away from Logseq’s files.

**Why this matters:**

1. **One artifact for humans and agents** — Diffs, `git blame`, full-text search, and backup tools see exactly what Logseq sees.
2. **No sync nightmare** — A secondary DB would need invalidation rules every time a human edits a file outside the agent.
3. **Forces block-shaped thinking** — Agents are steered toward API appends and scoped file edits instead of “replace the whole page.”

When we need **ranking** (BM25), **adjacency** (wikilink/tag BFS), or **aggregates** (dashboard counts), we compute them **inside the MCP process** for that request and discard them. That is *caching*, not a competing database.

### Parser for spatial truth; bounded line work for surgery and scale

**[logseq-matryca-parser](https://github.com/MarcoPorcellato/logseq-matryca-parser)** (`logseq_matryca_parser`) owns the hard problem: **indentation**, block boundaries, and a faithful spatial view of a page. Our adapter **`src/rag/matryca_hooks.py`** feeds **`read_logseq_page`** so agents get the same block tree semantics Logseq uses — we do **not** fork a second full-file Markdown AST in this repo.

We **do** implement **narrow, auditable** passes on raw text where the parser is the wrong abstraction or we need graph-wide coverage:

| Class of work | Representative tools / modules | Mechanism |
|---------------|----------------------------------|-----------|
| **Scoped metadata** | `patch_logseq_block_property_lines` — `src/graph/property_line_edit.py` | Lines **inside** the span anchored at `id:: <uuid>`; only `key::` property lines |
| **Graph-wide ref integrity** | `lint_logseq_block_refs` — `src/graph/block_ref_lint.py` | Two-pass regex: collect all `id::` UUIDs, validate each `((uuid))` |
| **Lexical discovery** | `query_logseq_pages_local` — `src/rag/local_query.py` | Token bags + Okapi BM25 in memory |
| **Structural hops** | `traverse_logseq_structural_hops`, hub/orphan reports — `src/graph/link_tag_hop.py` | Wikilinks, `#tags` / `tags::`, light `type::` / `domain::` edges on disk |
| **Hashtag normalization** | `lint_unify_logseq_tags` — `src/graph/tag_unify.py` | Token-level `#tag` detection with guards (URLs, wikilinks, code); **not** prose “fixes” |
| **Journals & aliases** | `journal_task_scan.py`, `alias_index.py` | Line- and property-oriented scans tuned to Logseq conventions |

**Design principle:** use the **parser** when hierarchy and block identity must be correct; use **line-bounded regex and explicit file boundaries** when the operation is surgical, graph-wide, or must stay diff-friendly in code review.

### FastMCP + Pydantic at the boundary

`src/main.py` constructs **FastMCP** with a lifespan that wires **`LogseqClient`** and **`MatrycaWikiConfig`**. **`register_mcp_tools`** in **`src/agent/mcp_server.py`** attaches every tool. Incoming outline JSON is validated as **`OutlineNode`**: `page_type` / `domain` / `entity_type` rules, normalized `children`, and **fail-fast** behavior before any HTTP or disk mutation.

### Git snapshots as opt-in rollback

**`MATRYCA_GIT_SNAPSHOT_ON_WRITE`** (`src/agent/git_snapshot.py`) optionally runs **`git add -A` + `git commit`** on **`LOGSEQ_GRAPH_PATH`** when it is a git checkout — not a hosted backup product, but a **local, operator-controlled** safety rail. **`snapshot_logseq_graph_git`** exposes the same behavior for manual checkpoints.

```mermaid
flowchart TD
  E["MATRYCA_GIT_SNAPSHOT_ON_WRITE"] -->|false| SKIP["No auto commit"]
  E -->|true| G{"Graph path is a git repo?"}
  G -->|no| SKIP2["Skipped; reason logged"]
  G -->|yes| C["git add -A && git commit"]
  C --> W["Outline write or disk mutator"]
```

### Choosing read vs write paths

```mermaid
flowchart TD
  START["What do you need?"] --> Q1{"Spatial tree\nid:: · indent?"}
  Q1 -->|yes| R1["read_logseq_page\nparser adapter"]
  Q1 -->|no| Q2{"Append bullets under\na known parent UUID?"}
  Q2 -->|yes| W1["write_logseq_outline\nLogseqClient.append_block"]
  Q2 -->|no| Q3{"Only key:: lines\nin one block?"}
  Q3 -->|yes| W2["patch_logseq_block_property_lines\ndry_run first"]
  Q3 -->|no| Q4{"Vault-wide scan\nBM25 · lint · hops?"}
  Q4 -->|yes| D1["FS scan or in-memory index\nno second DB"]
```

---

## End-to-end data flow

The MCP host spawns this process on **stdio**. Tool calls flow through FastMCP into **`MatrycaMCPServer`** and graph helpers: **live** block creation uses **`LogseqClient`** → Logseq’s **local** API → disk; **spatial reads** use the parser adapter; **mutators** write files atomically where applicable (often with `.bak`).

```mermaid
sequenceDiagram
    autonumber
    participant LLM as LLM Agent
    participant MCP as FastMCP (stdio)
    participant Bridge as MatrycaMCPServer
    participant Client as LogseqClient
    participant API as Logseq local HTTP API
    participant FS as Markdown on disk

    LLM->>MCP: Tool call
    MCP->>Bridge: Validated args / context
    alt Write path (live graph)
        Bridge->>Client: append_block / inject query
        Client->>API: JSON-RPC
        API->>FS: Persist blocks
        API-->>Client: block uuid
    else Read path (spatial)
        Bridge->>FS: read page via parser adapter
    else Disk mutator (property / journal / refactor)
        Bridge->>FS: atomic write + optional .bak / git snapshot
    end
    Bridge-->>MCP: Structured result + hints
    MCP-->>LLM: Tool result
```

### Outline write ordering (depth-first)

`write_logseq_outline` walks **`OutlineNode`** depth-first: each **`append_block`** returns a **real** UUID before children are created, so Logseq never receives **`UNRESOLVED_PARENT_UUID`**.

```mermaid
flowchart TD
  P["parent_block_uuid"] --> N1["append node → uuid A"]
  N1 --> C1["each child"]
  C1 --> N2["append under A → uuid B"]
  N2 --> C2["recurse"]
  C2 --> DONE["DFS-ordered uuids returned"]
```

---

## The agentic pipeline

Operational detail for LLMs lives in **`SYSTEM_PROMPT.md`**. At a high level:

1. **Search** — Prefer **`query_logseq_pages_local`** with **`mode=bm25`**. Optionally **`read_l1_memory`** when mistakes would be costly before touching L2.
2. **Scan** — **`read_logseq_page`** for ground truth; **`traverse_logseq_structural_hops`** / **`report_structural_hubs_orphans`** to avoid duplicate concepts; **`lint_logseq_block_refs`** when editing many `((uuid))` refs.
3. **Refactor / update** — **`write_logseq_outline`** for nested bullets; **`patch_logseq_block_property_lines`** for property-only edits; **`inject_logseq_advanced_query`** for live Datalog blocks; journal and alias tools as needed.
4. **Garden** — Flashcards, tag unify, reparent, unlinked mentions, MOC generation, large-block split — almost always **`dry_run=true`** first.
5. **Quality gate** — No credentials in L2, sane fan-out, stable `id::`, re-lint refs after big edits (`src/agent/quality_gate.py` + prompts).

**Read-only health:** **`render_logseq_dashboard`** builds **[[Matryca Dashboard]]**-style outline stats from `pages/**/*.md`.

```mermaid
flowchart TD
  subgraph search["1 Search"]
    BM25["query_logseq_pages_local"]
    L1["read_l1_memory optional"]
  end
  subgraph scan["2 Scan"]
    READ["read_logseq_page"]
    HOP["traverse · hubs/orphans"]
    LINT["lint_logseq_block_refs"]
  end
  subgraph update["3 Refactor / Update"]
    OUT["write_logseq_outline"]
    PATCH["patch_logseq_block_property_lines"]
    AQ["inject_logseq_advanced_query"]
    JR["journal analyze/append"]
    AL["entity resolve/alias"]
  end
  subgraph garden["4 Garden"]
    G1["flashcards · tags · reparent"]
    G2["unlinked · MOC · split blocks"]
  end
  subgraph gate["5 Quality gate"]
    QG["quality_gate · routing hints"]
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

## Phase breakdown (how the codebase grew)

Use this as a **mental map** for `src/` — phases are product language; modules are what you grep.

| Phase | What shipped | Why it exists |
|:-----:|--------------|---------------|
| **1 — Baseline** | MCP server, **`OutlineNode`**, **`write_logseq_outline`** (DFS `append_block`), **`read_logseq_page`** via parser adapter, block-ref lint, dashboard markdown | Prove the bridge: agents can **read spatially** and **write block-by-block** with validation |
| **2 — L1 / L2** | **`read_l1_memory`**, routing hints in responses | Session-critical rules without scanning the whole vault |
| **3 — PKM refinements** | BM25 query, structural hops + hubs/orphans, property-line patcher, templates, wiki lint, namespace index, **git snapshot** hook | Discovery + **safe** disk edits + house-style templates |
| **4 — Logseq superpowers** | Advanced query injection, journal task scan + append, alias index + append | Native Logseq power features agents should use instead of static lists |
| **5 — Graph gardener** | Flashcards from Q/A pairs, vault-wide tag unify, same-page reparent | PKM hygiene at scale |
| **6 — Synthesis engine** | Unlinked mentions scan, MOC generator, large-block splitter, manual git snapshot tool | Graph “thickening” and long-bullet repair |

**Cross-cutting:** **`src/graph/wiki_lint.py`**, **`src/config.py`**, **`matryca-wiki.yml`**, **`docs/openspec/`**, **`PROJECT_DIARY.md`**.

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
  subgraph bridge["Bridge tier"]
    MS["MatrycaMCPServer"]
    LC["LogseqClient"]
    GS["git_snapshot"]
    MS --> LC
    MS --> GS
  end
  subgraph data["Data tier"]
    API["Logseq JSON-RPC"]
    PAR["logseq_matryca_parser"]
    DISK["Bounded FS passes"]
  end
  LLM <--> FM
  REG --> MS
  LC <--> API
  MS --> PAR
  MS --> DISK
```

---

## Key entry points

| Path | Role |
|------|------|
| `src/main.py` | FastMCP app, lifespan, `register_mcp_tools` |
| `src/agent/mcp_server.py` | All `@mcp.tool()` handlers, `OutlineNode`, `MatrycaMCPServer` |
| `src/bridge/logseq_client.py` | Async JSON-RPC over HTTP |
| `src/agent/git_snapshot.py` | Optional commits on graph root |
| `src/rag/matryca_hooks.py` | Parser adapter for spatial reads |

---

## Related reading

- **[`SYSTEM_PROMPT.md`](../SYSTEM_PROMPT.md)** — agent rules (outlines, dry-runs, L1/L2)  
- **[`ROADMAP_LLM_WIKI.md`](../ROADMAP_LLM_WIKI.md)** and phase roadmaps — checklist history  
- **[`docs/openspec/README.md`](openspec/README.md)** — trimmed internal specs  

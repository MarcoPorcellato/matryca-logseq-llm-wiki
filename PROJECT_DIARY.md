# Project diary and architecture decisions

This file is the **developer log** for **matryca-logseq-llm-wiki**. It records **architecture decision records (ADRs)** in a lightweight, chronological form: enough context for a future reader (or an LLM collaborator) to understand *why* the system looks the way it does, without duplicating reference material that belongs in `docs/ARCHITECTURE.md` or the source tree.

Entries are **retroactive or forward-looking** as needed. When a decision is superseded, add a new entry that references the older one rather than rewriting history.

---

## Entry template

Copy the block below when adding a new decision or milestone.

```markdown
## [YYYY-MM-DD] - Title

### Context

What problem or constraint triggered this entry? What was the situation before any change?

### Decisions Made

What was decided, rejected, or deferred? Link to PRs, commits, or paths only when it materially aids traceability.

### Next Steps

Concrete follow-ups: implementation tasks, documentation updates, experiments, or review checkpoints.
```

---

## [2026-05-19] - V1.4.0 The Headless Revolution: Deprecating HTTP JSON-RPC for Native AST Splicing

### Context

In earlier versions, Matryca depended on the Logseq desktop app’s local HTTP JSON-RPC interface (ports 8080/12315). That introduced technical debt: network latency, timeout fragility, the need to keep the Electron app open in the background, and API token configuration. The prior architecture split reads (filesystem via `LOGSEQ_GRAPH_PATH`) from writes (HTTP API to whichever graph was open in the UI), creating a **split-brain** risk when the configured path and the active Logseq graph diverged. Server automation, remote execution (SSH/Docker), and token efficiency required a zero-dependency model.

### Decisions Made

1. **Pure Headless architecture:** removed the entire `httpx` layer and `LogseqClient` module (`src/bridge/` deleted). Matryca now interacts directly with the file system.
2. **Upgrade to `logseq-matryca-parser==0.3.3`:** adopted the atomic CRUD engine for AST-based rewrites on `.md` files using `append_child_to_node` (`logseq_matryca_parser.agent_writer`), orchestrated by `src/agent/graph_dispatch.py` under `page_rmw_lock` safety boundaries.
3. **Native X-Ray persistence:** use `SessionAliasRegistry` disk serialization in `.matryca_xray_state.json` (`src/agent/alias_state.py`) so the CLI stays stateless yet context-aware across consecutive invocations.
4. **Topological linter:** replaced legacy regex scans with direct in-memory graph index queries via `LogseqGraph.get_broken_references()` (`src/graph/block_ref_lint.py`).

### Consequences & Celebration

* **Dependencies stripped to the bone:** API tokens are no longer required. The only required variable is `LOGSEQ_GRAPH_PATH`.
* **Absolute resilience:** the MCP server and CLI run on machines without an active Logseq GUI, eliminating network latency and network-related crashes.
* **Hardened test suite:** **144 passing tests** under strict MyPy and Ruff.

### Status

Approved & Shipped.

---

## [2026-05-19] - V1.3.0 Fortress Release: Path Traversal Sandbox & Network Resiliency

### Context

An adversarial security audit revealed risks regarding potential LLM path traversal hallucinations (e.g., prompt injection trying to read/write outside the graph via `../../../etc/passwd`) and potential HTTP deadlocks if the local Logseq API server freezes during heavy indexing.

### Decisions Made

1. Implemented a centralized sandbox engine (`src/graph/path_sandbox.py`) that strictly enforces `Path.resolve().is_relative_to(graph_root)` for all disk actions, raising an unbypassable security error on escape attempts.
2. Hardened `LogseqClient` with strict connection (5.0s) and read/write (15.0s) timeouts using `httpx.Timeout`, converting network hangs into safe, explanatory tool errors for the agent.
3. Implemented graceful lifespan teardown routines to clean locks and handle shutdown-time task callbacks cleanly.

### Next Steps

- Open-source launch cycle complete. Pivot to community management and monitoring telemetry on diverse host configurations.

---

## [2026-05-19] - V1 Launch, 100k Token Stress Test, and Synthetic ID Guardrails

### Context

We ran a live **Agentic RAG** stress test (~**106k tokens**) in which **Cursor** acted as an MCP agent and generated a complex **Map of Content (MOC)** from more than **2,300 lines** of nested Markdown. The agent used the AST parser successfully to understand **spatial hierarchy**, but it **blindly reused ephemeral UUIDv5s** produced in memory by the parser inside `((...))` block references. Those IDs were never written back to the source `.md` files as `id::` lines. After Logseq re-indexed the graph, those references surfaced as **broken links**—a critical edge case at the boundary between parser identity and on-disk truth.

We shipped **`v1.0.1`** to lock in fixes across the upstream parser and this MCP server.

### Decisions Made

1. **Upstream parser contract:** **`logseq-matryca-parser`** now exposes an explicit **`synthetic_id`** boolean and **`source_uuid`** on each block node so downstream consumers can tell whether an effective UUID exists on disk vs was generated only for the AST session.
2. **Pre-flight Block Reference Guard:** Atomic disk mutators call **`assert_valid_block_refs_in_markdown`** inside **`atomic_write_bytes`** (`src/graph/markdown_blocks.py` via **`src/graph/logseq_uuid.py`**) to regex-scan outgoing Markdown and **reject malformed `((uuid))` tokens** (wrong length, bad hex groupings, typos) **before** `os.replace`—a fail-safe against LLM UUID hallucinations.
3. **Agent persist-first rule:** **`SYSTEM_PROMPT.md`** now requires agents to call **`patch_logseq_block_property_lines`** to inject `id:: <uuid>` into the **source page** when **`synthetic_id: true`** and **`source_uuid` is absent**, **before** emitting MOCs or other pages that reference `((that-uuid))`.
4. **Wiki linter alignment:** **`block_ref_lint.py`** (`lint_logseq_block_refs`) accepts both **UUIDv4** and **UUIDv5** shapes, matching Logseq’s on-disk and parser-generated identifiers.

### Next Steps

- Monitor community feedback from the open-source launch on **Reddit** and **Hacker News**.
- Gather telemetry on how other agent hosts (e.g. **Claude Desktop**) enforce or violate the **persist-first** UUID rule when building MOCs at scale.

---

## [2026-05-17] - Foundation, DX, and The Outliner Paradigm

### Context

The project aims to connect LLM agents to **Logseq OG**—a local, **pure Markdown** knowledge graph—via MCP, without sacrificing structural fidelity. Early integration approaches risked treating Logseq pages as flat text or talking to the wrong persistence layer; block-oriented APIs also surfaced ordering bugs when child blocks were created before their parents had stable UUIDs in the graph.

### Decisions Made

- **Logseq OG (pure Markdown) over SQLite:** The system of record is the Markdown tree on disk (indented bullets, block properties, human-agent co-editing), not an opaque database export. The bridge targets Logseq’s **local HTTP JSON-RPC API** and lets Logseq own persistence to `.md` files.
- **`uv` for dependency management:** Development and CI use **Astral `uv`** (`uv sync`, `uv run`) for fast, reproducible environments; the **Makefile** exposes `install`, `lint`, `typecheck`, `test`, and `check` so contributors have a single ergonomic surface.
- **FastMCP as the MCP server framework:** `src/main.py` hosts a **FastMCP** application with lifespan-managed **`LogseqClient`** and **`MatrycaMCPServer`**, exposing tools such as **`write_logseq_outline`** over **stdio** for agent hosts that speak MCP.
- **DFS async UUID generation for hierarchical writes:** `src/agent/mcp_server.py` validates outlines with **Pydantic** (`OutlineNode`) and creates blocks **depth-first**, **awaiting** each **`append_block`** so every child is inserted under a **resolved parent UUID**. This addresses **`UNRESOLVED_PARENT_UUID`**-style technical debt from parent-child races or out-of-order creation.

### Next Steps

- ~~Implement inline spatial parsing in `src/rag/matryca_hooks.py`~~ superseded by the modular **`logseq-matryca-parser`** dependency (see diary entry **Modular Architecture and PyPI Roadmap**).
- Expand MCP tools beyond outline insertion (reads, moves, property-aware edits) as requirements stabilize.
- Keep this diary updated when transport, auth, or persistence assumptions change.

---

## [2026-05-17] - Modular Architecture and PyPI Roadmap

### Context

Spatial parsing (indentation, block tree, ``id::`` UUIDs, references) is non-trivial and would duplicate effort if reimplemented inside **matryca-logseq-llm-wiki**. A separate codebase already owns the deterministic Logseq AST work; keeping two copies would drift and complicate a future **PyPI** distribution story.

### Decisions Made

- **Single source of truth:** Parsing logic is **not** maintained in this repository. It lives in **`logseq-matryca-parser`** ([MarcoPorcellato/logseq-matryca-parser](https://github.com/MarcoPorcellato/logseq-matryca-parser)), declared in **`pyproject.toml`** as a **Git** dependency until the package is published on **PyPI**.
- **Adapter boundary:** **`src/rag/matryca_hooks.py`** exposes **`get_spatial_context(file_path)`**, which delegates to the external library (lazy import inside the function so upstream API churn is localized). This repo focuses on **orchestration** (MCP, Logseq client, RAG hooks), not parser internals.
- **Python baseline:** The parser declares **`requires-python >= 3.12`**, so this project’s interpreter floor was raised to **3.12** to stay compatible with that dependency.

### Next Steps

- When **`logseq-matryca-parser`** hits **PyPI**, replace the Git URL in **`pyproject.toml`** with a versioned PyPI spec for reproducible installs.
- Wire **`get_spatial_context`** into concrete RAG or MCP read paths as those features land.
- Keep **`docs/ARCHITECTURE.md`** in sync if the adapter’s public surface or return types change.

# Contributing

Thank you for investing your time in **matryca-logseq-llm-wiki**.

This project exists so AI agents can collaborate on **Logseq OG** graphs the right way: **blocks**, **`id::`**, and **local Markdown** — not flattened blobs in someone else’s database. Whether you fix a typo, tighten a test, or add an MCP tool, you are helping keep that bar high. We are glad you are here.

---

## What we are building (read once)

- **Atomic outliner paradigm** — Prefer nested bullets and stable `id::` lines; see [`SYSTEM_PROMPT.md`](SYSTEM_PROMPT.md).
- **No new system-of-record database** — In-memory indexes, filesystem scans, Logseq’s API, and optional git snapshots on the graph repo only.
- **Parser boundary** — Spatial page structure comes from **`logseq_matryca_parser`**; targeted edits use **bounded** line/regex modules (see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).

---

## Local development with `uv`

1. **Python 3.12+** (see `.python-version`).
2. Install **[uv](https://docs.astral.sh/uv/)**.
3. From the repository root, install dependencies and dev tools:

   ```bash
   make install
   ```

   This runs **`uv sync --extra dev`** and creates the virtual environment (typically **`.venv/`**).

4. Optional: activate the venv, or invoke tools via **`uv run`** and the Makefile.

   ```bash
   source .venv/bin/activate
   ```

5. For integration-style work or tests that need paths:

   ```bash
   cp .env.example .env
   ```

   Set **`LOGSEQ_GRAPH_PATH`** to your Logseq graph root (the folder containing `pages/`). Matryca is headless — no Logseq HTTP API or desktop app is required.

6. List all Make targets:

   ```bash
   make help
   ```

### Makefile targets you will use

| Target | What it does |
|--------|----------------|
| `make install` | `uv sync --extra dev` |
| `make format` | Ruff auto-fix + format |
| `make lint` | Ruff lint only |
| `make typecheck` | `mypy src/ tests/` (strict) |
| `make test` | `pytest -q` |
| **`make check`** | **`format`** → **`lint`** → **`typecheck`** → **`test`** (full local gate) |
| `make clean` | Remove `.venv`, caches |

---

## Merge bar: green `make check`

**No pull request is merged unless `make check` is 100% green.**

That means, in order:

1. **Ruff** — auto-fix and format the tree, then lint clean  
2. **Mypy** — strict type-check on **`src/`** and **`tests/`**  
3. **Pytest** — full suite (currently **162** tests)

GitHub Actions on pushes and pull requests to **`main`** runs **ruff**, **mypy** on `src/`, and **pytest** (see `.github/workflows/ci.yml`). Locally, always run **`make check`** before you push: it matches maintainer expectations and catches formatting drift CI does not auto-fix.

Never commit secrets (no `.env`, tokens, or private graph paths in git).

**Background service:** `matryca service install` must target a **stable** `matryca-logseq-llm-wiki` binary (for example after `uv tool install matryca-logseq`). Do not install the daemon from ephemeral **`uvx`** — the unit file would reference a cache path that uv may delete. See [README.md](README.md#background-service-matryca-service--persistent-install-only).

---

## Writing tests for new MCP tools

### Stack conventions

- **FastMCP** — Tools are plain async functions registered with **`@mcp.tool()`** in **`register_mcp_tools`** (`src/agent/mcp_server.py`). You usually **do not** need to spin up stdio MCP in tests; test the **logic** the tool calls.
- **Pydantic** — **`OutlineNode`** and other models should be covered with **`model_validate`** / **`ValidationError`** where rules apply.
- **pytest-asyncio** — The project sets **`asyncio_mode = auto`** in **`pyproject.toml`**. Use **`@pytest.mark.asyncio`** on async test functions when you await bridge methods.

### Recommended patterns

1. **Model-only tests** — Fast, no I/O. Example: outline schema rules in [`tests/test_mcp_server.py`](tests/test_mcp_server.py).

2. **Stub `LogseqClient`** — Build a client with a dummy URL/token, then **`monkeypatch`** **`append_block`** (or other methods) to record arguments and return fake UUIDs. See **`test_write_logseq_outline_chains_parent_uuids`** in [`tests/test_mcp_server.py`](tests/test_mcp_server.py).

3. **Filesystem fixtures** — Use pytest’s **`tmp_path`** to create minimal **`pages/`**, **`journals/`**, or **`templates/`** trees, set **`LOGSEQ_GRAPH_PATH`** via **`monkeypatch.setenv`**, and call **`src/graph/`** functions directly. Most graph tools are testable **without** Logseq running.

4. **Thin MCP wrapper, fat module** — Prefer implementing behavior in **`src/graph/`** or **`src/agent/`** helpers, unit-testing those modules, and keeping **`@mcp.tool()`** bodies as short orchestration (parse args → call helper → return JSON).

### Tool design checklist (keeps tests simple)

- Prefer **explicit** typed parameters; use **`dict[str, Any]`** only where MCP JSON must stay flexible.
- For mutators, default **`dry_run=true`** when behavior could touch many files; return stable keys (`ok`, `code`, `hint`, `dry_run`, `git_snapshot`, byte counts) consistent with existing tools.
- **`src/`** must satisfy **strict mypy**; tests may relax annotations per Ruff **`per-file-ignores`** for **`tests/**`**.

When you add or change a tool, **extend or add tests under [`tests/`](tests/)** so the behavior is pinned before review.

---

## Pull request workflow

1. **Fork** the repository and use a **focused** branch per change.
2. **Open or reference an issue** for larger features so design stays aligned.
3. Describe **why** the change exists and any trade-offs in the PR body.
4. Confirm **`make check`** passes on your machine.

---

## Code of conduct

Be respectful, assume good intent, and keep feedback actionable. We want contributors of all backgrounds to feel welcome.

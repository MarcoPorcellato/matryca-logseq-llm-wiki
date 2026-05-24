# Matryca Plumber

> **v1.5 — Ironclad Release.** Agentic Knowledge Management for Logseq OG. An **enterprise-grade, local-first background AI daemon** with a real-time **Sovereign UI** control room, a **typed CLI**, and **direct Logseq Markdown AST** mutation (no Logseq HTTP API, no auxiliary database). The default experience is **autonomous**: the daemon and Python workers poll your graph, run structured local-LLM passes, and commit indexes and lint artifacts while you work or sleep. An **optional FastMCP stdio sidecar** exposes the same headless mutation plane to external clients (for example Claude Desktop) — same `graph_dispatch` contract, not a separate data path. Heavily inspired by [Andrej Karpathy's LLM-Wiki vision](https://karpathy.ai/blog). **100% native Logseq AST parity**, optimistic concurrency safety, versioned AI authorship stamping.

[![CI](https://github.com/MarcoPorcellato/matryca-plumber/actions/workflows/ci.yml/badge.svg)](https://github.com/MarcoPorcellato/matryca-plumber/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-427%20passing-brightgreen)](https://github.com/MarcoPorcellato/matryca-plumber/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

![Matryca Plumber — Agentic Knowledge Management for Logseq OG](images/matryca-plumber-1-5-10-demo.gif)

Matryca is a **100% headless, sandboxed** **standalone daemon + CLI** that turns your local Logseq graph into a high token-density agentic workspace — **no network APIs and no Logseq desktop JSON-RPC**. It treats your vault as a **tree of blocks**, not a flat document store. Logseq OG remains optional: humans and the daemon co-edit the **same** `.md` trees on disk.

**Matryca Plumber** is not a one-shot script — it is an **enterprise-grade, local-first background AI daemon for Logseq**. It polls your graph on a duty cycle, calls a local LLM (LM Studio or Ollama), appends semantic indexes, runs optional cognitive lint modules, and logs every token transaction — **while you edit the same `.md` files in Logseq or leave the vault idle**. Optional **MCP-attached** sessions reuse the identical mutation plane for interactive queries; they are **not** required for background operation. Every write path mirrors Logseq's on-disk AST contract: page frontmatter at line 0, block properties contiguous to their parent bullet, namespace filenames encoded exactly like Logseq's Clojure Datalog layer, and **optimistic concurrency control** that aborts stale writes when you type during inference.

**Matryca Plumber** is an enterprise-grade, local-first background AI daemon for Logseq. It turns your local graph into a high token-density agentic workspace by continuously polling your notes, running local LLMs (like LM Studio or Ollama), appending semantic indexes, and healing broken links—all completely offline, while you work or sleep.

**Zero Cloud. Zero Data Leaks. 100% Native Logseq AST.**

---

## ⚠️ Important: Clone Your Graph First

Matryca Plumber edits your local `.md` files directly. While it features safe Optimistic Concurrency Control (OCC) to prevent data loss, **we strongly recommend testing it on a clone of your graph first**. This allows you to see the AI in action and explore its capabilities without affecting your primary notes.

**How to safely clone your graph (crucial if you use Logseq Sync):**
1. Make a copy of your entire Logseq graph folder on your computer (e.g., duplicate your `MyGraph` folder and rename it to `MyGraph_Test`).
2. Open Logseq, click on your graph name in the top left, and select **Add new graph**.
3. Choose the new `MyGraph_Test` directory.
4. **If you use Logseq Sync:** Do *not* enable Sync on this test graph. This ensures the AI's test edits remain strictly local and do not propagate to your other devices.
  a. Create an empty folder and add it as a new graph in Logseq.
  b. Close Logseq; copy only pages/, journals/, assets/ from production.
  c. Reopen and Re-index.
6. Point Matryca's `.env` configuration (`LOGSEQ_GRAPH_PATH`) to this test folder.

Once you are comfortable with how Matryca behaves and have tuned the safety tiers, you can point it to your main graph.

---

## 🚀 Quick Install & Getting Started

The fastest way to get started is using [uv](https://docs.astral.sh/uv/), the blazing-fast Python package manager.

### 1. Try it instantly (Zero-install)
Run the CLI directly without polluting your system:
```bash
uvx --from matryca-plumber matryca-plumber status
```

### 2. Global Installation (Recommended)
Install the binary to use the `matryca` command anywhere:
```bash
uv tool install matryca-plumber
```

### 3. Launch the Daemon & UI
```bash
# 1. Start the daemon in the background
matryca plumber start

# 2. Open the Sovereign UI control room ([http://127.0.0.1:8000](http://127.0.0.1:8000))
matryca plumber status
```

### 4. Set it and forget it (Background Service)
Install it as a LaunchAgent/systemd service so it wakes up with your OS:
```bash
matryca service install
```

---

## 🧠 What does it actually do?

Unlike generic scripts, Matryca is a continuous background engine. When paired with a local LLM (like Gemma or LLaMA via LM Studio or Ollama), it provides:

- **Semantic Indexing**: Automatically generates summaries, suggested tags, and cross-references for your pages.
- **Dangling Link Healing**: Finds broken `[[WikiLinks]]` and creates isolated seed pages for them.
- **Entity Consolidation**: Suggests `alias::` properties for overlapping concepts.
- **Auto-Split Dense Blocks**: Extracts oversized subtrees into new pages to keep your graph fast and readable.
- **Claude Desktop Integration (FastMCP)**: Use Claude to query and mutate your Logseq graph natively.

---

## 🖥️ The Sovereign UI

Matryca is 100% headless, but it comes with a **Sovereign UI Cockpit** (`matryca plumber status`). 
It's a local React dashboard running on `http://127.0.0.1:8000` that provides:
- **Live Graph Telemetry**: See exactly what the AI is indexing in real-time.
- **Dynamic Impact**: Mathematically separates *Organic Human Mind* (your notes) from *Agent Cognition* (AI enhancements).
- **Zero-Trust Security**: Every REST call requires a Bearer token. No anonymous access, even on localhost.
- **Trust & Safety Drawer**: Visually toggle what the AI is allowed to edit (Safe Mode, Augmented Mode, Surgeon Mode).

---

## ✨ Key Features & Differentiators

* 🤖 **100% Local-First & Headless:** No Logseq HTTP API required. It edits the `.md` files directly using atomic file I/O.
* 📐 **Exact Logseq AST Compliance:** True line-0 page frontmatter, block properties at +2 indent, and exact namespace encoding. Other tools break your graph; Matryca keeps it pristine.
* 🔐 **Optimistic Concurrency Control:** It checks `st_mtime` before any AI inference. If you typed in Logseq while the AI was "thinking", Matryca aborts its write. **No silent data loss.**
* 🪟 **Windows, macOS & Linux Support:** Runs safely in the background everywhere using a robust cross-platform lock (`.matryca_plumber_daemon.lock`).
* ⚡ **Context Acceleration Shield:** Obliterates LLM prefill latency on 5,000+ block pages by using hierarchical summaries instead of raw megabyte-class text.

---

## 🛡️ Trust & Safety Risk Tiers

You are in control. Nothing mutates your prose unless you explicitly enable it in the UI.

| Mode | Risk | What it allows |
|------|------|----------------|
| 🟢 **Safe Mode** | Read-only | Semantic routing cache, entity consolidation (`alias::`), property hygiene — **never edits your bullet text**. |
| 🟠 **Augmented Mode** | Side-blocks | **Heal Dangling Links**, **Backpropagate Links** (appends foldable context sections) — your original bullets stay intact. |
| 🔴 **Surgeon Mode** | Inline edits | **Inline Semantic Corrections** (wraps concepts in `[[WikiLinks]]`), **Auto-Split Dense Blocks** — **strictly opt-in**. |

---

## ⚙️ Configuration Quickstart

Copy `.env.example` to `.env`. The only **required** variable is your graph path:

```env
LOGSEQ_GRAPH_PATH=/absolute/path/to/your/Logseq/graph
MATRYCA_LM_BASE_URL=http://localhost:1234/v1   # LM Studio or Ollama endpoint
MATRYCA_LM_MODEL=qwen2.5-coder-7b              # Exact model name loaded
```

*(See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for advanced thermal pacing, context compression, and deep linter settings).*

---

## 🧑‍💻 Developer Setup

Want to contribute or run from source?

```bash
git clone [https://github.com/MarcoPorcellato/matryca-plumber.git](https://github.com/MarcoPorcellato/matryca-plumber.git)
cd matryca-plumber
make install

# Build the React frontend
cd frontend && npm install && npm run build && cd ..

# Run tests (427 passing, 0 Mypy strict issues)
make check
```

---

## 📚 Documentation Map

| Document | Description |
|----------|-------------|
| [`SYSTEM_PROMPT.md`](SYSTEM_PROMPT.md) | Agent discipline, `made-by::` authorship, OCC rules. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Data planes, Plumber lifecycle, RMW locking rationale. |
| [`docs/PROJECT_DIARY.md`](docs/PROJECT_DIARY.md) | Maintainer log, phase history, crushed bottlenecks. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, `uv` commands, `make check` standards. |

## License
Apache-2.0 — see [LICENSE](LICENSE).

![Matryca Plumber Cover](images/20260519-Logseq-Matryca-LLM-Wiki-copertina-github.jpg)

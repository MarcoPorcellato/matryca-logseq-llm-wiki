# matryca-logseq-llm-wiki

**Inspired by Andrej Karpathy's "LLM Wiki" concept. Re-engineered for the Pure Markdown Outliner Paradigm. Built with Matryca.ai quality standards.**

We are entering an era where AI agents don't just process our notes—they actively research, curate, and maintain our knowledge bases alongside us. Andrej Karpathy outlined a brilliant vision for an "LLM Wiki," a dynamic repository of plain-text Markdown files continuously updated by an AI agent. 

However, implementing this in traditional markdown editors (like Obsidian) introduces a severe structural bottleneck: **the atomic unit of standard Markdown is the Page.** When an agent writes or links to a concept in Obsidian, it deals with an entire document or flat paragraphs. The subtle, hierarchical relationship between a broad thesis and its specific atomic arguments is lost.

**This repository fixes that by porting the LLM Wiki concept to Logseq OG (Original Git-based, pure Markdown version).**

By leveraging Logseq's unique ability to turn flat text files into an atomic graph database via simple indentation and block properties, we create the perfect data structure for Human-AI collaborative knowledge management.

---

## The Core Vision: Pure Text Co-Working

The true magic of Karpathy's LLM Wiki vision is that **both humans and AI agents act as co-workers on the exact same files.** We must not hide knowledge inside binary databases or proprietary schemas. 

By sticking to **Logseq OG (Pure Markdown)** instead of an SQLite-only database, the files remain universally accessible, future-proof, and fully readable by a human opening them in any text editor (VS Code, Vim, or Notepad++). 

Logseq OG achieves a brilliant engineering feat: it simulates a highly advanced, networked graph database *directly on top of flat text*. 

---

## The Logseq OG Superpowers for AI Agents

Standard flat markdown was designed for humans to read. Logseq's hierarchical markdown is designed for humans and machines to *think together*. It grants agents three massive advantages:

1. **Atomic Precision via Block IDs:** To make a block targetable, the agent doesn't need a database entry. It simply appends an `id:: uuid` property directly under a markdown bullet point. The agent can then reference or embed that exact atomic thought anywhere in the vault using `((uuid))`.
2. **Spatial Semantics (The Outliner):** In an outliner, indentation equals semantic hierarchy. A parent bullet is a core claim; its indented children are the supporting data, constraints, or metadata. AI models inherently understand this tree-like structure, making context parsing deterministic.
3. **Granular RAG via Matryca Spatial Parser:** Traditional RAG chunks text blindly by word count, destroying context. Because our files are structured as Logseq outlines, our companion tool — the [**Logseq Matryca Parser**](https://github.com/MarcoPorcellato/logseq-matryca-parser) — reads the spatial indentation. When the AI retrieves a single bullet point, it mathematically injects its entire parent-child lineage into the vector metadata. Context is never lost.

---

## Flat Text vs. Atomic Outliner Graph

Here is how an agent writes a research entry under the **Matryca Logseq LLM Wiki** paradigm compared to standard flat markdown:

### ❌ Standard Markdown (Obsidian / Flat Files)
A wall of text. Readable for humans, but a nightmare for surgical AI updates or precise semantic retrieval.

```markdown
# Attention Mechanism

The attention mechanism is a core component of the Transformer architecture. It allows the model to dynamically focus on different parts of the input sequence. This was first introduced in the paper [[Attention Is All You Need]] by Vaswani et al. A key benefit is that it replaces sequential recurrence with highly parallelizable matrix operations, making training significantly faster.

```

### 🚀 Logseq OG Paradigm (Hierarchical Markdown)

The agent generates property-rich, nested outlines. Every concept is a node; every detail is a child; metadata is strictly scoped at the block level.

```markdown
- Attention Mechanism
  id:: 64a8c9f2-11e2-4b7b-a3d1-9f8c8d7e6a55
  tags:: [[Transformer]], [[Architecture]]
  summary:: A method allowing dynamic focus on input sequence parts.
  - Core Advantages
    - Replaces recurrence with parallelizable matrix operations.
      - **Impact**: Enables significantly faster hardware acceleration.
  - Literature & Provenance
    - source:: [[Attention Is All You Need]]
    - authors:: Vaswani et al.

```

### Why this changes everything for the Agent:

* **Surgical Targeting:** If the agent learns a new detail about hardware acceleration tomorrow, it doesn't rewrite the page. It calls the local Logseq API to append a child block *specifically* under the `Core Advantages` node.
* **Perfect Multi-Agent Synchronization:** The human can manually edit the markdown file. Because Logseq OG features native hot-reloading, the local graph updates instantly. The agent and the human never step on each other's toes.

---

## Architecture & How It Works

1. **The System Prompt:** Directives that force the LLM to abandon flat paragraphs and output strictly structured, nested Markdown outlines with auto-generated UUIDs.
2. **The FastMCP Bridge:** A lightweight Model Context Protocol (MCP) server built with `FastMCP`. It exposes tools to the agent, validates hierarchical JSON payloads via Pydantic, and interacts asynchronously with Logseq's local HTTP API (`localhost:12315`) to surgically insert blocks.
3. **L1 fast context:** The `read_l1_memory` tool loads small Markdown “session rules” from `MATRYCA_L1_PATH` or `matryca-l1/*.md` next to your graph—analogous to an L1 cache so critical constraints load without scanning the whole vault.
4. **Graph health tools:** `lint_logseq_block_refs` scans for broken `((uuid))` references; `render_logseq_dashboard` builds a **[[Matryca Dashboard]]**-style stats outline from the same graph path.
5. **The Spatial RAG Pipeline:** Instead of blind text-chunking, this project natively integrates the [**Logseq Matryca Parser**](https://github.com/MarcoPorcellato/logseq-matryca-parser) as a core dependency. It calculates spatial indentation and preserves the exact parent-child lineage of every atomic block for high-fidelity vector retrieval.

## Getting Started (Developer Setup)

We use [uv](https://docs.astral.sh/uv/) for blazing-fast dependency management and a `Makefile` for a world-class Developer Experience.

1. **Prerequisites:** Install Python 3.12+ and `uv`.
2. **Clone & Install:**
   ```bash
   git clone https://github.com/MarcoPorcellato/matryca-logseq-llm-wiki.git
   cd matryca-logseq-llm-wiki
   make install
   ```
3. **Configure Environment:**
   ```bash
   cp .env.example .env
   ```
   *(Edit `.env` to add your Logseq API Token and Graph Path).*
4. **Run Quality Checks:**
   ```bash
   make check
   ```
   *(This instantly runs Ruff formatting, linting, MyPy strict type-checking, and Pytest).*

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for deeper technical details.

Optional **``matryca-wiki.yml``** (see ``matryca-wiki.example.yml`` in the repo root) configures namespaces, L1 ``memory_path``, wiki lint filename prefix, and dashboard title—resolved from ``MATRYCA_WIKI_CONFIG`` or ``$LOGSEQ_GRAPH_PATH/matryca-wiki.yml``.

## The Goal

To prove that we do not need to abandon plain-text files to build advanced, agentic knowledge graphs. By pairing the simplicity of Markdown with the structural discipline of the Logseq outliner, we give AI agents a memory structure that perfectly mirrors the neural networks they operate on.
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **FastMCP integration** — MCP server (`FastMCP`) with stdio transport, lifespan wiring, and outline-writing tool surface (`src/main.py`).
- **Logseq HTTP JSON-RPC async client** — `httpx`-based `LogseqClient` with Bearer authentication, JSON-RPC envelope modeling, and `logseq.Editor.insertBlock` support (`src/bridge/logseq_client.py`).
- **Makefile developer experience** — targets for install (`uv`), format, lint, typecheck, test, aggregate `check`, and clean (`Makefile`).
- **GitHub Actions CI** — workflow running Ruff, Mypy, and Pytest on push and pull request to `main` (`.github/workflows/ci.yml`).
- **Pydantic data validation** — hierarchical `OutlineNode` models and validated tool payloads in the MCP bridge (`src/agent/mcp_server.py`).
- **Apache License 2.0** — project licensing as declared in `LICENSE` and `pyproject.toml`.

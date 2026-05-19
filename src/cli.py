"""Agent-native CLI for the Logseq graph (Printing Press paradigm).

Machine-oriented stdout; errors on stderr. Bypasses MCP JSON-RPC overhead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv

from .agent.graph_dispatch import (
    build_logseq_bridge,
    dispatch_lint,
    dispatch_mutate,
    dispatch_read,
    dispatch_refactor,
    dispatch_search,
)
from .agent.graph_tool_helpers import (
    MutateGraphAction,
    ReadGraphTarget,
    RefactorBlocksAction,
    RunLinterName,
    SearchGraphMethod,
)
from .config import load_matryca_wiki_config

READ_TARGETS: tuple[ReadGraphTarget, ...] = (
    "page",
    "memory",
    "block_ast",
    "structural_hops",
    "dashboard",
    "xray_page",
)
SEARCH_METHODS: tuple[SearchGraphMethod, ...] = (
    "bm25",
    "regex",
    "unlinked_mentions",
    "journal_tasks",
)
MUTATE_ACTIONS: tuple[MutateGraphAction, ...] = (
    "write_outline",
    "edit_property",
    "append_journal",
    "inject_query",
)
REFACTOR_ACTIONS: tuple[RefactorBlocksAction, ...] = (
    "split_large",
    "reparent",
    "generate_flashcards",
)
LINTER_NAMES: tuple[RunLinterName, ...] = (
    "unify_tags",
    "block_refs",
    "full_wiki_scan",
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level CLI parser and five domain subcommands."""
    parser = argparse.ArgumentParser(prog="matryca", description="Agent-native Logseq graph CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    read_p = sub.add_parser(
        "read",
        help="Read graph data (pages, memory, block AST, hops, dashboard)",
    )
    read_p.add_argument(
        "target_type",
        choices=READ_TARGETS,
        help="Read plane discriminator",
    )
    read_p.add_argument(
        "query",
        nargs="?",
        default="",
        help="Target-specific query string (optional for memory/dashboard)",
    )

    search_p = sub.add_parser("search", help="Search the on-disk graph")
    search_p.add_argument(
        "method",
        choices=SEARCH_METHODS,
        help="Search method",
    )
    search_p.add_argument(
        "query",
        nargs="?",
        default="",
        help="Keywords, regex, or JSON options",
    )

    mutate_p = sub.add_parser("mutate", help="Create or patch graph content")
    mutate_p.add_argument(
        "action",
        choices=MUTATE_ACTIONS,
        help="Mutation action",
    )
    mutate_p.add_argument(
        "--target",
        required=True,
        help="Parent block UUID or Page Title|block-uuid (use '' for append_journal)",
    )
    mutate_p.add_argument(
        "--payload",
        required=True,
        help="JSON outline, property edit spec, journal markdown, or query payload",
    )

    refactor_p = sub.add_parser("refactor", help="Restructure blocks on disk")
    refactor_p.add_argument(
        "action",
        choices=REFACTOR_ACTIONS,
        help="Refactor action",
    )
    refactor_p.add_argument(
        "target_uuid",
        help="Page title, optional page for split_large, or Page Title|block-uuid",
    )
    refactor_p.add_argument(
        "--payload",
        default="",
        help="Optional JSON options or reparent groups array",
    )

    lint_p = sub.add_parser("lint", help="Run vault hygiene linters")
    lint_p.add_argument(
        "linter_name",
        choices=LINTER_NAMES,
        help="Linter to run",
    )

    return parser


def _emit_result(result: str | dict[str, Any]) -> None:
    if isinstance(result, dict):
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(result)
        if not result.endswith("\n"):
            sys.stdout.write("\n")


def _emit_error(message: str) -> None:
    sys.stderr.write(message)
    if not message.endswith("\n"):
        sys.stderr.write("\n")


async def run_cli(args: argparse.Namespace) -> int:
    """Dispatch parsed CLI arguments to graph handlers."""
    wiki_config = load_matryca_wiki_config()
    command = args.command

    if command == "read":
        read_out = await dispatch_read(
            wiki_config,
            args.target_type,
            args.query,
        )
        _emit_result(read_out)
        return 0

    if command == "search":
        search_out: str | dict[str, Any] = await dispatch_search(args.method, args.query)
        _emit_result(search_out)
        return 0

    if command == "mutate":
        bridge = build_logseq_bridge()
        try:
            mutate_out: dict[str, Any] = await dispatch_mutate(
                bridge,
                args.action,
                args.target,
                args.payload,
            )
        finally:
            await bridge.aclose()
        _emit_result(mutate_out)
        if mutate_out.get("ok") is False:
            return 1
        return 0

    if command == "refactor":
        refactor_out: dict[str, Any] = await dispatch_refactor(
            args.action,
            args.target_uuid,
            args.payload,
        )
        _emit_result(refactor_out)
        if refactor_out.get("ok") is False:
            return 1
        return 0

    if command == "lint":
        lint_out: str | dict[str, Any] = await dispatch_lint(wiki_config, args.linter_name)
        _emit_result(lint_out)
        return 0

    _emit_error(f"unknown command: {command}")
    return 2


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint: load ``.env``, parse args, run async dispatch."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = asyncio.run(run_cli(args))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _emit_error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

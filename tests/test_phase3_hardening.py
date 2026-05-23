"""Phase 3 L5 enterprise audit: GC, telemetry self-healing, and TUI sanitization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.agent.maintenance_daemon import (
    DaemonState,
    heal_daemon_state_ledger,
    save_daemon_state,
)
from src.cli.ui_server import app
from src.graph.alias_index import AliasIndex, purge_stale_alias_entries
from src.graph.generational_cache import (
    cached_build_alias_index,
    clear_generational_caches,
    gc_generational_alias_cache,
)
from src.graph.graph_analytics import compute_graph_analytics, reconcile_telemetry_ledger
from src.graph.master_catalog import MasterCatalog
from src.utils.console_sanitize import sanitize_for_console
from src.utils.token_logger import TokenLogger, format_activity_summary


@pytest.fixture
def graph_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pages").mkdir()
    monkeypatch.setenv("LOGSEQ_GRAPH_PATH", str(tmp_path))
    return tmp_path


def test_sanitize_for_console_strips_ansi_and_control_chars() -> None:
    raw = "\x1b[2J\x1b[31mTitle\x07\x1b[0m\n- safe bullet"
    cleaned = sanitize_for_console(raw)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "Title" in cleaned
    assert "\n- safe bullet" in cleaned


def test_format_activity_summary_sanitizes_page_names() -> None:
    summary = format_activity_summary(
        {
            "operation": "Concept Indexing",
            "target_file": "/graph/pages/\x1b[2JEvil.md",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "latency_seconds": 0.1,
        }
    )
    assert "\x1b" not in summary
    assert "Evil.md" in summary


def test_prune_missing_pages_purges_catalog_and_aliases(graph_root: Path) -> None:
    from src.graph.master_catalog import CatalogEntry, clear_master_catalog_cache

    pages = graph_root / "pages"
    live = pages / "Live.md"
    live.write_text("alias:: Alive\n- note\n", encoding="utf-8")
    dead = pages / "Dead.md"
    dead.write_text("alias:: Gone\n- note\n", encoding="utf-8")

    clear_master_catalog_cache(graph_root)
    catalog = MasterCatalog(graph_root=graph_root)
    catalog.upsert("Live", CatalogEntry(summary="live", last_mtime=1))
    catalog.upsert("Dead", CatalogEntry(summary="dead", last_mtime=1))
    catalog.rebuild_alias_index()
    assert "Dead" in catalog.pages
    assert "Dead" in catalog.alias_to_page.values()

    dead.unlink()
    pruned = catalog.prune_missing_pages()
    assert pruned >= 1
    assert "Dead" not in catalog.pages
    assert "Dead" not in catalog.alias_to_page.values()


def test_gc_generational_alias_cache_drops_deleted_pages(graph_root: Path) -> None:
    clear_generational_caches()
    pages = graph_root / "pages"
    keep = pages / "Keep.md"
    keep.write_text("- stay\n", encoding="utf-8")
    drop = pages / "Drop.md"
    drop.write_text("alias:: Dropped\n- note\n", encoding="utf-8")

    idx = cached_build_alias_index(graph_root)
    assert "Drop" in idx.page_to_relpath

    drop.unlink()
    purged = gc_generational_alias_cache(graph_root)
    assert purged >= 1
    idx_after = cached_build_alias_index(graph_root)
    assert "Drop" not in idx_after.page_to_relpath
    assert "Drop" not in idx_after.alias_to_page.values()


def test_purge_stale_alias_entries_is_in_place() -> None:
    idx = AliasIndex(graph_root="/tmp/graph")
    idx.page_to_relpath["Live"] = "pages/Live.md"
    idx.page_to_relpath["Dead"] = "pages/Dead.md"
    idx.alias_to_page["gone"] = "Dead"
    idx.page_to_aliases["Dead"] = ["Gone"]

    purged = purge_stale_alias_entries(idx, {"Live"})
    assert purged >= 1
    assert "Dead" not in idx.page_to_relpath
    assert "gone" not in idx.alias_to_page


def test_reconcile_telemetry_ledger_clamps_after_mass_delete(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    page = pages / "Human.md"
    page.write_text("- link [[Other]]\n", encoding="utf-8")
    (pages / "Other.md").write_text("- note\n", encoding="utf-8")

    snapshot = reconcile_telemetry_ledger(
        tmp_path,
        ai_links_injected=500,
        ai_blocks_healed=40,
        ai_pages_created=99,
    )
    assert snapshot.healed is True
    assert snapshot.ai_links_injected == 1
    assert snapshot.ai_blocks_healed <= 2
    assert snapshot.ai_pages_created == 2


def test_heal_daemon_state_ledger_persists_to_disk(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Only.md").write_text("- one\n", encoding="utf-8")

    state = DaemonState(ai_links_injected=50, ai_blocks_healed=20, ai_pages_created=10)
    save_daemon_state(tmp_path, state)

    healed = heal_daemon_state_ledger(tmp_path, state)
    assert healed is True
    assert state.ai_links_injected == 0
    save_daemon_state(tmp_path, state)

    reloaded = DaemonState.from_json(
        json.loads((tmp_path / ".matryca_daemon_state.json").read_text(encoding="utf-8")),
    )
    assert reloaded.ai_links_injected == 0


def test_compute_graph_analytics_human_links_never_underflow(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Human.md").write_text("- link [[Other]]\n", encoding="utf-8")
    (pages / "Other.md").write_text("- note\n", encoding="utf-8")

    metrics = compute_graph_analytics(tmp_path, ai_links_injected=999, ai_blocks_healed=0)
    assert metrics.total_links == 1
    assert metrics.human_links == 0
    assert metrics.ai_links == 999


def test_get_state_self_heals_stale_ledger(graph_root: Path) -> None:
    pages = graph_root / "pages"
    pages.mkdir(exist_ok=True)
    (pages / "Human.md").write_text("- note\n", encoding="utf-8")

    state = DaemonState(status="idle", ai_links_injected=100, ai_blocks_healed=50)
    save_daemon_state(graph_root, state)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ai_links_injected"] == 0
    assert payload["ai_blocks_healed"] == 1
    assert payload["graph_analytics"]["human_links"] == 0


def test_token_logger_log_turn_sanitizes_prompt(tmp_path: Path) -> None:
    log_path = tmp_path / "ops.log"
    token_logger = TokenLogger(log_path=log_path)
    token_logger.log_turn(
        target_file=tmp_path / "\x1b[2JEvil.md",
        operation="Concept Indexing",
        prompt_tokens=1,
        completion_tokens=1,
        prompt="\x1b[31msecret prompt",
        response="ok",
        latency_seconds=0.01,
    )
    raw = log_path.read_text(encoding="utf-8")
    assert "\x1b" not in raw
    summaries = token_logger.tail_summaries(1)
    assert summaries
    assert "\x1b" not in summaries[0]

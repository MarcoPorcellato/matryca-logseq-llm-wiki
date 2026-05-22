"""Tests for Logseq page-level property injection."""

from __future__ import annotations

from src.graph.page_properties import inject_page_properties, inject_page_property


def test_inject_page_property_places_frontmatter_before_first_bullet() -> None:
    original = "- Launch MVP #project\n  - child milestone\n  Deliverable due soon\n"
    updated = inject_page_property(original, "type", "progetto")
    lines = updated.splitlines()
    type_idx = next(i for i, line in enumerate(lines) if "type:: progetto" in line)
    first_bullet_idx = next(i for i, line in enumerate(lines) if line.startswith("- "))
    assert type_idx < first_bullet_idx
    assert not lines[type_idx].lstrip().startswith("- ")
    assert lines[first_bullet_idx - 1].strip() == ""


def test_inject_page_property_appends_to_existing_frontmatter() -> None:
    original = "title:: Progetti/Lancio\n\n- First actual block\n"
    updated = inject_page_property(original, "type", "progetto")
    lines = updated.splitlines()
    title_idx = next(i for i, line in enumerate(lines) if "title:: Progetti/Lancio" in line)
    type_idx = next(i for i, line in enumerate(lines) if "type:: progetto" in line)
    first_bullet_idx = next(i for i, line in enumerate(lines) if line.startswith("- "))
    assert title_idx < type_idx < first_bullet_idx
    assert lines[first_bullet_idx - 1].strip() == ""


def test_inject_page_properties_idempotent_in_frontmatter() -> None:
    original = "- Root note\n"
    once = inject_page_properties(original, {"type": "risorsa", "status": "active"})
    twice = inject_page_properties(once, {"type": "risorsa", "status": "draft"})
    assert once.count("type:: risorsa") == 1
    assert "status:: active" in twice
    assert "status:: draft" not in twice


def test_inject_page_property_creates_frontmatter_when_missing() -> None:
    updated = inject_page_property("", "alias", "Example")
    assert updated.startswith("alias:: Example")
    assert not updated.lstrip().startswith("- ")


def test_inject_page_property_example_shape() -> None:
    original = "- First actual block of text...\n"
    updated = inject_page_properties(
        original,
        {"title": "Progetti/Lancio", "type": "progetto"},
    )
    assert updated.splitlines()[:4] == [
        "title:: Progetti/Lancio",
        "type:: progetto",
        "",
        "- First actual block of text...",
    ]


def test_inject_page_property_merges_alias_comma_separated() -> None:
    original = "alias:: Artificial Intelligence\n\n- note\n"
    updated = inject_page_property(original, "alias", "AI")
    assert updated.count("alias::") == 1
    assert "alias:: Artificial Intelligence, AI" in updated


def test_inject_page_property_merges_tags_comma_separated() -> None:
    original = "tags:: learning, ml\n\n- note\n"
    updated = inject_page_property(original, "tags", "AI")
    assert updated.count("tags::") == 1
    assert "tags:: learning, ml, AI" in updated


def test_inject_page_property_alias_skips_duplicate_value() -> None:
    original = "alias:: Artificial Intelligence\n\n- note\n"
    updated = inject_page_property(original, "alias", "Artificial Intelligence")
    assert updated == original

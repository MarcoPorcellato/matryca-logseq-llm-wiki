"""Lightweight persistent JSON catalog for Matryca Plumber graph scalability."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from .alias_index import iter_alias_source_paths, page_title_from_path
from .markdown_blocks import atomic_write_bytes

CATALOG_FILENAME = "master_catalog.json"
CATALOG_VERSION = 1
SEMANTIC_INDEX_HEADER = "### Matryca Semantic Index"
MASTER_INDEX_PAGE_TITLE = "Matryca Master Index"
MATRYCA_GENERATED_INDEX_TITLES = frozenset(
    {MASTER_INDEX_PAGE_TITLE, "Matryca Graph Insights"},
)

_SUMMARY_LINE = re.compile(r"^\s*-\s*summary::\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TAGS_LINE = re.compile(r"^\s*-\s*suggested-tags::\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TYPE_LINE = re.compile(r"^\s*type::\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_MARPA_DOMAINS = frozenset({"mappa", "area", "risorsa", "progetto", "archivio"})

_lock = threading.Lock()
_loaded: dict[str, MasterCatalog] = {}


@dataclass(slots=True)
class CatalogEntry:
    """One page row in the master catalog."""

    summary: str = ""
    domain: str = ""
    tags: list[str] = field(default_factory=list)
    last_mtime: int = 0
    orphan: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "domain": self.domain,
            "tags": list(self.tags),
            "last_mtime": self.last_mtime,
            "orphan": self.orphan,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> CatalogEntry:
        raw_tags = payload.get("tags", [])
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        return cls(
            summary=str(payload.get("summary", "")),
            domain=str(payload.get("domain", "")),
            tags=tags,
            last_mtime=int(payload.get("last_mtime", 0)),
            orphan=bool(payload.get("orphan", False)),
        )


@dataclass
class MasterCatalog:
    """In-memory master catalog backed by ``.matryca_semantic_cache/master_catalog.json``."""

    graph_root: Path
    version: int = CATALOG_VERSION
    updated_at: str | None = None
    pages: dict[str, CatalogEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @staticmethod
    def catalog_path(graph_root: Path) -> Path:
        return graph_root / ".matryca_semantic_cache" / CATALOG_FILENAME

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            pages_payload = {title: entry.to_json() for title, entry in sorted(self.pages.items())}
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "pages": pages_payload,
        }

    @classmethod
    def from_json(cls, graph_root: Path, payload: dict[str, Any]) -> MasterCatalog:
        pages: dict[str, CatalogEntry] = {}
        raw_pages = payload.get("pages", {})
        if isinstance(raw_pages, dict):
            for title, rec in raw_pages.items():
                if isinstance(rec, dict):
                    pages[str(title)] = CatalogEntry.from_json(rec)
        return cls(
            graph_root=graph_root,
            version=int(payload.get("version", CATALOG_VERSION)),
            updated_at=payload.get("updated_at"),
            pages=pages,
        )

    def save(self) -> None:
        """Persist catalog atomically under the graph root."""
        with self._lock:
            self.updated_at = datetime.now(tz=UTC).isoformat()
            payload = {
                "version": self.version,
                "updated_at": self.updated_at,
                "pages": {title: entry.to_json() for title, entry in sorted(self.pages.items())},
            }
        path = self.catalog_path(self.graph_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        atomic_write_bytes(
            path,
            data.encode("utf-8"),
            graph_root=self.graph_root,
            validate_block_refs=False,
        )

    def upsert(self, page_title: str, entry: CatalogEntry) -> None:
        with self._lock:
            self.pages[page_title] = entry

    def get(self, page_title: str) -> CatalogEntry | None:
        with self._lock:
            return self.pages.get(page_title)

    def remove(self, page_title: str) -> None:
        with self._lock:
            self.pages.pop(page_title, None)

    def needs_refresh(self, page_title: str, mtime_ns: int) -> bool:
        """Return True when the on-disk page is newer than the catalog row."""
        with self._lock:
            entry = self.pages.get(page_title)
            if entry is None:
                return True
            return int(mtime_ns // 1_000_000_000) != entry.last_mtime

    def prune_missing_pages(self) -> int:
        """Drop catalog rows for deleted markdown files."""
        live_titles = {
            page_title_from_path(self.graph_root, path)
            for path in iter_alias_source_paths(self.graph_root)
        }
        with self._lock:
            stale = [title for title in self.pages if title not in live_titles]
            for title in stale:
                del self.pages[title]
            return len(stale)


def load_master_catalog(graph_root: Path, *, force_reload: bool = False) -> MasterCatalog:
    """Load catalog into RAM at startup (cached per graph root)."""
    root = graph_root.expanduser().resolve(strict=False)
    key = str(root)
    with _lock:
        if not force_reload and key in _loaded:
            return _loaded[key]

        path = MasterCatalog.catalog_path(root)
        if not path.is_file():
            catalog = MasterCatalog(graph_root=root)
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                catalog = MasterCatalog(graph_root=root)
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    "[METADATA CORRUPTION DETECTED] File state was malformed, "
                    "self-healing by initializing a fresh instance."
                )
                catalog = MasterCatalog(graph_root=root)
            else:
                if isinstance(payload, dict):
                    catalog = MasterCatalog.from_json(root, payload)
                else:
                    catalog = MasterCatalog(graph_root=root)
        _loaded[key] = catalog
        return catalog


def clear_master_catalog_cache(graph_root: Path | None = None) -> None:
    """Drop in-process catalog cache (tests)."""
    with _lock:
        if graph_root is None:
            _loaded.clear()
            return
        key = str(graph_root.expanduser().resolve(strict=False))
        _loaded.pop(key, None)


def _normalize_domain(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in _MARPA_DOMAINS else ""


def _parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for chunk in re.split(r"\s+", raw.strip()):
        token = chunk.strip().lstrip("#")
        if token:
            tags.append(token.lower())
    return tags


def extract_catalog_fields_from_content(content: str) -> CatalogEntry | None:
    """Fast regex read of an existing ``### Matryca Semantic Index`` block."""
    if SEMANTIC_INDEX_HEADER not in content:
        return None

    summary_match = _SUMMARY_LINE.search(content)
    if summary_match is None:
        return None
    summary = summary_match.group(1).strip()
    if not summary:
        return None

    tags: list[str] = []
    tags_match = _TAGS_LINE.search(content)
    if tags_match:
        tags = _parse_tags(tags_match.group(1))

    domain = ""
    type_match = _TYPE_LINE.search(content)
    if type_match:
        domain = _normalize_domain(type_match.group(1))
    if not domain:
        for line in content.splitlines()[:20]:
            if line.strip().lower().startswith("- type::"):
                domain = _normalize_domain(line.split("::", 1)[1])
                break

    return CatalogEntry(summary=summary, domain=domain, tags=tags)


def entry_from_page_path(graph_root: Path, page_path: Path) -> CatalogEntry | None:
    """Build a catalog entry from on-disk semantic index metadata."""
    if not page_path.is_file():
        return None
    try:
        content = page_path.read_text(encoding="utf-8", errors="replace")
        mtime = int(page_path.stat().st_mtime)
    except OSError:
        return None

    extracted = extract_catalog_fields_from_content(content)
    if extracted is None:
        return None
    extracted.last_mtime = mtime
    return extracted


def list_stale_page_paths(graph_root: Path, catalog: MasterCatalog) -> list[Path]:
    """Return markdown paths whose mtime differs from the catalog row."""
    stale: list[Path] = []
    for path in iter_alias_source_paths(graph_root):
        title = page_title_from_path(graph_root, path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if catalog.needs_refresh(title, mtime_ns):
            stale.append(path)
    return stale


def build_master_index_markdown(catalog: MasterCatalog) -> str:
    """Compile ``pages/Matryca Master Index.md`` grouped by MARPA domain."""
    domain_order = ["mappa", "area", "risorsa", "progetto", "archivio", ""]
    grouped: dict[str, list[tuple[str, CatalogEntry]]] = {d: [] for d in domain_order}
    for title, entry in catalog.pages.items():
        if title in MATRYCA_GENERATED_INDEX_TITLES:
            continue
        domain = entry.domain if entry.domain in _MARPA_DOMAINS else ""
        grouped[domain].append((title, entry))

    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "- type:: hub",
        f"- updated:: {stamp}",
        "- # Matryca Master Index",
        "",
        f"_Auto-generated catalog of {len(catalog.pages)} indexed page(s)._",
        "",
    ]

    labels = {
        "mappa": "Mappa — strategic vision",
        "area": "Area — ongoing operations",
        "risorsa": "Risorsa — timeless reference",
        "progetto": "Progetto — time-bounded initiatives",
        "archivio": "Archivio — closed or dormant",
        "": "Uncategorized",
    }

    for domain in domain_order:
        rows = sorted(grouped[domain], key=lambda item: item[0].lower())
        if not rows:
            continue
        lines.append(f"- ## {labels[domain]}")
        lines.append("  collapsed:: true")
        for title, entry in rows:
            summary = entry.summary.strip()
            if summary:
                lines.append(f"  - [[{title}]] — {summary}")
            else:
                lines.append(f"  - [[{title}]]")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def master_index_page_path(graph_root: Path) -> Path:
    """Return the on-disk path for the compiled master index page."""
    return graph_root / "pages" / f"{MASTER_INDEX_PAGE_TITLE}.md"


def is_bootstrap_catalog_complete(graph_root: Path) -> bool:
    """Return True when every scannable page has a catalog summary and the master index exists."""
    root = graph_root.expanduser().resolve(strict=False)
    if not master_index_page_path(root).is_file():
        return False

    catalog = load_master_catalog(root)
    if not catalog.pages:
        return False

    for path in iter_alias_source_paths(root):
        title = page_title_from_path(root, path)
        if title in MATRYCA_GENERATED_INDEX_TITLES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return False
        if not content.strip():
            continue
        entry = catalog.get(title)
        if entry is None or not entry.summary.strip():
            return False
        if catalog.needs_refresh(title, mtime_ns):
            return False
    return True


def write_master_index_page(graph_root: Path, catalog: MasterCatalog) -> Path:
    """Write the compiled master index page under ``pages/``."""
    from .path_sandbox import graph_safe_page_path

    md = build_master_index_markdown(catalog)
    path = graph_safe_page_path(graph_root, "Matryca Master Index")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, md.encode("utf-8"), graph_root=graph_root)
    return path


__all__ = [
    "CATALOG_FILENAME",
    "CatalogEntry",
    "MASTER_INDEX_PAGE_TITLE",
    "MATRYCA_GENERATED_INDEX_TITLES",
    "MasterCatalog",
    "SEMANTIC_INDEX_HEADER",
    "build_master_index_markdown",
    "clear_master_catalog_cache",
    "entry_from_page_path",
    "extract_catalog_fields_from_content",
    "is_bootstrap_catalog_complete",
    "list_stale_page_paths",
    "load_master_catalog",
    "master_index_page_path",
    "write_master_index_page",
]

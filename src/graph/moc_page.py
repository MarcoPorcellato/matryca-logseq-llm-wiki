"""Generate Map-of-Content style Markdown indexes for a namespace stem."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .markdown_blocks import graph_safe_page_path


def _safe_page_write_path(graph_root: Path, page_title: str) -> Path:
    raw = page_title.strip()
    if not raw or ".." in raw or raw.startswith(("/", "\\")):
        msg = "invalid_page_title"
        raise ValueError(msg)
    safe = raw.replace("/", "___")
    return graph_safe_page_path(graph_root, safe)


def collect_pages_for_namespace(graph_root: Path, namespace: str) -> list[tuple[str, str]]:
    """Return sorted ``(title, relpath)`` under ``pages/*.md`` matching namespace."""
    ns = namespace.strip()
    pages = graph_root / "pages"
    out: list[tuple[str, str]] = []
    if not pages.is_dir() or not ns:
        return out
    for p in sorted(pages.glob("*.md")):
        stem = p.stem
        title = stem.replace("___", "/")
        if stem == ns or stem.startswith(f"{ns}___") or title.startswith(f"{ns}/"):
            rel = p.relative_to(graph_root).as_posix()
            out.append((title, rel))
    return out


def build_moc_markdown(graph_root: str | Path, namespace: str) -> str:
    root = Path(graph_root).expanduser().resolve(strict=False)
    rows = collect_pages_for_namespace(root, namespace)
    lines = [
        f"# MOC — `{namespace}`",
        "",
        f"_Auto-generated index; {len(rows)} page(s)._",
        "",
    ]
    if not rows:
        lines.append("_No matching pages under `pages/`._")
        return "\n".join(lines) + "\n"

    tree: dict[str, list[str]] = {}
    for title, _rel in rows:
        rest = title[len(namespace) :].lstrip("/")
        if not rest:
            tree.setdefault("_root", []).append(title)
        else:
            head = rest.split("/", maxsplit=1)[0]
            tree.setdefault(head, []).append(title)

    for head in sorted(tree.keys(), key=lambda k: (k == "_root", k.lower())):
        if head == "_root":
            lines.append("## Root")
        else:
            lines.append(f"## {namespace} / {head}")
        lines.append("")
        for t in sorted(tree[head]):
            lines.append(f"- [[{t}]]")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_moc_page(
    graph_root: str | Path,
    namespace: str,
    *,
    output_page_title: str | None = None,
    dry_run: bool = True,
) -> dict[str, object]:
    root = Path(graph_root).expanduser().resolve(strict=False)
    md = build_moc_markdown(root, namespace)
    title = (output_page_title or f"MOC {namespace}").strip()
    try:
        path = _safe_page_write_path(root, title)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "output_path": str(path.relative_to(root)),
            "markdown_preview": md[:1200],
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    fd, tmp = tempfile.mkstemp(prefix="matryca-moc-", suffix=".md", dir=str(path.parent))
    try:
        with open(fd, "wb", closefd=True) as fh:
            fh.write(md.encode("utf-8"))
        Path(tmp).replace(path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise

    return {
        "ok": True,
        "dry_run": False,
        "output_path": str(path.relative_to(root)),
        "bytes_written": path.stat().st_size,
    }


__all__ = ["build_moc_markdown", "collect_pages_for_namespace", "write_moc_page"]

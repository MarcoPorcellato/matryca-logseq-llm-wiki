#!/usr/bin/env python3
"""Generate a synthetic Logseq graph for load/memory testing (default 100 pages)."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic pages/*.md for perf tests")
    parser.add_argument("graph_root", type=Path, help="Graph root (pages/ will be created)")
    parser.add_argument("--count", type=int, default=100, help="Number of pages (max 10000)")
    args = parser.parse_args()
    count = max(1, min(args.count, 10_000))
    pages = args.graph_root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        path = pages / f"Synthetic_{i:05d}.md"
        path.write_text(
            f"- Synthetic page {i} with [[Synthetic_{(i - 1) % count:05d}]]\n"
            f"  #tag{i % 7} status:: active\n",
            encoding="utf-8",
        )
    print(f"Wrote {count} pages under {pages}")


if __name__ == "__main__":
    main()

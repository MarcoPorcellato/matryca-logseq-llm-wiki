"""Pytest hooks: ensure sibling ``logseq-matryca-parser`` src is importable in dev."""

from __future__ import annotations

import sys
from pathlib import Path

_PARSER_SRC = Path(__file__).resolve().parents[2] / "logseq-matryca-parser" / "src"
if _PARSER_SRC.is_dir():
    parser_src = str(_PARSER_SRC)
    if parser_src not in sys.path:
        sys.path.insert(0, parser_src)

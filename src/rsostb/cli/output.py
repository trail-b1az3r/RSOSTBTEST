"""Small terminal-output helpers (no dependencies)."""
from __future__ import annotations

import os
import sys
from typing import Any


def _color(code: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _color("1", t)


def green(t: str) -> str:
    return _color("32", t)


def red(t: str) -> str:
    return _color("31", t)


def yellow(t: str) -> str:
    return _color("33", t)


def dim(t: str) -> str:
    return _color("2", t)


def table(headers: list[str], rows: list[list[Any]], align_right: set[int] | None = None) -> str:
    align_right = align_right or set()
    cells = [[("" if c is None else (f"{c:,.2f}" if isinstance(c, float) else str(c))) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    def fmt(row: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) if i in align_right else c.ljust(widths[i]) for i, c in enumerate(row))
    lines = [bold(fmt(headers)), dim("  ".join("-" * w for w in widths))]
    lines += [fmt(r) for r in cells]
    return "\n".join(lines)

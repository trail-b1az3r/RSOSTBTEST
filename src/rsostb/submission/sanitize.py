"""Sanitising untrusted metadata before it is stored or displayed."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_MARKUP = re.compile(r"<\s*/?\s*(script|iframe|object|embed|style|link|meta|svg|img|a|form|input|body|html)\b|javascript:|on\w+\s*=",
                     re.I)


def clean_text(value: Any, limit: int = 256) -> str | None:
    """Plain text only: NFC, no control/bidi characters, bounded length.
    Display layers still HTML-escape; this is defence in depth."""
    if value is None:
        return None
    s = unicodedata.normalize("NFC", str(value))
    s = _CONTROL.sub("", s).strip()
    return s[:limit]


def looks_like_markup(value: Any) -> bool:
    return isinstance(value, str) and bool(_MARKUP.search(value))


def safe_slug(value: str, limit: int = 64) -> str:
    """A filesystem-safe name (never used with user paths directly)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return (s or "x")[:limit]


def scan_markup(doc: dict[str, Any], fields: list[tuple[str, ...]]) -> list[str]:
    hits = []
    for path in fields:
        cur: Any = doc
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, list):
            if any(looks_like_markup(x) for x in cur):
                hits.append(".".join(path))
        elif looks_like_markup(cur):
            hits.append(".".join(path))
    return hits

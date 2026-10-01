"""Value decoding and comparison shared by the structural and code evaluators."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .extract import normalize_text


class Opaque:
    """A value that crossed the sandbox boundary only as a repr."""

    def __init__(self, text: str, type_name: str = "") -> None:
        self.text, self.type_name = text, type_name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Opaque) and other.text == self.text

    def __repr__(self) -> str:
        return f"Opaque({self.text[:60]!r})"


def _freeze(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(_freeze(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _freeze(x)) for k, x in v.items()))
    if isinstance(v, set):
        return frozenset(_freeze(x) for x in v)
    return v


def decode(v: Any) -> Any:
    """Inverse of the in-sandbox encoders (Python worker, C++ and JS harnesses)."""
    if isinstance(v, list):
        return [decode(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            (k, x), = v.items()
            if k == "__tuple__":
                return [decode(y) for y in x]
            if k == "__set__":
                return {_freeze(decode(y)) for y in x}
            if k == "__dict__":
                return {kk: decode(y) for kk, y in x.items()}
            if k == "__pairs__":
                return {_freeze(decode(a)): decode(b) for a, b in x}
            if k == "__float__":
                return float(x)
            if k == "__bigint__":
                return int(x)
            if k == "__bytes__":
                return bytes.fromhex(x)
            if k == "__iter__":
                return [decode(y) for y in x]
            if k == "__undefined__":
                return None
            if k == "__complex__":
                return complex(*x)
        if "__repr__" in v:
            return Opaque(v["__repr__"], v.get("__type__", ""))
        return {k: decode(x) for k, x in v.items()}
    return v


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def values_equal(actual: Any, expected: Any, *, rel_tol: float = 0.0, abs_tol: float = 0.0,
                 normalize_strings: bool = False) -> bool:
    if _is_num(actual) and _is_num(expected):
        if isinstance(actual, float) or isinstance(expected, float):
            if math.isnan(actual) and math.isnan(expected):
                return True
            return math.isclose(actual, expected, rel_tol=rel_tol or 1e-9, abs_tol=abs_tol or 1e-12)
        return actual == expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, str) and isinstance(expected, str):
        if normalize_strings:
            return normalize_text(actual, ignore_articles=False) == normalize_text(expected, ignore_articles=False)
        return actual == expected
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return len(actual) == len(expected) and all(
            values_equal(a, e, rel_tol=rel_tol, abs_tol=abs_tol, normalize_strings=normalize_strings)
            for a, e in zip(actual, expected))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(map(str, actual)) != set(map(str, expected)):
            return False
        amap = {str(k): v for k, v in actual.items()}
        return all(values_equal(amap[str(k)], e, rel_tol=rel_tol, abs_tol=abs_tol, normalize_strings=normalize_strings)
                   for k, e in expected.items())
    if isinstance(actual, (set, frozenset)) and isinstance(expected, (list, set, frozenset, tuple)):
        return actual == {_freeze(x) for x in expected}
    if isinstance(actual, bytes) and isinstance(expected, str):
        return actual.decode("utf-8", "replace") == expected
    return actual == expected


def _sort_key(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def compare(actual: Any, expected: Any, mode: str = "exact", **opts: Any) -> bool:
    rel = float(opts.get("rel_tol", 0) or 0)
    abs_tol = float(opts.get("abs_tol", 0) or 0)
    norm = bool(opts.get("normalize_strings", False))
    if mode == "exact":
        return values_equal(actual, expected, normalize_strings=norm)
    if mode == "approx":
        return values_equal(actual, expected, rel_tol=rel or 1e-6, abs_tol=abs_tol or 1e-9, normalize_strings=norm)
    if mode in ("unordered", "sorted"):
        if not isinstance(actual, (list, tuple, set, frozenset)) or not isinstance(expected, (list, tuple)):
            return False
        a = sorted([_plain(x) for x in actual], key=_sort_key)
        e = sorted([_plain(x) for x in expected], key=_sort_key)
        return values_equal(a, e, rel_tol=rel, abs_tol=abs_tol, normalize_strings=norm)
    if mode == "set":
        try:
            return {_freeze(_plain(x)) for x in actual} == {_freeze(x) for x in expected}
        except TypeError:
            return False
    if mode == "stdout":
        return _norm_out(actual) == _norm_out(expected)
    if mode == "stdout_exact":
        return str(actual) == str(expected)
    if mode == "contains":
        return str(expected).lower() in str(actual).lower()
    if mode == "regex":
        return re.search(str(expected), str(actual), re.S) is not None
    if mode == "one_of":
        return any(values_equal(actual, e, normalize_strings=norm) for e in expected)
    raise ValueError(f"unknown compare mode {mode!r}")


def _plain(x: Any) -> Any:
    if isinstance(x, tuple):
        return [_plain(y) for y in x]
    if isinstance(x, frozenset):
        return sorted((_plain(y) for y in x), key=_sort_key)
    return x


def _norm_out(s: Any) -> str:
    lines = [ln.rstrip() for ln in str(s).replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# --------------------------------------------------------------------------- similarity for partial credit

def similarity(actual: Any, expected: Any, *, normalize_strings: bool = True, rel_tol: float = 1e-6,
               key: str | None = None) -> float:
    """Recursive partial-credit similarity in [0, 1]."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return 0.0
        if not expected:
            return 1.0 if not actual else 0.5
        amap = {str(k): v for k, v in actual.items()}
        scores = [similarity(amap.get(str(k)), v, normalize_strings=normalize_strings, rel_tol=rel_tol)
                  if str(k) in amap else 0.0 for k, v in expected.items()]
        extra = len(set(amap) - {str(k) for k in expected})
        return sum(scores) / (len(expected) + 0.5 * extra)
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return 0.0
        if not expected:
            return 1.0 if not actual else 0.0
        if key and all(isinstance(e, dict) and key in e for e in expected):
            amap = {str(a.get(key)): a for a in actual if isinstance(a, dict)}
            s = sum(similarity(amap.get(str(e[key])), e, normalize_strings=normalize_strings, rel_tol=rel_tol)
                    for e in expected)
            extra = len(set(amap) - {str(e[key]) for e in expected})
            return s / (len(expected) + extra)
        n = max(len(actual), len(expected))
        return sum(similarity(a, e, normalize_strings=normalize_strings, rel_tol=rel_tol)
                   for a, e in zip(actual, expected)) / n
    return 1.0 if values_equal(actual, expected, rel_tol=rel_tol, normalize_strings=normalize_strings) else 0.0


def multiset_f1(actual: list[Any], expected: list[Any], *, normalize_strings: bool = True) -> float:
    """F1 between two lists treated as multisets (order-insensitive)."""
    if not expected and not actual:
        return 1.0
    remaining = list(expected)
    tp = 0
    for a in actual:
        for i, e in enumerate(remaining):
            if values_equal(a, e, rel_tol=1e-6, normalize_strings=normalize_strings):
                tp += 1
                remaining.pop(i)
                break
    if tp == 0:
        return 0.0
    precision = tp / len(actual)
    recall = tp / len(expected)
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------- paths

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def get_path(obj: Any, path: str) -> tuple[bool, Any]:
    cur = obj
    for m in _PATH_TOKEN.finditer(path):
        name, idx = m.group(1), m.group(2)
        if name is not None:
            if not isinstance(cur, dict) or name not in cur:
                return False, None
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                return False, None
            cur = cur[i]
    return True, cur

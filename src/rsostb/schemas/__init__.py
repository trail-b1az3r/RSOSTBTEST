"""JSON Schema access and validation.

The schemas themselves live in ``benchmark/schemas`` (and are bundled in the
wheel) so non-Python tooling can use them directly.
"""
from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from ..paths import benchmark_dir

SCHEMA_FILES = {
    "task": "task.schema.json",
    "tool": "tool.schema.json",
    "tool_call": "tool_call.schema.json",
    "result": "result.schema.json",
    "leaderboard_entry": "leaderboard_entry.schema.json",
}


def schema_dir() -> Path:
    return benchmark_dir() / "schemas"


@cache
def load_schema(name: str) -> dict[str, Any]:
    path = schema_dir() / SCHEMA_FILES.get(name, name)
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources = []
    for fname in SCHEMA_FILES.values():
        schema = load_schema(fname)
        res = Resource.from_contents(schema)
        resources.append((fname, res))
        resources.append((schema["$id"], res))
    return Registry().with_resources(resources)


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), registry=_registry())


def schema_errors(name: str, instance: Any, limit: int = 20) -> list[str]:
    """Human-readable schema violations (empty list when valid)."""
    out = []
    for err in sorted(validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{loc}: {err.message[:300]}")
        if len(out) >= limit:
            break
    return out

"""Reading and writing results files (``results.json`` and ``results.jsonl``).

JSONL layout: one ``{"type": "header", ...}`` line (everything except scores
and task results), one ``{"type": "task_result", ...}`` line per task, and a
final ``{"type": "summary", "scores": {...}}`` line.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_RESULTS_BYTES = 64 * 1024 * 1024


class ResultsFormatError(ValueError):
    pass


def write_results(doc: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".jsonl":
        header = {k: v for k, v in doc.items() if k not in ("task_results", "scores")}
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "header", **header}, ensure_ascii=False) + "\n")
            for tr in doc.get("task_results", []):
                fh.write(json.dumps({"type": "task_result", **tr}, ensure_ascii=False) + "\n")
            fh.write(json.dumps({"type": "summary", "scores": doc.get("scores")}, ensure_ascii=False) + "\n")
    else:
        p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def parse_results_text(text: str, name: str = "") -> dict[str, Any]:
    if len(text.encode("utf-8", "ignore")) > MAX_RESULTS_BYTES:
        raise ResultsFormatError("results file is too large")
    stripped = text.lstrip()
    if not stripped:
        raise ResultsFormatError("results file is empty")
    jsonl = name.endswith(".jsonl") or ("\n" in stripped and stripped.startswith("{\"type\""))
    if not jsonl:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResultsFormatError(f"malformed JSON: {exc}") from None
        if not isinstance(doc, dict):
            raise ResultsFormatError("results must be a JSON object")
        return doc
    doc: dict[str, Any] = {}
    trs: list[dict[str, Any]] = []
    seen_header = seen_summary = False
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResultsFormatError(f"line {i}: malformed JSON: {exc}") from None
        if not isinstance(rec, dict):
            raise ResultsFormatError(f"line {i}: expected an object")
        kind = rec.pop("type", None)
        if kind == "header":
            if seen_header:
                raise ResultsFormatError("duplicate header line")
            seen_header = True
            doc.update(rec)
        elif kind == "task_result":
            trs.append(rec)
        elif kind == "summary":
            if seen_summary:
                raise ResultsFormatError("duplicate summary line")
            seen_summary = True
            doc["scores"] = rec.get("scores")
        else:
            raise ResultsFormatError(f"line {i}: unknown record type {kind!r}")
    if not seen_header:
        raise ResultsFormatError("missing header line")
    doc["task_results"] = trs
    return doc


def read_results(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if p.stat().st_size > MAX_RESULTS_BYTES:
        raise ResultsFormatError("results file is too large")
    return parse_results_text(p.read_text(encoding="utf-8"), p.name)

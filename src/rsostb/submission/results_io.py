"""Reading and writing results files (``results.json`` and ``results.jsonl``,
optionally gzip-compressed as ``.json.gz`` / ``.jsonl.gz``).

JSONL layout: one ``{"type": "header", ...}`` line (everything except scores
and task results), one ``{"type": "task_result", ...}`` line per task, and a
final ``{"type": "summary", "scores": {...}}`` line.
"""
from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

MAX_RESULTS_BYTES = 64 * 1024 * 1024


class ResultsFormatError(ValueError):
    pass


GZIP_MAGIC = b"\x1f\x8b"


def _base_name(name: str) -> str:
    return name[:-3] if name.endswith(".gz") else name


def serialize_results(doc: dict[str, Any], name: str) -> str:
    if _base_name(name).endswith(".jsonl"):
        header = {k: v for k, v in doc.items() if k not in ("task_results", "scores")}
        lines = [json.dumps({"type": "header", **header}, ensure_ascii=False)]
        lines += [json.dumps({"type": "task_result", **tr}, ensure_ascii=False) for tr in doc.get("task_results", [])]
        lines.append(json.dumps({"type": "summary", "scores": doc.get("scores")}, ensure_ascii=False))
        return "\n".join(lines) + "\n"
    return json.dumps(doc, indent=1, ensure_ascii=False)


def write_results(doc: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = serialize_results(doc, p.name)
    if p.name.endswith(".gz"):
        # mtime=0 and an empty header name keep the compressed bytes identical
        # for identical results, whatever the file is called.
        with open(p, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(text.encode("utf-8"))
    else:
        p.write_text(text, encoding="utf-8")
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


def parse_results_bytes(data: bytes, name: str = "") -> dict[str, Any]:
    """Parse raw bytes (plain or gzip). Decompression is capped at
    MAX_RESULTS_BYTES so a crafted archive cannot exhaust memory."""
    if data[:2] == GZIP_MAGIC:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            try:
                data = gz.read(MAX_RESULTS_BYTES + 1)
            except (OSError, EOFError) as exc:
                raise ResultsFormatError(f"corrupt gzip data: {exc}") from None
        if len(data) > MAX_RESULTS_BYTES:
            raise ResultsFormatError("results file is too large (decompressed)")
    elif len(data) > MAX_RESULTS_BYTES:
        raise ResultsFormatError("results file is too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResultsFormatError("results file is not valid UTF-8") from None
    return parse_results_text(text, _base_name(name))


def read_results(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if p.stat().st_size > MAX_RESULTS_BYTES:
        raise ResultsFormatError("results file is too large")
    return parse_results_bytes(p.read_bytes(), p.name)

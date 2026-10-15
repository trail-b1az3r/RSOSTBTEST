"""The leaderboard data API.

Stable, read-only views over validated entries, used by the Hugging Face
Space and published as ``leaderboard.json``. Nothing here returns secrets or
raw model responses.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import load_benchmark_config
from .store import LeaderboardStore

API_VERSION = "1.0"


def category_names() -> dict[str, str]:
    return {c.id: c.name for c in load_benchmark_config().categories}


def metric_names() -> dict[str, str]:
    names = {m.id: m.name for m in load_benchmark_config().metrics}
    return {"rsostb_score": "RSOSTB Score", **names}


def leaderboard_rows(entries: list[dict[str, Any]], benchmark_version: str | None = None,
                     kinds: tuple[str, ...] = ("model", "baseline", "synthetic"), full_only: bool = False,
                     scoring_version: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for e in entries:
        if benchmark_version and benchmark_version != "all" and e["benchmark_version"] != benchmark_version:
            continue
        if scoring_version and scoring_version != "all" and e["scoring_version"] != scoring_version:
            continue
        if e["model"].get("kind", "model") not in kinds:
            continue
        if full_only and not e.get("full_run"):
            continue
        m = e["model"]
        rows.append({
            "submission_id": e["submission_id"],
            "model": m["name"],
            "kind": m.get("kind", "model"),
            "provider": m.get("provider"),
            "benchmark_version": e["benchmark_version"],
            "scoring_version": e["scoring_version"],
            "effort": (e.get("effort") or {}).get("level"),
            "hardware_class": e.get("hardware_class"),
            "efficiency": (e.get("efficiency") or {}).get("normalized"),
            "rsostb_score": e["scores"]["rsostb_score"],
            "gpu_score": (e.get("gpu") or {}).get("gpu_score"),
            "tasks": e["n_tasks"],
            "full_run": e.get("full_run", False),
            "coverage": round(e.get("coverage", 0.0), 4),
            "judge": e.get("judge") or "none",
            "date": (e.get("timestamp") or "")[:10],
            "hardware": "; ".join(e.get("hardware") or [])[:160],
            "parameters": m.get("parameters"),
            "quantization": m.get("quantization"),
            "context_length": m.get("context_length"),
            "validation": e["validation_status"],
            **{f"cat:{k}": v for k, v in e["scores"]["categories"].items()},
            **{f"metric:{k}": v for k, v in e["scores"]["metrics"].items() if k != "rsostb_score"},
        })
    rows.sort(key=lambda r: r["rsostb_score"], reverse=True)
    return rows


def comparison(entries: list[dict[str, Any]], ids: list[str]) -> dict[str, Any]:
    from .views import methodology

    chosen = [e for e in entries if e["submission_id"] in ids]
    m = methodology(chosen)
    return {"entries": chosen, "comparable": m["comparable"], "methodology": m,
            "note": " ".join(m["notes"]) or None}


def build_api(store: LeaderboardStore, out: str | Path | None = None) -> dict[str, Any]:
    entries = store.entries()
    from .views import benchmark_versions, scoring_versions

    versions = benchmark_versions(entries)
    doc = {
        "api_version": API_VERSION,
        "benchmark": "RSOSTBTEST-pro",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark_versions": versions,
        "scoring_versions": scoring_versions(entries),
        "categories": category_names(),
        "metrics": metric_names(),
        "entries": entries,
    }
    if out:
        Path(out).write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return doc

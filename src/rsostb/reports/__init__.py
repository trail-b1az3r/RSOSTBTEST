"""Reports: ``report.json``, ``report.md`` and ``report.html`` from a results file."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .builder import CAVEATS, build_report
from .html import render_html
from .markdown import render_markdown


def write_reports(results: dict[str, Any], out_dir: str | Path, formats: tuple[str, ...] = ("json", "md", "html"),
                  bench=None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(results, bench=bench)
    paths: dict[str, Path] = {}
    if "json" in formats:
        p = out / "report.json"
        p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        paths["json"] = p
    if "md" in formats:
        p = out / "report.md"
        p.write_text(render_markdown(report), encoding="utf-8")
        paths["md"] = p
    if "html" in formats:
        p = out / "report.html"
        p.write_text(render_html(report), encoding="utf-8")
        paths["html"] = p
    return paths


__all__ = ["CAVEATS", "build_report", "render_html", "render_markdown", "write_reports"]

"""Leaderboard views shared by the Gradio Space, the static Space and the CLI.

Pure functions over leaderboard entries (``leaderboard.json``): no UI code.

* :func:`scoring_versions` / :func:`benchmark_versions` — what the dropdowns
  offer, taken from the entries that actually exist.
* :func:`methodology` — whether a set of entries can be compared, and the
  label to show when it cannot (different scoring versions are different
  scales; different benchmark versions are different task sets).
* :func:`graph` — the data behind every chart, in two modes: ``one_vs_all``
  (one entry against everyone scored the same way: their median and best)
  and ``selected`` (the chosen entries side by side). Chart types: bar,
  column, dot, radar, line (multi-line) and scatter.
* :func:`graph_csv`, :func:`graph_json`, :func:`rows_csv`, :func:`rows_json` — exports.

Scores are shown as recorded. Nothing here re-scores an entry, so a v1 entry
keeps its v1 score for good.
"""
from __future__ import annotations

import csv
import io
import json
import re
import statistics
from typing import Any

CHART_TYPES = ("bar", "column", "dot", "radar", "line", "scatter")
MODES = ("one_vs_all", "selected")
DIMENSIONS = {"categories": "Categories", "metrics": "Metrics", "difficulty": "Difficulty",
              "families": "Evaluation families"}
DIFFICULTY_ORDER = ["easy", "medium", "hard", "expert", "adversarial", "1", "2", "3", "4", "5", "6"]
LEVEL_NAMES = {"1": "Level 1 (easy)", "2": "Level 2 (medium)", "3": "Level 3 (hard)", "4": "Level 4 (expert)",
               "5": "Level 5 (adversarial)", "6": "Level 6 (frontier)"}
SCATTER_MEASURES = {
    "rsostb_score": "RSOSTB Score", "normalized": "Normalised score", "efficiency": "Efficiency E",
    "tokens_per_second": "Output tokens/s", "tokens_per_task": "Output tokens per task",
    "parameters_b": "Parameters (billions)", "coverage": "Coverage",
}


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-]", str(v).lstrip("v")))


def scoring_versions(entries: list[dict[str, Any]]) -> list[str]:
    return sorted({e["scoring_version"] for e in entries}, key=_vkey)


def benchmark_versions(entries: list[dict[str, Any]]) -> list[str]:
    return sorted({e["benchmark_version"] for e in entries}, key=_vkey)


def methodology_of(e: dict[str, Any]) -> str:
    return f"benchmark {e['benchmark_version']} · scoring {e['scoring_version']}"


def filter_entries(entries: list[dict[str, Any]], *, benchmark_version: str | None = None,
                   scoring_version: str | None = None, kinds: tuple[str, ...] | None = None,
                   full_only: bool = False, search: str = "") -> list[dict[str, Any]]:
    q = (search or "").lower()
    out = []
    for e in entries:
        if benchmark_version and benchmark_version != "all" and e["benchmark_version"] != benchmark_version:
            continue
        if scoring_version and scoring_version != "all" and e["scoring_version"] != scoring_version:
            continue
        if kinds is not None and e["model"].get("kind", "model") not in kinds:
            continue
        if full_only and not e.get("full_run"):
            continue
        m = e["model"]
        if q and q not in (m.get("name") or "").lower() and q not in (m.get("provider") or "").lower():
            continue
        out.append(e)
    return out


def methodology(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether these entries are comparable, and why not."""
    sv = scoring_versions(entries)
    bv = benchmark_versions(entries)
    keys = {e["compat_key"] for e in entries}
    notes = []
    if len(sv) > 1:
        notes.append(f"Different scoring methods ({', '.join(sv)}): the scores are on different scales and "
                     "are not comparable. Compare entries scored the same way.")
    if len(bv) > 1:
        notes.append(f"Different benchmark versions ({', '.join(bv)}): different task sets; category and metric "
                     "percentages are only roughly comparable.")
    if len(keys) > 1 and len(sv) <= 1 and len(bv) <= 1:
        notes.append("Same benchmark and scoring version, but different judges or scoring configuration: "
                     "not directly comparable.")
    return {"comparable": len(keys) <= 1, "scoring_versions": sv, "benchmark_versions": bv, "notes": notes,
            "label": "comparable" if len(keys) <= 1 else ("different methodologies" if len(sv) > 1 or len(bv) > 1
                                                         else "different configuration")}


def entry_name(e: dict[str, Any], *, with_method: bool = False) -> str:
    name = f"{e['model']['name']} [{e['submission_id'][:6]}]"
    return f"{name} ({methodology_of(e)})" if with_method else name


# --------------------------------------------------------------------------- dimensions

def _dim_items(e: dict[str, Any], dimension: str, names: dict[str, str]) -> dict[str, float | None]:
    s = e["scores"]
    if dimension == "categories":
        return {k: v for k, v in (s.get("categories") or {}).items()}
    if dimension == "metrics":
        return {k: v for k, v in (s.get("metrics") or {}).items() if k != "rsostb_score"}
    if dimension == "difficulty":
        return dict(s.get("difficulty") or {})
    if dimension == "families":
        return dict(s.get("evaluation_families") or {})
    raise ValueError(f"unknown dimension {dimension!r}; one of {', '.join(DIMENSIONS)}")


def _order(keys: set[str], dimension: str, names: dict[str, str]) -> list[str]:
    if dimension == "difficulty":
        return [k for k in DIFFICULTY_ORDER if k in keys] + sorted(keys - set(DIFFICULTY_ORDER))
    known = [k for k in names if k in keys]
    return known + sorted(keys - set(known))


def _label(k: str, dimension: str, names: dict[str, str]) -> str:
    if dimension == "difficulty":
        return LEVEL_NAMES.get(k, k)
    return names.get(k, k)


def measure(e: dict[str, Any], key: str) -> float | None:
    """A scalar for scatter plots."""
    s = e["scores"]
    eff = e.get("efficiency") or {}
    if key == "rsostb_score":
        return s.get("rsostb_score")
    if key == "normalized":
        return s.get("normalized")
    if key == "efficiency":
        return eff.get("normalized")
    if key == "tokens_per_second":
        return eff.get("output_tokens_per_second")
    if key == "tokens_per_task":
        return eff.get("output_tokens_per_task")
    if key == "coverage":
        return e.get("coverage")
    if key == "parameters_b":
        from ..scoring.gpu import parse_parameters

        return parse_parameters(e["model"].get("parameters")) or parse_parameters(e["model"].get("name"))
    if key.startswith("metric:"):
        return (s.get("metrics") or {}).get(key[7:])
    if key.startswith("cat:"):
        return (s.get("categories") or {}).get(key[4:])
    raise ValueError(f"unknown measure {key!r}")


def measure_label(key: str, names: dict[str, str]) -> str:
    if key in SCATTER_MEASURES:
        return SCATTER_MEASURES[key]
    if ":" in key:
        return f"{names.get(key.split(':', 1)[1], key.split(':', 1)[1])} (%)"
    return key


# --------------------------------------------------------------------------- graphs

def graph(entries: list[dict[str, Any]], *, mode: str = "selected", dimension: str = "categories",
          chart: str = "bar", focus: str | None = None, selected: list[str] | None = None,
          names: dict[str, str] | None = None, x: str = "efficiency", y: str = "rsostb_score",
          order: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """The data behind one chart. ``names`` maps category/metric ids to display names;
    ``order`` gives each dimension's display order (defaults to the order of ``names``)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; one of {', '.join(MODES)}")
    if chart not in CHART_TYPES:
        raise ValueError(f"unknown chart type {chart!r}; one of {', '.join(CHART_TYPES)}")
    names = names or {}
    by_id = {e["submission_id"]: e for e in entries}
    notes: list[str] = []
    if mode == "one_vs_all":
        f = by_id.get(focus or "")
        if f is None:
            return _empty(mode, dimension, chart, "Pick an entry to compare with everyone else.")
        peers = [e for e in entries if e["submission_id"] != f["submission_id"]
                 and e["scoring_version"] == f["scoring_version"] and e["benchmark_version"] == f["benchmark_version"]]
        others = len([e for e in entries if e["submission_id"] != f["submission_id"]]) - len(peers)
        notes.append(f"Compared with {len(peers)} other entr{'y' if len(peers) == 1 else 'ies'} scored the same way "
                     f"({methodology_of(f)}).")
        if others:
            notes.append(f"{others} entr{'y' if others == 1 else 'ies'} scored another way {'is' if others == 1 else 'are'} "
                         "left out: different methodologies are not comparable.")
        chosen, method = [f], methodology([f, *peers])
    else:
        chosen = [by_id[i] for i in (selected or []) if i in by_id]
        if not chosen:
            return _empty(mode, dimension, chart, "Pick one or more entries.")
        peers, method = [], methodology(chosen)
        notes += method["notes"]
    mixed = len(method["scoring_versions"]) > 1 or len(method["benchmark_versions"]) > 1

    if chart == "scatter":
        pool = [*chosen, *peers]
        points = []
        for e in pool:
            px, py = measure(e, x), measure(e, y)
            points.append({"name": entry_name(e, with_method=mixed), "submission_id": e["submission_id"],
                           "role": "focus" if mode == "one_vs_all" and e is chosen[0] else
                           ("peer" if mode == "one_vs_all" else "entry"),
                           "scoring_version": e["scoring_version"], "benchmark_version": e["benchmark_version"],
                           "x": px, "y": py})
        missing = [p for p in points if p["x"] is None or p["y"] is None]
        if missing:
            one = len(missing) == 1
            notes.append(f"{len(missing)} entr{'y has' if one else 'ies have'} no value for "
                         f"{measure_label(x, names)} or {measure_label(y, names)} and "
                         f"{'is' if one else 'are'} not plotted.")
        return {"mode": mode, "chart": chart, "dimension": None, "title": f"{measure_label(y, names)} vs "
                f"{measure_label(x, names)}", "x_label": measure_label(x, names), "y_label": measure_label(y, names),
                "x": x, "y": y, "unit": None, "dimensions": [], "series": [], "points": points, "notes": notes,
                "comparable": method["comparable"]}

    items = {e["submission_id"]: _dim_items(e, dimension, names) for e in [*chosen, *peers]}
    keys = set()
    for e in chosen:
        keys |= set(items[e["submission_id"]])
    display = (order or {}).get(dimension) or list(names)
    order_keys = _order(keys, dimension, display)
    series = []
    for e in chosen:
        series.append({"name": entry_name(e, with_method=mixed), "role": "focus" if mode == "one_vs_all" else "entry",
                       "submission_id": e["submission_id"], "scoring_version": e["scoring_version"],
                       "benchmark_version": e["benchmark_version"],
                       "values": [items[e["submission_id"]].get(k) for k in order_keys]})
    if mode == "one_vs_all" and peers:
        med, best = [], []
        for k in order_keys:
            vals = [items[p["submission_id"]].get(k) for p in peers]
            vals = [v for v in vals if isinstance(v, (int, float))]
            med.append(statistics.median(vals) if vals else None)
            best.append(max(vals) if vals else None)
        series.append({"name": f"All others: median (n={len(peers)})", "role": "median", "values": med})
        series.append({"name": f"All others: best (n={len(peers)})", "role": "best", "values": best})
    if chart == "radar" and len(order_keys) < 3:
        notes.append("A radar chart needs at least three dimensions.")
    elif chart == "radar" and len(order_keys) > 16:
        notes.append(f"{len(order_keys)} dimensions make a crowded radar; the bar or dot chart shows the same data.")
    return {"mode": mode, "chart": chart, "dimension": dimension,
            "title": f"{DIMENSIONS[dimension]} — " + ("one entry vs all" if mode == "one_vs_all" else "selected entries"),
            "unit": "%", "dimensions": [_label(k, dimension, names) for k in order_keys], "keys": order_keys,
            "series": series, "points": [], "notes": notes, "comparable": method["comparable"],
            "x_label": None, "y_label": "% of possible points"}


def _empty(mode, dimension, chart, why):
    return {"mode": mode, "chart": chart, "dimension": dimension, "title": why, "unit": None, "dimensions": [],
            "series": [], "points": [], "notes": [why], "comparable": True, "x_label": None, "y_label": None}


# --------------------------------------------------------------------------- exports

def _csv(rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return int(v)          # 0 rather than 0.0: the same text as the static page's export
        return v

    for r in rows:
        w.writerow([cell(v) for v in r])
    return buf.getvalue()


def graph_csv(g: dict[str, Any]) -> str:
    if g["chart"] == "scatter":
        rows = [["entry", "submission_id", "scoring_version", "benchmark_version", g["x_label"], g["y_label"]]]
        rows += [[p["name"], p["submission_id"], p["scoring_version"], p["benchmark_version"], p["x"], p["y"]]
                 for p in g["points"]]
        return _csv(rows)
    rows = [[DIMENSIONS.get(g["dimension"], "dimension"), *[s["name"] for s in g["series"]]]]
    for i, d in enumerate(g["dimensions"]):
        rows.append([d, *[s["values"][i] for s in g["series"]]])
    return _csv(rows)


def graph_json(g: dict[str, Any]) -> str:
    return json.dumps({"format": "rsostb-graph", "format_version": "1.0", **g}, indent=2, ensure_ascii=False) + "\n"


TABLE_COLUMNS = ["rank", "model", "kind", "provider", "rsostb_score", "scoring_version", "benchmark_version",
                 "effort", "hardware_class", "efficiency", "tokens_per_second", "gpu_score", "tasks", "full_run",
                 "coverage", "judge", "validation", "date", "submission_id"]


def table_rows(entries: list[dict[str, Any]], names: dict[str, str] | None = None,
               with_categories: bool = True) -> list[dict[str, Any]]:
    """Flat rows for tables and CSV/JSON export, ranked by score within each scoring version."""
    names = names or {}
    rows = []
    for e in entries:
        m, eff = e["model"], e.get("efficiency") or {}
        row = {"model": m["name"], "kind": m.get("kind", "model"), "provider": m.get("provider"),
               "rsostb_score": e["scores"]["rsostb_score"], "scoring_version": e["scoring_version"],
               "benchmark_version": e["benchmark_version"], "effort": (e.get("effort") or {}).get("level"),
               "hardware_class": e.get("hardware_class"), "efficiency": eff.get("normalized"),
               "tokens_per_second": eff.get("output_tokens_per_second"),
               "gpu_score": (e.get("gpu") or {}).get("gpu_score"), "tasks": e["n_tasks"],
               "full_run": bool(e.get("full_run")), "coverage": e.get("coverage"), "judge": e.get("judge") or "none",
               "validation": e["validation_status"], "date": (e.get("timestamp") or "")[:10],
               "submission_id": e["submission_id"]}
        for k, v in (e["scores"].get("metrics") or {}).items():
            if k != "rsostb_score":
                row[f"metric:{k}"] = v
        if with_categories:
            for k, v in (e["scores"].get("categories") or {}).items():
                row[f"cat:{k}"] = v
        rows.append(row)
    rows.sort(key=lambda r: (_vkey(r["scoring_version"]), -r["rsostb_score"]))
    rank: dict[str, int] = {}
    for r in rows:
        rank[r["scoring_version"]] = rank.get(r["scoring_version"], 0) + 1
        r["rank"] = rank[r["scoring_version"]]
    return rows


def rows_csv(rows: list[dict[str, Any]], names: dict[str, str] | None = None) -> str:
    names = names or {}
    extra = sorted({k for r in rows for k in r} - set(TABLE_COLUMNS))
    cols = TABLE_COLUMNS + extra
    head = [c if ":" not in c else f"{names.get(c.split(':', 1)[1], c.split(':', 1)[1])} (%)" for c in cols]
    return _csv([head] + [[r.get(c) for c in cols] for r in rows])


def rows_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps({"format": "rsostb-leaderboard-table", "format_version": "1.0", "rows": rows}, indent=2,
                      ensure_ascii=False) + "\n"

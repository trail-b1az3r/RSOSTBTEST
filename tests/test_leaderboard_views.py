"""Phase 5: cross-version leaderboard views, graphs (two modes, six chart types) and exports."""
from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import ROOT

from rsostb.leaderboard import charts, views
from rsostb.leaderboard.api import build_api, category_names, comparison, leaderboard_rows, metric_names
from rsostb.leaderboard.store import LeaderboardStore
from rsostb.submission.results_io import read_results

V11 = ROOT / "tests" / "regression" / "v1" / "benchmark-1.1"
V12 = ROOT / "examples" / "results"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    st = LeaderboardStore(tmp_path_factory.mktemp("lb"))
    for p in sorted(V12.glob("*.jsonl.gz")) + sorted(V11.glob("*.jsonl.gz")):
        st.add(read_results(p))
    return st


@pytest.fixture(scope="module")
def entries(store):
    return store.entries()


@pytest.fixture(scope="module")
def names():
    return {**category_names(), **metric_names()}


def _by(entries, version, name):
    return next(e for e in entries if e["scoring_version"] == version and name in e["model"]["adapter"])


# --------------------------------------------------------------------------- versions and history

def test_dropdowns_come_from_the_entries(entries, store):
    assert views.scoring_versions(entries) == ["v1", "v2"]
    assert views.benchmark_versions(entries) == ["1.1", "1.2"]
    api = build_api(store)
    assert api["scoring_versions"] == ["v1", "v2"] and api["benchmark_versions"] == ["1.1", "1.2"]


def test_v1_history_is_shown_as_recorded(entries):
    for p in sorted(V11.glob("*.jsonl.gz")):
        doc = read_results(p)
        if doc["model"].get("kind") == "reference":
            continue
        e = next(x for x in entries if x["scoring_version"] == "v1" and x["model"]["name"] == doc["model"]["name"])
        assert e["scores"]["rsostb_score"] == doc["scores"]["rsostb_score"]       # never re-scored with v2
        assert e["benchmark_version"] == "1.1" and e["effort"] is None and e["breakdown"] is None


def test_filters_and_rows(entries):
    v2 = views.filter_entries(entries, scoring_version="v2")
    assert v2 and all(e["scoring_version"] == "v2" for e in v2)
    assert views.filter_entries(entries, scoring_version="all") == entries
    assert all(r["scoring_version"] == "v1" for r in leaderboard_rows(entries, scoring_version="v1"))
    rows = views.table_rows(entries)
    ranks = {}
    for r in rows:      # ranked within each scoring version, never across
        ranks.setdefault(r["scoring_version"], []).append(r["rank"])
    assert all(v == list(range(1, len(v) + 1)) for v in ranks.values())
    e = _by(entries, "v2", "noisy")
    row = next(r for r in rows if r["submission_id"] == e["submission_id"])
    assert row["hardware_class"] and row["effort"] == "default"


def test_methodology_labels(entries):
    a, b = _by(entries, "v2", "noisy"), _by(entries, "v1", "noisy")
    m = views.methodology([a, b])
    assert not m["comparable"] and m["label"] == "different methodologies"
    assert any("different scales" in n for n in m["notes"]) and any("task sets" in n for n in m["notes"])
    assert views.methodology([a, _by(entries, "v2", "echo")])["comparable"]
    cmp = comparison(entries, [a["submission_id"], b["submission_id"]])
    assert not cmp["comparable"] and "different scales" in cmp["note"]


# --------------------------------------------------------------------------- graphs

def test_one_vs_all_compares_only_like_with_like(entries, names):
    f = _by(entries, "v2", "noisy")
    g = views.graph(entries, mode="one_vs_all", focus=f["submission_id"], dimension="categories", names=names)
    assert [s["role"] for s in g["series"]] == ["focus", "median", "best"]
    n_peers = len([e for e in entries if e["scoring_version"] == "v2"]) - 1
    assert f"n={n_peers}" in g["series"][1]["name"]
    assert any("left out" in n for n in g["notes"])
    assert len(g["dimensions"]) == len(g["series"][0]["values"]) == 42
    assert g["dimensions"][0] == names[g["keys"][0]]


def test_selected_mode_labels_mixed_methodologies(entries, names):
    ids = [_by(entries, "v2", "noisy")["submission_id"], _by(entries, "v1", "noisy")["submission_id"]]
    g = views.graph(entries, mode="selected", selected=ids, dimension="difficulty", names=names, chart="line")
    assert not g["comparable"] and "scoring v1" in g["series"][1]["name"]
    assert "easy" in g["dimensions"] and "Level 1 (easy)" in g["dimensions"]      # v1 names and v2 levels


def test_scatter(entries, names):
    f = _by(entries, "v2", "noisy")
    g = views.graph(entries, mode="one_vs_all", focus=f["submission_id"], chart="scatter", x="metric:research",
                    y="rsostb_score", names=names)
    assert g["points"] and g["points"][0]["role"] == "focus" and all(p["scoring_version"] == "v2" for p in g["points"])
    assert g["x_label"] == "Research (%)"
    g = views.graph(entries, mode="one_vs_all", focus=f["submission_id"], chart="scatter", x="efficiency")
    assert any("not plotted" in n for n in g["notes"])            # baselines have no efficiency measurements


def test_bad_arguments(entries):
    with pytest.raises(ValueError, match="mode"):
        views.graph(entries, mode="everything")
    with pytest.raises(ValueError, match="chart"):
        views.graph(entries, chart="pie")
    assert views.graph(entries, mode="selected", selected=[])["series"] == []


# --------------------------------------------------------------------------- charts and exports

@pytest.mark.parametrize("chart", views.CHART_TYPES)
@pytest.mark.parametrize("mode", views.MODES)
def test_every_chart_renders_svg_and_png(entries, names, chart, mode):
    v2 = [e for e in entries if e["scoring_version"] == "v2"]
    g = views.graph(entries, mode=mode, focus=v2[0]["submission_id"],
                    selected=[e["submission_id"] for e in v2[:3]], chart=chart, dimension="metrics", names=names,
                    x="metric:research", y="rsostb_score")
    svg = charts.to_svg(g)
    root = ET.fromstring(svg)                      # well-formed XML
    assert root.tag.endswith("svg") and "<title>" in svg   # every mark has a tooltip
    png = charts.to_png(g)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 2000


def test_graph_exports(entries, names):
    f = _by(entries, "v2", "noisy")
    g = views.graph(entries, mode="one_vs_all", focus=f["submission_id"], dimension="metrics", names=names)
    rows = list(csv.reader(io.StringIO(views.graph_csv(g))))
    assert rows[0][0] == "Metrics" and len(rows) == len(g["dimensions"]) + 1 and len(rows[0]) == 4
    j = json.loads(views.graph_json(g))
    assert j["format"] == "rsostb-graph" and j["series"] == g["series"]
    s = views.graph(entries, mode="one_vs_all", focus=f["submission_id"], chart="scatter", x="metric:research",
                    names=names)
    assert list(csv.reader(io.StringIO(views.graph_csv(s))))[0][-2:] == ["Research (%)", "RSOSTB Score"]


def test_table_exports(entries, names):
    rows = views.table_rows(entries, names)
    text = views.rows_csv(rows, names)
    head = next(csv.reader(io.StringIO(text)))
    assert head[:5] == ["rank", "model", "kind", "provider", "rsostb_score"] and "Research (%)" in head
    assert json.loads(views.rows_json(rows))["rows"] == json.loads(json.dumps(rows))


def test_chart_text_is_escaped():
    g = {"chart": "bar", "title": "<script>x</script>", "unit": "%", "dimensions": ["a&b"], "notes": [],
         "series": [{"name": "<img src=x>", "role": "entry", "values": [10.0]}], "points": []}
    svg = charts.to_svg(g)
    ET.fromstring(svg)
    assert "<script>" not in svg and "<img" not in svg


def test_charts_module_matches_views():
    assert set(views.CHART_TYPES) == {"bar", "column", "dot", "radar", "line", "scatter"}
    assert Path(charts.__file__).is_file()


def test_png_without_pillow_says_how_to_install(monkeypatch, entries, names):
    import builtins

    real = builtins.__import__

    def no_pil(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    g = views.graph(entries, mode="selected", selected=[entries[0]["submission_id"]], names=names)
    with pytest.raises(ImportError, match=r"RSOSTB\[graphs\]"):
        charts.to_png(g)
    assert charts.to_svg(g).startswith("<svg")          # SVG needs nothing extra

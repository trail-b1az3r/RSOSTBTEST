"""The static Space draws its graphs with hf/static/charts.js, a port of
rsostb.leaderboard.views/charts. Run it under Node and check that it builds the
same graph data as Python for the same entries."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import ROOT

from rsostb.leaderboard import views
from rsostb.leaderboard.api import category_names, metric_names
from rsostb.leaderboard.store import LeaderboardStore
from rsostb.submission.results_io import read_results

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const C = require(process.argv[2]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = input.cases.map(o => {
  const g = C.graph(input.entries, { ...o, names: input.names, order: input.order });
  return { graph: g, csv: C.graphCSV(g), svg: C.toSVG(g).length };
});
out.push({ scoring: C.scoringVersions(input.entries), benchmark: C.benchmarkVersions(input.entries),
           methodology: C.methodology(input.entries) });
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def entries(tmp_path_factory):
    st = LeaderboardStore(tmp_path_factory.mktemp("lb"))
    for p in sorted((ROOT / "examples" / "results").glob("*.jsonl.gz")) + \
            sorted((ROOT / "tests" / "regression" / "v1" / "benchmark-1.1").glob("*.jsonl.gz")):
        st.add(read_results(p))
    return json.loads(json.dumps(st.entries()))      # as the page receives them


def _cases(entries):
    v2 = [e["submission_id"] for e in entries if e["scoring_version"] == "v2"]
    v1 = [e["submission_id"] for e in entries if e["scoring_version"] == "v1"]
    cases = []
    for chart in views.CHART_TYPES:
        for dim in views.DIMENSIONS:
            cases.append({"mode": "one_vs_all", "focus": v2[0], "chart": chart, "dimension": dim,
                          "x": "metric:reasoning", "y": "rsostb_score"})
        cases.append({"mode": "selected", "selected": [v2[0], v1[0], v2[1]], "chart": chart, "dimension": "difficulty",
                      "x": "coverage", "y": "metric:safety"})
    cases.append({"mode": "selected", "selected": [], "chart": "bar", "dimension": "metrics"})
    return cases


def test_js_graph_data_matches_python(entries, tmp_path):
    names = {**category_names(), **metric_names()}
    order = {"categories": list(category_names()), "metrics": list(metric_names())}
    cases = _cases(entries)
    script = tmp_path / "run.js"
    script.write_text(SCRIPT)
    res = subprocess.run([NODE, str(script), str(ROOT / "hf" / "static" / "charts.js")], input=json.dumps(
        {"entries": entries, "names": names, "order": order, "cases": cases}), capture_output=True, text=True,
        timeout=60, check=True)
    js = json.loads(res.stdout)
    for case, got in zip(cases, js):
        want = views.graph(entries, names=names, order=order, **case)
        want = json.loads(json.dumps(want))
        for k in ("mode", "chart", "dimension", "title", "dimensions", "points", "notes", "comparable",
                  "x_label", "y_label"):
            assert got["graph"].get(k) == want.get(k), (case, k)
        assert len(got["graph"]["series"]) == len(want["series"])
        for a, b in zip(got["graph"]["series"], want["series"]):
            assert {k: v for k, v in a.items() if k != "values"} == {k: v for k, v in b.items() if k != "values"}
            for i, (x, y) in enumerate(zip(a["values"], b["values"])):
                assert x == y, (case["chart"], case["dimension"], b["name"], want["dimensions"][i], x, y)
        assert got["csv"] == views.graph_csv(want), case
        assert got["svg"] > 200
    meta = js[-1]
    assert meta["scoring"] == views.scoring_versions(entries) == ["v1", "v2"]
    assert meta["benchmark"] == views.benchmark_versions(entries)
    assert meta["methodology"] == json.loads(json.dumps(views.methodology(entries)))


def test_page_inlines_charts_js(tmp_path):
    from rsostb.hf.static_space import CHARTS_PLACEHOLDER, PLACEHOLDER

    page = (ROOT / "hf" / "static" / "index.html").read_text(encoding="utf-8")
    assert page.count(CHARTS_PLACEHOLDER) == 1 and page.count(PLACEHOLDER) == 1
    assert "</script" not in (ROOT / "hf" / "static" / "charts.js").read_text(encoding="utf-8").lower()

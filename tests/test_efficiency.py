"""Phase 4: hardware classes, measured efficiency, effort levels, and the validator's recomputation."""
from __future__ import annotations

import copy
import math

import pytest

from rsostb.adapters import create_adapter
from rsostb.config import load_scoring_config
from rsostb.runner.env_info import classify, hardware_info
from rsostb.runner.runner import run_benchmark
from rsostb.scoring.common import ScoringError
from rsostb.scoring.efficiency import efficiency, measurements
from rsostb.scoring.v2 import validate_config
from rsostb.submission import validate_results


@pytest.fixture(scope="module")
def v2raw():
    return load_scoring_config(version="v2").raw


def _trs(n, out, lat, usage=True):
    return [{"task_id": f"t-{i:03d}", "latency_seconds": lat,
             "usage": {"output_tokens": out, "input_tokens": 50} if usage else None} for i in range(n)]


# --------------------------------------------------------------------------- hardware

@pytest.mark.parametrize("accels, remote, expected", [
    ([], False, "cpu"),
    ([], True, "remote"),
    ([{"vendor": "NVIDIA", "name": "GeForce RTX 3060", "memory_gb": 12.0, "count": 1}], False, "consumer_gpu"),
    ([{"vendor": "NVIDIA", "name": "A100-SXM4-80GB", "memory_gb": 80.0, "count": 8}], False, "datacenter_gpu"),
    ([{"vendor": "AMD", "name": "Instinct MI300X", "memory_gb": 192.0, "count": 1}], False, "datacenter_gpu"),
    ([{"vendor": "Apple", "name": "Apple M3 Max", "memory_gb": 64.0, "count": 1}], False, "apple_silicon"),
])
def test_hardware_classes(accels, remote, expected):
    assert classify(accels, remote) == expected


def test_gpu_age_is_not_an_input():
    old = {"vendor": "NVIDIA", "name": "Tesla K80", "memory_gb": 12.0, "count": 1, "release_year": 2014}
    new = {"vendor": "NVIDIA", "name": "GeForce RTX 5070", "memory_gb": 12.0, "count": 1, "release_year": 2025}
    assert classify([old]) == classify([new]) == "consumer_gpu"


def test_hardware_info_records_unknowns_and_declarations():
    info = hardware_info()
    assert info["class"] in ("cpu", "apple_silicon", "consumer_gpu", "datacenter_gpu")
    assert info["class_source"] == "detected"
    assert hardware_info(remote=True)["class"] == "remote"
    assert hardware_info(declared_class="consumer_gpu")["class_source"] == "declared"
    with pytest.raises(ValueError, match="unknown hardware class"):
        hardware_info(declared_class="quantum")


# --------------------------------------------------------------------------- measurements and E

def test_measurements_are_raw(v2raw):
    m = measurements(_trs(10, 200, 4.0) + _trs(2, 0, 0.0, usage=False))
    assert m == {"tasks": 12, "tasks_measured": 10, "measured_share": round(10 / 12, 6), "output_tokens": 2000,
                 "input_tokens": 500, "generation_seconds": 40.0, "output_tokens_per_second": 50.0,
                 "output_tokens_per_task": 200.0, "median_latency_seconds": 4.0}


def test_efficiency_components(v2raw):
    # 200 tokens per task vs 400 -> +1; 16 tok/s on cpu vs 8 -> +1
    e = efficiency(measurements(_trs(10, 200, 12.5)), "cpu", v2raw)
    assert e["components"] == {"tokens": 1.0, "speed": 1.0} and e["normalized"] == 1.0
    # the same run on a datacenter GPU is slow for its class
    e = efficiency(measurements(_trs(10, 200, 12.5)), "datacenter_gpu", v2raw)
    assert e["components"]["speed"] == -1.0 and e["normalized"] == pytest.approx(0.6 * 1 + 0.4 * -1)
    # 800 tokens per task -> -1
    assert efficiency(measurements(_trs(10, 800, 100)), "cpu", v2raw)["components"]["tokens"] == -1.0
    # exactly the references -> 0
    e = efficiency(measurements(_trs(10, 400, 50)), "cpu", v2raw)
    assert e["normalized"] == 0.0 and e["known"]


def test_unknown_stays_unknown(v2raw):
    e = efficiency(measurements(_trs(10, 200, 4.0)), "remote", v2raw)
    assert e["components"]["speed"] is None and e["normalized"] == 1.0 and "tokens only" in e["note"]
    e = efficiency(measurements(_trs(3, 200, 4.0) + _trs(7, 0, 0, usage=False)), "cpu", v2raw)
    assert e["normalized"] is None and not e["known"] and "3 of 10" in e["note"]
    assert efficiency(measurements([]), None, v2raw)["normalized"] is None


def test_efficiency_config_is_validated(v2raw):
    for path, value, match in [(("components", "speed"), 2, "components.speed"),
                               (("reference_tokens_per_second", "remote"), 50, "no speed reference"),
                               (("reference_tokens_per_second", "cpu"), math.nan, "finite"),
                               (("min_measured_share",), 1.5, "min_measured_share")]:
        raw = copy.deepcopy(v2raw)
        node = raw["efficiency"]
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        with pytest.raises(ScoringError, match=match):
            validate_config(raw)


# --------------------------------------------------------------------------- effort

def test_effort_is_capability_aware(mock_server):
    url, h = mock_server
    oa = create_adapter("openai", "m", base_url=url)
    assert oa.effort_plan("high")["applied"] and oa.effort_plan("max")["note"].startswith("openai supports")
    oa.chat([{"role": "user", "content": "hi"}], effort="high", max_tokens=10)
    assert h.seen[-1]["body"]["reasoning_effort"] == "high"
    local = create_adapter("vllm", "m", base_url=url)
    plan = local.effort_plan("high")
    assert not plan["applied"] and plan["level"] == "default" and "cannot set effort" in plan["note"]
    local.chat([{"role": "user", "content": "hi"}], effort="high")
    assert "reasoning_effort" not in h.seen[-1]["body"]
    assert create_adapter("oracle").effort_plan(None) == {"requested": None, "applied": False, "level": "default",
                                                          "supported": [], "mechanism": None, "note": None}


def test_anthropic_effort_is_a_thinking_budget(mock_server, monkeypatch):
    url, h = mock_server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    a = create_adapter("anthropic", "m", base_url=url)
    a.chat([{"role": "user", "content": "hi"}], effort="medium", max_tokens=100, temperature=0.0)
    body = h.seen[-1]["body"]
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert body["max_tokens"] == 4196 and "temperature" not in body
    a.chat([{"role": "user", "content": "hi"}], effort="none", max_tokens=100, temperature=0.0)
    assert "thinking" not in h.seen[-1]["body"] and h.seen[-1]["body"]["temperature"] == 0.0


# --------------------------------------------------------------------------- end to end

@pytest.fixture()
def measured_run(mock_server):
    url, h = mock_server
    h.replies["text"] = "ANSWER: 42"
    return run_benchmark(create_adapter("openai", "m", base_url=url), categories=["math"], sandbox="none",
                         check_model=False, effort="low", max_workers=2)


def test_run_records_effort_hardware_and_efficiency_apart(measured_run):
    run = measured_run["run"]
    assert run["effort"]["requested"] == "low" and run["effort"]["applied"] and run["effort"]["level"] == "low"
    eff = run["efficiency"]
    assert eff["measurements"]["tasks_measured"] == eff["measurements"]["tasks"] > 0
    assert eff["measurements"]["output_tokens_per_task"] == 2.0          # the mock reports 2 tokens per reply
    assert eff["known"] and eff["components"]["tokens"] == 1.0
    assert eff["hardware_class"] == measured_run["runtime"]["hardware_info"]["class"]
    b = measured_run["scores"]["breakdown"]
    assert b["effort"]["level"] == "low" and b["effort"]["multiplier"] == 1.0
    assert b["efficiency"]["E"] == eff["normalized"]
    # fast but (here) wrong: the adjustment is scaled by quality, so it is 0.10 x E x 10000 x quality
    assert b["efficiency"]["adjustment"] == pytest.approx(0.10 * eff["normalized"] * 10000 * b["quality"], abs=1e-3)


def test_validator_recomputes_efficiency(measured_run):
    assert validate_results(copy.deepcopy(measured_run)).ok
    doc = copy.deepcopy(measured_run)
    doc["run"]["efficiency"]["normalized"] = 1.0 if doc["run"]["efficiency"]["normalized"] != 1.0 else 0.5
    rep = validate_results(doc)
    assert not rep.ok and any("efficiency does not recompute" in e for e in rep.errors)
    doc = copy.deepcopy(measured_run)
    doc["task_results"][0]["latency_seconds"] *= 100     # measurements no longer match the claimed block
    rep = validate_results(doc)
    assert not rep.ok and any("efficiency" in e for e in rep.errors)


def test_effort_never_moves_the_score(mock_server):
    url, h = mock_server
    h.replies["text"] = "ANSWER: 42"
    common = {"categories": ["math"], "sandbox": "none", "check_model": False, "max_workers": 1}
    a = run_benchmark(create_adapter("openai", "m", base_url=url), effort="low", **common)
    b = run_benchmark(create_adapter("openai", "m", base_url=url), effort="high", **common)
    assert a["scores"]["breakdown"]["base"] == b["scores"]["breakdown"]["base"]
    assert a["scores"]["breakdown"]["effort"]["multiplier"] == b["scores"]["breakdown"]["effort"]["multiplier"] == 1.0

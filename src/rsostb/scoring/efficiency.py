"""Hardware-aware token efficiency for scoring v2.

Two things are kept apart:

* **measurements** — what the run actually recorded: tokens in and out,
  seconds spent waiting for the model, output tokens per second and per
  task, and how many tasks reported usage at all. Never adjusted.
* **efficiency** — a normalised E in [-1, 1] derived from them, which v2
  adds to the score as a small bounded term (``efficiency.weight`` 0.10–0.51
  × E × ``efficiency.span`` × quality).

E is the weighted mean of the components that could be measured:

    tokens = clamp(log2(reference_output_tokens_per_task / output_tokens_per_task), -1, 1)
    speed  = clamp(log2(output_tokens_per_second / reference_tokens_per_second[class]), -1, 1)

The speed reference depends on the hardware *class* (cpu, apple_silicon,
consumer_gpu, datacenter_gpu), so a model is compared with what its kind of
machine typically does, not punished for running on a CPU. The class comes
from vendor and memory, never from a release date: the age of a GPU is not an
input. Remote (API) and unknown hardware have no speed reference. When too
few tasks reported usage, E is unknown (``None``), which v2 treats as 0.

Everything here is recomputed by the validator from the per-task records;
a claimed E that does not recompute is rejected.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from .common import ScoringError

_IN = ("input_tokens", "prompt_tokens")
_OUT = ("output_tokens", "completion_tokens")


def _tokens(usage: dict[str, Any] | None, keys: tuple[str, ...]) -> int | None:
    for k in keys:
        v = (usage or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
            return int(v)
    return None


def measurements(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw measurements over the tasks that reported output tokens (> 0) and a positive latency."""
    rows = []
    for tr in task_results:
        out = _tokens(tr.get("usage"), _OUT)
        lat = tr.get("latency_seconds")
        # A reply with no output tokens says nothing about cost (synthetic runs report 0).
        if not out or not isinstance(lat, (int, float)) or not math.isfinite(lat) or lat <= 0:
            continue
        rows.append((out, _tokens(tr.get("usage"), _IN) or 0, float(lat)))
    n = len(task_results)
    m: dict[str, Any] = {"tasks": n, "tasks_measured": len(rows),
                         "measured_share": round(len(rows) / n, 6) if n else 0.0}
    if not rows:
        m.update(output_tokens=None, input_tokens=None, generation_seconds=None, output_tokens_per_second=None,
                 output_tokens_per_task=None, median_latency_seconds=None)
        return m
    out = sum(r[0] for r in rows)
    secs = math.fsum(r[2] for r in rows)
    m.update(output_tokens=out, input_tokens=sum(r[1] for r in rows), generation_seconds=round(secs, 4),
             output_tokens_per_second=round(out / secs, 4) if secs > 0 else None,
             output_tokens_per_task=round(out / len(rows), 4),
             median_latency_seconds=round(statistics.median(r[2] for r in rows), 4))
    return m


def _log_ratio(reference: float, measured: float | None) -> float | None:
    if measured is None or measured <= 0:
        return None
    return max(-1.0, min(1.0, math.log2(reference / measured)))


def efficiency(meas: dict[str, Any], hardware_class: str | None, scoring_raw: dict[str, Any]) -> dict[str, Any]:
    """The normalised efficiency record for a run."""
    cfg = scoring_raw.get("efficiency") or {}
    if "components" not in cfg:
        raise ScoringError("this scoring configuration has no efficiency settings")
    cls = hardware_class or "unknown"
    enough = meas["measured_share"] >= float(cfg["min_measured_share"]) and meas["tasks_measured"] > 0
    comps: dict[str, float | None] = {"tokens": None, "speed": None}
    if enough:
        comps["tokens"] = _log_ratio(float(cfg["reference_output_tokens_per_task"]), meas["output_tokens_per_task"])
        ref_tps = (cfg.get("reference_tokens_per_second") or {}).get(cls)
        tps = meas["output_tokens_per_second"]
        if ref_tps and tps:
            comps["speed"] = max(-1.0, min(1.0, math.log2(tps / float(ref_tps))))
    weights = {k: float(v) for k, v in cfg["components"].items()}
    known = {k: v for k, v in comps.items() if v is not None and weights.get(k, 0) > 0}
    e = None
    if known:
        e = round(math.fsum(weights[k] * v for k, v in known.items()) / math.fsum(weights[k] for k in known), 6)
    why = None
    if not enough:
        why = (f"only {meas['tasks_measured']} of {meas['tasks']} tasks reported token usage "
               f"(need {float(cfg['min_measured_share']):.0%})")
    elif comps["speed"] is None and "speed" in weights:
        why = f"no speed reference for {cls} hardware; tokens only"
    return {"normalized": e, "known": e is not None, "hardware_class": cls,
            "components": {k: (round(v, 6) if v is not None else None) for k, v in comps.items()},
            "reference": {"output_tokens_per_task": cfg["reference_output_tokens_per_task"],
                          "tokens_per_second": (cfg.get("reference_tokens_per_second") or {}).get(cls)},
            "note": why}


def efficiency_block(task_results: list[dict[str, Any]], hardware_class: str | None,
                     scoring_raw: dict[str, Any]) -> dict[str, Any]:
    """``run.efficiency`` as the runner records it: raw measurements and the normalised result, apart."""
    meas = measurements(task_results)
    return {"measurements": meas, **efficiency(meas, hardware_class, scoring_raw)}


def same(a: dict[str, Any] | None, b: dict[str, Any], tol: float = 1e-6) -> bool:
    """Whether a claimed efficiency block matches a recomputed one."""
    if not isinstance(a, dict):
        return False
    x, y = a.get("normalized"), b.get("normalized")
    if (x is None) != (y is None):
        return False
    if x is not None and (not isinstance(x, (int, float)) or abs(float(x) - y) > tol):
        return False
    return a.get("hardware_class") == b.get("hardware_class") and a.get("measurements") == b.get("measurements")

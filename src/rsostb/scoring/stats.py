"""Descriptive statistics for reports (see docs/STATISTICS.md).

These describe *this run on this task set*. They are not significance tests,
and the bootstrap interval captures task-sampling variance only — not
sampling variance of the model itself, prompt sensitivity, or judge noise.
"""
from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from typing import Any

REFUSAL_FLAGS = {"refusal", "correct_refusal", "over_refusal", "soft_refusal", "unhelpful_refusal", "refusal_with_leak"}


def describe(values: Sequence[float]) -> dict[str, Any]:
    vals = sorted(float(v) for v in values)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 6),
        "median": round(statistics.median(vals), 6),
        "stdev": round(statistics.stdev(vals), 6) if len(vals) > 1 else 0.0,
        "min": round(vals[0], 6),
        "max": round(vals[-1], 6),
        "percentiles": {f"p{p}": round(percentile(vals, p), 6) for p in (5, 10, 25, 50, 75, 90, 95)},
    }


def percentile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (same definition as numpy's default)."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def histogram(values: Sequence[float], bins: int = 10, lo: float = -1.0, hi: float = 1.0) -> dict[str, Any]:
    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    counts = [0] * bins
    for v in values:
        idx = min(bins - 1, max(0, int((v - lo) / (hi - lo) * bins)))
        counts[idx] += 1
    return {"edges": [round(e, 4) for e in edges], "counts": counts}


def rate(num: int, den: int) -> float | None:
    return round(num / den, 6) if den else None


def run_statistics(trs: list[dict[str, Any]], tasks: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    n = len(trs)
    status = [tr["status"] for tr in trs]
    flags = [set(tr.get("flags") or []) for tr in trs]
    raw = [tr["raw_score"] for tr in trs]

    halluc_items = [i for i, tr in enumerate(trs) if "hallucination" in tasks[tr["task_id"]].tags
                    or tasks[tr["task_id"]].expected_behavior == "abstain"]
    beh = {"refuse": [], "comply": [], "safe_complete": []}
    for i, tr in enumerate(trs):
        eb = tasks[tr["task_id"]].expected_behavior
        if eb in beh and tasks[tr["task_id"]].evaluation_type == "behavior":
            beh[eb].append(i)
    matrix = {
        "correct_refusal": sum(1 for i in beh["refuse"] if "correct_refusal" in flags[i]),
        "under_refusal": sum(1 for i in beh["refuse"] if "under_refusal" in flags[i]),
        "correct_compliance": sum(1 for i in beh["comply"] + beh["safe_complete"] if "correct_compliance" in flags[i]),
        "over_refusal": sum(1 for i in beh["comply"] + beh["safe_complete"]
                            if flags[i] & {"over_refusal", "unhelpful_refusal"}),
        "refuse_items": len(beh["refuse"]),
        "comply_items": len(beh["comply"]) + len(beh["safe_complete"]),
    }
    tool_idx = [i for i, tr in enumerate(trs) if tr["evaluation_type"] in ("tool_call", "agentic")]
    code_idx = [i for i, tr in enumerate(trs) if tr["evaluation_type"] in ("unit_test", "code_execution")]
    passed = sum(int((trs[i].get("details") or {}).get("passed", 0) or 0) for i in code_idx)
    total_cases = sum(int((trs[i].get("details") or {}).get("total", 0) or 0) for i in code_idx)
    wsum = sum(tr["weight"] for tr in trs)
    judge_idx = [i for i, tr in enumerate(trs) if tasks[tr["task_id"]].requires_judge]
    return {
        "task_scores": describe(raw),
        "score_histogram": histogram(raw),
        "completion_rate": rate(sum(1 for s in status if s in ("scored", "partial")), n),
        "invalid_output_rate": rate(sum(1 for s in status if s == "invalid_output"), n),
        "error_rate": rate(sum(1 for s in status if s in ("error", "timeout")), n),
        "unavailable_rate": rate(sum(1 for s in status if s == "unavailable"), n),
        "hallucination_rate": rate(sum(1 for i in halluc_items if "hallucination" in flags[i]), len(halluc_items)),
        "hallucination_items": len(halluc_items),
        "refusal_rate": rate(sum(1 for f in flags if f & REFUSAL_FLAGS), n),
        "over_refusal_rate": rate(matrix["over_refusal"], matrix["comply_items"]),
        "under_refusal_rate": rate(matrix["under_refusal"], matrix["refuse_items"]),
        "safety_matrix": matrix,
        "tool_call_accuracy": round(statistics.fmean(trs[i]["credit"] for i in tool_idx), 6) if tool_idx else None,
        "coding_test_pass_rate": rate(passed, total_cases),
        "coding_tasks_fully_passing": rate(sum(1 for i in code_idx if trs[i]["credit"] >= 0.999), len(code_idx)),
        "judge_based_share": rate(sum(1 for tr in trs if tr.get("judge_based")), n),
        "judge_items": len(judge_idx),
        "coverage": round(sum(tr["weight"] * tr.get("coverage", 1.0) for tr in trs) / wsum, 6) if wsum else None,
        "penalized_tasks": sum(1 for tr in trs if tr.get("penalty", 0) > 0),
        "bonus_tasks": sum(1 for tr in trs if tr.get("bonus", 0) > 0),
    }


def bootstrap_ci(n_items: int, statistic: Callable[[list[int]], float], resamples: int = 1000, seed: int = 0,
                 confidence: float = 0.95) -> dict[str, Any] | None:
    if n_items < 2 or resamples < 10:
        return None
    rng = random.Random(seed)
    vals = sorted(statistic([rng.randrange(n_items) for _ in range(n_items)]) for _ in range(resamples))
    a = (1 - confidence) / 2 * 100
    return {"low": round(percentile(vals, a), 2), "high": round(percentile(vals, 100 - a), 2),
            "confidence": confidence, "resamples": resamples, "seed": seed,
            "method": "percentile bootstrap over tasks (task-sampling variance only)"}

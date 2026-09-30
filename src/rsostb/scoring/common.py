"""Scoring primitives shared by all scoring versions.

Per task (documented in docs/SCORING.md#per-task-score)::

    penalty   = sum_e min(cap_e, magnitude_e * count_e)          # events from the evaluator
    bonus     = min(bonus_cap, sum_b magnitude_b * count_b)
    r         = clamp(min(1, credit + bonus) - penalty, -1, 1)   # raw task score
    points    = r * max_score        if r >= 0
              = r * |min_score|      if r <  0
    W         = weight_class * difficulty_multiplier * category_weight * evaluation_quality
    weighted  = W * points

Everything is recomputed from the task definition and the scoring config;
per-task numbers claimed in a results file are never trusted.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from ..config import BenchmarkConfig, ScoringConfig


class ScoringError(ValueError):
    pass


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def task_weight(task, bench: BenchmarkConfig, scoring: ScoringConfig) -> float:
    try:
        return (
            scoring.weight_classes[task.weight_class]
            * scoring.difficulty_multipliers[task.difficulty]
            * bench.category(task.category).weight
            * scoring.evaluation_quality[task.evaluation_type]
        )
    except KeyError as exc:
        raise ScoringError(f"task {task.id}: no weight configured for {exc}") from None


def penalty_total(events: dict[str, float], scoring: ScoringConfig) -> float:
    mags, caps = scoring.penalties, scoring.penalty_caps
    total = 0.0
    for name, count in (events or {}).items():
        if name not in mags:
            raise ScoringError(f"unknown penalty event {name!r}")
        if count < 0 or not math.isfinite(count):
            raise ScoringError(f"invalid count for event {name!r}")
        amount = mags[name] * count
        if name in caps:
            amount = min(amount, caps[name])
        total += amount
    return total


def bonus_total(events: dict[str, float], scoring: ScoringConfig) -> float:
    mags = scoring.bonuses
    total = 0.0
    for name, count in (events or {}).items():
        if name not in mags:
            raise ScoringError(f"unknown bonus event {name!r}")
        if count < 0 or not math.isfinite(count):
            raise ScoringError(f"invalid count for bonus {name!r}")
        total += mags[name] * count
    return min(total, scoring.bonus_cap)


def raw_task_score(credit: float, events: dict[str, float], bonus_events: dict[str, float],
                   scoring: ScoringConfig) -> tuple[float, float, float]:
    """Returns (r, penalty, bonus)."""
    if not math.isfinite(credit):
        raise ScoringError("credit must be finite")
    credit = clamp(float(credit), 0.0, 1.0)
    pen = penalty_total(events, scoring)
    bon = bonus_total(bonus_events, scoring)
    r = clamp(min(1.0, credit + bon) - pen, -1.0, 1.0)
    return r, pen, bon


def points_for(r: float, max_score: float, min_score: float) -> float:
    return r * max_score if r >= 0 else r * abs(min_score)


def score_task_result(task, tr: dict[str, Any], bench: BenchmarkConfig, scoring: ScoringConfig) -> dict[str, Any]:
    """Fill the numeric fields of a task result from credit + events."""
    r, pen, bon = raw_task_score(tr.get("credit", 0.0), tr.get("events") or {}, tr.get("bonus_events") or {}, scoring)
    w = task_weight(task, bench, scoring)
    pts = points_for(r, task.max_score, task.min_score)
    out = dict(tr)
    out.update(
        task_id=task.id,
        task_version=task.version,
        category=task.category,
        difficulty=task.difficulty,
        weight_class=task.weight_class,
        evaluation_type=task.evaluation_type,
        credit=round(clamp(float(tr.get("credit", 0.0)), 0.0, 1.0), 6),
        penalty=round(pen, 6),
        bonus=round(bon, 6),
        raw_score=round(r, 6),
        points=round(pts, 4),
        max_points=task.max_score,
        min_points=task.min_score,
        weight=round(w, 6),
        weighted_points=round(w * pts, 4),
    )
    return out


def normalize(total: float, max_total: float, min_total: float) -> float:
    """Map weighted points to [-1, 1]: positive side by the achievable maximum,
    negative side by the worst possible total."""
    if total >= 0:
        return clamp(total / max_total, 0.0, 1.0) if max_total > 0 else 0.0
    return clamp(total / abs(min_total), -1.0, 0.0) if min_total < 0 else 0.0


def to_reported(n: float, scoring: ScoringConfig) -> float:
    """Map a normalised score in [-1, 1] onto the published range, then clamp."""
    val = n * scoring.range_max if n >= 0 else n * abs(scoring.range_min)
    return round(clamp(val, scoring.range_min, scoring.range_max), 2)


def percentage(total: float, max_total: float, min_total: float) -> float:
    return round(100.0 * normalize(total, max_total, min_total), 4)


def aggregate(rows: Iterable[tuple[dict[str, Any], Any]], scoring: ScoringConfig) -> dict[str, Any]:
    """Sum weighted points over (task_result, task) pairs."""
    tot = mx = mn = wsum = 0.0
    n = 0
    for tr, task in rows:
        w = tr["weight"]
        tot += tr["weighted_points"]
        mx += w * task.max_score
        mn += w * task.min_score
        wsum += w
        n += 1
    return {
        "points": round(tot, 4),
        "max_points": round(mx, 4),
        "min_points": round(mn, 4),
        "percentage": percentage(tot, mx, mn),
        "task_count": n,
        "weighted_task_count": round(wsum, 4),
    }

"""Hybrid evaluator: a weighted combination of other evaluators, typically a
deterministic part (tests, structure, exact answer) plus a rubric part."""
from __future__ import annotations

import copy

from .base import EVALUATORS, EvalContext, EvalResult, Response, register


@register("hybrid")
def evaluate_hybrid(task, response: Response, ctx: EvalContext) -> EvalResult:
    from ..datasets.task import Task

    res = EvalResult()
    comps = task.evaluation.get("components") or []
    total = earned = covered = 0.0
    statuses = []
    for i, comp in enumerate(comps):
        w = float(comp.get("weight", 1.0))
        data = copy.deepcopy(task.data)
        data["evaluation_type"] = comp["type"]
        data["evaluation"] = comp.get("evaluation", {})
        sub_task = Task(data=data, source=task.source, resources=task.resources)
        sub = EVALUATORS[comp["type"]](sub_task, response, ctx).clamp()
        total += w
        earned += w * sub.credit
        covered += w * sub.coverage
        statuses.append(sub.status)
        for k, v in sub.events.items():
            res.event(k, v)
        for k, v in sub.bonus_events.items():
            res.bonus(k, v)
        res.flag(*sub.flags)
        res.judge_based = res.judge_based or sub.judge_based
        res.details[f"component_{i}_{comp['type']}"] = {"credit": round(sub.credit, 4), "coverage": sub.coverage,
                                                        **{k: v for k, v in sub.details.items() if k != "code_chars"}}
        if comp.get("gate") and sub.credit < 0.5:
            res.flag("gate_failed")
            res.credit = 0.0
            res.coverage = covered / total if total else 1.0
            return res
    res.credit = earned / total if total else 0.0
    res.coverage = covered / total if total else 1.0
    if all(s == "invalid_output" for s in statuses):
        res.status = "invalid_output"
    elif any(s == "unavailable" for s in statuses) or res.coverage < 1.0:
        res.status = "partial"
    return res

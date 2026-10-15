"""Human-readable account of how a score was built (``rsostb score --explain``)."""
from __future__ import annotations

from typing import Any


def _n(x: float) -> str:
    return f"{x:,.2f}"


def explain_scores(scores: dict[str, Any], task_results: list[dict[str, Any]] | None = None,
                   task_id: str | None = None) -> str:
    b = scores.get("breakdown")
    if not b:
        return (f"scoring {scores.get('scoring_version')}: {_n(scores['weighted_points'])} of "
                f"{_n(scores['max_weighted_points'])} weighted points -> normalised {scores['normalized']} -> "
                f"{_n(scores['rsostb_score'])} (see docs/SCORING.md)")
    eff, pen, bon = b["efficiency"], b["penalties"], b["bonus"]
    lines = [
        f"scoring {scores['scoring_version']}   {b['formula']}",
        f"  base         {_n(b['base']):>12}   quality {b['quality']:.6f} = earned / possible ({_n(b['possible'])})",
        f"+ bonus        {_n(bon['applied']):>12}   claimed {_n(bon['uncapped'])}, run cap {_n(bon['cap'])}",
        f"+ efficiency   {_n(eff['adjustment']):>12}   weight {eff['weight']} × E {eff['E']:+.3f}"
        f"{'' if eff['known'] else ' (unknown)'} × span {_n(eff['span'])} × quality; bound ±{_n(eff['bound'])}",
        f"− penalties    {_n(pen['applied']):>12}",
    ]
    for ev, row in pen["by_event"].items():
        extra = f", superseded on {row['superseded']}" if row["superseded"] else ""
        lines.append(f"      {ev:<24} {row['tasks']:>4} task(s), {row['raw']:g} raw points{extra}")
    for cat, row in pen["by_category"].items():
        if row["capped"]:
            lines.append(f"      category cap hit: {cat} ({_n(row['weighted'])} -> {_n(row['applied'])})")
    lines += [
        f"= before clamp {_n(b['pre_clamp']):>12}",
        f"  final        {_n(b['final']):>12}{'   (clamped to the published range)' if b['clamped'] else ''}",
        f"  effort {b['effort']['level']} (×{b['effort']['multiplier']}), reliability measured on "
        f"{b['reliability']['measured_tasks']} task(s), config {b['scoring_config_hash'][:12]}",
    ]
    if task_id and task_results:
        tr = next((t for t in task_results if t["task_id"] == task_id), None)
        if tr is None:
            lines.append(f"  no task {task_id} in this run")
        else:
            tb = tr["breakdown"]
            f = tb["factors"]
            lines += [
                f"task {task_id}: raw {tb['raw']} × weight {f['task_weight']} × difficulty L{f['difficulty_level']} "
                f"{f['difficulty']} × category {f['category']} × eval {f['eval_quality']} × reliability "
                f"{f['reliability']} × effort {f['effort']} = earned {tb['earned']} of {tb['possible']}",
                f"  bonus {tb['bonus']['applied_raw']} raw ({tb['bonus']['weighted']} weighted), penalty "
                f"{tb['penalty_raw']} raw ({tb['penalty_weighted']} weighted)"
                f"{', task cap reached' if tb['penalty_task_cap_reached'] else ''}",
            ]
            for it in tb["penalties"]:
                state = "applied" if it["applied"] else f"superseded by {it['superseded_by']}"
                lines.append(f"    {it['event']} ×{it['count']:g}: {it['amount']:g}"
                             f"{' (capped)' if it['capped'] else ''}, {state}")
    return "\n".join(lines)

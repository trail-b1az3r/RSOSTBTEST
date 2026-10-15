"""Scoring algorithm v2.

    Final = clamp( Base + Bonus + Efficiency − Penalties , range.min, range.max )

Per task (``score_task``)::

    raw        = raw_scale · credit                                          0..100
    base_w     = TaskWeight · Difficulty(level 1-6) · CategoryWeight · EvalQuality
    multiplier = base_w · Reliability · Effort
    possible   = raw_scale · base_w
    earned     = min(raw · multiplier, possible)
    bonus      = min(Σ bonus events, task_bonus_cap, raw_scale − raw) · multiplier   (never past possible)
    penalty    = min(Σ events after per-event caps and exclusive groups, task_penalty_cap) · base_w

Run (``score_run``)::

    P          = Σ possible
    quality    = Σ earned / P                                                0..1
    Base       = range.max · quality
    Bonus      = min(range.max · Σ bonus / P, run_bonus_cap · range.max)
    Penalties  = range.max · Σ_category min(Σ penalty, category_penalty_cap · category possible) / P
    Efficiency = efficiency.weight · E · efficiency.span · quality           E in [-1, 1], unknown -> 0

Every number comes from ``configs/scoring-v2.yaml``; every score carries a
``breakdown`` that shows each term, each cap that bit and each penalty that was
superseded, so a score can be audited by hand. Inputs that are not finite are
rejected, never coerced. v1 is untouched: v2 is a separate module and config.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from ..config import BenchmarkConfig, ScoringConfig, scoring_config_hash
from ..datasets.task import EVALUATION_FAMILIES
from .common import ScoringError, clamp
from .stats import bootstrap_ci, run_statistics

VERSION = "v2"
DESCRIPTION = ("Raw 0-100 × task weight × difficulty (1-6) × category × eval quality × reliability × effort, "
               "plus capped bonuses and a bounded efficiency adjustment, minus capped penalties.")
FORMULA = ("clamp(range.max · Σearned/Σpossible + Bonus + efficiency.weight · E · efficiency.span · quality "
           "− Penalties, range.min, range.max)")

EFFICIENCY_WEIGHT_RANGE = (0.10, 0.51)
EFFORT_MULTIPLIER_RANGE = (0.5, 1.5)
LEVELS = range(1, 7)


# --------------------------------------------------------------------------- config

def _finite(x: Any, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ScoringError(f"{what} must be a number, got {x!r}")
    if not math.isfinite(x):
        raise ScoringError(f"{what} must be finite, got {x!r}")
    return float(x)


def _within(x: Any, lo: float, hi: float, what: str) -> float:
    v = _finite(x, what)
    if not lo <= v <= hi:
        raise ScoringError(f"{what} must be within [{lo}, {hi}], got {v}")
    return v


def validate_config(raw: dict[str, Any]) -> None:  # noqa: C901
    """Reject a configuration that could break the bounds or double count."""
    try:
        if raw.get("scoring_version") != VERSION:
            raise ScoringError("scoring_version must be v2")
        lo, hi = _finite(raw["range"]["min"], "range.min"), _finite(raw["range"]["max"], "range.max")
        if not lo <= 0 < hi:
            raise ScoringError("scoring range must satisfy min <= 0 < max")
        scale = _within(raw["raw_scale"], 1, 10_000, "raw_scale")
        tp = raw["task_points"]
        if not _finite(tp["default_min"], "task_points.default_min") <= 0 < _finite(tp["default_max"],
                                                                                   "task_points.default_max"):
            raise ScoringError("task_points defaults must satisfy min <= 0 < max")
        if raw.get("judge_unavailable_policy", "zero") not in ("zero", "exclude"):
            raise ScoringError("judge_unavailable_policy must be 'zero' or 'exclude'")
        for k, v in raw["weight_classes"].items():
            _within(v, 1e-6, 100, f"weight_classes.{k}")
        levels = raw["difficulty_levels"]
        for k, v in levels.items():
            if int(v) not in LEVELS or v != int(v):
                raise ScoringError(f"difficulty_levels.{k} must be an integer 1-6")
        mults = {int(k): v for k, v in raw["difficulty_multipliers"].items()}
        if set(mults) != set(LEVELS):
            raise ScoringError("difficulty_multipliers must define levels 1-6")
        for k, v in mults.items():
            _within(v, 1e-6, 100, f"difficulty_multipliers.{k}")
        for k, v in raw["evaluation_quality"].items():
            _within(v, 1e-6, 1.0, f"evaluation_quality.{k}")
        rel = raw["reliability"]
        _within(rel["default"], 0, 1, "reliability.default")
        _within(rel["floor"], 0, 1, "reliability.floor")
        for k, v in raw["effort_multipliers"].items():
            _within(v, *EFFORT_MULTIPLIER_RANGE, f"effort_multipliers.{k}")
        if "unknown" not in raw["effort_multipliers"]:
            raise ScoringError("effort_multipliers must define 'unknown'")
        pens = raw["penalties"]
        for k, v in pens.items():
            _within(v, 0, 2 * scale, f"penalties.{k}")
        for k, v in (raw.get("penalty_caps") or {}).items():
            if k not in pens:
                raise ScoringError(f"penalty_caps.{k}: unknown penalty")
            _within(v, 0, 2 * scale, f"penalty_caps.{k}")
        seen: dict[str, str] = {}
        for g, members in (raw.get("exclusive_penalty_groups") or {}).items():
            for m in members:
                if m not in pens:
                    raise ScoringError(f"exclusive_penalty_groups.{g}: unknown penalty {m!r}")
                if m in seen:
                    raise ScoringError(f"penalty {m!r} is in two exclusive groups ({seen[m]}, {g})")
                seen[m] = g
        _within(raw["task_penalty_cap"], 0, 5 * scale, "task_penalty_cap")
        _within(raw["category_penalty_cap"], 0, 5, "category_penalty_cap")
        for k, v in raw["bonuses"].items():
            _within(v, 0, scale / 2, f"bonuses.{k}")
        _within(raw["task_bonus_cap"], 0, scale / 2, "task_bonus_cap")
        _within(raw["run_bonus_cap"], 0, 0.2, "run_bonus_cap")
        eff = raw["efficiency"]
        _within(eff["weight"], *EFFICIENCY_WEIGHT_RANGE, "efficiency.weight")
        _within(eff["span"], 0, 0.2 * hi, "efficiency.span")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ScoringError(f"scoring-v2 config is incomplete: {exc!r}") from None


class V2:
    """Typed view of a v2 :class:`ScoringConfig`."""

    def __init__(self, scoring: ScoringConfig):
        r = scoring.raw
        validate_config(r)
        self.raw = r
        self.range_min, self.range_max = float(r["range"]["min"]), float(r["range"]["max"])
        self.scale = float(r["raw_scale"])
        self.levels = {k: int(v) for k, v in r["difficulty_levels"].items()}
        self.level_mult = {int(k): float(v) for k, v in r["difficulty_multipliers"].items()}
        self.penalties = {k: float(v) for k, v in r["penalties"].items()}
        self.caps = {k: float(v) for k, v in (r.get("penalty_caps") or {}).items()}
        self.group_of = {m: g for g, ms in (r.get("exclusive_penalty_groups") or {}).items() for m in ms}
        self.bonuses = {k: float(v) for k, v in r["bonuses"].items()}
        self.task_pen_cap, self.cat_pen_cap = float(r["task_penalty_cap"]), float(r["category_penalty_cap"])
        self.task_bonus_cap, self.run_bonus_cap = float(r["task_bonus_cap"]), float(r["run_bonus_cap"])
        self.rel_default, self.rel_floor = float(r["reliability"]["default"]), float(r["reliability"]["floor"])
        self.effort = {k: float(v) for k, v in r["effort_multipliers"].items()}
        self.eff_weight, self.eff_span = float(r["efficiency"]["weight"]), float(r["efficiency"]["span"])

    def level(self, task) -> int:
        lvl = task.data.get("difficulty_level")
        if lvl is not None:
            if lvl not in LEVELS:
                raise ScoringError(f"task {task.id}: difficulty_level must be 1-6")
            return int(lvl)
        try:
            return self.levels[task.difficulty]
        except KeyError:
            raise ScoringError(f"task {task.id}: no difficulty level for {task.difficulty!r}") from None


# --------------------------------------------------------------------------- run context

def effort_of(run: dict[str, Any] | None, cfg: V2) -> dict[str, Any]:
    """The effort level a run was made at, and its (neutral by default) multiplier."""
    e = (run or {}).get("effort")
    level = (e.get("level") if isinstance(e, dict) else e) or "unknown"
    level = str(level).lower()
    known = level in cfg.effort
    return {"level": level, "recognised": known, "multiplier": cfg.effort[level if known else "unknown"]}


def efficiency_of(run: dict[str, Any] | None) -> dict[str, Any]:
    """E in [-1, 1] from the run's efficiency record; absent or unknown -> 0."""
    rec = (run or {}).get("efficiency")
    if not isinstance(rec, dict) or rec.get("normalized") is None:
        return {"E": 0.0, "known": False}
    e = _finite(rec["normalized"], "run.efficiency.normalized")
    return {"E": clamp(e, -1.0, 1.0), "known": True, "clamped": not -1.0 <= e <= 1.0}


def reliability_of(tr: dict[str, Any], cfg: V2) -> tuple[float, float | None]:
    """(multiplier, measured agreement or None)."""
    agree = tr.get("reliability")
    if agree is None:
        return cfg.rel_default, None
    a = _within(agree, 0, 1, f"task {tr.get('task_id')}: reliability")
    return cfg.rel_floor + (1 - cfg.rel_floor) * a, a


# --------------------------------------------------------------------------- per task

def _counts(events: dict[str, Any] | None, table: dict[str, float], kind: str, tid: str) -> dict[str, float]:
    out = {}
    for name, count in sorted((events or {}).items()):
        if name not in table:
            raise ScoringError(f"task {tid}: unknown {kind} event {name!r}")
        c = _finite(count, f"task {tid}: {kind} {name} count")
        if c < 0:
            raise ScoringError(f"task {tid}: negative {kind} count for {name!r}")
        if c:
            out[name] = c
    return out


def penalty_items(events: dict[str, float], cfg: V2, tid: str) -> tuple[list[dict[str, Any]], float, bool]:
    """Every penalty with its amount, per-event cap and exclusive-group outcome.
    Returns (items, applied raw points, task cap reached)."""
    items = []
    for name, count in _counts(events, cfg.penalties, "penalty", tid).items():
        amount = cfg.penalties[name] * count
        capped = name in cfg.caps and amount > cfg.caps[name]
        items.append({"event": name, "count": count, "magnitude": cfg.penalties[name],
                      "amount": min(amount, cfg.caps[name]) if capped else amount, "capped": capped,
                      "group": cfg.group_of.get(name), "applied": True})
    best: dict[str, dict[str, Any]] = {}
    for it in sorted(items, key=lambda i: (-i["amount"], i["event"])):
        g = it["group"]
        if g is None:
            continue
        if g in best:
            it["applied"], it["superseded_by"] = False, best[g]["event"]
        else:
            best[g] = it
    total = math.fsum(it["amount"] for it in items if it["applied"])
    return items, min(total, cfg.task_pen_cap), total > cfg.task_pen_cap


def score_task(task, tr: dict[str, Any], bench: BenchmarkConfig, scoring: ScoringConfig, cfg: V2 | None = None,
               effort: float = 1.0) -> dict[str, Any]:
    cfg = cfg or V2(scoring)
    tid = task.id
    credit = round(clamp(_finite(tr.get("credit", 0.0), f"task {tid}: credit"), 0.0, 1.0), 6)
    try:
        wc = float(scoring.raw["weight_classes"][task.weight_class])
        eq = float(scoring.raw["evaluation_quality"][task.evaluation_type])
        cw = float(bench.category(task.category).weight)
    except KeyError as exc:
        raise ScoringError(f"task {tid}: no weight configured for {exc}") from None
    level = cfg.level(task)
    dm = cfg.level_mult[level]
    rel, agreement = reliability_of(tr, cfg)
    base_w = wc * dm * cw * eq
    mult = base_w * rel * effort
    raw = cfg.scale * credit
    possible = cfg.scale * base_w
    earned = min(raw * mult, possible)

    bonus_counts = _counts(tr.get("bonus_events"), cfg.bonuses, "bonus", tid)
    bonus_claimed = math.fsum(cfg.bonuses[b] * c for b, c in bonus_counts.items())
    bonus_raw = max(0.0, min(bonus_claimed, cfg.task_bonus_cap, cfg.scale - raw))
    bonus_w = min(bonus_raw * mult, possible - earned)

    items, pen_raw, task_capped = penalty_items(tr.get("events") or {}, cfg, tid)
    pen_w = pen_raw * base_w
    net_raw = raw + bonus_raw - pen_raw

    out = dict(tr)
    out.update(
        task_id=tid, task_version=task.version, category=task.category, difficulty=task.difficulty,
        weight_class=task.weight_class, evaluation_type=task.evaluation_type, credit=credit,
        penalty=round(pen_raw / cfg.scale, 6), bonus=round(bonus_raw / cfg.scale, 6),
        raw_score=round(clamp(net_raw / cfg.scale, -1.0, 1.0), 6),
        points=round(net_raw, 4), max_points=cfg.scale, min_points=-cfg.task_pen_cap,
        weight=round(mult, 6), weighted_points=round(earned + bonus_w - pen_w, 4),
        breakdown={
            "raw": round(raw, 4),
            "factors": {"task_weight": wc, "difficulty": dm, "difficulty_level": level, "category": cw,
                        "eval_quality": eq, "reliability": round(rel, 6), "effort": effort},
            "reliability_measured": agreement,
            "possible": round(possible, 6),
            "earned": round(earned, 6),
            "bonus": {"claimed_raw": round(bonus_claimed, 6), "applied_raw": round(bonus_raw, 6),
                      "weighted": round(bonus_w, 6), "events": bonus_counts},
            "penalties": items,
            "penalty_raw": round(pen_raw, 6),
            "penalty_task_cap_reached": task_capped,
            "penalty_weighted": round(pen_w, 6),
        },
    )
    return out


# --------------------------------------------------------------------------- run

def _combine(rows: list[dict[str, Any]], cfg: V2, eff_e: float) -> dict[str, Any]:
    """Run terms from scored task rows (each with a ``breakdown``)."""
    poss, earn, bon = [], [], []
    cat_pen: dict[str, list[float]] = defaultdict(list)
    cat_poss: dict[str, list[float]] = defaultdict(list)
    for tr in rows:
        b = tr["breakdown"]
        poss.append(b["possible"])
        earn.append(b["earned"])
        bon.append(b["bonus"]["weighted"])
        cat_pen[tr["category"]].append(b["penalty_weighted"])
        cat_poss[tr["category"]].append(b["possible"])
    p = math.fsum(poss)
    by_cat = {}
    for cat in sorted(cat_pen):
        raw_pen, cap = math.fsum(cat_pen[cat]), cfg.cat_pen_cap * math.fsum(cat_poss[cat])
        by_cat[cat] = {"weighted": raw_pen, "cap": cap, "applied": min(raw_pen, cap), "capped": raw_pen > cap}
    if p <= 0:
        quality = bonus_uncapped = pen_points = 0.0
    else:
        quality = clamp(math.fsum(earn) / p, 0.0, 1.0)
        bonus_uncapped = cfg.range_max * math.fsum(bon) / p
        pen_points = cfg.range_max * math.fsum(c["applied"] for c in by_cat.values()) / p
    base = cfg.range_max * quality
    bonus = min(bonus_uncapped, cfg.run_bonus_cap * cfg.range_max)
    eff = cfg.eff_weight * eff_e * cfg.eff_span * quality
    pre = base + bonus + eff - pen_points
    final = round(clamp(pre, cfg.range_min, cfg.range_max), 2)
    return {"possible": p, "quality": quality, "base": base, "bonus_uncapped": bonus_uncapped, "bonus": bonus,
            "efficiency": eff, "penalties": pen_points, "penalty_by_category": by_cat, "pre_clamp": pre,
            "final": final}


def _normalized(final: float, cfg: V2) -> float:
    return final / cfg.range_max if final >= 0 else final / abs(cfg.range_min)


def _aggregate(rows: list[dict[str, Any]], cfg: V2) -> dict[str, Any]:
    """Points, possible and percentage of a group of tasks, penalties included
    (category caps apply at run level only)."""
    pts = math.fsum(tr["weighted_points"] for tr in rows)
    mx = math.fsum(tr["breakdown"]["possible"] for tr in rows)
    mn = -math.fsum(cfg.task_pen_cap * tr["breakdown"]["possible"] / cfg.scale for tr in rows)
    pct = clamp(100.0 * pts / mx, -100.0, 100.0) if mx > 0 else 0.0
    return {"points": round(pts, 4), "max_points": round(mx, 4), "min_points": round(mn, 4),
            "percentage": round(pct, 4), "task_count": len(rows),
            "weighted_task_count": round(mx / cfg.scale, 4)}


def _flag_counts(rows):
    c = {"errors": 0, "refusals": 0, "hallucinations": 0, "execution_failures": 0, "invalid_outputs": 0,
         "unavailable": 0}
    for tr in rows:
        f = set(tr.get("flags") or [])
        c["errors"] += tr["status"] in ("error", "timeout")
        c["refusals"] += bool(f & {"refusal", "correct_refusal", "over_refusal", "soft_refusal", "unhelpful_refusal"})
        c["hallucinations"] += "hallucination" in f
        c["execution_failures"] += "execution_failure" in f
        c["invalid_outputs"] += tr["status"] == "invalid_output"
        c["unavailable"] += tr["status"] == "unavailable"
    return c


def _metric_rows(metric, rows, tasks):
    out = []
    for tr in rows:
        t = tasks[tr["task_id"]]
        if t.category in metric.categories or set(metric.tags) & set(t.tags) or (
                metric.non_english and t.language not in ("en", "code", "mixed")):
            out.append(tr)
    return out


def score_run(task_results: list[dict[str, Any]], tasks: dict[str, Any], bench: BenchmarkConfig,
              scoring: ScoringConfig, *, run: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recompute every task result and the run summary with v2.

    ``run`` is the results file's ``run`` block; v2 reads ``run.effort`` and
    ``run.efficiency.normalized`` from it (both optional, both neutral when absent)."""
    if scoring.version != VERSION:
        raise ScoringError(f"scoring v2 needs the v2 configuration, got {scoring.version}")
    cfg = V2(scoring)
    effort = effort_of(run, cfg)
    eff = efficiency_of(run)
    scored = [score_task(tasks[tr["task_id"]], tr, bench, scoring, cfg, effort["multiplier"]) for tr in task_results]
    terms = _combine(scored, cfg, eff["E"])
    final = terms["final"]
    n = _normalized(final, cfg)

    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for tr in scored:
        groups["category"][tr["category"]].append(tr)
        groups["level"][str(tr["breakdown"]["factors"]["difficulty_level"])].append(tr)
        groups["evaluation_type"][tr["evaluation_type"]].append(tr)
        groups["family"][EVALUATION_FAMILIES.get(tr["evaluation_type"], tr["evaluation_type"])].append(tr)

    p = terms["possible"]
    categories = {}
    for cat in bench.category_ids:
        rows = groups["category"].get(cat)
        if not rows:
            continue
        a = _aggregate(rows, cfg)
        pc = terms["penalty_by_category"][cat]
        a["earned"] = round(math.fsum(tr["breakdown"]["earned"] for tr in rows), 4)
        a["bonus"] = round(math.fsum(tr["breakdown"]["bonus"]["weighted"] for tr in rows), 4)
        a["penalty"] = round(pc["applied"], 4)
        a["penalty_capped"] = pc["capped"]
        a["contribution"] = round(cfg.range_max * (a["earned"] + a["bonus"] - pc["applied"]) / p, 4) if p else 0.0
        a.update(_flag_counts(rows))
        a["mean_credit"] = round(math.fsum(tr["credit"] for tr in rows) / len(rows), 6)
        categories[cat] = a

    metrics: dict[str, float | None] = {"rsostb_score": final}
    for m in bench.metrics:
        mrows = _metric_rows(m, scored, tasks)
        metrics[m.id] = _aggregate(mrows, cfg)["percentage"] if mrows else None

    ci = None
    boot = scoring.raw.get("bootstrap") or {}
    if boot.get("resamples", 0) and len(scored) >= 2:
        def stat(idx: list[int]) -> float:
            return _combine([scored[i] for i in idx], cfg, eff["E"])["final"]

        ci = bootstrap_ci(len(scored), stat, int(boot["resamples"]), int(boot.get("seed", 0)),
                          float(boot.get("confidence", 0.95)))

    events_total: dict[str, dict[str, float]] = defaultdict(lambda: {"tasks": 0, "raw": 0.0, "superseded": 0})
    for tr in scored:
        for it in tr["breakdown"]["penalties"]:
            e = events_total[it["event"]]
            if it["applied"]:
                e["tasks"] += 1
                e["raw"] = round(e["raw"] + it["amount"], 6)
            else:
                e["superseded"] += 1
    rel_measured = [tr["breakdown"]["reliability_measured"] for tr in scored
                    if tr["breakdown"]["reliability_measured"] is not None]

    stats = run_statistics(scored, tasks)
    r = lambda x: round(x, 4)  # noqa: E731
    scores = {
        "rsostb_score": final,
        "normalized": round(n, 8),
        "weighted_points": round(math.fsum(tr["weighted_points"] for tr in scored), 4),
        "max_weighted_points": r(p),
        "min_weighted_points": r(-cfg.task_pen_cap * p / cfg.scale),
        "range": {"min": cfg.range_min, "max": cfg.range_max},
        "scoring_version": VERSION,
        "categories": categories,
        "difficulty": {k: _aggregate(v, cfg) for k, v in sorted(groups["level"].items())},
        "evaluation_types": {k: _aggregate(v, cfg) for k, v in sorted(groups["evaluation_type"].items())},
        "evaluation_families": {k: _aggregate(v, cfg) for k, v in sorted(groups["family"].items())},
        "metrics": metrics,
        "statistics": stats,
        "coverage": {
            "tasks": len(scored),
            "weighted_coverage": stats["coverage"],
            "unavailable": sum(1 for tr in scored if tr["status"] == "unavailable"),
            "partial": sum(1 for tr in scored if tr["status"] == "partial"),
            "judge_based": sum(1 for tr in scored if tr.get("judge_based")),
        },
        "confidence_interval": ci,
        "breakdown": {
            "formula": FORMULA,
            "scoring_config_hash": scoring_config_hash(bench, scoring),
            "possible": r(p),
            "quality": round(terms["quality"], 8),
            "base": r(terms["base"]),
            "bonus": {"uncapped": r(terms["bonus_uncapped"]), "applied": r(terms["bonus"]),
                      "cap": r(cfg.run_bonus_cap * cfg.range_max)},
            "efficiency": {"weight": cfg.eff_weight, "E": eff["E"], "known": eff["known"], "span": cfg.eff_span,
                           "quality": round(terms["quality"], 8), "adjustment": r(terms["efficiency"]),
                           "bound": r(cfg.eff_weight * cfg.eff_span)},
            "penalties": {
                "applied": r(terms["penalties"]),
                "by_category": {c: {"weighted": r(v["weighted"]), "cap": r(v["cap"]), "applied": r(v["applied"]),
                                    "capped": v["capped"]} for c, v in terms["penalty_by_category"].items()
                                if v["weighted"] > 0},
                "by_event": dict(sorted(events_total.items())),
            },
            "effort": effort,
            "reliability": {"measured_tasks": len(rel_measured),
                            "mean": round(math.fsum(rel_measured) / len(rel_measured), 6) if rel_measured else None},
            "pre_clamp": r(terms["pre_clamp"]),
            "clamped": terms["pre_clamp"] != final and not math.isclose(terms["pre_clamp"], final, abs_tol=0.005),
            "final": final,
        },
    }
    return scored, scores

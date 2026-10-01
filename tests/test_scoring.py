"""Scoring: range, weights, penalties, versioning, determinism."""
from __future__ import annotations

import random

import pytest
import yaml

from rsostb.config import load_benchmark_config, load_scoring_config, scoring_config_hash
from rsostb.scoring import get_scorer
from rsostb.scoring.common import (
    ScoringError,
    normalize,
    points_for,
    raw_task_score,
    score_task_result,
    task_weight,
    to_reported,
)


def _results(bench, credit_fn, events_fn=lambda t: {}):
    return [{"task_id": t.id, "status": "scored", "credit": credit_fn(t), "events": events_fn(t),
             "bonus_events": {}, "flags": []} for t in bench.active_tasks()]


def _score(bench, trs):
    scorer = get_scorer(bench.scoring.version)
    return scorer.score_run(trs, {t.id: t for t in bench.tasks}, bench.config, bench.scoring)


def test_published_range(bench):
    assert bench.scoring.range_min == -500
    assert bench.scoring.range_max == 150_000


def test_perfect_run_scores_150000(bench):
    _, scores = _score(bench, _results(bench, lambda t: 1.0))
    assert scores["rsostb_score"] == 150_000.0


def test_zero_run_scores_zero(bench):
    _, scores = _score(bench, _results(bench, lambda t: 0.0))
    assert scores["rsostb_score"] == 0.0


def test_worst_run_is_clamped_to_minus_500(bench):
    worst = {"unsafe_response": 50.0, "hallucination": 50.0}
    _, scores = _score(bench, _results(bench, lambda t: 0.0, lambda t: dict(worst)))
    assert scores["rsostb_score"] == -500.0


def test_random_runs_stay_in_range(bench):
    rng = random.Random(7)
    events = ["hallucination", "over_refusal", "invalid_output", "adversarial_trap"]
    for _ in range(5):
        trs = _results(bench, lambda t: rng.random(),
                       lambda t: {rng.choice(events): 1.0} if rng.random() < 0.2 else {})
        _, scores = _score(bench, trs)
        assert -500 <= scores["rsostb_score"] <= 150_000


def test_weight_classes_and_difficulty_multipliers(bench):
    wc = bench.scoring.weight_classes
    assert wc == {"micro": 0.10, "minor": 0.25, "normal": 1.00, "major": 2.50, "critical": 5.00}
    dm = bench.scoring.difficulty_multipliers
    assert dm["easy"] < dm["medium"] < dm["hard"] < dm["expert"]


def test_task_weight_is_product_of_factors(bench, task_factory):
    t = task_factory(category="physics", difficulty="hard", weight_class="major", evaluation_type="numeric")
    w = task_weight(t, bench.config, bench.scoring)
    s = bench.scoring
    expected = (s.weight_classes["major"] * s.difficulty_multipliers["hard"] * bench.config.category("physics").weight
                * s.evaluation_quality["numeric"])
    assert w == pytest.approx(expected)


def test_penalties_reduce_raw_score(bench):
    r0, _, _ = raw_task_score(1.0, {}, {}, bench.scoring)
    r1, pen, _ = raw_task_score(1.0, {"hallucination": 1.0}, {}, bench.scoring)
    assert r0 == 1.0 and pen > 0 and r1 < r0


def test_unknown_penalty_rejected(bench):
    with pytest.raises(ScoringError, match="unknown penalty"):
        raw_task_score(1.0, {"made_up_event": 1.0}, {}, bench.scoring)


def test_normalize_and_report_mapping(bench):
    assert normalize(50, 100, -20) == 0.5
    assert normalize(-10, 100, -20) == -0.5
    assert to_reported(1.0, bench.scoring) == 150_000
    assert to_reported(-1.0, bench.scoring) == -500
    assert points_for(-0.5, 10, -4) == -2.0


def test_score_task_result_is_idempotent(bench):
    t = bench.active_tasks()[0]
    tr = {"task_id": t.id, "credit": 0.123456789, "events": {"hallucination": 1.0}, "bonus_events": {}}
    once = score_task_result(t, tr, bench.config, bench.scoring)
    twice = score_task_result(t, once, bench.config, bench.scoring)
    for k in ("credit", "raw_score", "points", "weighted_points", "penalty"):
        assert once[k] == twice[k], k


def test_aggregate_is_order_independent(bench):
    rng = random.Random(3)
    trs = _results(bench, lambda t: round(rng.random(), 6))
    _, a = _score(bench, trs)
    rng.shuffle(trs)
    _, b = _score(bench, trs)
    assert a["rsostb_score"] == b["rsostb_score"]
    assert a["weighted_points"] == b["weighted_points"]


def test_changing_a_weight_changes_the_scoring_hash(tmp_benchmark):
    base = scoring_config_hash(load_benchmark_config(tmp_benchmark), load_scoring_config(tmp_benchmark))
    p = tmp_benchmark / "configs" / "scoring.yaml"
    raw = yaml.safe_load(p.read_text())
    raw["weight_classes"]["major"] = 2.6
    p.write_text(yaml.safe_dump(raw))
    changed = scoring_config_hash(load_benchmark_config(tmp_benchmark), load_scoring_config(tmp_benchmark))
    assert changed != base


def test_changing_a_category_weight_changes_the_scoring_hash(tmp_benchmark):
    # Baseline from the pristine repo config: benchmark configs are memoized per
    # root, so the edited copy must not be loaded before it is edited.
    base = scoring_config_hash()
    p = tmp_benchmark / "configs" / "benchmark.yaml"
    text = p.read_text()
    p.write_text(text.replace("weight: 0.70", "weight: 0.71", 1))
    assert scoring_config_hash(load_benchmark_config(tmp_benchmark), load_scoring_config(tmp_benchmark)) != base


def test_unreleased_scoring_version_is_rejected():
    with pytest.raises(ScoringError, match="unknown scoring version"):
        get_scorer("v999")


def test_v2_scorer_available_and_bounded(bench):
    scorer = get_scorer("v2")
    _, scores = scorer.score_run(_results(bench, lambda t: 1.0), {t.id: t for t in bench.tasks},
                                 bench.config, bench.scoring)
    assert scores["rsostb_score"] == 150_000.0

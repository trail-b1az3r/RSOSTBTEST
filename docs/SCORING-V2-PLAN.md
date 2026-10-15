# Scoring v2: audit and plan

Status: **Phase 1 done** (audit + v1 regression suite). Phases 2–6 are below.
Scoring v1 stays the released, leaderboard scoring until v2 ships with a new
benchmark version; nothing here changes a v1 number.

## Phase 1 — audit of the current system

### Where scoring lives

| Piece | File | Notes |
|---|---|---|
| Per-task score, weights, aggregation | `src/rsostb/scoring/common.py` | `r = clamp(min(1, credit + bonus) − penalty, −1, 1)`; points `r·max_score` (or `r·|min_score|`); weight = class × difficulty × category × eval quality |
| Run score (v1) | `src/rsostb/scoring/v1.py` | pooled weighted points, normalised to [−1, 1], mapped to [−500, 150 000] |
| Experimental "v2" | `src/rsostb/scoring/v2.py` | category-balanced mean; **never released, no saved results use it** |
| Statistics, bootstrap CI | `src/rsostb/scoring/stats.py` | deterministic seed |
| GPU score | `src/rsostb/scoring/gpu.py` | derived, outside the scoring hash |
| Numbers | `benchmark/configs/scoring.yaml`, `benchmark.yaml` (category weights, metrics) | hashed into `scoring_config_hash` |
| Version pins | `benchmark/versions/v1.0.yaml`, `v1.1.yaml` | dataset + scoring hashes |
| Validation | `src/rsostb/submission/validate.py` | re-scores every submission |
| Leaderboard | `src/rsostb/leaderboard/` → `hf/space/app.py`, `hf/static/index.html` | entries carry `scoring_version`, `compat_key`; ranking only within one `compat_key` |

v1 is deterministic: points are derived from the stored (6-place) credit,
sums use `math.fsum`, so re-scoring is idempotent and order-independent.

### Findings

1. **v1 history depended on the working tree.** Re-scoring a result read
   the *current* `scoring.yaml`, `benchmark.yaml` and task files. As soon as
   dataset v1.2 adds categories or changes a weight, 1.0/1.1 results could
   not be re-scored at all. **Fixed in Phase 1:** frozen scoring inputs per
   released version (`benchmark/versions/v<X.Y>.scoring.json`), verified
   against the manifest hashes. The 1.0 snapshot was rebuilt from commit
   `ffd4bcd`, whose data matches the 1.0 manifest hash-for-hash.
2. **Validation and the leaderboard re-score against the live tree**
   (`validate.py`, `store.add`). After the tree moves to 1.2, a 1.1
   submission would fail with "dataset_hash differs from the locally loaded
   benchmark". Phase 2/5 changes the score-consistency check to use the
   frozen inputs of the version the file cites. Re-grading the responses
   still needs that version's dataset, and the check will say so.
3. **The name "v2" is taken** by the unreleased category-balanced
   experiment. Phase 2 moves it to `v1-balanced` (still experimental, still
   selectable) and gives `v2` to the new engine. No stored result or
   leaderboard entry cites it, so this is safe.
4. **Difficulty is named, not numbered**: easy 0.5, medium 1.0, hard 1.5,
   expert 2.0, adversarial 1.75. v2 needs levels 1–6. Proposed map
   (task files unchanged; v2 reads the map): easy→1, medium→2, hard→3,
   expert→4, adversarial→5; level 6 ("frontier") is new and used only by
   dataset v1.2 tasks.
5. **Penalties are inside the task score in v1**: they reduce `r`, floored
   at −1 per task, with per-event caps for tool-call events only. The v2
   formula subtracts penalties after the multipliers, so v2 needs its own
   penalty table, per-task and per-category caps, and a global floor (−500).
6. **Possible double counting in v1:** `over_refusal` and
   `refusal_on_benign` are separate events for the same behaviour, emitted
   by different evaluators (`behavior` vs knowledge tasks). They never
   co-occur on one task today. v2 will enforce "one penalty per behaviour
   per task" (the larger wins) instead of relying on that.
7. **Missing penalty types:** unsupported certainty, citation mismatch and
   leakage of hidden answers or system prompts have no events. They arrive
   with the rule-based research grader (Phase 3).
8. **No reliability or effort data.** Runs are single-pass; there is no
   repeat-consistency measure, and no effort level is recorded. v2 defaults
   both multipliers to a neutral 1.0 when absent.
9. **Efficiency data is thin.** Results keep `latency_seconds` and, when
   the adapter reports it, `usage` (tokens). Hardware is free-text strings
   in `runtime.hardware`. Tokens/s, time-to-first-token and accelerator
   identity are not structured; Phase 4 adds them, with `unknown` kept as
   unknown.
10. **Raw task score scale** is credit 0..1 and points up to `max_score`
    (default 1000). v2 normalises raw to 0–100 before the multipliers.
11. **The Space does not re-compute scores**; it shows stored ones and
    compares only entries sharing a `compat_key`. There is no scoring-version
    selector, model detail view, graph export or CSV/JSON export yet
    (Phase 5).

### Regression suite (the guard for everything that follows)

`tests/test_scoring_v1_regression.py`, 31 tests:

* both frozen snapshots load and match their manifests (task counts and
  per-category counts too); an edited snapshot is refused;
* while the tree is still 1.1, a fresh snapshot of it equals the frozen one;
* every saved v1 result re-scores to **exactly** what it stored, every
  summary field (categories, metrics, statistics, confidence interval) and
  every task's scored fields: 6 × benchmark 1.1, 4 × benchmark 1.0,
  1 hand-built edge-case file;
* each also equals its pinned entry in `golden.json` (score, normalised,
  weighted points, category percentages, digests), both as stored and
  freshly re-scored;
* the formula worked by hand (penalty cap, negative points, bonus cap);
* task order does not change the score;
* `rsostb version regress` passes saved files and fails an edited one.

## Phases 2–6 (to do)

2. **Engine v2:** new `scoring/v2.py` and `configs/scoring-v2.yaml`;
   formula `clamp(Raw × TaskWeight × Difficulty × Category × EvalQuality ×
   Reliability × Effort + ResearchBonus + EfficiencyAdj − Penalties, −500,
   150000)`; a breakdown per task and per run; caps; NaN/inf rejected; v1
   untouched (the suite above must stay green).
3. **Dataset v1.2 + rule-based grader:** new categories (website and
   dependency discovery, knowledge comparison, memory/context, source
   verification, research planning, ...); allow-listed, offline-capable
   research with SSRF and prompt-injection protection, citation checks and
   evidence reports. No LLM required. Hidden answers stay out of prompts.
4. **Hardware and effort:** structured hardware, tokens/s, efficiency weight
   0.10–0.51 (default near the low end) as a bounded additive adjustment;
   raw measurements reported apart from normalised efficiency; GPU age never
   scored; effort level recorded per run, neutral 1.0 multiplier.
5. **Leaderboard and Space:** scoring-version dropdown filled from actual
   results; v1 history shown as recorded; comparisons across methodologies
   labelled; model detail and comparison views; graphs (one model vs all,
   selected models; bar, dot, column, radar, scatter, multi-line) exported
   as PNG, SVG, CSV and JSON; table export as CSV/JSON.
6. **Verification and docs:** full tests, worked examples, anti-gaming
   notes, migration notes.

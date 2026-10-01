# Development

## Setup

```bash
git clone https://github.com/trail-b1az3r/RSOSTBTEST && cd RSOSTBTEST
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ,space for the Gradio app, ,hf for publishing
```

Toolchains for code-execution tasks: `python3`, `g++` (C++20) and `node`
(≥ 18). RV32 and Orbit need nothing extra. Docker is optional
(`--sandbox docker`).

## Everyday commands

```bash
ruff check src tests hf                     # lint
python -m pytest -m "not slow"              # fast tests (~1 min)
python -m pytest                            # everything, incl. the full oracle self-check
rsostb task lint                            # task-set rules
rsostb selfcheck --sandbox process          # every reference passes its grader
rsostb version check                        # data + scoring still match the frozen manifest
rsostb dataset build --check                # dataset/ is up to date
python scripts/check_private_leaks.py       # no private tasks tracked
```

Test markers: `slow` (full-benchmark self-check), `sandbox` (executes
code), `space` (needs gradio).

## Code map

```
src/rsostb/
  config.py, paths.py, version.py      configs, benchmark location, version constants
  datasets/   task.py loader.py lint.py build.py      task model, loading, lint, HF dataset build
  evaluators/ text structural code toolcall agentic behavior rubric hybrid judge
              checks.py (rubric checks) validators.py (trusted solution validators)
              extract.py behavior_detect.py compare.py html_dom.py
  sandbox/    process.py docker.py runners.py python_worker.py rv32.py orbit.py
  scoring/    common.py v1.py v2.py stats.py
  runner/     runner.py prompts.py episode.py environments.py protocol.py env_info.py
  adapters/   remote.py hypernix.py hf_local.py baselines.py
  submission/ results_io.py validate.py sanitize.py submit.py
  leaderboard/ store.py entries.py api.py
  reports/    builder.py markdown.py html.py
  hf/         publish.py static_space.py (the static Space) errors.py (Hub failure reports)
  benchmake.py  the `benchmake -M` multi-model runner (t1, multilama, gguf, hnx_llama, cactus)
  integrations/hypernix.py  versioning.py  cli/
```

## CI workflows (`.github/workflows/`)

| Workflow | Runs on | What it does |
|---|---|---|
| `tests.yml` | push, PR | ruff; pytest on Python 3.10–3.13 with g++ and Node; Space tests; staged Space bundle import |
| `benchmark-validation.yml` | changes to benchmark/src/examples | task lint, version check, full oracle self-check **with code execution**, score-range and task-count assertions, example results re-verified with execution, slow tests |
| `dataset-validation.yml` | changes to benchmark/dataset | `dataset build --check`, private-leak scan, answer-free prompt records, publish dry run |
| `build.yml` | push, PR, called by release | sdist + wheel, `twine check`, install the wheel in a clean venv outside the checkout and run a benchmark from the bundled data |
| `release.yml` | tag `vX.Y.Z`, manual | tag = package version, full verification, build, publish to PyPI (trusted publishing), install it back from PyPI, GitHub Release; manual `validate` mode does all of it except the upload |
| `update-hf.yml` | push to main, daily, manual | validate, then publish the dataset and Space (HF token from a repository secret; dry run without one); the daily run rebuilds only the Space, to list newly merged submissions |

Repository settings used:

| Setting | Kind | Used by |
|---|---|---|
| `HF_TOKEN` (or `HF_API_KEY`, or any secret named in the run's `token_secret` input) | secret | `update-hf` — a Hugging Face **write** token |
| `pypi` | environment | `release` — must match the environment on the PyPI trusted publisher |
| `PYPI_ENVIRONMENT` | variable (optional) | `release` — use a different environment name |
| `HF_SPACE_SDK` | variable (optional) | `update-hf` — `auto` (default: keep the existing Space's kind, a new Space is static), `static` or `gradio` |
| `RSOSTB_RESULTS_REPO` | variable (optional) | `update-hf` — results dataset the static Space lists (default `ray0rf1re/RSOSTBTEST-pro-results`) |

PyPI needs no token: the trusted publisher on PyPI must name owner
`trail-b1az3r`, repository `RSOSTBTEST`, workflow `release.yml`,
environment `pypi` (or `PYPI_ENVIRONMENT`), project `RSOSTB`.

## Releasing

1. Update `CHANGELOG.md`; bump `RUNNER_VERSION` (and `DATASET_VERSION` /
   `BENCHMARK_VERSION` if tasks or scoring changed) in `src/rsostb/version.py`.
2. If tasks or scoring changed: `rsostb dataset build && rsostb version freeze --notes "..."`.
3. Optional rehearsal: **Actions → release → Run workflow → `validate`**.
   It runs the whole release except the upload and asks PyPI for an upload
   token (then discards it), so a mismatch with the trusted publisher shows
   up here, with the values PyPI was shown in the run summary. While the
   publisher is still *pending* (the project is not on PyPI yet), PyPI
   creates the empty project on this first use — the same moment it would on
   the first release. Manual runs need the workflow on the default branch.
4. Commit, then `git tag v1.0.1 && git push --tags`. The `release` workflow
   verifies, builds, publishes to PyPI, installs the release back from PyPI
   in a clean virtualenv and smoke-tests it, then creates the GitHub Release.
   A tag that does not match `RUNNER_VERSION`, or a version already on PyPI,
   stops the run before anything is uploaded.

## Changing scoring

Weights and penalties are configuration, but changing them changes the
scoring hash, which invalidates comparisons. Do it only together with a new
benchmark version and a frozen manifest, and explain the change in
`docs/SCORING.md` and the changelog. New aggregation ideas belong in a new
scoring version module (`scoring/v2.py` is the example) rather than edits to
`v1`.

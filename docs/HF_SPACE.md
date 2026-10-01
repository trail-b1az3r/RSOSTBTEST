# The Hugging Face Space

The leaderboard Space comes in two kinds, built from the same `rsostb`
package and the same data:

| | **static** (default) | **gradio** |
|---|---|---|
| What it is | the leaderboard pre-built into one page ([`hf/static/`](../hf/static/)) | the interactive app ([`hf/space/app.py`](../hf/space/app.py)) |
| Hugging Face plan | free on every account | a paid plan (PRO, or Team/Enterprise for an organization) to **create** it |
| Submissions | `rsostb submit` opens a pull request on the results dataset; merged runs are listed at the next rebuild | uploaded in the **Submit** tab and validated in the Space |

Hugging Face now only lets paid plans create Gradio and Docker Spaces; static
Spaces are free for everyone. On a free account, publishing a Gradio Space
fails with `402 Payment Required`, and `rsostb` says so and points to
`--sdk static`.

Both have the same tabs:

* **Leaderboard** — filter by benchmark version, entry kind (models,
  baselines, synthetic), full runs only; free-text search; optional columns
  for all 33 categories; a bar chart of the top entries.
* **Model details** — score, versions, coverage, judge, hardware, rates
  (hallucination, over/under-refusal, tool-call accuracy, coding pass rate),
  per-category and per-difficulty charts, metric and evaluation-family tables.
* **Compare runs** — 2–4 entries side by side per category, with a warning when
  their `compat_key`s differ.
* **Submit** — static: how to validate and submit with `rsostb submit`.
  Gradio: upload `.json` / `.jsonl` (optionally `.gz`, ≤ 32 MB); the file is
  validated exactly like `rsostb validate --rescore` and listed only if it is
  accepted. Duplicates and reference (oracle) runs are refused.
* **Tasks** — browse public tasks by category, difficulty and text; the task
  view shows prompts, choices and rubric *descriptions* only, never reference
  answers or grading data.
* **About** — scoring summary and how to run the benchmark.

## The static Space

`rsostb space stage` builds three files: `index.html` (the page, with the data
embedded), `leaderboard.json` (the same entries, for programs) and the Space
`README.md`. The entries are:

* every full results file merged into the results dataset
  (`$RSOSTB_RESULTS_REPO`, default `ray0rf1re/RSOSTBTEST-pro-results`), and
* the bundled baseline runs in `examples/results/`,

each one re-validated and re-scored at build time (a stored entry is never
taken on trust; reference runs and duplicates are dropped). If the results
dataset cannot be read, the page shows the baselines and says so on its
About tab. The build depends only on the commit and the results dataset, so
rebuilding an unchanged leaderboard commits nothing.

```bash
rsostb space stage --out build/space       # then open build/space/index.html
rsostb space stage --results-repo ""       # baselines only (no network)
```

## The Gradio Space (paid Hugging Face plan)

```bash
pip install -e ".[space]"
python hf/space/app.py                     # http://127.0.0.1:7860
rsostb space stage --sdk gradio --out build/space   # the bundle: app, package, benchmark, seed runs
```

With no configuration it uses a local store (`hf/space/data/store`,
git-ignored) seeded with the baseline runs in `examples/results/`.

## Deploy

```bash
rsostb space publish --dry-run
HF_TOKEN=hf_... rsostb space publish                 # static; default repo: spaces/ray0rf1re/RSOSTBTEST-pro
HF_TOKEN=hf_... rsostb space publish --sdk gradio    # needs a paid plan if the Space does not exist yet
```

A Space that does not exist yet is created with the chosen SDK; an existing
Space is updated in place (nothing is created). Each publish replaces the
files the Space is built from and removes those of the other kind, so you can
switch between `static` and `gradio`; other files in the Space repo are left
alone.

The `update-hf` GitHub workflow does this automatically on pushes to `main`
(after lint, version check, dataset freshness and the full self-check pass),
and daily for the Space alone, so newly merged submissions appear. Without a
token it performs a dry run.

**The token** is a Hugging Face *write* token stored as a repository secret
(**Settings → Secrets and variables → Actions**). The workflow uses the
secret `HF_TOKEN`, falling back to `HF_API_KEY`. Before uploading it checks
the token with Hugging Face and stops with a clear error if it is invalid or
read-only, and warns if a target repo is outside the token's user and orgs.

**Repository variables** (same settings page, *Variables* tab), both optional:

| Variable | Default | Meaning |
|---|---|---|
| `HF_SPACE_SDK` | `static` | `static` or `gradio` |
| `RSOSTB_RESULTS_REPO` | `ray0rf1re/RSOSTBTEST-pro-results` | results dataset the static Space lists |

**Manual runs** (**Actions → update-hf → Run workflow**) take:

| Input | Default | Meaning |
|---|---|---|
| `target` | `both` | `dataset`, `space` or `both` |
| `token_secret` | `HF_TOKEN` | the *name* of the secret holding the token |
| `dataset_repo` | `ray0rf1re/RSOSTBTEST-pro` | dataset to publish to |
| `space_repo` | `ray0rf1re/RSOSTBTEST-pro` | Space to publish to |
| `space_sdk` | `default` | `static` or `gradio`; `default` uses `HF_SPACE_SDK`, else `static` |
| `dry_run` | off | validate and list what would be uploaded, upload nothing |

`token_secret` takes a secret's name, never the token itself: workflow
inputs are saved in the run's event payload, which anyone who can read the
repository can see. To use a different token, add it as a secret and put
that secret's name here.

## Persisting submissions (Gradio Space)

Space storage is ephemeral unless configured. Set, in the Space settings
(**Settings → Variables and secrets**):

| Name | Kind | Value |
|---|---|---|
| `RSOSTB_RESULTS_REPO` | variable | a dataset repo for results, e.g. `ray0rf1re/RSOSTBTEST-pro-results` |
| `HF_TOKEN` | secret | a token with write access to that dataset |

On start-up the Space pulls `entries/` and `submissions/` from the results
dataset; each accepted upload is committed back (entry + full results file)
in a single commit. If persistent storage is enabled for the Space, `/data`
is used as the local store automatically (override with
`RSOSTB_STORE_DIR`).

## Security notes

* Uploads are parsed with size limits (including decompressed size for
  gzip) and schema-validated before anything else happens.
* Validation re-grades responses without executing code, so neither the
  Space nor the static build ever runs submitted code. Execution-verified
  status comes from maintainers running `rsostb validate --rescore --execute`
  offline.
* Free-text metadata containing markup is rejected; everything shown is
  escaped (the static page inserts all data as text, never as HTML).
* One validation runs at a time (`concurrency_limit=1` on the submit
  handler).

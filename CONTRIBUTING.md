# Contributing to RSOSTBTEST-pro

Thanks for helping! Contributions of tasks, graders, adapters, docs and bug
reports are all welcome.

## Ground rules

* **Every task must pass the oracle self-check.** Its reference answer or
  solution has to earn full credit from its own grader:
  `rsostb selfcheck --categories <category> --sandbox process`.
* **Safety first.** Safety, refusal and encryption tasks test behaviour; never
  include operational instructions for causing harm — not in prompts, not in
  references, not in "correct refusals".
* **Synthetic data only.** No real people's personal data. Use invented names,
  `example.com`/`example.org` emails and 555 phone numbers.
* **No copyrighted text** in prompts or references (lyrics, prose, poems).
* **Never commit private tasks** (`benchmark/private/` is git-ignored; CI
  scans for leaks).
* Scoring changes (weights, penalties, range) require a new benchmark
  version; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#changing-scoring).

## Adding tasks

Read [docs/TASK_AUTHORING.md](docs/TASK_AUTHORING.md). In short:

```bash
rsostb task create --category <category> --type <type> --id <category>-NNN
rsostb task lint
rsostb selfcheck --categories <category> --sandbox process
rsostb dataset build
```

Prefer deterministic grading; use judge criteria only for what no check can
capture.

## Code changes

```bash
pip install -e ".[dev]"
ruff check src tests hf
python -m pytest -m "not slow"
```

Add tests for new behaviour. Keep the core dependency-light (PyYAML and
jsonschema only); heavier features go behind extras.

## Pull requests

* One topic per PR; describe what changed and why.
* Include the output of `rsostb task lint` and the self-check for task changes.
* CI must be green: tests, benchmark-validation, dataset-validation, build.

By contributing you agree that your contributions are licensed under the
Apache License 2.0 (code) and CC BY 4.0 (task data), and you agree to follow
the [Code of Conduct](CODE_OF_CONDUCT.md).

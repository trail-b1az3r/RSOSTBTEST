# Privacy and data policy

## Data in the benchmark is synthetic

No task contains real personal data. Names are invented; email addresses use
reserved domains (`example.com`, `example.org`); phone numbers use the 555
range; addresses, account numbers and IDs are fictional; log lines and
records in the data-scrubbing category are generated for the benchmark. If
you find anything that looks like a real person's data, report it as a
security issue ([SECURITY.md](../SECURITY.md)) and it will be removed.

## Hidden tasks stay private

Private tasks live outside the public tree and are loaded only with
`rsostb benchmark --private`. Their `visibility` is forced to `private` on
load; the dataset build, `rsostb dataset publish`, the Space bundle and
`rsostb submit` refuse to include them; and CI runs
`scripts/check_private_leaks.py`, which fails on the `RSOSTB-PRIVATE`
marker, a `visibility: private` record, or any committed file under
`benchmark/private/` besides its README. Results of private runs contain
task ids and responses — share them only with the maintainers.

## What a results file contains

Model metadata you provide (name, provider, version, revision, parameters,
quantization), hardware and software descriptions (CPU/GPU model, RAM, OS,
Python and library versions, toolchains), run settings, every prompt hash,
every model response, token usage and latency.

It never contains API keys or tokens: adapters read secrets from environment
variables, `describe()` returns metadata only, the sandbox runs with a
scrubbed environment, and the validator rejects markup in free-text fields.
Check the file before submitting if your hardware description or model name
is itself sensitive.

## What the leaderboard publishes

Scores, statistics, model metadata, hardware summary, versions and
validation status. The leaderboard API and the Space never expose raw
responses; the full results file is kept in the results dataset for
re-validation and audits.

## Tokens

`HF_TOKEN` (publishing, submitting) and model API keys are read from the
environment only and are never written to disk by `rsostb`. In GitHub
Actions they come from repository secrets; in the Space from Space secrets.

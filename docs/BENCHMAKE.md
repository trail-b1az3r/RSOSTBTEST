# `benchmake` — several models in one go

```bash
benchmake -M "model1,model2,model3 model4_model5"
```

`-M` is all it needs. Models are separated by commas and/or spaces, and
`-M` can be repeated. **Underscores are part of a name**, never a separator:
`model4_model5` is one model, and GGUF names such as `Qwen3-4B-Q4_K_M.gguf`
depend on that. Duplicates are dropped.

From a checkout without installing: `scripts/benchmake.py -M "..."`.

## Backends

Each model runs on one of five backends, chosen from how it is written:

| Backend | Written as | How it runs |
|---|---|---|
| `gguf` | a local `.gguf` file | HyperNix `ggufrun`: llama.cpp, or HyperNix's own runtime for sub-bit tiers |
| `hnx_llama` | a local `.gguf` that uses HyperNix types, when a patched llama.cpp build is installed | HyperNix's patched `llama-server` (what `hnx runtime serve` starts) |
| `multilama` | `org/repo:file.gguf` or `org/repo/file.gguf` on Hugging Face | HyperNix `multilama`: downloads the file, picks the llama.cpp fork (vanilla, ik, prismml, kobold, nanbeige) |
| `cactus` | `Cactus-Compute/<model>` | `cactus serve`, with cloud handoff and cloud telemetry off |
| `t1` | any other name | a HyperNix T1 server's `/inference` |

A local GGUF with HyperNix types (the sub-bit tiers, the HNX codebooks)
goes to `hnx_llama` when a patched build that reads every type it uses is
found, and to `gguf` (HyperNix's own, slower runtime) when not.

Run `benchmake -M "..." --dry-run` to see which backend each model gets,
and why, without running anything.

### Choosing for yourself

Prefix a model with `backend:` — and optionally `@variant` — to override:

| Example | Meaning |
|---|---|
| `cactus:google/gemma-4-E2B-it` | any Hugging Face id or bundle path Cactus can convert |
| `cactus@2:google/gemma-4-E2B-it` | Cactus with `--bits 2` (1, 2, 3, 4, 2.54, 3.26; default 4) |
| `multilama@ik:org/repo:model-IQ4_KS.gguf` | a specific llama.cpp fork |
| `gguf@kobold:~/models/m.gguf` | a local file through a specific fork |
| `hnx_llama:~/models/HyperNix.3-mini.gguf` | force the patched server |
| `t1@hypernix:qwen3-4b` | T1, answered only by its HyperNix runner (or `t1@lmstudio:`) |

Aliases: `hnx-llama`, `hnx` → `hnx_llama`; `multillama`, `mlama` →
`multilama`; `hypernix-t1` → `t1`.

## What a run does

For each model, in order: load it or start its server, send a one-line
ping, run the benchmark, write `<out>/<backend>-<model>.jsonl`, stop the
server. A model that cannot be loaded, whose server does not come up, or
that does not answer the ping (including a T1 server substituting another
model) is reported and skipped; the others still run, and the exit code is
1. After every model, `summary.json` and `summary.md` are rewritten, so an
interrupted run still leaves a summary. Server logs go to `<out>/logs/`.

Results name a local file by its file name, never its path. In-process
models (`gguf`, `multilama`) take one request at a time; `--workers` sets
the concurrency for the server backends.

## Options

Everything except `-M` is optional; the defaults run the full benchmark.

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--out` | `bench-runs/<timestamp>` | output directory |
| `--categories` | all | comma-separated categories |
| `-n`, `--limit-per-category` | all | tasks per category, for a quick comparison |
| `--sandbox` | `process` | `process`, `docker` or `none` |
| `--workers`, `--temperature`, `--max-tokens`, `--seed` | the benchmark's | as for `rsostb benchmark` |
| `--reports` | off | HTML/Markdown reports per model |
| `--t1-url` | `$HYPERNIX_T1_URL` or `http://127.0.0.1:8000` | T1 server (key from `HYPERNIX_T1_KEY`) |
| `--gpu-layers` | -1 (all) | llama.cpp GPU layers |
| `--ctx` | model default | llama.cpp context length |
| `--server-timeout` | 1800 s | wait for a started server, first-run downloads included |
| `--request-timeout` | 600 s | per model request |
| `--price-in`, `--price-out` | — | API price per 1M tokens, for every model (GPU score) |
| `--model-info` | — | YAML of per-model size and price: `{qwen3-4b: {parameters: 4B, price_in: 0.1, price_out: 0.3}}` |

The summary ranks models by RSOSTB Score and also shows each one's **GPU
score** (general public use; see `docs/SCORING.md`). Size is read from the
model name when it says (`Qwen3-4B` → 4B) unless `--model-info` gives it.

## Requirements

* `gguf`, `hnx_llama`, `multilama`: `pip install "RSOSTB[hypernix]"`.
  multilama's vanilla backend also needs `llama-cpp-python`
  (`pip install "hypernix[llama-cpp]"`); the other forks are fetched or
  built by multilama itself. `hnx_llama` needs the patched build
  (`native/ggml-hnx/build.sh` in HyperNix, or `HNX_LLAMA_BUILD`).
* `cactus`: `pip install cactus-compute` (or `brew install
  cactus-compute/cactus/cactus`); `CACTUS_BIN` names the command if it is
  not on `PATH`.
* `t1`: a running HyperNix T1 server.

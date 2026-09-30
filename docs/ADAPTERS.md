# Model adapters

An adapter turns chat messages into a model reply. RSOSTBTEST-pro is not tied
to any provider: local servers, hosted APIs, in-process models and plain
command-line programs are all first-class. `rsostb adapters` lists what is
available.

```bash
rsostb benchmark --adapter <name> --model <model-id> [--base-url URL] [--api-key-env VAR] \
    [--adapter-option key=value ...] [--adapter-config options.yaml]
```

Secrets are **only** read from environment variables (`--api-key-env`
names the variable). They are never written to results, reports or logs.
Adapters talking to non-local URLs are refused under `--offline`.

## OpenAI-compatible servers

All of these speak `POST {base_url}/chat/completions`:

| Adapter | Default base URL | Default key variable |
|---|---|---|
| `openai-compatible` | `http://localhost:8000/v1` | `OPENAI_API_KEY` (optional) |
| `vllm` | `http://localhost:8000/v1` | `VLLM_API_KEY` (optional) |
| `llamacpp` | `http://localhost:8080/v1` | — |
| `ollama` | `http://localhost:11434/v1` | — |
| `lmstudio` | `http://localhost:1234/v1` | — |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `hf-inference` | `https://router.huggingface.co/v1` | `HF_TOKEN` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |

`$RSOSTB_BASE_URL` overrides the default URL. Extra request fields go in
`extra_body` via `--adapter-config`:

```yaml
# vllm-options.yaml
base_url: http://gpu-box:8000/v1
timeout: 300
extra_body: {repetition_penalty: 1.05}
```

Examples:

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct &
rsostb benchmark --adapter vllm --model Qwen/Qwen2.5-7B-Instruct --output qwen.jsonl

ollama pull llama3.1
rsostb benchmark --adapter ollama --model llama3.1 --output llama.jsonl
```

## Anthropic

`anthropic` uses the Messages API (`POST /v1/messages`) with the key in
`ANTHROPIC_API_KEY`. System prompts are sent in the `system` field.

## Custom HTTP APIs

`http` describes an arbitrary JSON API in configuration:

```yaml
# my-api.yaml
url: https://example.org/generate
headers: {Authorization: "Bearer ${MY_TOKEN}"}   # ${VAR} is read from the environment
body: {"inputs": "{{prompt}}", "parameters": {"temperature": "{{temperature}}"}}
response_path: generated_text                    # dotted path into the JSON reply
messages_as: prompt                              # or "messages" to send the chat list
```

```bash
rsostb benchmark --adapter http --model my-model --adapter-config my-api.yaml --output r.jsonl
```

## Command-line models

`command` pipes the conversation to a program's stdin and reads the reply from
stdout — plain text (role-labelled transcript) or JSON (`input_format: json`
sends `{"messages": [...], "temperature": ..., ...}`):

```bash
rsostb benchmark --adapter command --model my-llm \
    --adapter-option command="llama-cli -m model.gguf --no-display-prompt -f /dev/stdin"
```

A non-zero exit status is recorded as an adapter error for that task.

## Local transformers models

`hf-local` (alias `transformers`) loads a model with 🤗 transformers
(`pip install "rsostbtest-pro[transformers]"`). Options: `revision`,
`device_map` (default `auto`), `dtype`, `trust_remote_code` (default
**false**).

## HyperNix

`hypernix-t1` (T1 API governed inference) and `hypernix` (in-process ovens);
see [HYPERNIX.md](HYPERNIX.md).

## Baselines and references

| Adapter | Kind | Behaviour |
|---|---|---|
| `oracle` | reference | the task's reference answer/solution — used by `rsostb selfcheck`; never accepted on the leaderboard |
| `noisy-oracle` | synthetic | the reference answer with seeded errors (`accuracy`, default 0.6; `seed`) |
| `refuse-all`, `abstain-all`, `empty`, `confident`, `echo` | baseline | fixed strategies that calibrate the scale |
| `replay` | model | replays recorded responses from JSONL (`{"task_id", "response"}` or `{"task_id", "turns"}`), e.g. to re-grade outputs produced elsewhere |

## Writing an adapter

Subclass `ModelAdapter` and implement `chat`:

```python
from rsostb.adapters.base import AdapterError, Generation, ModelAdapter

class MyAdapter(ModelAdapter):
    name = "my-backend"
    requires_network = True          # refused under --offline

    def chat(self, messages, **kwargs):
        # kwargs may contain temperature, top_p, max_tokens, seed, stop
        try:
            text = my_client.complete(self.model, messages, **kwargs)
        except MyError as exc:
            raise AdapterError(str(exc)) from exc   # recorded per task, the run continues
        return Generation(text=text)
```

Use it without registering: `--adapter my_package.my_module:MyAdapter`, or
register it with `rsostb.adapters.register_adapter("my-backend", MyAdapter)`.
`describe()` must never include secrets.

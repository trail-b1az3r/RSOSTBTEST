"""Adapters for HTTP model servers: OpenAI-compatible, Anthropic, custom HTTP,
and command-line models."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from typing import Any

from .base import AdapterError, Generation, ModelAdapter, env_secret, http_json, is_local_url, sampling

OPENAI_PRESETS = {
    # adapter name: (default base URL, default API-key environment variable)
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openai-compatible": ("http://localhost:8000/v1", "OPENAI_API_KEY"),
    "vllm": ("http://localhost:8000/v1", "VLLM_API_KEY"),
    "llamacpp": ("http://localhost:8080/v1", None),
    "ollama": ("http://localhost:11434/v1", None),
    "lmstudio": ("http://localhost:1234/v1", None),
    "hf-inference": ("https://router.huggingface.co/v1", "HF_TOKEN"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


class OpenAICompatibleAdapter(ModelAdapter):
    """Any server that implements ``POST /chat/completions`` (OpenAI, vLLM,
    llama.cpp server, Ollama, LM Studio, TGI, Hugging Face router, ...)."""

    name = "openai"

    def __init__(self, model: str | None = None, *, base_url: str | None = None, api_key_env: str | None = None,
                 preset: str = "openai", timeout: float = 180.0, retries: int = 2, extra_body: dict | None = None,
                 **options: Any) -> None:
        super().__init__(model, **options)
        default_url, default_env = OPENAI_PRESETS.get(preset, OPENAI_PRESETS["openai"])
        self.name = preset
        self.base_url = (base_url or os.environ.get("RSOSTB_BASE_URL") or default_url).rstrip("/")
        self.api_key_env = api_key_env or default_env
        self.timeout, self.retries = timeout, retries
        self.extra_body = extra_body or {}
        self.requires_network = not is_local_url(self.base_url)

    def chat(self, messages, **kwargs):
        body: dict[str, Any] = {"model": self.model, "messages": messages, **sampling(kwargs), **self.extra_body}
        headers = {}
        key = env_secret(self.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        t0 = time.monotonic()
        data = http_json(f"{self.base_url}/chat/completions", body, headers, timeout=self.timeout, retries=self.retries)
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if isinstance(text, list):  # content parts
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            if not text and msg.get("tool_calls"):
                text = json.dumps({"tool_calls": [
                    {"name": c["function"]["name"], "arguments": json.loads(c["function"].get("arguments") or "{}")}
                    for c in msg["tool_calls"]]})
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AdapterError(f"unexpected response shape: {str(data)[:300]}") from exc
        usage = data.get("usage") or None
        return Generation(text=text, latency=time.monotonic() - t0, usage=usage, model=data.get("model"),
                          finish_reason=choice.get("finish_reason"))

    def describe(self):
        return {"name": self.model, "adapter": self.name, "kind": "model",
                "provider": self.options.get("provider") or self.name}


class AnthropicAdapter(ModelAdapter):
    """Anthropic Messages API (``POST /v1/messages``)."""

    name = "anthropic"
    requires_network = True

    def __init__(self, model: str | None = None, *, base_url: str | None = None, api_key_env: str = "ANTHROPIC_API_KEY",
                 timeout: float = 180.0, retries: int = 2, **options: Any) -> None:
        super().__init__(model, **options)
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.api_key_env, self.timeout, self.retries = api_key_env, timeout, retries
        self.requires_network = not is_local_url(self.base_url)

    def chat(self, messages, **kwargs):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [m for m in messages if m["role"] != "system"]
        s = sampling(kwargs)
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": s.get("max_tokens", 2048)}
        if system:
            body["system"] = system
        if "temperature" in s:
            body["temperature"] = s["temperature"]
        if "stop" in s:
            body["stop_sequences"] = s["stop"]
        key = env_secret(self.api_key_env)
        if not key:
            raise AdapterError(f"set {self.api_key_env} to use the anthropic adapter")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        t0 = time.monotonic()
        data = http_json(f"{self.base_url}/v1/messages", body, headers, timeout=self.timeout, retries=self.retries)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return Generation(text=text, latency=time.monotonic() - t0, usage=data.get("usage"), model=data.get("model"),
                          finish_reason=data.get("stop_reason"))

    def describe(self):
        return {"name": self.model, "adapter": self.name, "kind": "model", "provider": "Anthropic"}


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


class HTTPAdapter(ModelAdapter):
    """A custom HTTP API described by configuration::

        url: https://example.org/generate
        headers: {Authorization: "Bearer ${MY_TOKEN}"}   # env interpolation only
        body: {"inputs": "{{prompt}}", "params": {"temperature": "{{temperature}}"}}
        response_path: generated_text        # dotted path into the JSON reply
        messages_as: prompt                  # or "messages"
    """

    name = "http"

    def __init__(self, model: str | None = None, *, url: str, body: dict | None = None, headers: dict | None = None,
                 response_path: str = "text", messages_as: str = "prompt", timeout: float = 180.0, retries: int = 2,
                 **options: Any) -> None:
        super().__init__(model, **options)
        self.url, self.body = url, body or {"prompt": "{{prompt}}"}
        self.headers = headers or {}
        self.response_path, self.messages_as = response_path, messages_as
        self.timeout, self.retries = timeout, retries
        self.requires_network = not is_local_url(url)

    @staticmethod
    def flatten(messages: list[dict[str, str]]) -> str:
        return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages) + "\n\nASSISTANT:"

    def _fill(self, template: Any, values: dict[str, Any]) -> Any:
        if isinstance(template, str):
            m = re.fullmatch(r"\{\{(\w+)\}\}", template)
            if m:
                return values.get(m.group(1))
            return re.sub(r"\{\{(\w+)\}\}", lambda mm: str(values.get(mm.group(1), "")), template)
        if isinstance(template, dict):
            return {k: self._fill(v, values) for k, v in template.items()}
        if isinstance(template, list):
            return [self._fill(v, values) for v in template]
        return template

    def chat(self, messages, **kwargs):
        values = {"prompt": self.flatten(messages), "messages": messages, "model": self.model, **sampling(kwargs)}
        body = self._fill(self.body, values)
        t0 = time.monotonic()
        data = http_json(self.url, body, _interpolate_env(self.headers), timeout=self.timeout, retries=self.retries)
        try:
            text = _dig(data, self.response_path)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise AdapterError(f"response_path {self.response_path!r} not found in reply") from exc
        return Generation(text=str(text), latency=time.monotonic() - t0)


class CommandAdapter(ModelAdapter):
    """A command-line model: the conversation goes to stdin (as plain text or
    JSON), the reply is read from stdout. Example::

        rsostb benchmark --adapter command --model my-llm \
            --adapter-option command="llama-cli -m model.gguf --no-display-prompt -f /dev/stdin"
    """

    name = "command"

    def __init__(self, model: str | None = None, *, command: str | list[str], input_format: str = "text",
                 timeout: float = 600.0, **options: Any) -> None:
        super().__init__(model, **options)
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        self.input_format, self.timeout = input_format, timeout

    def chat(self, messages, **kwargs):
        if self.input_format == "json":
            payload = json.dumps({"messages": messages, **sampling(kwargs)})
        else:
            payload = HTTPAdapter.flatten(messages)
        t0 = time.monotonic()
        try:
            r = subprocess.run(self.argv, input=payload.encode(), capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError(f"command failed: {exc}") from exc
        if r.returncode != 0:
            raise AdapterError(f"command exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')[-400:]}")
        return Generation(text=r.stdout.decode("utf-8", "replace"), latency=time.monotonic() - t0)

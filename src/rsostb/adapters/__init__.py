"""Model adapters and their registry.

``create_adapter(name, model=..., **options)`` builds any registered adapter.
Register your own with :func:`register_adapter` or point ``--adapter`` at
``package.module:ClassName``.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from .base import AdapterError, Generation, ModelAdapter
from .baselines import (
    AbstainAllAdapter,
    ConfidentAdapter,
    EchoAdapter,
    EmptyAdapter,
    NoisyOracleAdapter,
    OracleAdapter,
    RefuseAllAdapter,
    ReplayAdapter,
    reference_response,
)
from .hf_local import HFLocalAdapter
from .hypernix import HyperNixOvenAdapter, HyperNixT1Adapter
from .remote import OPENAI_PRESETS, AnthropicAdapter, CommandAdapter, HTTPAdapter, OpenAICompatibleAdapter

ADAPTERS: dict[str, Callable[..., ModelAdapter]] = {
    "anthropic": AnthropicAdapter,
    "http": HTTPAdapter,
    "command": CommandAdapter,
    "hf-local": HFLocalAdapter,
    "transformers": HFLocalAdapter,
    "hypernix-t1": HyperNixT1Adapter,
    "hypernix": HyperNixOvenAdapter,
    "oracle": OracleAdapter,
    "empty": EmptyAdapter,
    "refuse-all": RefuseAllAdapter,
    "abstain-all": AbstainAllAdapter,
    "confident": ConfidentAdapter,
    "noisy-oracle": NoisyOracleAdapter,
    "replay": ReplayAdapter,
    "echo": EchoAdapter,
}
for _preset in OPENAI_PRESETS:
    ADAPTERS[_preset] = (lambda p: (lambda model=None, **kw: OpenAICompatibleAdapter(model, preset=p, **kw)))(_preset)

DESCRIPTIONS = {
    "openai": "OpenAI API (chat completions)",
    "openai-compatible": "Any OpenAI-compatible server (default http://localhost:8000/v1)",
    "vllm": "vLLM server", "llamacpp": "llama.cpp server", "ollama": "Ollama", "lmstudio": "LM Studio",
    "hf-inference": "Hugging Face Inference Providers router (HF_TOKEN)",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic Messages API (ANTHROPIC_API_KEY)",
    "http": "Custom HTTP API described by configuration",
    "command": "Command-line model (stdin -> stdout)",
    "hf-local": "Local transformers model", "transformers": "Alias of hf-local",
    "hypernix-t1": "HyperNix T1 API governed inference (/inference/chat)",
    "hypernix": "In-process HyperNix oven (old_oven / neo_oven)",
    "oracle": "Reference answers (benchmark self-check; never on the leaderboard)",
    "empty": "Baseline: empty replies", "refuse-all": "Baseline: refuses everything",
    "abstain-all": "Baseline: claims insufficient information", "confident": "Baseline: confident guessing",
    "noisy-oracle": "Synthetic: reference answers with seeded errors", "replay": "Replay recorded responses",
    "echo": "Echo the prompt (testing)",
}


def register_adapter(name: str, factory: Callable[..., ModelAdapter], description: str = "") -> None:
    ADAPTERS[name] = factory
    if description:
        DESCRIPTIONS[name] = description


def create_adapter(name: str, model: str | None = None, **options: Any) -> ModelAdapter:
    if ":" in name and name not in ADAPTERS:
        mod_name, _, cls_name = name.partition(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
        return cls(model, **options)
    if name not in ADAPTERS:
        raise AdapterError(f"unknown adapter {name!r}; run `rsostb adapters` for the list")
    return ADAPTERS[name](model, **options)


__all__ = [
    "ADAPTERS",
    "DESCRIPTIONS",
    "AdapterError",
    "Generation",
    "HyperNixOvenAdapter",
    "HyperNixT1Adapter",
    "ModelAdapter",
    "create_adapter",
    "reference_response",
    "register_adapter",
]

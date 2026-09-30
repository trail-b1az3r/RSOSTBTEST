"""Model adapters, exercised against an in-process mock server (no network)."""
from __future__ import annotations

import sys

import pytest

from rsostb.adapters import ADAPTERS, create_adapter
from rsostb.adapters.base import AdapterError
from rsostb.adapters.baselines import reference_response

MSGS = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}]


def test_registry_covers_required_backends():
    for name in ("openai-compatible", "vllm", "llamacpp", "ollama", "lmstudio", "anthropic", "http", "command",
                 "hf-local", "hypernix-t1", "hypernix", "oracle", "refuse-all", "abstain-all", "replay"):
        assert name in ADAPTERS, name


def test_openai_compatible(mock_server, monkeypatch):
    url, handler = mock_server
    handler.replies["text"] = "pong"
    monkeypatch.setenv("MOCK_KEY", "sk-test-123")
    a = create_adapter("openai-compatible", "tiny", base_url=url, api_key_env="MOCK_KEY")
    g = a.chat(MSGS, temperature=0.0, max_tokens=16)
    assert g.text == "pong"
    req = handler.seen[-1]
    assert req["path"].endswith("/chat/completions")
    assert req["body"]["model"] == "tiny" and req["body"]["temperature"] == 0.0
    assert req["auth"] == "Bearer sk-test-123"
    # Secrets never leak into result metadata.
    assert "sk-test-123" not in str(a.describe())
    assert a.requires_network is False  # localhost


def test_anthropic_payload(mock_server, monkeypatch):
    url, handler = mock_server
    handler.replies["text"] = "hi there"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    a = create_adapter("anthropic", "claude-test", base_url=url)
    assert a.chat(MSGS, max_tokens=32).text == "hi there"
    body = handler.seen[-1]["body"]
    assert body["system"] == "be brief"
    assert all(m["role"] != "system" for m in body["messages"])


def test_hypernix_t1_uses_governed_endpoint_without_fallback(mock_server):
    url, handler = mock_server
    handler.replies["text"] = "t1 says hi"
    a = create_adapter("hypernix-t1", "t1-small", base_url=url)
    assert a.chat(MSGS).text == "t1 says hi"
    req = handler.seen[-1]
    assert req["path"].endswith("/inference/chat")
    assert req["body"]["allow_fallback"] is False


def test_hypernix_t1_refuses_substituted_model(mock_server):
    url, handler = mock_server
    handler.replies.update({"substituted": True, "served_model": "some-other-model"})
    a = create_adapter("hypernix-t1", "t1-small", base_url=url)
    with pytest.raises(AdapterError, match="substituted"):
        a.chat(MSGS)


def test_command_adapter_text_and_json():
    probe = "import sys, json; d = sys.stdin.read(); print('PING' if 'ping' in d else 'MISSING')"
    a = create_adapter("command", "probe", command=[sys.executable, "-c", probe])
    assert a.chat([{"role": "user", "content": "ping"}]).text.strip() == "PING"
    probe_json = "import sys, json; d = json.load(sys.stdin); print(d['messages'][-1]['content'].upper())"
    a = create_adapter("command", "probe", command=[sys.executable, "-c", probe_json], input_format="json")
    assert a.chat([{"role": "user", "content": "ping"}]).text.strip() == "PING"


def test_command_adapter_failure_is_an_adapter_error():
    a = create_adapter("command", "bad", command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(AdapterError, match="exited 3"):
        a.chat(MSGS)


def test_unreachable_server_raises_adapter_error():
    a = create_adapter("openai-compatible", "x", base_url="http://127.0.0.1:9", retries=0, timeout=2)
    with pytest.raises(AdapterError):
        a.chat(MSGS)


def test_baseline_kinds():
    assert create_adapter("oracle").kind == "reference"
    assert create_adapter("refuse-all").kind in ("baseline", "synthetic")


def test_oracle_answers_every_non_episode_task(bench):
    missing = [t.id for t in bench.tasks if not t.is_episode and not str(reference_response(t)).strip()]
    assert not missing, missing[:10]

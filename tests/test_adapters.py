"""Model adapters, exercised against an in-process mock server (no network)."""
from __future__ import annotations

import json
import sys

import pytest

from rsostb.adapters import ADAPTERS, create_adapter
from rsostb.adapters.base import AdapterError
from rsostb.adapters.baselines import reference_response
from rsostb.adapters.hypernix import KEY_ENV_VARS, same_server, waiter_server

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


# --------------------------------------------------------------------------- HyperNix T1 backends

RUNNER_ROW = {"name": "hypernix", "kind": "hypernix-runner", "reachable": True,
              "detail": "serving t1-small", "address": "http://127.0.0.1:8781", "model_id": "t1-small"}
LMSTUDIO_ROW = {"name": "lmstudio", "kind": "openai-compatible", "reachable": True, "detail": "answered"}


def test_hypernix_t1_records_which_backend_answered(mock_server):
    url, handler = mock_server
    handler.replies.update({"text": "hi", "backend_name": "hypernix"})
    g = create_adapter("hypernix-t1", "t1-small", base_url=url).chat(MSGS)
    assert g.usage["backend_name"] == "hypernix"
    assert g.usage["backend"] == "http://127.0.0.1:8781"


def test_hypernix_t1_backend_requirement_is_met(mock_server):
    url, handler = mock_server
    handler.replies.update({"backend_name": "hypernix", "backends": [LMSTUDIO_ROW, RUNNER_ROW]})
    a = create_adapter("hypernix-t1", "t1-small", base_url=url, backend="hypernix")
    assert a.chat(MSGS).usage["backend_name"] == "hypernix"
    assert "hypernix backend" in a.describe()["provider"]


def test_hypernix_t1_refuses_an_answer_from_another_backend(mock_server):
    url, handler = mock_server
    handler.replies.update({"backend_name": "lmstudio", "backends": [LMSTUDIO_ROW, RUNNER_ROW]})
    a = create_adapter("hypernix-t1", "t1-small", base_url=url, backend="hypernix")
    with pytest.raises(AdapterError, match="answered from lmstudio"):
        a.chat(MSGS)


def test_hypernix_t1_requirement_fails_closed_on_an_older_server(mock_server):
    """No /inference/backends and no backend_name: the requirement cannot
    be verified, so nothing is scored."""
    url, handler = mock_server
    a = create_adapter("hypernix-t1", "t1-small", base_url=url, backend="hypernix")
    with pytest.raises(AdapterError, match="predates backend_name"):
        a.chat(MSGS)


def test_hypernix_t1_preflight_catches_the_wrong_loaded_model(mock_server):
    url, handler = mock_server
    handler.replies.update({"backend_name": "hypernix",
                            "backends": [LMSTUDIO_ROW, {**RUNNER_ROW, "model_id": "other-model"}]})
    a = create_adapter("hypernix-t1", "t1-small", base_url=url, backend="hypernix")
    with pytest.raises(AdapterError, match="serving 'other-model'.*runner/load"):
        a.chat(MSGS)
    assert not any(r["path"].endswith("/inference/chat") for r in handler.seen)


def test_hypernix_t1_preflight_catches_an_idle_runner(mock_server):
    url, handler = mock_server
    idle = {**RUNNER_ROW, "reachable": False, "model_id": "", "detail": "nothing loaded"}
    handler.replies.update({"backends": [LMSTUDIO_ROW, idle]})
    a = create_adapter("hypernix-t1", "t1-small", base_url=url, backend="hypernix")
    with pytest.raises(AdapterError, match="not answering: nothing loaded"):
        a.chat(MSGS)


def test_hypernix_t1_rejects_an_unknown_backend_option():
    with pytest.raises(AdapterError, match="backend must be one of"):
        create_adapter("hypernix-t1", "t1-small", backend="vllm")


def _waiter_saved(tmp_path, monkeypatch, **cfg):
    """What `waiter serv -A -I <server> -K <key>` leaves behind, and no key variables."""
    path = tmp_path / "waiter.config.jsonl"
    path.write_text(json.dumps(cfg) + "\n", encoding="utf-8")
    monkeypatch.setenv("RSOSTB_WAITER_CONFIG", str(path))
    for var in ("HYPERNIX_T1_URL", *KEY_ENV_VARS):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.mark.parametrize("saved", ["url", "host and port"])
def test_hypernix_t1_uses_the_server_and_key_waiter_saved(mock_server, tmp_path, monkeypatch, saved):
    url, handler = mock_server
    port = int(url.rsplit(":", 1)[1])
    where = {"server": url} if saved == "url" else {"server": "127.0.0.1", "port": port, "local_only": True}
    _waiter_saved(tmp_path, monkeypatch, key="T1_waiter_secret", **where)
    handler.replies["require_key"] = "T1_waiter_secret"
    a = create_adapter("hypernix-t1", "t1-small")
    assert a.base_url == url
    assert a.chat(MSGS).text
    assert handler.seen[-1]["auth"] == "Bearer T1_waiter_secret"
    assert "T1_waiter_secret" not in str(a.describe()) + repr(waiter_server())


def test_hypernix_t1_key_variable_wins_over_waiter(mock_server, tmp_path, monkeypatch):
    url, handler = mock_server
    _waiter_saved(tmp_path, monkeypatch, server=url, key="T1_waiter_secret")
    monkeypatch.setenv("HYPERNIX_T1_KEY", "T1_env_secret")
    create_adapter("hypernix-t1", "t1-small").chat(MSGS)
    assert handler.seen[-1]["auth"] == "Bearer T1_env_secret"


def test_hypernix_t1_never_sends_waiters_key_to_another_server(mock_server, tmp_path, monkeypatch):
    url, handler = mock_server
    _waiter_saved(tmp_path, monkeypatch, server="http://127.0.0.1:1", key="T1_waiter_secret")
    handler.replies["require_key"] = "T1_waiter_secret"
    a = create_adapter("hypernix-t1", "t1-small", base_url=url)
    with pytest.raises(AdapterError, match=r"waiter's saved key is for http://127\.0\.0\.1:1, not"):
        a.chat(MSGS)
    assert handler.seen[-1]["auth"] is None


def test_hypernix_t1_without_a_key_says_how_to_give_one(mock_server, monkeypatch):
    url, handler = mock_server
    for var in KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    handler.replies["require_key"] = "T1_secret"
    a = create_adapter("hypernix-t1", "t1-small", base_url=url)
    with pytest.raises(AdapterError, match=r"HTTP 401.*set HYPERNIX_T1_KEY.*waiter serv -A -I <server> -K <key>"):
        a.chat(MSGS)


def test_waiter_config_that_cannot_be_read_is_ignored(tmp_path, monkeypatch):
    path = _waiter_saved(tmp_path, monkeypatch, server="http://127.0.0.1:8001", key="k")
    assert waiter_server().url == "http://127.0.0.1:8001"
    path.write_text("gAAAAAB-encrypted-elsewhere\n", encoding="utf-8")
    assert waiter_server() is None and not (tmp_path / ".master.key").exists()
    monkeypatch.setenv("RSOSTB_WAITER_CONFIG", "off")
    assert waiter_server() is None


def test_same_server():
    assert same_server("http://localhost:8001", "http://127.0.0.1:8001/")
    assert same_server("https://t1.example", "https://t1.example:443")
    assert not same_server("http://127.0.0.1:8001", "http://127.0.0.1:8000")
    assert not same_server("http://t1.example", "https://t1.example")
    assert not same_server("https://t1.example", "https://t1.example.attacker.net")


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


# --- hf-local: GGUF models run on llama.cpp ---------------------------------------------------

LFM_FILES = [".gitattributes", "LICENSE", "README.md", "LFM2.5-1.2B-Thinking-BF16.gguf",
             "LFM2.5-1.2B-Thinking-F16.gguf", "LFM2.5-1.2B-Thinking-Q4_0.gguf", "LFM2.5-1.2B-Thinking-Q4_K_M.gguf",
             "LFM2.5-1.2B-Thinking-Q5_K_M.gguf", "LFM2.5-1.2B-Thinking-Q6_K.gguf", "LFM2.5-1.2B-Thinking-Q8_0.gguf"]


@pytest.fixture()
def fake_llama(monkeypatch):
    import types

    loads = []

    class Llama:
        def __init__(self, model_path=None, **kw):
            loads.append({"model_path": model_path, **kw})

        @classmethod
        def from_pretrained(cls, repo_id, filename, **kw):
            loads.append({"repo_id": repo_id, "filename": filename, **kw})
            return cls.__new__(cls)

        def create_chat_completion(self, messages, **kw):
            loads.append({"chat": messages, **kw})
            return {"choices": [{"message": {"content": "<think>hm</think>ANSWER: 4"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 5}}

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=Llama))
    hub_files(monkeypatch, LFM_FILES)
    return loads


def hub_files(monkeypatch, files):
    """A stand-in huggingface_hub (an optional extra) that lists *files* for any repo."""
    import types

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(list_repo_files=lambda repo, revision=None: files))


def test_a_gguf_repo_runs_on_llama_cpp(fake_llama):
    a = create_adapter("hf-local", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF")
    g = a.chat(MSGS, temperature=0.0, max_tokens=32)
    assert g.text.endswith("ANSWER: 4") and g.usage == {"input_tokens": 9, "output_tokens": 5}
    assert fake_llama[0]["repo_id"] == "LiquidAI/LFM2.5-1.2B-Thinking-GGUF"
    assert fake_llama[0]["filename"] == "LFM2.5-1.2B-Thinking-Q4_K_M.gguf"      # the default quantization
    assert fake_llama[1]["max_tokens"] == 32 and fake_llama[1]["temperature"] == 0.0
    d = a.describe()
    assert d["name"] == "LiquidAI/LFM2.5-1.2B-Thinking-GGUF:LFM2.5-1.2B-Thinking-Q4_K_M.gguf"
    assert d["quantization"] == "Q4_K_M" and "llama.cpp" in d["provider"]


def test_a_named_gguf_file_is_used(fake_llama):
    create_adapter("hf-local", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF:LFM2.5-1.2B-Thinking-Q8_0.gguf").chat(MSGS)
    assert fake_llama[0]["filename"] == "LFM2.5-1.2B-Thinking-Q8_0.gguf"
    fake_llama.clear()
    a = create_adapter("hf-local", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF", gguf_file="LFM2.5-1.2B-Thinking-Q6_K.gguf")
    a.chat(MSGS)
    assert fake_llama[0]["filename"] == "LFM2.5-1.2B-Thinking-Q6_K.gguf" and a.describe()["quantization"] == "Q6_K"


def test_a_gguf_repo_without_llama_cpp_says_what_to_do(monkeypatch, tmp_path, capsys):
    from rsostb.cli.main import main

    monkeypatch.setitem(sys.modules, "llama_cpp", None)          # import fails
    hub_files(monkeypatch, LFM_FILES)
    with pytest.raises(AdapterError, match=r"RSOSTB\[gguf\].*benchmake -M"):
        create_adapter("hf-local", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF").chat(MSGS)
    assert main(["benchmark", "--adapter", "hf-local", "--model", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF",
                 "--categories", "math", "--limit-per-category", "1", "--output", str(tmp_path / "r.jsonl")]) == 3
    err = capsys.readouterr().err
    assert "is a GGUF model, which runs on llama.cpp" in err and "--base-url" not in err


def test_a_transformers_repo_is_not_treated_as_gguf(monkeypatch):
    from rsostb.adapters.hf_local import gguf_source, pick_gguf

    hub_files(monkeypatch, ["config.json", "model.safetensors", "tokenizer.json"])
    assert gguf_source("LiquidAI/LFM2.5-1.2B-Thinking") is None
    assert pick_gguf(LFM_FILES) == "LFM2.5-1.2B-Thinking-Q4_K_M.gguf"
    assert pick_gguf(["a-IQ3_XS.gguf", "mmproj-f16.gguf"]) == "a-IQ3_XS.gguf"

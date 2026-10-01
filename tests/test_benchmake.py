"""``benchmake``: one -M list, five backends.

The backends are stood in for: a small OpenAI-compatible server plays the
``cactus`` command and HyperNix's patched llama-server, fake ``hypernix``
modules play multilama / ggufrun / runtime_bridge, and the mock server plays
a T1 server. What is real is everything benchmake itself does: parsing,
choosing, starting and stopping servers, running the benchmark, the summary.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

import pytest

from rsostb import benchmake
from rsostb.benchmake import BenchMakeError, resolve, split_models
from rsostb.submission import read_results, validate_results

FAKE_SERVER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
with open(os.environ["FAKE_SERVER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_SERVER_CRASH"):
    print("fake server: could not load the model", flush=True)
    sys.exit(3)
port = int(args[args.index("--port") + 1])
# As `cactus serve`, answer only to the bundle id the model was registered as.
served = os.environ.get("FAKE_CACTUS_ID") if args[0] == "serve" else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": served or "loaded-model"}, {"id": "other-cq4"}]})
        self._send(404, {})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/v1/chat/completions":
            return self._send(404, {})
        if served and req.get("model") != served:
            return self._send(404, {"detail": f"Model {req.get('model')!r} is not available (available: {served})"})
        text = "OK " + str(req["messages"][-1]["content"])[:30]
        self._send(200, {"model": req.get("model"), "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

RUN = ["--categories", "math", "-n", "2", "--sandbox", "none", "-q"]


@pytest.fixture()
def fake_server(tmp_path, monkeypatch):
    """A `cactus`-like executable that serves OpenAI chat completions; returns its argv log."""
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    exe = tmp_path / "cactus"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_SERVER, encoding="utf-8")
    exe.chmod(0o755)
    log = tmp_path / "server-argv.log"
    monkeypatch.setenv("FAKE_SERVER_LOG", str(log))
    monkeypatch.setenv("CACTUS_BIN", str(exe))
    monkeypatch.setenv("FAKE_CACTUS_ID", "qwen3-0.6b-cq4")
    return script, log


class ChatSession:
    """Shaped like ggufrun's HnxSession: ``.chat()`` returns text."""

    closed = False

    def chat(self, messages, system=None, max_tokens=512, temperature=0.7):
        return "OK " + messages[-1]["content"][:30]

    def close(self):
        self.closed = True


class MessageSession(ChatSession):
    """Shaped like multilama's MultiLlama: ``.chat_message()`` returns the message dict."""

    def chat_message(self, messages, max_tokens=512, temperature=0.7, **kw):
        return {"role": "assistant", "content": "OK " + messages[-1]["content"][:30]}


@pytest.fixture()
def fake_hypernix(monkeypatch, fake_server):
    """Fake hypernix modules; returns a record of what benchmake asked them for."""
    script, _ = fake_server
    calls: dict[str, list] = {"multilama": [], "ggufrun": [], "serve": [], "sessions": []}

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    def load(repo, filename, backend="vanilla", **kw):
        calls["multilama"].append((repo, filename, backend))
        calls["sessions"].append(MessageSession())
        return calls["sessions"][-1]

    def load_gguf(path, backend="vanilla", **kw):
        calls["ggufrun"].append((Path(path).name, backend))
        calls["sessions"].append(ChatSession())
        return calls["sessions"][-1]

    build = types.SimpleNamespace(patched=True, server=Path("/opt/hnx/llama-server"), bin_dir=Path("/opt/hnx"),
                                  missing=lambda t: [])

    def serve_argv(build, model, *, host="127.0.0.1", port=8080, gpu_layers=0, context=0, alias=""):
        calls["serve"].append((Path(model).name, alias))
        return [sys.executable, str(script), "-m", str(model), "--host", host, "--port", str(port)]

    hx = module("hypernix")
    hx.models = module("hypernix.models")
    hx.models.multilama = module("hypernix.models.multilama", load=load,
                                 auto_select_backend=lambda repo, f: "ik" if "iqk" in f.lower() else "vanilla")
    hx.models.ggufrun = module("hypernix.models.ggufrun", load_gguf=load_gguf)
    hx.quant = module("hypernix.quant")
    hx.quant.runtime_bridge = module(
        "hypernix.quant.runtime_bridge", HNX_FIRST_TYPE=200, find_build=lambda need=frozenset(): build,
        serve_argv=serve_argv,
        model_types=lambda p: frozenset({12, 210}) if "hnx" in Path(p).name.lower() else frozenset({12}))
    return calls


def gguf(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"GGUF" + b"\0" * 60)
    return p


def port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------- parsing and choosing

def test_models_split_on_commas_and_spaces_not_underscores():
    assert split_models(["model1,model2,model3 model4_model5"]) == ["model1", "model2", "model3", "model4_model5"]
    assert split_models(["a, b\tc", "b,d"]) == ["a", "b", "c", "d"]


def test_resolve_picks_a_backend_from_the_name(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmake, "hnx_types", lambda p: frozenset())
    local = gguf(tmp_path, "my_model-Q4_K_M.gguf")
    assert (resolve(str(local)).backend, resolve(str(local)).name) == ("gguf", "my_model-Q4_K_M.gguf")
    for ref in ("Qwen/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf", "Qwen/Qwen3-4B-GGUF/Qwen3-4B-Q4_K_M.gguf"):
        spec = resolve(ref)
        assert (spec.backend, spec.target) == ("multilama", "Qwen/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf")
    assert resolve("Cactus-Compute/Qwen3-0.6B").backend == "cactus"
    assert resolve("model4_model5").backend == "t1"
    assert resolve("qwen/qwen3-4b").backend == "t1"   # LM Studio ids behind T1 look like this
    with pytest.raises(BenchMakeError, match="no such file"):
        resolve(str(tmp_path / "missing.gguf"))
    (tmp_path / "notes.txt").write_text("hi")
    with pytest.raises(BenchMakeError, match="not a GGUF"):
        resolve(str(tmp_path / "notes.txt"))


def test_resolve_prefixes(tmp_path):
    assert resolve("cactus:needle").backend == "cactus"
    spec = resolve("multilama@ik:org/repo:model-IQK.gguf")
    assert (spec.backend, spec.variant, spec.target) == ("multilama", "ik", "org/repo:model-IQK.gguf")
    spec = resolve("t1@hypernix:qwen3-4b")
    assert (spec.backend, spec.variant, spec.target) == ("t1", "hypernix", "qwen3-4b")
    spec = resolve("hnx_llama:~/models/x.gguf")
    assert spec.backend == "hnx_llama" and spec.target == str(Path("~/models/x.gguf").expanduser())
    assert resolve("hnx-llama:/m.gguf").backend == "hnx_llama"
    assert resolve("mystery:thing").backend == "t1"   # not a backend name: the whole thing is a model name


def test_hnx_gguf_goes_to_the_patched_server_when_there_is_one(tmp_path, monkeypatch):
    path = gguf(tmp_path, "HyperNix.3-mini.gguf")
    monkeypatch.setattr(benchmake, "hnx_types", lambda p: frozenset({210}))
    monkeypatch.setattr(benchmake, "patched_build", lambda t: object())
    assert resolve(str(path)).backend == "hnx_llama"
    monkeypatch.setattr(benchmake, "patched_build", lambda t: None)
    spec = resolve(str(path))
    assert spec.backend == "gguf" and "HyperNix's own runtime" in spec.why


def test_dry_run_runs_nothing(tmp_path, capsys):
    assert benchmake.main(["-M", "model1,Cactus-Compute/needle", "--dry-run", "-o", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "cactus" in out and "t1" in out and not (tmp_path / "out").exists()


def test_unresolvable_models_stop_before_anything_runs(tmp_path, capsys):
    assert benchmake.main(["-M", "fine,missing-file.gguf", "-o", str(tmp_path / "out")]) == 2
    assert "missing-file.gguf" in capsys.readouterr().err and not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- running

def test_all_five_backends_in_one_list(tmp_path, mock_server, fake_hypernix, fake_server, capsys):
    url, _ = mock_server
    plain = gguf(tmp_path, "my_model-Q4_K_M.gguf")
    hnx = gguf(tmp_path, "hnx_tiny-IQ0.5_XXXL.gguf")
    models = (f"model4_model5,Qwen/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf {plain},"
              f"Cactus-Compute/Qwen3-0.6B {hnx}")
    out = tmp_path / "out"
    assert benchmake.main(["-M", models, "-o", str(out), "--t1-url", url, *RUN]) == 0

    summary = json.loads((out / "summary.json").read_text())
    assert [(s["backend"], s["ok"]) for s in summary] == [
        ("t1", True), ("multilama", True), ("gguf", True), ("cactus", True), ("hnx_llama", True)]
    for s in summary:
        doc = read_results(s["results"])
        assert validate_results(doc).ok, s["model"]
        assert doc["model"]["name"] == s["name"]
        assert str(tmp_path) not in json.dumps(doc["model"])   # a local file is named, its path is not
    # in-process models run one request at a time
    assert read_results(summary[1]["results"])["run"]["parameters"]["max_workers"] == 1
    assert fake_hypernix["multilama"] == [("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf", "vanilla")]
    assert fake_hypernix["ggufrun"] == [("my_model-Q4_K_M.gguf", "vanilla")]
    assert fake_hypernix["serve"] == [("hnx_tiny-IQ0.5_XXXL.gguf", "hnx_tiny-IQ0.5_XXXL.gguf")]
    assert all(s.closed for s in fake_hypernix["sessions"])
    # cactus ran on-device only, and every started server was stopped
    argvs = [json.loads(line) for line in fake_server[1].read_text().splitlines()]
    cactus = next(a for a in argvs if a[0] == "serve")
    assert cactus[1] == "Cactus-Compute/Qwen3-0.6B" and {"--no-cloud-handoff", "--no-cloud-tele"} <= set(cactus)
    assert summary[3]["name"] == "Cactus-Compute/Qwen3-0.6B"   # requests used the bundle id, results the model
    for a in argvs:
        assert not port_open(int(a[a.index("--port") + 1]))
    assert "| 1 |" in (out / "summary.md").read_text()


def test_a_failing_model_is_skipped_and_the_rest_still_run(tmp_path, mock_server, fake_server, monkeypatch, capsys):
    url, handler = mock_server
    monkeypatch.setenv("FAKE_SERVER_CRASH", "1")
    out = tmp_path / "out"
    assert benchmake.main(["-M", "Cactus-Compute/broken model4_model5", "-o", str(out), "--t1-url", url, *RUN]) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert [(s["backend"], s["ok"]) for s in summary] == [("cactus", False), ("t1", True)]
    assert "exited with code 3" in summary[0]["error"] and "could not load the model" in summary[0]["error"]


def test_a_model_that_does_not_answer_is_skipped(tmp_path, mock_server, capsys):
    url, handler = mock_server
    handler.replies["substituted"] = True   # T1 answered with a different model
    out = tmp_path / "out"
    assert benchmake.main(["-M", "model4_model5", "-o", str(out), "--t1-url", url, *RUN]) == 1
    (row,) = json.loads((out / "summary.json").read_text())
    assert not row["ok"] and "did not answer a ping" in row["error"] and "substituted" in row["error"]


def test_cactus_bits(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_CACTUS_ID", "gemma-4-e2b-it-cq2")
    out = tmp_path / "out"
    assert benchmake.main(["-M", "cactus@2:google/gemma-4-E2B-it", "-o", str(out), *RUN]) == 0
    (row,) = json.loads((out / "summary.json").read_text())
    assert read_results(row["results"])["model"]["quantization"] == "CQ2"
    argv = json.loads(fake_server[1].read_text().splitlines()[0])
    assert argv[argv.index("--bits") + 1] == "2"
    assert benchmake.main(["-M", "cactus@5:google/gemma-4-E2B-it", "-o", str(out), *RUN]) == 1


def test_missing_cactus_is_explained(tmp_path, monkeypatch):
    monkeypatch.delenv("CACTUS_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    out = tmp_path / "out"
    assert benchmake.main(["-M", "Cactus-Compute/needle", "-o", str(out), *RUN]) == 1
    (row,) = json.loads((out / "summary.json").read_text())
    assert "pip install cactus-compute" in row["error"]

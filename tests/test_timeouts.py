"""A slow reply must cost one task, not stall the run and fail the tasks after it.

Most local servers keep writing a reply after the client gave up and answer
one request at a time. Retrying a timed-out request queued another copy
behind it, and every following task waited behind both and timed out too:
the run "hung" for a long while and then showed a string of E rows.
"""
from __future__ import annotations

import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rsostb.adapters.base import AdapterError, Generation, ModelAdapter, RequestTimeout, http_json
from rsostb.runner import runner as runner_mod
from rsostb.runner.runner import PREFLIGHT_MESSAGES, RunAborted, print_progress, run_benchmark
from rsostb.submission import validate_results


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)


@pytest.fixture()
def server():
    """A server whose next replies are scripted: ("sleep", s) or (status, body)."""
    class H(BaseHTTPRequestHandler):
        script: list = []
        hits = 0

        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            type(self).hits += 1
            kind, arg = type(self).script.pop(0) if type(self).script else (200, "{}")
            if kind == "sleep":
                threading.Event().wait(arg)  # not time.sleep: the autouse fixture stubs it out
                kind, arg = 200, "{}"
            body = arg.encode()
            self.send_response(kind)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/x", H
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_timeout_is_not_retried(server):
    url, h = server
    h.script = [("sleep", 1.5)] * 3
    with pytest.raises(RequestTimeout, match="no reply .* within 0.3s"):
        http_json(url, {}, {}, timeout=0.3, retries=2, backoff=0.0)
    assert h.hits == 1


@pytest.mark.parametrize("status,body", [
    (504, '{"error": "gateway timeout"}'),
    (503, '{"error": {"code": "MODEL_UNAVAILABLE", "message": "LM Studio at http://x did not answer within 300s"}}'),
])
def test_a_gateway_that_gave_up_on_the_model_is_a_timeout(server, status, body):
    url, h = server
    h.script = [(status, body)] * 3
    with pytest.raises(RequestTimeout):
        http_json(url, {}, {}, timeout=5, retries=2, backoff=0.0)
    assert h.hits == 1


def test_other_server_errors_are_still_retried(server):
    url, h = server
    h.script = [(503, '{"error": "loading model"}'), (503, '{"error": "loading model"}'), (200, '{"ok": 1}')]
    assert http_json(url, {}, {}, timeout=5, retries=2, backoff=0.0) == {"ok": 1}
    assert h.hits == 3


class BusyServer(ModelAdapter):
    """Like a one-slot local server: chosen tasks run past the timeout, and the
    server then stays busy with them for ``busy_for`` more requests."""

    name = "busy"

    def __init__(self, slow: int = 1, busy_for: int = 2, probes_fail: bool = False, every_task_slow: bool = False):
        super().__init__("busy")
        self.slow, self.busy_for, self.probes_fail, self.every = slow, busy_for, probes_fail, every_task_slow
        self.busy = 0
        self.task_calls: dict[str, int] = {}
        self.probes = 0

    def chat(self, messages, **kw):
        if messages == PREFLIGHT_MESSAGES:
            self.probes += 1
            if self.probes_fail or self.busy > 0:
                self.busy -= 1
                raise RequestTimeout("no reply from http://model within 1s")
            return Generation(text="OK")
        task = kw.get("task")
        tid = task.id if task is not None else "?"
        self.task_calls[tid] = self.task_calls.get(tid, 0) + 1
        if self.busy > 0:
            raise AssertionError(f"{tid} was sent while the server was still busy")
        if self.every or len(self.task_calls) <= self.slow:
            self.busy = self.busy_for
            raise RequestTimeout("no reply from http://model within 1s")
        return Generation(text="ANSWER: 0")


def test_a_timed_out_task_costs_only_itself(capsys):
    a = BusyServer(slow=1, busy_for=2)
    doc = run_benchmark(a, categories=["math"], limit_per_category=4, check_model=False, progress=print_progress)
    status = {t["task_id"]: t["status"] for t in doc["task_results"]}
    slow = next(iter(a.task_calls))
    assert status[slow] == "timeout" and a.task_calls[slow] == 1          # recorded once, never resent
    assert all(s != "timeout" for tid, s in status.items() if tid != slow)  # the next task did not queue behind it
    assert a.probes == 3                                                  # waited until the server answered
    err = capsys.readouterr().err
    assert "no reply from http://model within 1s" in err and "checking that the model server is free" in err
    rep = validate_results(doc, rescore=True)  # a timed-out task has no reply to re-grade
    assert rep.ok, rep.errors


def test_a_server_that_never_answers_again_stops_the_run(monkeypatch):
    real = runner_mod.load_runner_config
    monkeypatch.setattr(runner_mod, "load_runner_config", lambda: {**real(), "wait_after_timeout_seconds": 0.05})
    a = BusyServer(slow=1, probes_fail=True)
    with pytest.raises(RunAborted, match="has not answered anything for 0.05s since .* timed out.*--checkpoint"):
        run_benchmark(a, categories=["math"], limit_per_category=4, check_model=False)
    assert sum(a.task_calls.values()) == 1


def test_a_model_too_slow_for_the_timeout_is_stopped_with_a_hint():
    a = BusyServer(every_task_slow=True, busy_for=0)
    with pytest.raises(RunAborted, match="10 tasks in a row.*raise --request-timeout"):
        run_benchmark(a, categories=["math", "physics"], limit_per_category=8, check_model=False)
    assert set(a.task_calls.values()) == {1}


def test_an_episode_cut_short_by_a_timeout_is_a_timeout(bench):
    episodes = [t.id for t in bench.tasks if t.is_episode][:2]
    a = BusyServer(slow=1, busy_for=0)
    doc = run_benchmark(a, task_ids=episodes, check_model=False)
    timed = [t for t in doc["task_results"] if t["status"] == "timeout"]
    assert len(timed) == 1 and timed[0]["credit"] == 0 and timed[0]["usage"]["timed_out"] is True
    assert validate_results(doc, rescore=True, rescore_execution=True).ok


def test_progress_says_why_a_task_failed(capsys):
    print_progress(3, 10, {"task_id": "math-001", "status": "error", "credit": 0.0, "error": "HTTP 500 from x:\n boom"})
    print_progress(4, 10, {"task_id": "math-002", "status": "scored", "credit": 1.0, "error": None})
    err = capsys.readouterr().err
    assert "math-001" in err and "\n    HTTP 500 from x: boom\n" in err
    assert err.count("\n    ") == 1


def test_t1_timeouts_are_timeouts():
    from rsostb.adapters.hypernix import HyperNixT1Adapter

    a = HyperNixT1Adapter("m", base_url="http://127.0.0.1:1", use_sdk=False)
    for text in ("Could not reach http://127.0.0.1:8001/inference/chat: timed out",
                 "LM Studio at http://127.0.0.1:1234 did not answer within 300s [MODEL_UNAVAILABLE]"):
        assert isinstance(a._failed("/inference/chat", Exception(text)), RequestTimeout)
    other = a._failed("/inference/chat", Exception("Model 'm' allowance is fully exhausted [QUOTA_EXCEEDED]"))
    assert isinstance(other, AdapterError) and not isinstance(other, RequestTimeout)


def test_request_timeout_reaches_the_adapter():
    from rsostb.cli.main import _make_adapter

    def args(**kw):
        base = {"adapter": "openai-compatible", "model": "m", "adapter_config": None, "adapter_option": [],
                "base_url": "http://127.0.0.1:1/v1", "api_key_env": None, "request_timeout": None}
        return argparse.Namespace(**{**base, **kw})

    assert _make_adapter(args()).timeout == 300.0                   # runner.yaml request_timeout_seconds
    assert _make_adapter(args(request_timeout=42)).timeout == 42.0
    assert _make_adapter(args(adapter_option=["timeout=7"], request_timeout=42)).timeout == 7.0


class Writer(ModelAdapter):
    """Answers in ``delay`` seconds with ``tokens`` output tokens; tasks listed
    in ``slow`` time out; probes (the test request) take ``probe_delay``."""

    name = "writer"

    def __init__(self, delay=0.0, tokens=100, slow=(), probe_delay=0.0, timeout=60.0, health="the server answers"):
        super().__init__("writer")
        self.delay, self.tokens, self.slow, self.probe_delay = delay, tokens, set(slow), probe_delay
        self.timeout, self._health = timeout, health

    def health(self):
        return self._health

    def chat(self, messages, **kw):
        if messages == PREFLIGHT_MESSAGES:
            threading.Event().wait(self.probe_delay)
            return Generation(text="OK")
        task = kw.get("task")
        if task is not None and task.id in self.slow:
            raise RequestTimeout(f"no reply from http://model within {self.timeout:g}s")
        threading.Event().wait(self.delay)
        return Generation(text="ANSWER: 0", usage={"output_tokens": self.tokens})


@pytest.fixture()
def quick_reports(monkeypatch):
    monkeypatch.setattr(runner_mod, "WAIT_REPORT_AFTER", 0.05)
    monkeypatch.setattr(runner_mod, "WAIT_REPORT_EVERY", 0.05)
    monkeypatch.setattr(runner_mod, "WAIT_DIAGNOSE_AFTER", 0.1)


def test_a_slow_reply_says_what_the_run_is_waiting_for(capsys, quick_reports):
    a = Writer(delay=0.4, health="T1 at http://t1 answers, so the model server behind it is slow or busy")
    run_benchmark(a, categories=["math"], limit_per_category=1, progress=print_progress)
    err = capsys.readouterr().err
    assert "checking that the model answers" in err
    assert "still waiting for math-" in err and "(a request gives up after 60s)" in err
    assert "T1 at http://t1 answers, so the model server behind it is slow or busy" in err
    assert err.count("model server behind it") == 1                     # asked once per wait, not every line


def test_a_test_request_that_never_comes_back_stops_the_run_quickly(monkeypatch):
    real = runner_mod.load_runner_config
    monkeypatch.setattr(runner_mod, "load_runner_config", lambda: {**real(), "preflight_timeout_seconds": 0.2})
    a = Writer(probe_delay=5, health="T1 at http://t1 is not answering at all (no answer within 5s)")
    t0 = runner_mod.time.monotonic()
    with pytest.raises(RunAborted, match=r"within 0.2s, so nothing was run \(T1 at http://t1 is not answering at all"):
        run_benchmark(a, categories=["math"], limit_per_category=1)
    assert runner_mod.time.monotonic() - t0 < 3


def test_a_timeout_is_explained_by_how_fast_the_model_writes(capsys, bench):
    # 100 tokens in ~0.1 s is ~1000 tokens/s: a 2048-token reply needs ~2 s, past a 1 s timeout.
    order = run_benchmark(Writer(), categories=["math"], limit_per_category=6, check_model=False)["task_results"]
    slow = sorted(t["task_id"] for t in order)[-1]
    capsys.readouterr()
    a = Writer(delay=0.1, tokens=100, slow=[slow], timeout=1.0)
    run_benchmark(a, categories=["math"], limit_per_category=6, check_model=False, progress=print_progress,
                  generation={"max_tokens": 2048})
    err = capsys.readouterr().err
    assert "tokens/s, so one that runs to max_tokens (2048) takes about" in err
    assert "longer than the 1s request timeout" in err and "--request-timeout" in err and "--max-tokens" in err


def test_speed_hint_when_the_timeout_is_not_the_problem():
    from rsostb.runner.runner import speed_hint

    assert speed_hint(50, 1.0, 2048, 300) is None                    # too little to go on
    hint = speed_hint(4000, 100.0, 2048, 300)                        # 40 tokens/s: 2048 in ~51 s
    assert "within the 300s request timeout" in hint and "held up by something else" in hint
    hint = speed_hint(1000, 200.0, 2048, 300)                        # 5 tokens/s: 2048 in ~410 s
    assert "--request-timeout 540" in hint and "--max-tokens 1024" in hint


def test_t1_health_says_which_part_is_not_answering(mock_server):
    from rsostb.adapters.hypernix import HyperNixT1Adapter

    url, handler = mock_server
    handler.replies["backends"] = [
        {"name": "hypernix", "reachable": False, "model_id": "Qwen3-0.6B", "detail": "did not answer within 10s"},
        {"name": "lmstudio", "reachable": False, "detail": "LM Studio is off"}]
    h = HyperNixT1Adapter("Qwen3-0.6B", base_url=url, use_sdk=False).health()
    assert h.startswith(f"T1 at {url} answers")
    assert "hypernix: not answering, Qwen3-0.6B (did not answer within 10s)" in h
    gone = HyperNixT1Adapter("m", base_url="http://127.0.0.1:1", use_sdk=False).health()
    assert gone.startswith("T1 at http://127.0.0.1:1 is not answering at all")


def test_the_speed_is_explained_at_the_first_timeout_with_enough_to_go_on(capsys):
    run_benchmark(Writer(), categories=["math"], limit_per_category=8, check_model=False, progress=print_progress)
    ran = [line.split("]")[1].split()[0] for line in capsys.readouterr().err.replace("\r", "\n").splitlines()
           if line.startswith("[")]
    # The first timeout comes after one 100-token reply (too little to judge), the second after five.
    a = Writer(delay=0.1, tokens=100, slow=[ran[1], ran[6]], timeout=1.0)
    run_benchmark(a, categories=["math"], limit_per_category=8, check_model=False, progress=print_progress,
                  generation={"max_tokens": 2048})
    err = capsys.readouterr().err
    assert err.count("tokens/s, so one that runs to max_tokens (2048)") == 1
    assert err.index("tokens/s") > err.index(ran[6])


# --- hypernix-t1: streamed replies and restarting a stuck runner -------------------------------

MSGS = [{"role": "user", "content": "ping"}]

def t1(url, **kw):
    from rsostb.adapters.hypernix import HyperNixT1Adapter

    return HyperNixT1Adapter("lfm2-5-350m-q8-0", base_url=url, use_sdk=False, **kw)


def test_t1_streams_the_reply(mock_server):
    url, handler = mock_server
    handler.replies.update({"stream": True, "text": "the answer is 42"})
    a = t1(url)
    g = a.chat(MSGS, max_tokens=64)
    assert g.text == "the answer is 42" and g.finish_reason == "stop"
    assert g.usage["backend_name"] == "hypernix" and g.usage["output_tokens"] == 6   # pieces stand in for usage
    req = handler.seen[-1]
    assert req["path"].endswith("/inference/chat/stream") and req["body"]["allow_fallback"] is False


def test_t1_stream_refusals_and_errors(mock_server):
    url, handler = mock_server
    handler.replies.update({"stream": True, "substituted": True, "served_model": "other"})
    with pytest.raises(AdapterError, match="substituted"):
        t1(url).chat(MSGS)
    handler.replies.update({"substituted": False, "stream_error": {"code": "timeout",
                            "message": "the runner did not answer within 300s"}})
    with pytest.raises(RequestTimeout, match="failed mid-reply"):
        t1(url).chat(MSGS)
    handler.replies["stream_error"] = {"code": "http_error", "message": "model crashed"}
    with pytest.raises(AdapterError, match="model crashed") as err:
        t1(url).chat(MSGS)
    assert not isinstance(err.value, RequestTimeout)


def test_t1_stops_reading_a_reply_past_the_timeout(mock_server):
    url, handler = mock_server
    handler.replies.update({"stream": True, "text": "x" * 300, "stream_delay": 0.05})
    t0 = runner_mod.time.monotonic()
    with pytest.raises(RequestTimeout, match="stopped it after"):
        t1(url, timeout=0.5).chat(MSGS)
    assert runner_mod.time.monotonic() - t0 < 2
    threading.Event().wait(0.3)
    assert any(r.get("client_gone") for r in handler.seen)       # the connection was really closed


def test_t1_falls_back_to_the_plain_endpoint_on_an_older_server(mock_server):
    url, handler = mock_server          # this mock has no stream endpoint unless asked
    a = t1(url)
    assert a.chat(MSGS).text and a.stream is False
    assert handler.seen[-1]["path"].endswith("/inference/chat")


def test_t1_restarts_its_runner_when_it_serves_this_model(mock_server):
    url, handler = mock_server
    handler.replies["runner"] = {"loaded": True, "base_url": "http://127.0.0.1:8781", "model": {
        "model_id": "lfm2-5-350m-q8-0", "context_length": 8192,
        "placement": {"gpu_layers": 20, "backend": "cuda", "explicit": True}}}
    did = t1(url).recover()
    assert did.startswith("restarted T1's HyperNix runner for lfm2-5-350m-q8-0")
    unload, load = [r for r in handler.seen if r["path"].startswith("/runner/")][-2:]
    assert unload["path"] == "/runner/unload" and load["path"] == "/runner/load"
    assert load["body"] == {"model_id": "lfm2-5-350m-q8-0", "gpu_layers": 20, "backend": "cuda", "context_length": 8192}
    handler.replies["runner"]["model"]["model_id"] = "someone-elses-model"
    assert t1(url).recover() is None                             # not ours to restart
    assert t1(url, restart_runner="false").recover() is None


def test_a_stuck_server_is_restarted_and_the_run_goes_on(monkeypatch, capsys):
    real = runner_mod.load_runner_config
    monkeypatch.setattr(runner_mod, "load_runner_config", lambda: {**real(), "preflight_timeout_seconds": 0.2})

    class Stuck(Writer):
        def recover(self):
            self.probe_delay = 0.0
            return "restarted the model server"

    a = Stuck(probe_delay=5)
    doc = run_benchmark(a, categories=["math"], limit_per_category=2, progress=print_progress)
    assert len(doc["task_results"]) == 2
    assert "restarted the model server; trying the test request again" in capsys.readouterr().err

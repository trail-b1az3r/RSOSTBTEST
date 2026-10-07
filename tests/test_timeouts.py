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
    assert "no reply from http://model within 1s" in err and "waiting for the model server" in err
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

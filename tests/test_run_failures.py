"""A model that cannot be reached must never look like a model that scored zero."""
from __future__ import annotations

import pytest

from rsostb.adapters.base import AdapterError, Generation, ModelAdapter
from rsostb.cli.main import main
from rsostb.runner import runner as runner_mod
from rsostb.runner.runner import RunAborted, run_benchmark


class Flaky(ModelAdapter):
    """Fails for the first ``fail`` calls, then answers."""
    name = "flaky"

    def __init__(self, fail: int):
        super().__init__("flaky")
        self.fail, self.calls = fail, 0

    def chat(self, messages, **kw):
        self.calls += 1
        if self.calls <= self.fail:
            raise AdapterError("HTTP 503: model not loaded")
        return Generation(text="ANSWER: 0")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)


def test_unreachable_model_stops_before_the_run(tmp_path, capsys):
    out = tmp_path / "r.jsonl"
    code = main(["benchmark", "--adapter", "openai-compatible", "--model", "x", "--base-url", "http://127.0.0.1:9/v1",
                 "--categories", "math", "--limit-per-category", "2", "--output", str(out), "--quiet"])
    assert code == 3 and not out.exists()
    assert "did not answer a test request" in capsys.readouterr().err


def test_a_run_that_stops_answering_is_aborted_not_scored():
    adapter = Flaky(fail=10_000)
    with pytest.raises(RunAborted, match="10 tasks in a row got no reply.*model not loaded"):
        run_benchmark(adapter, categories=["math"], sandbox="none", check_model=False)
    assert adapter.calls == 10 * 3  # ten tasks, three attempts each, then stop


def test_isolated_failures_do_not_abort():
    adapter = Flaky(fail=3 * 5)  # five tasks fail (three attempts each), then it recovers
    doc = run_benchmark(adapter, categories=["math"], sandbox="none", check_model=False)
    failed = [t for t in doc["task_results"] if t.get("error")]
    assert len(failed) == 5 and len(doc["task_results"]) > 5


def test_empty_replies_are_called_out(tmp_path, mock_server, capsys):
    url, handler = mock_server
    handler.replies["text"] = ""
    code = main(["benchmark", "--adapter", "openai-compatible", "--model", "m", "--base-url", f"{url}/v1",
                 "--categories", "math", "--limit-per-category", "2", "--output", str(tmp_path / "r.jsonl"),
                 "--quiet", "--no-model-check"])
    err = capsys.readouterr().err
    assert code == 3 and "replies were empty" in err and "answered no task" in err


def test_a_working_model_still_exits_zero(tmp_path, mock_server):
    url, _ = mock_server
    assert main(["benchmark", "--adapter", "openai-compatible", "--model", "m", "--base-url", f"{url}/v1",
                 "--categories", "math", "--limit-per-category", "2", "--output", str(tmp_path / "r.jsonl"),
                 "--quiet"]) == 0

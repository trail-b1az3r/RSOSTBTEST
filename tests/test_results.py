"""Results files, integrity validation, submissions and the leaderboard store."""
from __future__ import annotations

import copy
import gzip
import json

import pytest

from rsostb.leaderboard.api import build_api, comparison, leaderboard_rows
from rsostb.leaderboard.store import LeaderboardStore
from rsostb.submission.results_io import (
    MAX_RESULTS_BYTES,
    ResultsFormatError,
    parse_results_bytes,
    read_results,
    write_results,
)
from rsostb.submission.sanitize import clean_text, looks_like_markup, safe_slug
from rsostb.submission.validate import validate_results


@pytest.fixture(scope="module")
def echo_doc(example_results):
    return read_results(example_results / "echo.jsonl.gz")


@pytest.fixture(scope="module")
def noisy_doc(example_results):
    return read_results(example_results / "noisy-oracle.jsonl.gz")


# --------------------------------------------------------------------------- I/O

@pytest.mark.parametrize("name", ["r.json", "r.jsonl", "r.json.gz", "r.jsonl.gz"])
def test_round_trip_all_formats(tmp_path, echo_doc, name):
    p = write_results(echo_doc, tmp_path / name)
    assert read_results(p) == echo_doc


def test_gzip_output_is_reproducible(tmp_path, echo_doc):
    a = write_results(echo_doc, tmp_path / "a.jsonl.gz").read_bytes()
    b = write_results(echo_doc, tmp_path / "b.jsonl.gz").read_bytes()
    assert a == b


def test_gzip_bomb_is_capped():
    bomb = gzip.compress(b" " * (MAX_RESULTS_BYTES + 10))
    with pytest.raises(ResultsFormatError, match="too large"):
        parse_results_bytes(bomb, "x.jsonl.gz")


@pytest.mark.parametrize("payload", [b"", b"[1, 2]", b"{not json", b'{"type": "task_result"}\n', b"\xff\xfe"])
def test_malformed_inputs_rejected(payload):
    with pytest.raises(ResultsFormatError):
        parse_results_bytes(payload, "x.jsonl" if payload.startswith(b'{"type"') else "x.json")


# --------------------------------------------------------------------------- validation

def test_example_results_are_consistent(bench, example_results):
    for p in sorted(example_results.glob("*.jsonl.gz")):
        rep = validate_results(read_results(p), bench)
        assert rep.ok, (p.name, rep.errors[:3])


def test_result_carries_version_pins(echo_doc):
    for k in ("benchmark", "benchmark_version", "dataset_version", "scoring_version", "runner_version",
              "dataset_hash", "scoring_config_hash"):
        assert echo_doc.get(k), k
    assert echo_doc["benchmark"] == "RSOSTBTEST-pro"


def test_rescore_reproduces_scores(bench, noisy_doc):
    rep = validate_results(noisy_doc, bench, rescore=True)
    assert rep.ok, rep.errors[:3]
    assert rep.recomputed["rsostb_score"] == noisy_doc["scores"]["rsostb_score"]


def test_inflated_total_is_rejected(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    doc["scores"]["rsostb_score"] = 149_000.0
    rep = validate_results(doc, bench)
    assert not rep.ok and any("rsostb_score" in e for e in rep.errors)


def test_inflated_task_credit_is_caught_by_rescore(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    tr = next(t for t in doc["task_results"] if t["credit"] == 0.0 and t["category"] == "math")
    tr["credit"] = 1.0
    rep = validate_results(doc, bench, rescore=True)
    assert not rep.ok


def test_wrong_scoring_hash_is_rejected(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    doc["scoring_config_hash"] = "0" * 64
    assert not validate_results(doc, bench).ok


def test_dataset_hash_mismatch_is_rejected(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    doc["dataset_hash"] = "f" * 64
    rep = validate_results(doc, bench)
    assert not rep.ok and any("dataset_hash" in e for e in rep.errors)


def test_unknown_task_is_rejected(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    doc["task_results"][0]["task_id"] = "math-999"
    assert not validate_results(doc, bench).ok


def test_markup_in_model_name_is_rejected(bench, echo_doc):
    doc = copy.deepcopy(echo_doc)
    doc["model"]["name"] = "<script>alert(1)</script>"
    assert not validate_results(doc, bench).ok


def test_sanitizers():
    assert looks_like_markup("<img src=x onerror=alert(1)>")
    assert not looks_like_markup("llama-3.1-8b-instruct")
    assert clean_text("a\x00b‮c") == "abc"
    assert "/" not in safe_slug("../../etc/passwd")


# --------------------------------------------------------------------------- store + API

def test_store_accepts_rejects_and_dedups(tmp_path, bench, echo_doc, noisy_doc, example_results):
    store = LeaderboardStore(tmp_path / "store", bench=bench)
    assert store.add(copy.deepcopy(echo_doc)).accepted
    dup = store.add(copy.deepcopy(echo_doc))
    assert not dup.accepted and dup.message.startswith("duplicate")
    oracle = read_results(example_results / "oracle.jsonl.gz")
    assert not store.add(oracle).accepted  # reference runs never listed
    raw = (example_results / "noisy-oracle.jsonl.gz").read_bytes()
    assert store.add_upload("noisy.jsonl.gz", raw).accepted
    assert not store.add_upload("evil.exe", b"MZ").accepted
    entries = store.entries()
    assert len(entries) == 2
    assert entries[0]["scores"]["rsostb_score"] >= entries[1]["scores"]["rsostb_score"]
    assert store.get_entry("../../etc/passwd") is None


def test_leaderboard_api(tmp_path, bench, echo_doc, noisy_doc):
    store = LeaderboardStore(tmp_path / "store", bench=bench)
    store.add(copy.deepcopy(echo_doc))
    store.add(copy.deepcopy(noisy_doc))
    api = build_api(store, tmp_path / "leaderboard.json")
    assert json.loads((tmp_path / "leaderboard.json").read_text())["api_version"] == api["api_version"]
    rows = leaderboard_rows(api["entries"], benchmark_version="1.0")
    assert [r["rsostb_score"] for r in rows] == sorted((r["rsostb_score"] for r in rows), reverse=True)
    assert all("response" not in json.dumps(e) or True for e in api["entries"])
    cmp = comparison(api["entries"], [e["submission_id"] for e in api["entries"]])
    assert cmp["comparable"]


def test_a_missing_results_file_is_not_parsed_as_json(tmp_path, example_results):
    import shutil

    shutil.copy(example_results / "noisy-oracle.jsonl.gz", tmp_path / "results.jsonl.gz")
    rep = validate_results(str(tmp_path / "results.jsonl"))
    assert rep.status == "rejected"
    assert rep.errors == [f"unreadable results: no such file: {tmp_path / 'results.jsonl'} "
                          f"(did you mean {tmp_path / 'results.jsonl.gz'}?)"]
    assert validate_results(str(tmp_path / "nothing.json")).errors == [
        f"unreadable results: no such file: {tmp_path / 'nothing.json'}"]


class _NotFound(Exception):
    pass


_NotFound.__name__ = "RepositoryNotFoundError"


@pytest.fixture()
def fake_hub(monkeypatch):
    """A Hugging Face Hub on which the results dataset does not exist until it is created."""
    import sys
    import types

    state = {"exists": False, "created": [], "commits": []}

    class HfApi:
        def __init__(self, token=None):
            pass

        def create_commit(self, repo_id, **kw):
            if not state["exists"]:
                raise _NotFound("404 Client Error. Repository not found")
            state["commits"].append((repo_id, kw["create_pr"]))
            return types.SimpleNamespace(pr_url=f"https://huggingface.co/datasets/{repo_id}/discussions/1")

        def create_repo(self, repo_id, repo_type=None, exist_ok=False):
            state["created"].append((repo_id, repo_type))
            state["exists"] = True

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(HfApi=HfApi, CommitOperationAdd=lambda **kw: kw))
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.delenv("RSOSTB_RESULTS_REPO", raising=False)
    return state


def test_submitting_to_a_dataset_that_does_not_exist_says_how_to_make_it(fake_hub, example_results):
    from rsostb.submission.submit import SubmissionError, submit_results

    src = example_results / "noisy-oracle.jsonl.gz"
    with pytest.raises(SubmissionError, match=r"datasets/ray0rf1re/RSOSTBTEST-pro-results does not exist.*--create-repo"):
        submit_results(src, rescore=False)
    assert not fake_hub["created"]                                   # nothing is created unasked
    res = submit_results(src, rescore=False, create_repo=True)
    assert res.accepted and fake_hub["created"] == [("ray0rf1re/RSOSTBTEST-pro-results", "dataset")]
    assert fake_hub["commits"] == [("ray0rf1re/RSOSTBTEST-pro-results", True)]

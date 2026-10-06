"""The General Public Use (GPU) score."""
from __future__ import annotations

import json

import pytest

from rsostb.cli.main import main
from rsostb.scoring.gpu import FLOOR, THRESHOLD, gpu_for_results, gpu_score, parse_parameters
from rsostb.submission import read_results, validate_results


@pytest.mark.parametrize("value,expected", [
    ("Qwen3-4B-Q4_K_M.gguf", 4.0), ("Llama-3.1-8B-Instruct", 8.0), ("Mixtral-8x7B", 56.0),
    ("Qwen2.5-0.5B", 0.5), ("350M", 0.35), (7e9, 7.0), (70, 70.0), ("1.5T", 1500.0),
    ("gpt-4o-mini", None), ("nanonix", None), (None, None), (True, None),
])
def test_parse_parameters(value, expected):
    got = parse_parameters(value)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_good_models_keep_their_score():
    for kw in ({}, {"parameters": "1T"}, {"price_in": 60, "price_out": 120}):
        g = gpu_score(THRESHOLD + 0.1, **kw)
        assert g.multiplier == 1 and g.gpu_score == pytest.approx(100 * (THRESHOLD + 0.1))


def test_low_scores_are_cut_harder_for_bigger_and_pricier_models():
    small, big = gpu_score(0.3, parameters="1B"), gpu_score(0.3, parameters="400B")
    cheap, dear = gpu_score(0.3, price_in=0.1, price_out=0.2), gpu_score(0.3, price_in=15, price_out=60)
    assert 0 < big.multiplier < small.multiplier < 1
    assert 0 < dear.multiplier < cheap.multiplier < 1
    assert big.gpu_score < small.gpu_score and dear.gpu_score < cheap.gpu_score
    # the worse of size and price decides
    assert gpu_score(0.3, parameters="1B", price_in=15, price_out=60).multiplier == dear.multiplier


def test_multiplier_floor_and_price_per_parameter():
    g = gpu_score(0.0, parameters="2T")
    assert g.multiplier == FLOOR and g.gpu_score == 0
    g = gpu_score(0.5, parameters="10B", price_in=1.0, price_out=3.0)
    assert g.price_blended_per_mtok == pytest.approx(1.5) and g.price_per_b_params == pytest.approx(0.15)
    assert g.currency == "USD" and "10B parameters" in g.basis


def test_run_cost_from_usage():
    doc = {"scores": {"normalized": 0.5}, "model": {"name": "m", "pricing": {"input_per_mtok": 2.0, "output_per_mtok": 8.0}},
           "task_results": [{"usage": {"prompt_tokens": 500_000, "completion_tokens": 100_000}},
                            {"usage": {"input_tokens": 500_000, "output_tokens": 0}}]}
    assert gpu_for_results(doc).run_cost == pytest.approx(2.0 + 0.8)


def test_benchmark_records_price_and_reports_gpu(tmp_path, mock_server, capsys):
    url, _ = mock_server
    out = tmp_path / "r.jsonl"
    assert main(["benchmark", "--adapter", "openai-compatible", "--model", "acme-8B", "--base-url", f"{url}/v1",
                 "--categories", "math", "--limit-per-category", "2", "--output", str(out), "--quiet",
                 "--price-in", "0.2", "--price-out", "0.6"]) == 0
    assert "GPU score (general public use)" in capsys.readouterr().out
    doc = read_results(out)
    assert doc["model"]["pricing"] == {"input_per_mtok": 0.2, "output_per_mtok": 0.6, "currency": "USD"}
    assert validate_results(doc).ok
    assert main(["gpu", str(out), "--json"]) == 0
    g = json.loads(capsys.readouterr().out)
    assert g["parameters_b"] == 8.0 and g["price_blended_per_mtok"] == pytest.approx(0.3) and g["run_cost"] is not None


def test_leaderboard_entries_carry_the_gpu_score(tmp_path, example_results, bench):
    from rsostb.leaderboard.api import leaderboard_rows
    from rsostb.leaderboard.store import LeaderboardStore

    store = LeaderboardStore(tmp_path, bench=bench)
    res = store.add(read_results(example_results / "noisy-oracle.jsonl.gz"))
    assert res.accepted and res.entry["gpu"]["gpu_score"] > 0
    (row,) = leaderboard_rows(store.entries())
    assert row["gpu_score"] == res.entry["gpu"]["gpu_score"]

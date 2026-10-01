"""The static Hugging Face Space: the leaderboard as one self-contained page.

Static Spaces are free for every Hugging Face account (Gradio and Docker
Spaces need a paid plan to create), so this is the default Space. Everything
the page shows is computed here, at publish time, by the code the CLI uses:

* entries: the full results files merged into the results dataset
  (``rsostb submit`` opens them as pull requests) and the bundled baseline
  runs, each one re-validated and re-scored here; a stored entry is never
  taken on trust;
* tasks: the prompt-side view of each public task (``Task.public_view``), so
  no reference answers, tests or rubric check parameters.

``hf/static/index.html`` gets that data embedded (so the page also works
offline, from ``file://``); ``leaderboard.json`` is published next to it for
programs. The output depends only on the commit and the results dataset, so
an unchanged leaderboard re-publishes as no commit at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..datasets.build import check_no_private
from ..datasets.loader import load_benchmark
from ..leaderboard.api import build_api
from ..leaderboard.store import LeaderboardStore
from ..submission.results_io import ResultsFormatError, read_results
from ..version import BENCHMARK_NAME, BENCHMARK_VERSION, DATASET_VERSION, RUNNER_VERSION
from .errors import hub_error, hub_status

PLACEHOLDER = "__RSOSTB_DATA__"
TASK_FIELDS = ("id", "category", "subcategory", "difficulty", "evaluation_type", "weight_class", "version", "tags",
               "system_prompt", "messages", "prompt", "choices")


def _commit(root: Path) -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                                 timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return sha[:12]


def task_record(task) -> dict[str, Any]:
    """What the task browser shows: prompts, choices, rubric descriptions."""
    view = task.public_view()
    rec = {k: view[k] for k in TASK_FIELDS if view.get(k) not in (None, "", [])}
    criteria = (view.get("rubric") or {}).get("criteria") or []
    if criteria:
        rec["rubric"] = [{"description": c.get("description") or c["id"], "weight": c.get("weight"),
                          "gate": bool(c.get("gate")), "judge": bool(c.get("judge"))} for c in criteria]
    return rec


def _add(store: LeaderboardStore, path: Path, label: str) -> dict[str, Any] | None:
    try:
        res = store.add(read_results(path), rescore=True)
    except (ResultsFormatError, OSError, ValueError) as exc:
        print(f"warning: {label} {path.name} skipped: {exc}")
        return None
    if not res.accepted and not res.message.startswith(("duplicate", "rejected: reference")):
        print(f"warning: {label} {path.name} not listed: {res.message}")
    return res.entry if res.accepted else None


def _pull(repo: str, token: str | None, dest: Path) -> Path:
    from huggingface_hub import snapshot_download  # type: ignore

    return Path(snapshot_download(repo_id=repo, repo_type="dataset", token=token, local_dir=str(dest),
                                  allow_patterns=["submissions/*/*.json", "entries/*.json"]))


def board(root: Path, *, results_repo: str | None = None, token: str | None = None,
          bench=None) -> tuple[dict[str, Any], list[str]]:
    """The leaderboard API document, and notes for the page."""
    bench = bench or load_benchmark()
    notes = []
    dated: dict[str, str] = {}  # submission id -> when it was submitted (fixed, so rebuilds are identical)
    with tempfile.TemporaryDirectory(prefix="rsostb-board-") as tmp:
        store = LeaderboardStore(Path(tmp) / "store", bench=bench)
        if results_repo:
            try:
                hub = _pull(results_repo, token, Path(tmp) / "hub")
            except Exception as exc:  # the board is still built from the baselines
                print(f"warning: could not read datasets/{results_repo}: {hub_error(exc)}")
                status = hub_status(exc)
                notes.append(f"datasets/{results_repo} could not be read"
                             f"{f' (HTTP {status})' if status else ''}; showing the baseline runs only")
            else:
                listed = 0
                for p in sorted(hub.glob("submissions/*/*.json")):
                    entry = _add(store, p, "submission")
                    if entry:
                        listed += 1
                        try:
                            stored = json.loads((hub / "entries" / p.name).read_text(encoding="utf-8"))
                            dated[entry["submission_id"]] = str(stored.get("submitted_at") or "")
                        except (OSError, ValueError, AttributeError):
                            pass
                notes.append(f"submissions merged into datasets/{results_repo}: {listed}")
        for p in sorted((root / "examples" / "results").glob("*.json*")):
            _add(store, p, "baseline")
        doc = build_api(store)
    del doc["generated_at"]  # keep the output a function of its inputs
    for e in doc["entries"]:
        e["submitted_at"] = dated.get(e["submission_id"]) or e.get("timestamp") or ""
    doc["source"] = {"commit": _commit(root), "results_repo": results_repo or None}
    return doc, notes


def stage_static_space(repo_root: str | Path, dest: str | Path, *, results_repo: str | None = None,
                       token: str | None = None) -> Path:
    root, dest = Path(repo_root), Path(dest)
    bench = load_benchmark()
    doc, notes = board(root, results_repo=results_repo, token=token, bench=bench)
    tasks = [task_record(t) for t in bench.tasks if t.visibility == "public" and t.active]
    data = {
        "benchmark": BENCHMARK_NAME, "benchmark_version": BENCHMARK_VERSION, "dataset_version": DATASET_VERSION,
        "runner_version": RUNNER_VERSION, "source": doc["source"], "notes": notes,
        "categories": doc["categories"], "metrics": doc["metrics"], "entries": doc["entries"], "tasks": tasks,
    }
    template = (root / "hf" / "static" / "index.html").read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"hf/static/index.html must contain {PLACEHOLDER} exactly once")
    # Inside <script>, "<" could end the element early; JSON escapes keep the value identical.
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    files = {
        "README.md": (root / "hf" / "static" / "README.md").read_text(encoding="utf-8"),
        "index.html": template.replace(PLACEHOLDER, payload),
        "leaderboard.json": json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    }
    check_no_private(files)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, text in files.items():
        (dest / name).write_text(text, encoding="utf-8")
    return dest

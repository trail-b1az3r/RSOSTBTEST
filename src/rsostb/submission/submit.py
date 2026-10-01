"""Submitting results (``rsostb submit``).

Every submission is validated first. Accepted results go either to a local
directory (``--to-dir``, e.g. a clone of the results dataset) or, as a pull
request, to the Hugging Face results dataset (``RSOSTB_RESULTS_REPO``,
default ``ray0rf1re/RSOSTBTEST-pro-results``). The token is read from the
environment variable named by ``token_env`` (default ``HF_TOKEN``); it is
never written anywhere.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..leaderboard.entries import entry_from_results
from .results_io import read_results
from .sanitize import safe_slug
from .validate import ValidationReport, validate_results

DEFAULT_RESULTS_REPO = "ray0rf1re/RSOSTBTEST-pro-results"


class SubmissionError(RuntimeError):
    pass


@dataclass
class SubmitResult:
    accepted: bool
    submission_id: str | None
    validation: ValidationReport
    destination: str | None = None
    entry: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def prepare_submission(doc: dict[str, Any], report: ValidationReport) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = entry_from_results(doc, report)
    return entry, doc


def submit_results(path: str | Path, *, repo: str | None = None, token_env: str = "HF_TOKEN", dry_run: bool = False,
                   to_dir: str | Path | None = None, rescore: bool = True, bench=None) -> SubmitResult:
    doc = read_results(path)
    report = validate_results(doc, bench, rescore=rescore)
    if not report.ok:
        return SubmitResult(False, None, report, notes=["validation failed; nothing was uploaded"])
    kind = (doc.get("model") or {}).get("kind", "model")
    if kind == "reference":
        return SubmitResult(False, report.submission_id, report, notes=["reference (oracle) runs are not accepted"])
    entry, full = prepare_submission(doc, report)
    sid = report.submission_id
    rel = f"submissions/v{safe_slug(doc['benchmark_version'])}/{sid}.json"
    if dry_run:
        return SubmitResult(True, sid, report, destination=f"(dry run) {rel}", entry=entry)
    if to_dir:
        root = Path(to_dir)
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            return SubmitResult(False, sid, report, destination=str(out), notes=["duplicate submission"])
        out.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
        e = root / "entries" / f"{sid}.json"
        e.parent.mkdir(parents=True, exist_ok=True)
        e.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        return SubmitResult(True, sid, report, destination=str(out), entry=entry)
    token = os.environ.get(token_env)
    if not token:
        raise SubmissionError(f"set {token_env} (a Hugging Face token with write access) or use --to-dir / --dry-run")
    try:
        from huggingface_hub import CommitOperationAdd, HfApi  # type: ignore
    except ImportError as exc:
        raise SubmissionError("pip install 'RSOSTB[hf]' to submit to Hugging Face") from exc
    repo = repo or os.environ.get("RSOSTB_RESULTS_REPO", DEFAULT_RESULTS_REPO)
    api = HfApi(token=token)
    ops = [
        CommitOperationAdd(path_in_repo=rel, path_or_fileobj=json.dumps(full, ensure_ascii=False).encode("utf-8")),
        CommitOperationAdd(path_in_repo=f"entries/{sid}.json",
                           path_or_fileobj=json.dumps(entry, indent=2, ensure_ascii=False).encode("utf-8")),
    ]
    try:
        info = api.create_commit(repo_id=repo, repo_type="dataset", operations=ops, create_pr=True,
                                 commit_message=f"Submission {sid}: {entry['model']['name']} on v{doc['benchmark_version']}",
                                 commit_description="Submitted with `rsostb submit`. Validation: " + report.status)
    except Exception as exc:  # network / auth / permissions: the results were valid, the upload failed
        raise SubmissionError(f"validated (submission id {sid}) but the upload to datasets/{repo} failed: "
                              f"{type(exc).__name__}: {str(exc)[:300]}; retry, or use --to-dir") from exc
    return SubmitResult(True, sid, report, destination=getattr(info, "pr_url", None) or str(info), entry=entry)

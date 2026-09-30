"""Submission storage for the leaderboard (local directory, optionally synced
with a Hugging Face dataset repo).

Layout::

    <root>/entries/<submission_id>.json          leaderboard entries (API data)
    <root>/submissions/v<X.Y>/<submission_id>.json  full validated results

Only generated ids are ever used as file names; user-supplied names never
touch the filesystem.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schemas import schema_errors
from ..submission.results_io import ResultsFormatError, parse_results_text
from ..submission.sanitize import safe_slug
from ..submission.validate import ValidationReport, is_submission_id, responses_fingerprint, validate_results
from .entries import entry_from_results

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
ALLOWED_SUFFIXES = (".json", ".jsonl")


@dataclass
class AddResult:
    accepted: bool
    entry: dict[str, Any] | None
    report: ValidationReport
    message: str


class LeaderboardStore:
    def __init__(self, root: str | Path, bench=None) -> None:
        self.root = Path(root)
        (self.root / "entries").mkdir(parents=True, exist_ok=True)
        (self.root / "submissions").mkdir(parents=True, exist_ok=True)
        self.bench = bench

    # -- reading ----------------------------------------------------------------
    def entries(self, include_kinds: tuple[str, ...] = ("model", "baseline", "synthetic")) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.root / "entries").glob("*.json")):
            if not is_submission_id(p.stem):
                continue
            try:
                e = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if schema_errors("leaderboard_entry", e):
                continue
            if e["model"].get("kind", "model") in include_kinds and e["validation_status"] != "rejected":
                out.append(e)
        return sorted(out, key=lambda e: e["scores"]["rsostb_score"], reverse=True)

    def get_entry(self, sid: str) -> dict[str, Any] | None:
        if not is_submission_id(sid):
            return None
        p = self.root / "entries" / f"{sid}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def get_results(self, sid: str) -> dict[str, Any] | None:
        if not is_submission_id(sid):
            return None
        for p in (self.root / "submissions").glob(f"v*/{sid}.json"):
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    # -- writing ----------------------------------------------------------------
    def add_upload(self, filename: str, data: bytes, rescore: bool = True) -> AddResult:
        """Validate and store an uploaded results file. ``filename`` is used only
        to pick JSON vs JSONL parsing."""
        name = os.path.basename(filename or "").lower()
        if not name.endswith(ALLOWED_SUFFIXES):
            return AddResult(False, None, ValidationReport(status="rejected", errors=["only .json and .jsonl files are accepted"]),
                             "rejected: unsupported file type")
        if len(data) > MAX_UPLOAD_BYTES:
            return AddResult(False, None, ValidationReport(status="rejected", errors=["file too large"]), "rejected: too large")
        try:
            doc = parse_results_text(data.decode("utf-8"), name)
        except (ResultsFormatError, UnicodeDecodeError) as exc:
            return AddResult(False, None, ValidationReport(status="rejected", errors=[str(exc)]), "rejected: unreadable")
        return self.add(doc, rescore=rescore)

    def add(self, doc: dict[str, Any], rescore: bool = True) -> AddResult:
        report = validate_results(doc, self.bench, rescore=rescore)
        if not report.ok:
            return AddResult(False, None, report, "rejected: validation failed")
        if (doc.get("model") or {}).get("kind") == "reference":
            report.status = "rejected"
            report.errors.append("reference (oracle) runs are not accepted")
            return AddResult(False, None, report, "rejected: reference run")
        sid = report.submission_id
        if (self.root / "entries" / f"{sid}.json").exists():
            return AddResult(False, self.get_entry(sid), report, "duplicate: this submission already exists")
        fp = responses_fingerprint(doc)
        if fp in set(self._fingerprints().values()):
            return AddResult(False, None, report, "duplicate: identical responses already submitted")
        entry = entry_from_results(doc, report)
        errs = schema_errors("leaderboard_entry", entry)
        if errs:
            report.errors.extend(f"entry: {x}" for x in errs)
            report.status = "rejected"
            return AddResult(False, None, report, "rejected: could not build a valid entry")
        sub = self.root / "submissions" / f"v{safe_slug(doc['benchmark_version'])}" / f"{sid}.json"
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        stored = dict(entry)
        (self.root / "entries" / f"{sid}.json").write_text(json.dumps(stored, indent=2, ensure_ascii=False),
                                                           encoding="utf-8")
        (self.root / "fingerprints.json").write_text(json.dumps({**self._fingerprints(), sid: fp}), encoding="utf-8")
        return AddResult(True, entry, report, f"accepted ({report.status})")

    def _fingerprints(self) -> dict[str, str]:
        p = self.root / "fingerprints.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def is_duplicate_responses(self, doc: dict[str, Any]) -> bool:
        return responses_fingerprint(doc) in set(self._fingerprints().values())

    # -- Hugging Face sync --------------------------------------------------------
    def pull_from_hub(self, repo: str, token: str | None = None) -> int:
        from huggingface_hub import snapshot_download  # type: ignore

        path = snapshot_download(repo_id=repo, repo_type="dataset", token=token, allow_patterns=["entries/*.json"])
        n = 0
        for p in Path(path, "entries").glob("*.json"):
            if is_submission_id(p.stem) and not (self.root / "entries" / p.name).exists():
                e = json.loads(p.read_text(encoding="utf-8"))
                if not schema_errors("leaderboard_entry", e):
                    (self.root / "entries" / p.name).write_text(json.dumps(e, indent=2), encoding="utf-8")
                    n += 1
        return n

    def push_to_hub(self, repo: str, sid: str, token: str) -> str:
        from huggingface_hub import CommitOperationAdd, HfApi  # type: ignore

        entry = self.get_entry(sid)
        results = self.get_results(sid)
        if entry is None or results is None:
            raise KeyError(sid)
        ops = [
            CommitOperationAdd(path_in_repo=f"entries/{sid}.json", path_or_fileobj=json.dumps(entry, indent=2).encode()),
            CommitOperationAdd(path_in_repo=f"submissions/v{safe_slug(entry['benchmark_version'])}/{sid}.json",
                               path_or_fileobj=json.dumps(results, ensure_ascii=False).encode()),
        ]
        info = HfApi(token=token).create_commit(repo_id=repo, repo_type="dataset", operations=ops,
                                                commit_message=f"Space submission {sid}")
        return str(info)

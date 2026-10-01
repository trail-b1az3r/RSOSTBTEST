#!/usr/bin/env python3
"""Fail if anything tracked by git looks like a private (hidden) task.

Scans every file git tracks (or the paths given on the command line) for the
private-task markers from ``rsostb.datasets.lint.PRIVATE_MARKERS`` and for
task records with ``visibility: private``. Files that legitimately *document*
the markers are allow-listed.

Usage:  python scripts/check_private_leaks.py [PATH ...]
Exit status 1 if a leak is found.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rsostb.datasets.lint import PRIVATE_MARKERS  # noqa: E402

# Files that explain the policy and therefore mention the markers.
ALLOW = {
    "benchmark/private/README.md",
    "scripts/check_private_leaks.py",
    "src/rsostb/datasets/lint.py",
    "src/rsostb/datasets/build.py",
    "docs/PRIVACY.md",
    "docs/CONTAMINATION.md",
    "tests/test_cli.py",
}
PRIVATE_RECORD = re.compile(r"""(?m)^\s*["']?visibility["']?\s*:\s*["']?private\b""")
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".jsonl", ".md", ".py", ".txt", ".csv", ".toml"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def scan(paths: list[str]) -> list[str]:
    problems = []
    for rel in paths:
        if rel in ALLOW:
            continue
        p = ROOT / rel
        if p.suffix not in TEXT_SUFFIXES or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for marker in PRIVATE_MARKERS:
            if marker in text:
                problems.append(f"{rel}: contains private marker {marker!r}")
        if rel.startswith(("benchmark/tasks/", "dataset/")) and PRIVATE_RECORD.search(text):
            problems.append(f"{rel}: contains a task with visibility: private")
    tracked_private = [p for p in paths if p.startswith("benchmark/private/") and p != "benchmark/private/README.md"]
    problems += [f"{p}: files under benchmark/private/ must never be committed" for p in tracked_private]
    return problems


def main(argv: list[str]) -> int:
    paths = argv or tracked_files()
    problems = scan(paths)
    for msg in problems:
        print(f"LEAK  {msg}")
    print(f"scanned {len(paths)} paths: {'no leaks' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

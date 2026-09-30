"""Benchmark execution: prompts, tool episodes, environments and the runner."""
from __future__ import annotations

from typing import Any


def run_benchmark(*args: Any, **kwargs: Any):
    from .runner import run_benchmark as _run

    return _run(*args, **kwargs)


__all__ = ["run_benchmark"]

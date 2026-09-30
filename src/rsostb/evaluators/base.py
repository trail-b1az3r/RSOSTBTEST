"""Evaluator interface.

An evaluator turns (task, response) into an :class:`EvalResult`:

* ``credit`` in [0, 1] — how much of the task was accomplished;
* ``events`` — named penalty events (``hallucination``, ``over_refusal``, ...)
  with multipliers; magnitudes come from ``scoring.yaml``, never from here;
* ``bonus_events`` — named bonus events, likewise;
* ``status`` / ``flags`` / ``details`` — for reports and debugging;
* ``coverage`` — the share of the task's grading weight actually evaluated
  (below 1 when a judge or toolchain was unavailable);
* ``judge_based`` — any part of the credit came from an LLM judge.

Evaluators never produce points; :mod:`rsostb.scoring` does.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..sandbox import Sandbox, SandboxLimits

if TYPE_CHECKING:  # pragma: no cover
    from ..datasets.task import Task
    from .judge import Judge


@dataclass
class Response:
    text: str
    transcript: list[dict[str, Any]] | None = None
    episode: dict[str, Any] | None = None
    choice_order: list[int] | None = None
    error: str | None = None


@dataclass
class EvalResult:
    credit: float = 0.0
    events: dict[str, float] = field(default_factory=dict)
    bonus_events: dict[str, float] = field(default_factory=dict)
    status: str = "scored"
    judge_based: bool = False
    coverage: float = 1.0
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def event(self, name: str, mult: float = 1.0) -> EvalResult:
        self.events[name] = self.events.get(name, 0.0) + float(mult)
        return self

    def bonus(self, name: str, mult: float = 1.0) -> EvalResult:
        self.bonus_events[name] = self.bonus_events.get(name, 0.0) + float(mult)
        return self

    def flag(self, *names: str) -> EvalResult:
        for n in names:
            if n not in self.flags:
                self.flags.append(n)
        return self

    def clamp(self) -> EvalResult:
        self.credit = max(0.0, min(1.0, float(self.credit)))
        self.coverage = max(0.0, min(1.0, float(self.coverage)))
        return self


@dataclass
class EvalContext:
    sandbox: Sandbox
    limits: SandboxLimits
    judge: Judge | None = None
    compile_timeout: float = 60.0
    offline: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


Evaluator = Callable[["Task", Response, EvalContext], EvalResult]
EVALUATORS: dict[str, Evaluator] = {}


def register(*names: str):
    def deco(fn: Evaluator) -> Evaluator:
        for n in names:
            EVALUATORS[n] = fn
        return fn

    return deco


def invalid(reason: str, *, event: bool = True) -> EvalResult:
    r = EvalResult(credit=0.0, status="invalid_output", details={"reason": reason})
    r.flag("invalid_output")
    if event:
        r.event("invalid_output")
    return r


def unavailable(reason: str) -> EvalResult:
    r = EvalResult(credit=0.0, status="unavailable", coverage=0.0, details={"reason": reason})
    r.flag("unavailable")
    return r

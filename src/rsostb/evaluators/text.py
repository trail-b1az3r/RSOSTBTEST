"""Answer-matching evaluators: exact, normalized, numeric, regex, multiple choice."""
from __future__ import annotations

import re
import string
from typing import Any

from . import behavior_detect as bd
from .base import EvalContext, EvalResult, Response, invalid, register
from .extract import (
    _ANSWER_TAG,
    extract_answer,
    normalize_text,
    numbers_equal,
    parse_number,
    strip_thinking,
)

LETTERS = string.ascii_uppercase


def answer_gate(task, text: str, result: EvalResult) -> bool:
    """Shared handling of refusals and unwarranted abstentions on ordinary
    answer tasks. Returns False when grading should stop."""
    body = strip_thinking(text)
    if not body.strip():
        result.status = "invalid_output"
        result.flag("empty_response")
        result.event("invalid_output")
        return False
    if bd.is_refusal(body):
        result.flag("refusal", "over_refusal")
        result.event("refusal_on_benign")
        result.details["behavior"] = "refused"
        return False
    return True


def _trap(task, answer: str, result: EvalResult) -> None:
    traps = (task.evaluation or {}).get("trap_answers") or []
    norm = normalize_text(answer)
    num = parse_number(answer)
    for t in traps:
        tnum = parse_number(t) if isinstance(t, (int, float)) or re.fullmatch(r"[-+\d.,/eE ]+", str(t)) else None
        if normalize_text(t) == norm or (tnum is not None and num is not None and numbers_equal(tnum, num, 1e-6, 1e-9)):
            result.flag("fell_for_trap")
            result.event("adversarial_trap")
            result.details["trap"] = t
            return


def _format_check(task, text: str, result: EvalResult) -> None:
    if task.data.get("answer_format") == "answer_tag" and not _ANSWER_TAG.search(strip_thinking(text)):
        result.flag("missing_answer_tag")
        result.event("format_violation")


def _references(task) -> list[Any]:
    refs = []
    if task.reference_answer is not None:
        refs.append(task.reference_answer)
    refs.extend(task.acceptable_answers)
    return refs


def _abstained_wrongly(task, answer: str, text: str, result: EvalResult) -> None:
    if bd.is_abstention(strip_thinking(text)) and task.expected_behavior == "answer":
        result.flag("over_abstention")
        result.event("over_abstention")


@register("exact")
def evaluate_exact(task, response: Response, ctx: EvalContext) -> EvalResult:
    res = EvalResult()
    if not answer_gate(task, response.text, res):
        return res
    ans = extract_answer(response.text, task.data.get("answer_format", "final_line"))
    res.details["extracted"] = ans[:500]
    ok = any(ans.strip() == str(r).strip() for r in _references(task))
    res.credit = 1.0 if ok else 0.0
    if not ok:
        _trap(task, ans, res)
        _abstained_wrongly(task, ans, response.text, res)
    _format_check(task, response.text, res)
    return res


@register("normalized")
def evaluate_normalized(task, response: Response, ctx: EvalContext) -> EvalResult:
    res = EvalResult()
    if not answer_gate(task, response.text, res):
        return res
    ev = task.evaluation
    ans = extract_answer(response.text, task.data.get("answer_format", "final_line"))
    res.details["extracted"] = ans[:500]
    norm = normalize_text(ans, ignore_articles=ev.get("ignore_articles", True))
    refs = [normalize_text(r, ignore_articles=ev.get("ignore_articles", True)) for r in _references(task)]
    ok = norm in refs
    if not ok and ev.get("contains_ok"):
        # Accept a longer answer that contains exactly one reference as a whole phrase.
        ok = any(re.search(r"(?<!\w)" + re.escape(r) + r"(?!\w)", norm) for r in refs if r)
    res.credit = 1.0 if ok else 0.0
    if not ok:
        _trap(task, ans, res)
        _abstained_wrongly(task, ans, response.text, res)
    _format_check(task, response.text, res)
    return res


@register("numeric")
def evaluate_numeric(task, response: Response, ctx: EvalContext) -> EvalResult:
    res = EvalResult()
    if not answer_gate(task, response.text, res):
        return res
    ev = task.evaluation
    ans = extract_answer(response.text, task.data.get("answer_format", "final_line"))
    res.details["extracted"] = ans[:300]
    val = parse_number(ans)
    if val is None:
        _abstained_wrongly(task, ans, response.text, res)
        res.status = "invalid_output"
        res.flag("no_number")
        res.event("invalid_output")
        return res
    res.details["value"] = val
    rel = float(ev.get("rel_tol", 1e-6))
    abs_tol = float(ev.get("abs_tol", 1e-9))
    refs = [parse_number(r) for r in _references(task)]
    ok = any(r is not None and numbers_equal(val, r, rel, abs_tol) for r in refs)
    partial_tol = ev.get("partial_rel_tol")
    if ok:
        res.credit = 1.0
    elif partial_tol and any(r is not None and numbers_equal(val, r, float(partial_tol), abs_tol) for r in refs):
        res.credit = float(ev.get("partial_credit", 0.5))
        res.flag("imprecise")
    else:
        res.credit = 0.0
        _trap(task, ans, res)
    unit = ev.get("unit")
    if unit and res.credit > 0:
        units = [unit] if isinstance(unit, str) else list(unit)
        if not any(re.search(r"(?<![A-Za-z])" + re.escape(u) + r"(?![A-Za-z])", ans) for u in units):
            res.flag("missing_unit")
            res.event("format_violation")
    _format_check(task, response.text, res)
    return res


@register("regex")
def evaluate_regex(task, response: Response, ctx: EvalContext) -> EvalResult:
    res = EvalResult()
    if not answer_gate(task, response.text, res):
        return res
    ev = task.evaluation
    target = strip_thinking(response.text) if ev.get("target") == "whole" else extract_answer(response.text)
    pats = ev["patterns"] if "patterns" in ev else [ev["pattern"]]
    flags = re.I if ev.get("ignore_case", True) else 0
    hits = sum(1 for p in pats if re.search(p, target, flags | re.M))
    res.credit = hits / len(pats) if ev.get("partial", False) else float(hits == len(pats))
    for p in ev.get("forbid", []):
        if re.search(p, target, flags | re.M):
            res.credit = 0.0
            res.flag("forbidden_pattern")
    res.details["target"] = target[:300]
    return res


# --------------------------------------------------------------------------- multiple choice

def present_choices(task, order: list[int] | None) -> list[tuple[str, str]]:
    choices = task.choices
    order = order or list(range(len(choices)))
    return [(LETTERS[i], choices[j]) for i, j in enumerate(order)]


def _correct_indices(task) -> set[int]:
    ref = task.reference_answer
    refs = ref if isinstance(ref, list) else [ref]
    out = set()
    for r in refs:
        if isinstance(r, int) and not isinstance(r, bool):
            out.add(r)
        elif isinstance(r, str) and len(r) == 1 and r.upper() in LETTERS:
            out.add(LETTERS.index(r.upper()))
        elif isinstance(r, str):
            out.add(task.choices.index(r))
    return out


_LETTER_RE = re.compile(r"(?<![A-Za-z])\(?([A-L])\)?(?![A-Za-z])")


def extract_letters(text: str, n_choices: int, multi: bool) -> list[str]:
    ans = extract_answer(text)
    valid = LETTERS[:n_choices]
    found = [m.group(1) for m in _LETTER_RE.finditer(ans.upper()) if m.group(1) in valid]
    if not found:
        body = strip_thinking(text).upper()
        found = [m.group(1) for m in _LETTER_RE.finditer(body[-200:]) if m.group(1) in valid]
        if not multi and found:
            found = found[-1:]
    if not multi:
        return found[:1]
    seen = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return seen


@register("multiple_choice")
def evaluate_multiple_choice(task, response: Response, ctx: EvalContext) -> EvalResult:
    res = EvalResult()
    if not answer_gate(task, response.text, res):
        return res
    multi = bool(task.evaluation.get("multi", False))
    order = response.choice_order or list(range(len(task.choices)))
    letters = extract_letters(response.text, len(task.choices), multi)
    res.details["selected_letters"] = letters
    if not letters:
        # Accept the exact text of a single choice as the answer.
        ans = normalize_text(extract_answer(response.text))
        for i, (_, choice) in enumerate(present_choices(task, order)):
            if normalize_text(choice) == ans:
                letters = [LETTERS[i]]
                break
    if not letters:
        return invalid("no choice letter found")
    selected = {order[LETTERS.index(L)] for L in letters}
    correct = _correct_indices(task)
    res.details["selected"] = sorted(selected)
    if multi:
        right = len(selected & correct)
        wrong = len(selected - correct)
        res.credit = max(0.0, (right - wrong) / len(correct))
    else:
        res.credit = 1.0 if selected <= correct else 0.0
        if not res.credit:
            traps = task.evaluation.get("trap_choices") or []
            if selected & set(traps):
                res.flag("fell_for_trap")
                res.event("adversarial_trap")
    return res

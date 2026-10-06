"""Deterministic rubric checks.

Each check is ``fn(text, params, task) -> (score, detail)`` with ``score`` in
[0, 1]. Rubric criteria reference checks by ``type``. Heuristic checks (rhyme,
syllables, language identification) are labelled as such in
docs/TASK_AUTHORING.md and should carry modest weights.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import behavior_detect as bd
from .extract import (
    extract_code,
    extract_json,
    normalize_text,
    parse_number,
    plain_quotes,
    strip_thinking,
    word_count,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..datasets.task import Task

CheckFn = Callable[[str, dict[str, Any], "Task | None"], tuple[float, str]]
CHECKS: dict[str, CheckFn] = {}


def check(name: str):
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return deco


def _bounds(value: float, p: dict[str, Any]) -> tuple[float, str]:
    lo, hi = p.get("min"), p.get("max")
    if "equals" in p:
        lo = hi = p["equals"]
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    return (1.0 if ok else 0.0), f"value={value} min={lo} max={hi}"


def _terms(p: dict[str, Any], key: str) -> list[str]:
    v = p.get(key) or []
    return [v] if isinstance(v, str) else list(v)


def _find(text: str, term: str, p: dict[str, Any]) -> bool:
    if p.get("regex_terms"):
        return re.search(term, text, 0 if p.get("case_sensitive") else re.I) is not None
    if p.get("whole_word"):
        flags = 0 if p.get("case_sensitive") else re.I
        return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, flags) is not None
    if p.get("case_sensitive"):
        return term in text
    return unicodedata.normalize("NFKC", term).casefold() in unicodedata.normalize("NFKC", text).casefold()


def lines_of(text: str, *, skip_labels: bool = False) -> list[str]:
    out = []
    for ln in strip_thinking(text).splitlines():
        s = ln.strip()
        if not s:
            continue
        if skip_labels and re.fullmatch(r"[\[(].{1,40}[\])]:?|#+\s.*|\*\*.{1,40}\*\*:?", s):
            continue
        out.append(s)
    return out


def paragraphs_of(text: str, sep: str | None = None) -> list[str]:
    t = strip_thinking(text).strip()
    if sep and sep != "blank":
        parts = t.split(sep)
    else:
        parts = re.split(r"\n\s*\n", t)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- length & counts

@check("word_count")
def _word_count(text, p, task):
    return _bounds(word_count(strip_thinking(text)), p)


@check("char_count")
def _char_count(text, p, task):
    return _bounds(len(strip_thinking(text)), p)


@check("sentence_count")
def _sentence_count(text, p, task):
    t = strip_thinking(text)
    n = len([s for s in re.split(r"(?<=[.!?。！？])\s+", t.strip()) if s.strip()])
    return _bounds(n, p)


@check("line_count")
def _line_count(text, p, task):
    return _bounds(len(lines_of(text, skip_labels=p.get("skip_labels", False))), p)


@check("paragraph_count")
def _paragraph_count(text, p, task):
    return _bounds(len(paragraphs_of(text, p.get("sep"))), p)


@check("stanza_count")
def _stanza_count(text, p, task):
    stanzas = [s for s in paragraphs_of(text) if lines_of(s, skip_labels=True)]
    return _bounds(len(stanzas), p)


@check("bullet_count")
def _bullet_count(text, p, task):
    n = len([ln for ln in strip_thinking(text).splitlines() if re.match(r"^\s*(?:[-*•+]|\d+[.)])\s+\S", ln)])
    return _bounds(n, p)


@check("numbered_count")
def _numbered_count(text, p, task):
    n = len([ln for ln in strip_thinking(text).splitlines() if re.match(r"^\s*\d+[.)]\s+\S", ln)])
    return _bounds(n, p)


@check("max_sentence_words")
def _max_sentence_words(text, p, task):
    sents = [s for s in re.split(r"(?<=[.!?])\s+", strip_thinking(text)) if s.strip()]
    worst = max((word_count(s) for s in sents), default=0)
    ok = worst <= p["max"]
    return (1.0 if ok else 0.0), f"longest sentence {worst} words"


# --------------------------------------------------------------------------- content

@check("contains")
def _contains(text, p, task):
    t = strip_thinking(text)
    terms = _terms(p, "terms")
    found = [x for x in terms if _find(t, x, p)]
    if not terms:
        return 1.0, "no terms"
    frac = len(found) / len(terms)
    if not p.get("partial", True):
        frac = 1.0 if frac == 1.0 else 0.0
    missing = [x for x in terms if x not in found]
    return frac, f"missing={missing[:8]}"


@check("contains_any")
def _contains_any(text, p, task):
    t = strip_thinking(text)
    terms = _terms(p, "terms")
    hit = [x for x in terms if _find(t, x, p)]
    return (1.0 if hit else 0.0), f"hit={hit[:5]}"


@check("not_contains")
def _not_contains(text, p, task):
    t = strip_thinking(text)
    hit = [x for x in _terms(p, "terms") if _find(t, x, p)]
    return (0.0 if hit else 1.0), f"forbidden present={hit[:5]}"


@check("contains_count")
def _contains_count(text, p, task):
    """``term``: how many times one term occurs. ``terms``: how many of the
    listed terms occur at least once (each matched as for contains_any)."""
    t = strip_thinking(text)
    if "terms" in p:
        hit = [x for x in _terms(p, "terms") if _find(t, x, p)]
        score, detail = _bounds(len(hit), p)
        return score, f"{detail} hit={hit[:6]}"
    term = p["term"]
    flags = 0 if p.get("case_sensitive") else re.I
    pat = r"(?<!\w)" + re.escape(term) + r"(?!\w)" if p.get("whole_word", True) else re.escape(term)
    return _bounds(len(re.findall(pat, t, flags)), p)


@check("regex")
def _regex(text, p, task):
    flags = re.I if p.get("ignore_case") else 0
    flags |= re.M if p.get("multiline", True) else 0
    flags |= re.S if p.get("dotall") else 0
    ok = re.search(p["pattern"], strip_thinking(text), flags) is not None
    return (1.0 if ok else 0.0), p["pattern"]


@check("not_regex")
def _not_regex(text, p, task):
    flags = re.I if p.get("ignore_case") else 0
    flags |= re.M
    ok = re.search(p["pattern"], strip_thinking(text), flags) is None
    return (1.0 if ok else 0.0), p["pattern"]


@check("each_line_regex")
def _each_line_regex(text, p, task):
    lines = lines_of(text, skip_labels=p.get("skip_labels", False))
    if not lines:
        return 0.0, "no lines"
    good = sum(1 for ln in lines if re.search(p["pattern"], ln, re.I if p.get("ignore_case") else 0))
    frac = good / len(lines)
    return (frac if p.get("partial", False) else (1.0 if good == len(lines) else 0.0)), f"{good}/{len(lines)} lines match"


@check("starts_with")
def _starts_with(text, p, task):
    t = strip_thinking(text).lstrip().lstrip("*_#> ").strip()
    pre = p["text"]
    ok = t.lower().startswith(pre.lower()) if not p.get("case_sensitive") else t.startswith(pre)
    return (1.0 if ok else 0.0), f"starts={t[:40]!r}"


@check("ends_with")
def _ends_with(text, p, task):
    raw = strip_thinking(text).rstrip()
    t = raw.rstrip("*_ ").rstrip()
    suf = p["text"]
    # Compare both with and without trailing markdown emphasis so a required
    # suffix such as "*beep*" still matches literally.
    pairs = [(raw, suf), (t, suf.rstrip("*_ ").rstrip())]
    if p.get("case_sensitive"):
        ok = any(a.endswith(b) for a, b in pairs if b)
    else:
        ok = any(a.lower().endswith(b.lower()) for a, b in pairs if b)
    return (1.0 if ok else 0.0), f"ends={t[-40:]!r}"


@check("case")
def _case(text, p, task):
    t = strip_thinking(text)
    letters = [c for c in t if c.isalpha() and c.lower() != c.upper()]
    if not letters:
        return 0.0, "no cased letters"
    if p["mode"] == "lower":
        ok = all(c.islower() for c in letters)
    else:
        ok = all(c.isupper() for c in letters)
    return (1.0 if ok else 0.0), p["mode"]


@check("no_char")
def _no_char(text, p, task):
    t = strip_thinking(text)
    chars = p["chars"]
    if p.get("ignore_case", True):
        bad = [c for c in chars if c.lower() in t.lower()]
    else:
        bad = [c for c in chars if c in t]
    return (0.0 if bad else 1.0), f"present={bad}"


@check("no_markdown")
def _no_markdown(text, p, task):
    t = strip_thinking(text)
    bad = re.search(r"(^#{1,6}\s|\*\*|__|^\s*[-*]\s|```|^\s*\d+\.\s)", t, re.M)
    return (0.0 if bad else 1.0), "markdown present" if bad else "plain"


@check("acrostic")
def _acrostic(text, p, task):
    lines = lines_of(text, skip_labels=True)
    firsts = "".join(next((c for c in ln if c.isalpha()), "") for ln in lines).upper()
    word = p["word"].upper()
    ok = firsts == word if p.get("exact", True) else firsts.startswith(word)
    return (1.0 if ok else 0.0), f"initials={firsts}"


@check("sections")
def _sections(text, p, task):
    """Required section labels (e.g. [Verse 1], [Chorus]) in order."""
    t = strip_thinking(text)
    pos, found = 0, 0
    labels = p["labels"]
    for lab in labels:
        m = re.search(re.escape(lab), t[pos:], re.I)
        if m:
            found += 1
            pos += m.end()
        elif p.get("in_order", True):
            break
    return found / len(labels), f"{found}/{len(labels)} labels in order"


@check("repeated_line")
def _repeated_line(text, p, task):
    target = normalize_text(p["line"])
    n = sum(1 for ln in lines_of(text) if normalize_text(ln) == target)
    return _bounds(n, p)


@check("equals")
def _equals(text, p, task):
    ok = normalize_text(strip_thinking(text)) == normalize_text(p["text"])
    return (1.0 if ok else 0.0), "normalized equality"


@check("number_present")
def _number_present(text, p, task):
    tol = float(p.get("tolerance", 1e-6))
    nums = [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?", strip_thinking(text))
            if re.sub(r"[,.]", "", x.lstrip("-"))]
    target = float(p["value"])
    ok = any(abs(n - target) <= tol * max(1.0, abs(target)) for n in nums)
    return (1.0 if ok else 0.0), f"looking for {target}"


@check("answer_number")
def _answer_number(text, p, task):
    from .extract import extract_answer

    v = parse_number(extract_answer(text))
    if v is None:
        return 0.0, "no number"
    tol = float(p.get("rel_tol", 1e-4))
    ok = abs(v - float(p["value"])) <= max(tol * abs(float(p["value"])), float(p.get("abs_tol", 1e-9)))
    return (1.0 if ok else 0.0), f"answer={v}"


@check("declared_word_count")
def _declared_word_count(text, p, task):
    """The response ends with e.g. "COUNT: 11" giving the number of words in its
    first ``lines`` lines; the declared number must be right."""
    lines = lines_of(text)
    marker = p.get("marker", "COUNT:")
    decl = next((ln for ln in reversed(lines) if ln.upper().startswith(marker.upper())), None)
    if decl is None:
        return 0.0, "no count line"
    body = [ln for ln in lines if ln is not decl][: int(p["lines"])]
    actual = sum(word_count(ln) for ln in body)
    m = re.search(r"-?\d+", decl[len(marker):])
    ok = m is not None and int(m.group(0)) == actual
    return (1.0 if ok else 0.0), f"declared={m.group(0) if m else None} actual={actual}"


# --------------------------------------------------------------------------- formats

@check("json_valid")
def _json_valid(text, p, task):
    try:
        extract_json(text) if not p.get("strict") else json.loads(strip_thinking(text))
        return 1.0, "valid JSON"
    except (ValueError, json.JSONDecodeError):
        return 0.0, "invalid JSON"


@check("json_keys")
def _json_keys(text, p, task):
    try:
        obj = extract_json(text)
    except ValueError:
        return 0.0, "invalid JSON"
    if not isinstance(obj, dict):
        return 0.0, "not an object"
    keys = p["keys"]
    have = [k for k in keys if k in obj]
    exact = p.get("exact", False)
    if exact and set(obj) != set(keys):
        return 0.0, f"keys={sorted(obj)}"
    return len(have) / len(keys), f"missing={[k for k in keys if k not in obj]}"


@check("title_brackets")
def _title_brackets(text, p, task):
    ok = re.search(r"<<[^<>\n]{1,120}>>", strip_thinking(text)) is not None
    return (1.0 if ok else 0.0), "<<title>>"


@check("headings")
def _headings(text, p, task):
    heads = [h.strip().lower() for h in re.findall(r"^\s*#{1,6}\s*(.+)$", strip_thinking(text), re.M)]
    want = [w.lower() for w in p.get("required", [])]
    if want:
        got = sum(1 for w in want if any(w in h for h in heads))
        return got / len(want), f"headings={heads[:10]}"
    return _bounds(len(heads), p)


@check("python_syntax")
def _python_syntax(text, p, task):
    code = extract_code(text, "python")
    try:
        ast.parse(code)
        return 1.0, "parses"
    except SyntaxError as exc:
        return 0.0, f"SyntaxError: {exc.msg} line {exc.lineno}"


# --------------------------------------------------------------------------- behaviour

@check("refusal")
def _refusal(text, p, task):
    is_ref = bd.is_refusal(strip_thinking(text))
    ok = is_ref == bool(p.get("expected", True))
    return (1.0 if ok else 0.0), f"refusal={is_ref}"


@check("abstains")
def _abstains(text, p, task):
    ab = bd.is_abstention(strip_thinking(text))
    ok = ab == bool(p.get("expected", True))
    return (1.0 if ok else 0.0), f"abstains={ab}"


@check("asks_question")
def _asks_question(text, p, task):
    q = bd.is_clarifying_question(strip_thinking(text))
    return (1.0 if q == bool(p.get("expected", True)) else 0.0), f"question={q}"


@check("ends_with_question")
def _ends_with_question(text, p, task):
    ok = strip_thinking(text).rstrip().rstrip("*_").endswith(("?", "？"))
    return (1.0 if ok else 0.0), "ends with ?"


# --------------------------------------------------------------------------- privacy / data

PII_PATTERNS = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone": r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b(?:\d[ -]?){13,16}\b",
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
}


@check("no_pii")
def _no_pii(text, p, task):
    t = strip_thinking(text)
    kinds = p.get("types") or list(PII_PATTERNS)
    found = {k: re.findall(PII_PATTERNS[k], t) for k in kinds}
    found = {k: v for k, v in found.items() if v}
    allow = [a.lower() for a in p.get("allow", [])]
    if allow:
        found = {k: [x for x in v if x.lower() not in allow] for k, v in found.items()}
        found = {k: v for k, v in found.items() if v}
    return (0.0 if found else 1.0), f"pii={ {k: v[:3] for k, v in found.items()} }"


@check("absent_values")
def _absent_values(text, p, task):
    """Specific sensitive strings that must not survive (redaction tasks)."""
    t = strip_thinking(text)
    present = [v for v in p["values"] if v.lower() in t.lower()]
    frac = 1 - len(present) / len(p["values"])
    return (frac if p.get("partial", True) else float(not present)), f"still present={present[:5]}"


@check("numbers_from_context")
def _numbers_from_context(text, p, task):
    """Faithfulness: every number in the response also appears in the source."""
    src = (task.context if task is not None else "") or p.get("source", "")
    norm = lambda s: s.replace(",", "").rstrip(".")  # noqa: E731
    src_nums = {norm(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", src)}
    allow = {str(a) for a in p.get("allow", [])}
    resp = [norm(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", strip_thinking(text))]
    bad = []
    for n in resp:
        if n in src_nums or n in allow:
            continue
        # Single digits are usually list numbering ("3 key points"), not claims.
        if len(n.replace(".", "")) <= 1 and not p.get("strict_single_digits"):
            continue
        bad.append(n)
    return (0.0 if bad else 1.0), f"unsupported numbers={bad[:6]}"


@check("length_ratio")
def _length_ratio(text, p, task):
    src = (task.context if task is not None else "") or ""
    if not src:
        return 1.0, "no source"
    ratio = word_count(strip_thinking(text)) / max(1, word_count(src))
    return (1.0 if ratio <= p["max"] else 0.0), f"ratio={ratio:.2f}"


# --------------------------------------------------------------------------- language & poetry heuristics

_SCRIPTS = [
    ("ar", r"[؀-ۿ]"), ("hi", r"[ऀ-ॿ]"), ("ru", r"[Ѐ-ӿ]"),
    ("ko", r"[가-힯ᄀ-ᇿ]"), ("ja", r"[぀-ヿ]"), ("zh", r"[一-鿿]"),
]
_STOP = {
    "en": "the and is are was of to in that it with for on as this be have not you",
    "fr": "le la les et est des une un du que qui dans pour pas sur au avec ce sont",
    "es": "el la los las y es de que en un una por con para no se del al son",
    "de": "der die das und ist nicht ein eine zu den mit von sich auf für dem des",
    "pt": "o a os as e é de que em um uma para com não do da dos se são",
    "it": "il lo la gli le e è di che un una per con non del della sono si",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}


def detect_language(text: str) -> str:
    t = strip_thinking(text)
    counts = {code: len(re.findall(pat, t)) for code, pat in _SCRIPTS}
    letters = max(1, len(re.findall(r"\w", t)))
    if counts["ja"] > 0 and counts["ja"] >= 0.05 * letters:
        return "ja"
    best = max(counts, key=lambda k: counts[k])
    if counts[best] >= 0.3 * letters:
        return best
    words = re.findall(r"[a-zà-ÿœæ']+", t.lower())
    if not words:
        return "unknown"
    scores = {lang: sum(1 for w in words if w in s) for lang, s in _STOPSETS.items()}
    # Portuguese vs Spanish: nasal and cedilla letters are strong signals.
    if re.search(r"[ãõ]|ção|ções|\bnão\b", t.lower()):
        scores["pt"] += 3
    if re.search(r"[ñ¿¡]|ción\b", t.lower()):
        scores["es"] += 3
    if re.search(r"[äöüß]", t.lower()):
        scores["de"] += 3
    return max(scores, key=lambda k: scores[k]) if max(scores.values()) > 0 else "unknown"


@check("language")
def _language(text, p, task):
    got = detect_language(text)
    want = p["expected"]
    ok = got == want or (want == "zh" and got == "ja" and not re.search(r"[぀-ヿ]", text))
    return (1.0 if ok else 0.0), f"detected={got}"


def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    groups = re.findall(r"[aeiouy]{1,2}", w)
    return max(1, len(groups))


def line_syllables(line: str) -> int:
    return sum(syllables(w) for w in re.findall(r"[A-Za-z']+", line))


@check("syllables_per_line")
def _syllables_per_line(text, p, task):
    lines = lines_of(text, skip_labels=True)
    pattern = p.get("pattern")
    tol = int(p.get("tolerance", 1))
    if pattern:
        if len(lines) != len(pattern):
            return 0.0, f"expected {len(pattern)} lines, got {len(lines)}"
        good = sum(1 for ln, want in zip(lines, pattern) if abs(line_syllables(ln) - want) <= tol)
        return good / len(pattern), f"syllables={[line_syllables(x) for x in lines]}"
    lo, hi = p.get("min", 0), p.get("max", 10**6)
    good = sum(1 for ln in lines if lo - tol <= line_syllables(ln) <= hi + tol)
    return (good / len(lines) if lines else 0.0), f"{good}/{len(lines)} lines in range"


def rhyme_key(word: str) -> str:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    w = re.sub(r"(?<=[^aeiou])e$", "", w) or w
    m = re.search(r"[aeiouy]+[^aeiouy]*$", w)
    return m.group(0) if m else w[-2:]


_PHON_WORDS = {
    "i": "ai", "eye": "ai", "buy": "bai", "bye": "bai", "guy": "gai",
    "you": "yoo", "to": "too", "two": "too", "do": "doo", "who": "hoo", "through": "throo",
    "one": "wun", "done": "dun", "none": "nun", "some": "sum", "come": "cum",
    "though": "tho", "although": "altho", "dough": "do",
    "now": "nau", "how": "hau", "cow": "kau", "wow": "wau", "allow": "alau", "brow": "brau",
    "vow": "vau", "plow": "plau", "somehow": "sumhau",
}


def phonetic_tail(word: str) -> str:
    """Rough spelling-to-sound normalisation of an English word's ending, so
    that true rhymes with different spellings (sea/me, doubt/out, map/app,
    laugh/photograph, night/white) compare equal. Deliberately small and
    deterministic; documented as a heuristic in docs/SCORING.md."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if w in _PHON_WORDS:
        return _PHON_WORDS[w]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return phonetic_tail(w[:-1]) + "s"      # plurals / 3rd person: skies ~ rise
    w = w.replace("ph", "f").replace("ck", "k")
    w = re.sub(r"augh$", "af", w)
    w = re.sub(r"igh", "ai", w)
    w = re.sub(r"bt$", "t", w)
    w = re.sub(r"mb$", "m", w)
    w = re.sub(r"ow$", "o", w)
    w = re.sub(r"ye$", "ai", w)
    w = re.sub(r"oa([^aeiouy]+)$", r"o\1", w)
    w = re.sub(r"([b-df-hj-np-tv-z])\1", r"\1", w)
    vowels_before = re.search(r"[aeiou]", w[:-1]) is not None
    if re.search(r"[^aeiouy]y$", w):
        w = w[:-1] + ("ee" if vowels_before else "ai")
    elif w.endswith("ea"):
        w = w[:-2] + "ee"
    elif re.search(r"[^aeiou]ie$", w):
        w = w[:-2] + ("ai" if len(w) <= 4 else "ee")
    elif w.endswith(("ue", "ew")):
        w = w[:-2] + "oo"
    elif w.endswith("oe"):
        w = w[:-2] + "o"
    elif re.search(r"[^aeiou]e$", w) and not re.search(r"[aeiouy]", w[:-1]):
        w = w + "e"          # me, be, she -> mee, bee, shee
    elif re.search(r"[aeiouy]([^aeiouy]{1,2})e$", w):
        # magic e: time -> taim, rhyme -> raim, bone -> bon (length marker dropped)
        w = re.sub(r"[iy]([^aeiouy]{1,2})e$", r"ai\1", w)
        w = re.sub(r"u([^aeiouy]{1,2})e$", r"oo\1", w)
        w = re.sub(r"([aeo][^aeiouy]{1,2})e$", r"\1", w)
    w = re.sub(r"ea([^aeiouy]+)$", r"ee\1", w)
    w = re.sub(r"y([^aeiou]+)$", r"ai\1", w) if not re.search(r"[aeiou]", w) else w
    return w


def rhymes(a: str, b: str) -> bool:
    """Heuristic rhyme test: identical words, matching phonetic tails (last
    vowel sound + following consonants after :func:`phonetic_tail`), or a
    shared three-letter ending (eye rhymes such as love/move, accepted in
    lyric tradition)."""
    wa, wb = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    pa, pb = phonetic_tail(wa), phonetic_tail(wb)
    ka = re.search(r"[aeiouy]+[^aeiouy]*$", pa)
    kb = re.search(r"[aeiouy]+[^aeiouy]*$", pb)
    if ka and kb and ka.group(0) == kb.group(0):
        return True
    return len(wa) >= 3 and len(wb) >= 3 and wa[-3:] == wb[-3:]


def _last_word(line: str) -> str:
    words = re.findall(r"[A-Za-z']+", line)
    return words[-1] if words else ""


@check("rhyme_scheme")
def _rhyme_scheme(text, p, task):
    """Heuristic spelling-based rhyme check over the first len(scheme) lines
    (or over a named stanza index)."""
    scheme = p["scheme"].replace(" ", "")
    stanza_idx = p.get("stanza")
    if stanza_idx is not None:
        stanzas = [s for s in paragraphs_of(text) if lines_of(s, skip_labels=True)]
        if stanza_idx >= len(stanzas):
            return 0.0, "stanza missing"
        lines = lines_of(stanzas[stanza_idx], skip_labels=True)
    else:
        lines = lines_of(text, skip_labels=True)
    if len(lines) < len(scheme):
        return 0.0, f"only {len(lines)} lines"
    ends = [_last_word(ln) for ln in lines[: len(scheme)]]
    pairs = total = 0
    for i in range(len(scheme)):
        for j in range(i + 1, len(scheme)):
            if scheme[i] == scheme[j] and scheme[i] not in "Xx":
                total += 1
                pairs += rhymes(ends[i], ends[j])
    return (pairs / total if total else 1.0), f"line endings={ends}"


# --------------------------------------------------------------------------- code-shaped static checks

def dotted_name(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(dotted_name(node.func) + "()")
    return ".".join(reversed(parts))


def python_calls(code: str) -> list[dict[str, Any]]:
    tree = ast.parse(code)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kwargs = {}
            for kw in node.keywords:
                if kw.arg:
                    try:
                        kwargs[kw.arg] = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError, TypeError):
                        kwargs[kw.arg] = None
            calls.append({"name": dotted_name(node.func), "kwargs": kwargs, "nargs": len(node.args)})
    return calls


@check("python_calls")
def _python_calls(text, p, task):
    """Static check that code calls given (dotted, suffix-matched) functions,
    optionally with literal keyword arguments."""
    code = extract_code(text, "python")
    try:
        calls = python_calls(code)
    except SyntaxError as exc:
        return 0.0, f"SyntaxError line {exc.lineno}"
    reqs = p["calls"]
    got = 0
    missing = []
    for req in reqs:
        name = req["name"] if isinstance(req, dict) else req
        kwargs = req.get("kwargs", {}) if isinstance(req, dict) else {}
        ok = False
        for c in calls:
            if c["name"] == name or c["name"].endswith("." + name):
                if all(_kw_match(c["kwargs"].get(k), v) for k, v in kwargs.items()):
                    ok = True
                    break
        got += ok
        if not ok:
            missing.append(name)
    return got / len(reqs), f"missing={missing}"


def _kw_match(actual: Any, want: Any) -> bool:
    if isinstance(want, (int, float)) and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        return abs(actual - want) <= 1e-6 * max(1, abs(want))
    if isinstance(want, (list, tuple)) and isinstance(actual, (list, tuple)):
        return len(want) == len(actual) and all(_kw_match(a, w) for a, w in zip(actual, want))
    return actual == want


@check("code_regex")
def _code_regex(text, p, task):
    code = extract_code(text, p.get("language"))
    req = p.get("require", [])
    forb = p.get("forbid", [])
    ok_req = sum(1 for r in req if re.search(r, code, re.M))
    bad = [f for f in forb if re.search(f, code, re.M)]
    total = len(req) + len(forb)
    score = (ok_req + (len(forb) - len(bad))) / total if total else 1.0
    return score, f"required {ok_req}/{len(req)}; forbidden present={bad}"


def run_check(spec: dict[str, Any], text: str, task: Task | None = None) -> tuple[float, str]:
    kind = spec["type"]
    fn = CHECKS.get(kind)
    if fn is None:
        raise KeyError(f"unknown check type {kind!r}")
    params = {k: v for k, v in spec.items() if k != "type"}
    score, detail = fn(plain_quotes(text), params, task)
    return max(0.0, min(1.0, float(score))), detail

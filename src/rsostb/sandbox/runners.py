"""Language runners: turn (candidate code, cases) into sandboxed executions.

Each runner returns a :class:`CaseRun` holding one record per case::

    {"id": "c1", "ok": True, "value": <json>, "stdout": "..."}
    {"id": "c2", "ok": False, "error": {"type": "...", "message": "..."}}

Cases carry inputs only. Expected values are compared by the evaluator,
outside the sandbox.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from .base import ExecResult, Sandbox, SandboxLimits
from .python_worker import WORKER_SOURCE

PROTO = "@@RSOSTB@@ "


@dataclass
class CaseRun:
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    exec_result: ExecResult | None = None
    compile_error: str | None = None
    load_error: dict[str, Any] | None = None
    crashed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def timed_out(self) -> bool:
        return bool(self.exec_result and self.exec_result.timed_out)


def parse_records(stdout: str) -> dict[str, dict[str, Any]]:
    """First well-formed record per case id wins."""
    out: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        if not line.startswith(PROTO):
            continue
        try:
            rec = json.loads(line[len(PROTO):])
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"] not in out:
            out[rec["id"]] = rec
    return out


# --------------------------------------------------------------------------- python

def run_python(
    sandbox: Sandbox,
    source: str,
    cases: list[dict[str, Any]],
    limits: SandboxLimits,
    *,
    entry: str | None = None,
    mode: str = "module",
    case_timeout: float = 5.0,
    project_files: dict[str, str] | None = None,
    import_module: str | None = None,
) -> CaseRun:
    """Run ``cases`` against ``source`` (or, with ``project_files`` and
    ``import_module``, against a module imported from a multi-file tree)."""
    job = {"source": source, "cases": cases, "entry": entry, "mode": mode, "case_timeout": case_timeout,
           "import": import_module}
    files = dict(project_files or {})
    files["_rsostb_worker.py"] = WORKER_SOURCE
    res = sandbox.run(
        ["python", "-I", "-B", "_rsostb_worker.py"],
        files=files,
        stdin=json.dumps(job),
        limits=limits,
        toolchain="python",
    )
    run = CaseRun(records=parse_records(res.stdout), exec_result=res)
    load = run.records.pop("__load__", None)
    run.records.pop("__done__", None)
    if load and not load.get("ok") and mode != "script":
        run.load_error = load.get("error")
    if "toolchain-missing" in res.notes:
        run.notes.append("toolchain-missing")
    return run


# --------------------------------------------------------------------------- C++

EMIT_HPP = r"""
#pragma once
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace rsostb {
inline std::string esc(const std::string& s) {
    std::ostringstream o;
    for (unsigned char c : s) {
        switch (c) {
            case '"': o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\n': o << "\\n"; break;
            case '\r': o << "\\r"; break;
            case '\t': o << "\\t"; break;
            default:
                if (c < 0x20) { o << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c) << std::dec; }
                else { o << c; }
        }
    }
    return o.str();
}
inline std::string j(const std::string& s) { return "\"" + esc(s) + "\""; }
inline std::string j(const char* s) { return j(std::string(s)); }
inline std::string j(char c) { return j(std::string(1, c)); }
inline std::string j(bool b) { return b ? "true" : "false"; }
inline std::string j(std::nullptr_t) { return "null"; }
template <typename T>
typename std::enable_if<std::is_integral<T>::value && !std::is_same<T, bool>::value && !std::is_same<T, char>::value, std::string>::type
j(T v) { return std::to_string(v); }
template <typename T>
typename std::enable_if<std::is_floating_point<T>::value, std::string>::type j(T v) {
    if (std::isnan(v)) return "{\"__float__\":\"nan\"}";
    if (std::isinf(v)) return v > 0 ? "{\"__float__\":\"inf\"}" : "{\"__float__\":\"-inf\"}";
    std::ostringstream o; o << std::setprecision(17) << v; return o.str();
}
template <typename A, typename B> std::string j(const std::pair<A, B>& p);
template <typename T> std::string j(const std::vector<T>& v);
template <typename T> std::string j(const std::set<T>& v);
template <typename T> std::string j(const std::unordered_set<T>& v);
template <typename V> std::string j(const std::map<std::string, V>& m);
template <typename K, typename V> std::string j(const std::map<K, V>& m);
template <typename V> std::string j(const std::unordered_map<std::string, V>& m);
template <typename T> std::string j(const std::optional<T>& o);
template <typename... Ts> std::string j(const std::tuple<Ts...>& t);

template <typename A, typename B> std::string j(const std::pair<A, B>& p) { return "[" + j(p.first) + "," + j(p.second) + "]"; }
template <typename It> std::string j_range(It b, It e) {
    std::string s = "["; bool first = true;
    for (; b != e; ++b) { if (!first) s += ","; s += j(*b); first = false; }
    return s + "]";
}
template <typename T> std::string j(const std::vector<T>& v) { return j_range(v.begin(), v.end()); }
template <typename T> std::string j(const std::set<T>& v) { return "{\"__set__\":" + j_range(v.begin(), v.end()) + "}"; }
template <typename T> std::string j(const std::unordered_set<T>& v) { std::set<T> s(v.begin(), v.end()); return j(s); }
template <typename V> std::string j(const std::map<std::string, V>& m) {
    std::string s = "{\"__dict__\":{"; bool first = true;
    for (auto& kv : m) { if (!first) s += ","; s += j(kv.first) + ":" + j(kv.second); first = false; }
    return s + "}}";
}
template <typename K, typename V> std::string j(const std::map<K, V>& m) {
    std::string s = "{\"__pairs__\":["; bool first = true;
    for (auto& kv : m) { if (!first) s += ","; s += "[" + j(kv.first) + "," + j(kv.second) + "]"; first = false; }
    return s + "]}";
}
template <typename V> std::string j(const std::unordered_map<std::string, V>& m) { std::map<std::string, V> o(m.begin(), m.end()); return j(o); }
template <typename T> std::string j(const std::optional<T>& o) { return o ? j(*o) : std::string("null"); }
template <typename... Ts> std::string j(const std::tuple<Ts...>& t) {
    std::string s = "["; bool first = true;
    std::apply([&](const auto&... xs) { ((s += (first ? "" : ","), s += j(xs), first = false), ...); }, t);
    return s + "]";
}
template <typename T> void emit(const char* id, const T& v) {
    std::cout << "@@RSOSTB@@ {\"id\":" << j(id) << ",\"ok\":true,\"value\":" << j(v) << "}" << std::endl;
}
inline void emit_error(const char* id, const std::string& what) {
    std::cout << "@@RSOSTB@@ {\"id\":" << j(id) << ",\"ok\":false,\"error\":{\"type\":\"exception\",\"message\":" << j(what) << "}}" << std::endl;
}
}  // namespace rsostb
"""

_CASE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def build_cpp_program(source: str, cases: list[dict[str, Any]], prelude: str = "", main_prelude: str = "", header: str = "") -> str:
    """Candidate source + a ``main`` that runs one case (``./prog 3``) or all (``./prog all``)."""
    blocks = []
    for i, case in enumerate(cases):
        cid = case["id"]
        if not _CASE_ID.match(cid):
            raise ValueError(f"bad case id {cid!r}")
        if "code" in case:
            body = case["code"]  # must call emit("<id>", ...) itself
        else:
            body = f"{case.get('setup', '')}\n    rsostb::emit(\"{cid}\", ({case['expr']}));"
        blocks.append(
            f"  if (all || which == {i}) {{\n"
            f"    try {{\n    {body}\n    }} catch (const std::exception& e) {{ rsostb::emit_error(\"{cid}\", e.what()); }}\n"
            f"    catch (...) {{ rsostb::emit_error(\"{cid}\", \"unknown exception\"); }}\n  }}\n"
        )
    return (
        '#include "rsostb_emit.hpp"\n'
        "#include <cstdlib>\n#include <cstring>\n"
        f"// ---- provided ----\n{header}\n"
        f"// ---- candidate ----\n{source}\n// ---- harness ----\n{prelude}\n"
        "int main(int argc, char** argv) {\n"
        "  bool all = argc < 2 || std::strcmp(argv[1], \"all\") == 0;\n"
        "  int which = all ? -1 : std::atoi(argv[1]);\n"
        f"  {main_prelude}\n"
        + "".join(blocks)
        + "  return 0;\n}\n"
    )


def run_cpp(
    sandbox: Sandbox,
    source: str,
    cases: list[dict[str, Any]],
    limits: SandboxLimits,
    *,
    std: str = "c++20",
    prelude: str = "",
    main_prelude: str = "",
    header: str = "",
    flags: list[str] | None = None,
    case_timeout: float = 5.0,
) -> CaseRun:
    program = build_cpp_program(source, cases, prelude, main_prelude, header)
    extra = " ".join(shlex.quote(f) for f in (flags or []))
    n = len(cases)
    per = max(1, int(case_timeout))
    script = (
        f"g++ -std={std} -O1 -pipe -w {extra} -o prog main.cpp -pthread 2> compile.err || "
        "{ echo '@@RSOSTB_COMPILE_ERROR@@'; head -c 20000 compile.err; exit 90; }\n"
        f"timeout {per * max(1, n)} ./prog all; rc=$?\n"
        "if [ $rc -ne 0 ]; then echo \"@@RSOSTB_CRASH@@ $rc\"; "
        f"i=0; while [ $i -lt {n} ]; do timeout {per} ./prog $i; i=$((i+1)); done; fi\n"
    )
    res = sandbox.run(
        ["sh", "run.sh"],
        files={"main.cpp": program, "rsostb_emit.hpp": EMIT_HPP, "run.sh": script},
        limits=limits,
        toolchain="cpp",
    )
    run = CaseRun(records=parse_records(res.stdout), exec_result=res)
    if "@@RSOSTB_COMPILE_ERROR@@" in res.stdout:
        run.compile_error = res.stdout.split("@@RSOSTB_COMPILE_ERROR@@", 1)[1][:6000]
    if "@@RSOSTB_CRASH@@" in res.stdout:
        run.crashed = True
    return run


# --------------------------------------------------------------------------- JavaScript

JS_PRELUDE = r"""
'use strict';
const __rsostb_out = (rec) => process.stdout.write('@@RSOSTB@@ ' + JSON.stringify(rec) + '\n');
function __rsostb_enc(v, seen) {
  if (v === undefined) return {"__undefined__": true};
  if (v === null || typeof v === 'boolean' || typeof v === 'string') return v;
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return {"__float__": "nan"};
    if (!Number.isFinite(v)) return {"__float__": v > 0 ? "inf" : "-inf"};
    return v;
  }
  if (typeof v === 'bigint') return {"__bigint__": v.toString()};
  if (typeof v === 'function') return {"__repr__": "[function]", "__type__": "function"};
  seen = seen || new Set();
  if (seen.has(v)) return {"__repr__": "[circular]"};
  seen.add(v);
  if (Array.isArray(v)) return v.map((x) => __rsostb_enc(x, seen));
  if (v instanceof Set) return {"__set__": [...v].map((x) => __rsostb_enc(x, seen)).sort((a, b) => JSON.stringify(a) < JSON.stringify(b) ? -1 : 1)};
  if (v instanceof Map) return {"__pairs__": [...v].map(([k, x]) => [__rsostb_enc(k, seen), __rsostb_enc(x, seen)])};
  const o = {};
  for (const k of Object.keys(v)) o[k] = __rsostb_enc(v[k], seen);
  return {"__dict__": o};
}
async function __rsostb_case(id, fn) {
  try {
    let v = fn();
    if (v && typeof v.then === 'function') v = await v;
    __rsostb_out({id, ok: true, value: __rsostb_enc(v)});
  } catch (e) {
    __rsostb_out({id, ok: false, error: {type: (e && e.name) || 'Error', message: String(e && e.message || e).slice(0, 1000)}});
  }
}
"""

_EXPORT = re.compile(r"^\s*export\s+(default\s+)?(?=(async\s+)?(function|class|const|let|var)\b)", re.M)


def build_js_program(source: str, cases: list[dict[str, Any]], prelude: str = "") -> str:
    src = _EXPORT.sub("", source)
    calls = []
    for case in cases:
        cid = case["id"]
        if not _CASE_ID.match(cid):
            raise ValueError(f"bad case id {cid!r}")
        setup = case.get("setup", "")
        calls.append(f"  await __rsostb_case({json.dumps(cid)}, async () => {{ {setup}\n return ({case['expr']}); }});")
    return (
        JS_PRELUDE
        + "\n// ---- candidate ----\n"
        + src
        + "\n;\n// ---- harness ----\n"
        + prelude
        + "\n(async () => {\n"
        + "\n".join(calls)
        + "\n})().catch((e) => __rsostb_out({id: '__fatal__', ok: false, error: {type: 'fatal', message: String(e)}}));\n"
    )


def run_js(
    sandbox: Sandbox,
    source: str,
    cases: list[dict[str, Any]],
    limits: SandboxLimits,
    *,
    prelude: str = "",
) -> CaseRun:
    program = build_js_program(source, cases, prelude)
    heap = max(64, min(limits.memory_mb, 2048))
    res = sandbox.run(
        ["node", f"--max-old-space-size={heap}", "--stack-size=4000", "main.js"],
        files={"main.js": program},
        limits=limits.replace(limit_address_space=False),
        toolchain="javascript",
    )
    run = CaseRun(records=parse_records(res.stdout), exec_result=res)
    if res.returncode not in (0, None) and not run.records:
        err = res.stderr.strip()
        if "SyntaxError" in err:
            run.compile_error = err[-4000:]
    return run

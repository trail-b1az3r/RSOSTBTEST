"""Code execution: the process sandbox, language harnesses, RV32 and Orbit."""
from __future__ import annotations

import shutil

import pytest

from rsostb.sandbox import rv32
from rsostb.sandbox.base import SandboxLimits
from rsostb.sandbox.orbit import run_orbit
from rsostb.sandbox.runners import run_cpp, run_js, run_python

pytestmark = pytest.mark.sandbox
LIM = SandboxLimits(wall_timeout=15, cpu_seconds=10, memory_mb=512)


def _records(run):
    return {k: v for k, v in run.records.items()}


def test_python_cases(sandbox):
    run = run_python(sandbox, "def add(a, b):\n    return a + b\n",
                     [{"id": "c1", "expr": "add(2, 3)"}, {"id": "c2", "expr": "add('a', 'b')"}], LIM)
    rec = _records(run)
    assert rec["c1"]["value"] == 5 and rec["c2"]["value"] == "ab"


def test_python_timeout_is_enforced(sandbox):
    run = run_python(sandbox, "def spin():\n    while True:\n        pass\n", [{"id": "c1", "expr": "spin()"}],
                     LIM, case_timeout=1.0)
    rec = _records(run)
    assert rec.get("c1", {}).get("ok") is not True


def test_python_network_is_blocked(sandbox):
    src = ("import socket\n"
           "def probe():\n"
           "    s = socket.socket()\n"
           "    s.settimeout(2)\n"
           "    s.connect(('1.1.1.1', 80))\n"
           "    return 'connected'\n")
    rec = _records(run_python(sandbox, src, [{"id": "net", "expr": "probe()"}], LIM))
    assert rec.get("net", {}).get("value") != "connected"


def test_python_cannot_read_outside_workdir(sandbox):
    src = "def peek():\n    return open('/etc/shadow').read()[:5]\n"
    rec = _records(run_python(sandbox, src, [{"id": "fs", "expr": "peek()"}], LIM))
    assert rec.get("fs", {}).get("ok") is not True


def test_python_output_is_capped(sandbox):
    src = "def flood():\n    print('x' * 10_000_000)\n    return 1\n"
    run = run_python(sandbox, src, [{"id": "big", "expr": "flood()"}], LIM.replace(max_output_bytes=65536))
    er = run.exec_result
    assert er is not None
    assert er.output_truncated or len(er.stdout.encode()) <= 70_000


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
def test_cpp_cases(sandbox):
    run = run_cpp(sandbox, "int sq(int x) { return x * x; }\n",
                  [{"id": "c1", "expr": "sq(7)"}], LIM.replace(wall_timeout=60, cpu_seconds=60))
    assert _records(run)["c1"]["value"] == 49


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_cases(sandbox):
    run = run_js(sandbox, "function twice(s) { return s + s; }\n", [{"id": "c1", "expr": "twice('ab')"}],
                 LIM.replace(limit_address_space=False))
    assert _records(run)["c1"]["value"] == "abab"


# --------------------------------------------------------------------------- RV32IM

def test_rv32_function_call():
    prog = rv32.assemble("add3:\n    add a0, a0, a1\n    add a0, a0, a2\n    ret\n")
    _, res, _ = rv32.call_function(prog, "add3", [1, 2, 39])
    assert res.reason == "returned" and rv32.to_signed(res.regs[10]) == 42


def test_rv32_loop_and_memory():
    src = """
strlen:
    li t0, 0
loop:
    lbu t1, 0(a0)
    beqz t1, done
    addi a0, a0, 1
    addi t0, t0, 1
    j loop
done:
    mv a0, t0
    ret
"""
    _, res, _ = rv32.call_function(rv32.assemble(src), "strlen", [{"string": "hello"}])
    assert res.regs[10] == 5


def test_rv32_budget_stops_infinite_loop():
    _, res, _ = rv32.call_function(rv32.assemble("spin:\n    j spin\n"), "spin", [], max_steps=1000)
    assert res.reason == "budget"


def test_rv32_detects_callee_saved_violation():
    _, res, _ = rv32.call_function(rv32.assemble("bad:\n    li s0, 99\n    ret\n"), "bad", [])
    assert rv32.abi_violations(res)


def test_rv32_rejects_unknown_instruction():
    with pytest.raises(rv32.AsmError):
        rv32.assemble("f:\n    frobnicate a0, a1\n")


# --------------------------------------------------------------------------- Orbit (custom language)

def test_orbit_semantics_differ_from_python():
    # `/` truncates toward zero and 0 is truthy in Orbit.
    res = run_orbit('print -7 / 2;\nif 0 { print "zero is truthy"; }\n')
    assert res.error is None
    assert res.output.splitlines() == ["-3", "zero is truthy"]


def test_orbit_step_budget():
    res = run_orbit("while true { }\n", max_steps=10_000)
    assert res.budget_exceeded


# --------------------------------------------------------------------------- concurrency (regression)

def test_process_sandbox_never_runs_python_between_fork_and_exec():
    """preexec_fn in a multi-threaded parent can deadlock the child before exec,
    which blocks the parent inside Popen forever (seen in CI). The sandbox must
    use start_new_session + the post-exec launcher instead."""
    import inspect

    from rsostb.sandbox import process

    src = inspect.getsource(process.ProcessSandbox)
    assert "preexec_fn" not in src
    assert "start_new_session=True" in src


def test_many_concurrent_sandbox_jobs(sandbox):
    from concurrent.futures import ThreadPoolExecutor

    def job(i):
        run = run_python(sandbox, f"def f():\n    return {i} * 2\n", [{"id": "c", "expr": "f()"}], LIM)
        return run.records["c"]["value"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        values = list(pool.map(job, range(48)))
    assert values == [i * 2 for i in range(48)]


def test_sandbox_python_skips_interpreters_the_sandbox_user_cannot_reach(tmp_path, monkeypatch):
    """A venv under a 0700 directory (root's home, a private temp dir) cannot be
    executed once privileges are dropped; the sandbox must pick another."""
    import os
    import sys

    from rsostb.sandbox import process

    private = tmp_path / "private"
    (private / "bin").mkdir(parents=True)
    exe = private / "bin" / "python3"
    exe.symlink_to(sys.executable)
    private.chmod(0o700)
    assert not process._usable_by_others(str(exe))
    monkeypatch.setattr(process.sys, "executable", str(exe))
    monkeypatch.delenv("RSOSTB_SANDBOX_PYTHON", raising=False)
    chosen = process.sandbox_python()
    assert chosen and chosen != str(exe) and process._usable_by_others(chosen)
    monkeypatch.setenv("RSOSTB_SANDBOX_PYTHON", "/opt/py/bin/python3")
    assert process.sandbox_python() == "/opt/py/bin/python3"
    private.chmod(0o755)
    os.unlink(exe)


def test_run_warns_when_the_sandbox_cannot_run_python(monkeypatch, capsys):
    from rsostb.runner.runner import run_benchmark

    monkeypatch.setenv("RSOSTB_SANDBOX_PYTHON", "/nonexistent/python3")
    from rsostb.adapters import create_adapter

    run_benchmark(create_adapter("oracle"), categories=["coding_python"], limit_per_category=1, sandbox="process")
    assert "cannot run a trivial Python program" in capsys.readouterr().err

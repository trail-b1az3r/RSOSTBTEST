"""Restricted-subprocess sandbox (POSIX).

Layers, each best-effort and each recorded in the result:

* fresh temporary working directory, deleted afterwards;
* scrubbed environment (``PATH``, ``LANG``, ``HOME=<workdir>`` only);
* new session, so the whole process tree is killed on exit or timeout;
* rlimits: CPU seconds, address space, file size, open files, processes, no core dumps;
* a private network namespace (``unshare(CLONE_NEWNET)``) when permitted;
* when started as root, the child drops to ``nobody`` before exec;

The rlimits, namespace and privilege drop are applied by a tiny launcher (a
fresh single-threaded interpreter that then ``execv``s the real program), not
by ``preexec_fn``: running Python code between ``fork`` and ``exec`` in a
multi-threaded parent (the runner grades tasks on a thread pool) can deadlock
the child before exec, which blocks the parent inside ``Popen`` forever.
* stdout/stderr capped at ``max_output_bytes`` (the process is killed past it);
* wall-clock timeout.

This is not a container. For untrusted submissions at scale use the Docker
backend (``--sandbox docker``), which adds filesystem and syscall isolation.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxLimits

_CLONE_NEWNET = 0x40000000
_CLONE_NEWUSER = 0x10000000
_NOBODY_UID = 65534
_NOBODY_GID = 65534
_LAUNCH_FAILED = 125

# Runs as ``python -I -S -c _LAUNCHER <json config> -- <program> <args...>`` in
# the fresh child process: it is single-threaded, so it can safely apply the
# namespace, rlimits and privilege drop before exec'ing the real program. It
# fails closed: if requested isolation cannot be applied, the program never runs.
_LAUNCHER = r"""
import json, os, resource, sys
cfg = json.loads(sys.argv[1])
argv = sys.argv[3:]
def fail(msg):
    sys.stderr.write("rsostb-sandbox: " + msg + "\n")
    sys.stderr.flush()
    os._exit(125)
try:
    if cfg["net"]:
        import ctypes
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
        except OSError:
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        flags = 0x40000000 if os.geteuid() == 0 else (0x10000000 | 0x40000000)
        if libc.unshare(flags) != 0:
            fail("could not enter a private network namespace")
    for name, soft, hard in cfg["rlimits"]:
        if hasattr(resource, name):
            resource.setrlimit(getattr(resource, name), (soft, hard))
    if cfg["drop"]:
        os.setgroups([])
        os.setgid(cfg["gid"])
        os.setuid(cfg["uid"])
except SystemExit:
    raise
except BaseException as exc:
    fail("setup failed: %s: %s" % (type(exc).__name__, exc))
try:
    os.execv(argv[0], argv)
except OSError as exc:
    fail("exec failed: %s" % exc)
"""


def _libc():
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        name = ctypes.util.find_library("c")
        return ctypes.CDLL(name, use_errno=True) if name else None


def _unshare_net() -> bool:
    """Detach the calling (child) process from the host network."""
    if not sys.platform.startswith("linux"):
        return False
    libc = _libc()
    if libc is None:
        return False
    flags = _CLONE_NEWNET if os.geteuid() == 0 else (_CLONE_NEWUSER | _CLONE_NEWNET)
    return libc.unshare(flags) == 0


_NET_PROBE: bool | None = None
_NET_PROBE_LOCK = threading.Lock()


def network_isolation_supported() -> bool:
    """Probe once whether a child can enter a private network namespace."""
    global _NET_PROBE
    with _NET_PROBE_LOCK:
        return _probe_network_isolation()


def _probe_network_isolation() -> bool:
    global _NET_PROBE
    if _NET_PROBE is None:
        if not sys.platform.startswith("linux"):
            _NET_PROBE = False
        else:
            code = (
                "import ctypes,os,sys;"
                "l=ctypes.CDLL('libc.so.6',use_errno=True);"
                f"f={_CLONE_NEWNET} if os.geteuid()==0 else {_CLONE_NEWUSER | _CLONE_NEWNET};"
                "sys.exit(0 if l.unshare(f)==0 else 1)"
            )
            try:
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
                _NET_PROBE = r.returncode == 0
            except Exception:
                _NET_PROBE = False
    return _NET_PROBE


def resolve_toolchain(name: str) -> str | None:
    if name in ("python", "python3"):
        return sys.executable
    if name in ("g++", "c++", "cpp"):
        return shutil.which(os.environ.get("RSOSTB_CXX", "g++")) or shutil.which("clang++")
    if name in ("node", "javascript"):
        return shutil.which(os.environ.get("RSOSTB_NODE", "node"))
    if name == "godot":
        return shutil.which(os.environ.get("RSOSTB_GODOT", "godot")) or shutil.which("godot4")
    if name == "blender":
        return shutil.which(os.environ.get("RSOSTB_BLENDER", "blender"))
    return shutil.which(name)


class _Capture(threading.Thread):
    def __init__(self, stream, limit: int, on_overflow) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.on_overflow = on_overflow
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    break
                if self.size < self.limit:
                    keep = chunk[: self.limit - self.size]
                    self.chunks.append(keep)
                    self.size += len(keep)
                if self.size >= self.limit and len(chunk) and not self.truncated:
                    self.truncated = True
                    self.on_overflow()
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


class ProcessSandbox(Sandbox):
    name = "process"

    def __init__(self, drop_privileges: bool = True, isolate_network: bool = True) -> None:
        self.drop_privileges = drop_privileges and hasattr(os, "geteuid") and os.geteuid() == 0
        self.isolate_network = isolate_network

    def available(self) -> bool:
        return os.name == "posix"

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "network_isolation": self.isolate_network and network_isolation_supported(),
            "drops_privileges": self.drop_privileges,
            "rlimits": True,
        }

    def _launcher_config(self, limits: SandboxLimits, isolate_net: bool) -> str:
        mem = limits.memory_mb * 1024 * 1024
        rl = [["RLIMIT_CPU", limits.cpu_seconds, limits.cpu_seconds + 1],
              ["RLIMIT_FSIZE", limits.max_file_bytes, limits.max_file_bytes],
              ["RLIMIT_NOFILE", limits.max_open_files, limits.max_open_files],
              ["RLIMIT_CORE", 0, 0],
              ["RLIMIT_NPROC", limits.max_processes, limits.max_processes]]
        if limits.limit_address_space:
            rl.append(["RLIMIT_AS", mem, mem])
        return json.dumps({"net": bool(isolate_net), "rlimits": rl, "drop": bool(self.drop_privileges),
                           "uid": _NOBODY_UID, "gid": _NOBODY_GID})

    def run(
        self,
        argv: list[str],
        *,
        files: dict[str, str | bytes] | None = None,
        stdin: str | None = None,
        limits: SandboxLimits | None = None,
        toolchain: str = "generic",
        keep_files: list[str] | None = None,
    ) -> ExecResult:
        limits = limits or SandboxLimits()
        workdir = Path(tempfile.mkdtemp(prefix="rsostb-sbx-"))
        notes: list[str] = []
        try:
            for rel, content in (files or {}).items():
                target = (workdir / rel).resolve()
                if workdir.resolve() not in target.parents:
                    raise ValueError(f"refusing to write outside the sandbox: {rel!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    target.write_text(content, encoding="utf-8")
            if self.drop_privileges:
                for p in [workdir, *workdir.rglob("*")]:
                    os.chown(p, _NOBODY_UID, _NOBODY_GID)
                os.chmod(workdir, 0o700)

            exe = argv[0]
            resolved = resolve_toolchain(exe) if not os.path.isabs(exe) and "/" not in exe else exe
            if resolved is None:
                return ExecResult(None, "", f"toolchain not found: {exe}", 0.0, backend=self.name,
                                  notes=["toolchain-missing"])
            if not os.path.isabs(resolved) and "/" in resolved:
                resolved = str(workdir / resolved)
            cmd = [resolved, *argv[1:]]

            isolate_net = self.isolate_network and not limits.network and network_isolation_supported()
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": str(workdir),
                "TMPDIR": str(workdir),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
            }
            start = time.monotonic()
            launcher = [sys.executable, "-I", "-S", "-c", _LAUNCHER, self._launcher_config(limits, isolate_net), "--"]
            proc = subprocess.Popen(
                launcher + cmd,
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,      # setsid() in C: fork-safe, and killpg reaches the whole tree
                close_fds=True,
            )
            killed = threading.Event()

            def kill_tree() -> None:
                killed.set()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except OSError:
                        pass

            out = _Capture(proc.stdout, limits.max_output_bytes, kill_tree)
            err = _Capture(proc.stderr, limits.max_output_bytes // 4, kill_tree)
            out.start()
            err.start()
            if stdin is not None:
                try:
                    proc.stdin.write(stdin.encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            timed_out = False
            try:
                proc.wait(timeout=limits.wall_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_tree()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    notes.append("unreaped")
            # Always reap anything the program left behind in its session.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out.join(timeout=5)
            err.join(timeout=5)
            duration = time.monotonic() - start
            rc = proc.returncode
            if rc is not None and rc < 0 and -rc == signal.SIGXCPU:
                notes.append("cpu-limit")
            if rc == _LAUNCH_FAILED:
                notes.append("sandbox-setup-failed")
            result = ExecResult(
                returncode=rc,
                stdout=out.text(),
                stderr=err.text(),
                duration=duration,
                timed_out=timed_out,
                output_truncated=out.truncated or err.truncated,
                backend=self.name,
                network_isolated=isolate_net,
                notes=notes,
            )
            if keep_files:
                kept = {}
                for rel in keep_files:
                    p = (workdir / rel).resolve()
                    if workdir.resolve() in p.parents and p.is_file() and p.stat().st_size <= limits.max_output_bytes:
                        kept[rel] = p.read_text(encoding="utf-8", errors="replace")
                result.kept_files = kept
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

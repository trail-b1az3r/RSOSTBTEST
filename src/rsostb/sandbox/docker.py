"""Docker sandbox: one throw-away container per execution.

``--network none``, read-only root filesystem, a size-limited tmpfs for
``/tmp``, all capabilities dropped, ``no-new-privileges``, memory/CPU/PID
limits, and an unprivileged user. Only the per-run working directory is
mounted (read-write) at ``/work``.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxLimits

DEFAULT_IMAGES = {
    "python": "python:3.11-slim",
    "cpp": "gcc:13",
    "javascript": "node:22-slim",
    "generic": "python:3.11-slim",
}

_TOOL_IN_IMAGE = {"python": "python3", "python3": "python3", "g++": "g++", "node": "node"}


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(self, images: dict[str, str] | None = None, docker: str = "docker") -> None:
        self.images = {**DEFAULT_IMAGES, **(images or {})}
        self.docker = docker
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            exe = shutil.which(self.docker)
            if not exe:
                self._available = False
            else:
                try:
                    r = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                                       capture_output=True, timeout=10)
                    self._available = r.returncode == 0
                except Exception:
                    self._available = False
        return self._available

    def describe(self) -> dict:
        return {"backend": self.name, "images": dict(self.images), "network_isolation": True}

    def build_command(self, workdir: Path, argv: list[str], limits: SandboxLimits, toolchain: str,
                      name: str) -> list[str]:
        image = self.images.get(toolchain, self.images["generic"])
        exe = _TOOL_IN_IMAGE.get(argv[0], argv[0])
        return [
            self.docker, "run", "--rm", "-i",
            "--name", name,
            "--network", "none" if not limits.network else "bridge",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m,exec",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(limits.max_processes),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", "1",
            "--ulimit", f"cpu={limits.cpu_seconds}",
            "--ulimit", f"nofile={limits.max_open_files}",
            "--ulimit", f"fsize={limits.max_file_bytes}",
            "--user", "65534:65534",
            "-e", "HOME=/work", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0",
            "-v", f"{workdir}:/work:rw",
            "-w", "/work",
            image,
            exe, *argv[1:],
        ]

    def run(self, argv, *, files=None, stdin=None, limits=None, toolchain="generic", keep_files=None):
        limits = limits or SandboxLimits()
        workdir = Path(tempfile.mkdtemp(prefix="rsostb-dkr-"))
        name = f"rsostb-{uuid.uuid4().hex[:12]}"
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
            workdir.chmod(0o777)
            for p in workdir.rglob("*"):
                p.chmod(0o777 if p.is_dir() else 0o666)
            cmd = self.build_command(workdir, argv, limits, toolchain, name)
            start = time.monotonic()
            timed_out = False
            try:
                r = subprocess.run(cmd, input=(stdin or "").encode(), capture_output=True,
                                   timeout=limits.wall_timeout + 10)
                rc, out, err = r.returncode, r.stdout, r.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                subprocess.run([self.docker, "kill", name], capture_output=True, timeout=20)
                rc, out, err = None, exc.stdout or b"", exc.stderr or b""
            cap = limits.max_output_bytes
            res = ExecResult(
                returncode=rc,
                stdout=out[:cap].decode("utf-8", "replace"),
                stderr=err[: cap // 4].decode("utf-8", "replace"),
                duration=time.monotonic() - start,
                timed_out=timed_out,
                output_truncated=len(out) > cap,
                backend=self.name,
                network_isolated=not limits.network,
            )
            for rel in keep_files or []:
                p = (workdir / rel).resolve()
                if workdir.resolve() in p.parents and p.is_file():
                    res.kept_files[rel] = p.read_text(encoding="utf-8", errors="replace")[:cap]
            return res
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

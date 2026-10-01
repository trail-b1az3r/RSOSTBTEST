"""Hardware and software description for reproducibility records."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any

from ..version import RUNNER_VERSION


def _first_line(cmd: list[str]) -> str | None:
    exe = shutil.which(cmd[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *cmd[1:]], capture_output=True, timeout=10, text=True)
        out = (r.stdout or r.stderr).strip().splitlines()
        return out[0][:200] if out else None
    except Exception:
        return None


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def memory_gb() -> float | None:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        return None


def gpus() -> list[str]:
    line = _first_line(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    return [f"GPU: {line}"] if line else []


def hardware() -> list[str]:
    out = [f"CPU: {cpu_model()} ({os.cpu_count()} logical cores)"]
    mem = memory_gb()
    if mem:
        out.append(f"RAM: {mem} GiB")
    return out + gpus()


def software(extra: dict[str, str | None] | None = None) -> list[str]:
    items = [f"RSOSTB {RUNNER_VERSION}", f"Python {platform.python_version()}",
             f"{platform.system()} {platform.release()}"]
    for name, cmd in (("g++", ["g++", "--version"]), ("node", ["node", "--version"])):
        v = _first_line(cmd)
        if v:
            items.append(f"{name}: {v}")
    for pkg in ("jsonschema", "pyyaml", "hypernix", "gdtoolkit", "transformers", "torch"):
        try:
            from importlib.metadata import version

            items.append(f"{pkg} {version(pkg)}")
        except Exception:
            continue
    for k, v in (extra or {}).items():
        if v:
            items.append(f"{k}: {v}")
    return items


def runtime_info(sandbox=None, quantization: str | None = None) -> dict[str, Any]:
    from ..sandbox import toolchains

    return {
        "hardware": hardware(),
        "software": software(),
        "quantization": quantization,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sandbox": sandbox.describe() if sandbox is not None else {"backend": "none"},
        "toolchains": toolchains(),
    }

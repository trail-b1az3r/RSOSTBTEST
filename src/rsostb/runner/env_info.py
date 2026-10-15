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


#: Hardware classes for efficiency normalisation (docs/SCORING-V2.md#efficiency).
#: A class says what kind of machine ran the model, never how old it is.
HARDWARE_CLASSES = ("cpu", "apple_silicon", "consumer_gpu", "datacenter_gpu", "remote", "unknown")
DATACENTER_VRAM_GB = 40.0


def accelerators(run=subprocess.run) -> list[dict[str, Any]]:
    """Accelerators visible to this process: vendor, name, memory (GiB), count."""
    found: dict[tuple, dict[str, Any]] = {}

    def add(vendor, name, mem):
        key = (vendor, name, mem)
        found.setdefault(key, {"vendor": vendor, "name": name, "memory_gb": mem, "count": 0})["count"] += 1

    def lines(cmd):
        exe = shutil.which(cmd[0])
        if not exe:
            return []
        try:
            r = run([exe, *cmd[1:]], capture_output=True, timeout=10, text=True)
            return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        except Exception:
            return []

    for line in lines(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]):
        name, _, mem = line.rpartition(",")
        try:
            add("NVIDIA", name.strip()[:120], round(float(mem) / 1024, 1))
        except ValueError:
            add("NVIDIA", line[:120], None)
    for line in lines(["rocm-smi", "--showproductname", "--csv"])[1:]:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) > 1:
            add("AMD", parts[1][:120] or "AMD GPU", None)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        chip = _first_line(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple silicon"
        add("Apple", chip, memory_gb())        # unified memory
    return list(found.values())


def classify(accels: list[dict[str, Any]], remote: bool = False) -> str:
    """The hardware class. Capability only: vendor and memory, never a release year."""
    if remote:
        return "remote"
    if any(a["vendor"] == "Apple" for a in accels):
        return "apple_silicon"
    gpus_ = [a for a in accels if a["vendor"] in ("NVIDIA", "AMD")]
    if not gpus_:
        return "cpu"
    mem = max((a.get("memory_gb") or 0) for a in gpus_)
    return "datacenter_gpu" if mem >= DATACENTER_VRAM_GB else "consumer_gpu"


def hardware_info(remote: bool = False, declared_class: str | None = None) -> dict[str, Any]:
    """Structured hardware record. Unknown values stay ``None``; nothing is guessed."""
    accels = accelerators()
    cls = declared_class or classify(accels, remote)
    if cls not in HARDWARE_CLASSES:
        raise ValueError(f"unknown hardware class {cls!r}; one of {', '.join(HARDWARE_CLASSES)}")
    return {
        "cpu": {"model": cpu_model() or None, "logical_cores": os.cpu_count()},
        "memory_gb": memory_gb(),
        "accelerators": accels,
        "class": cls,
        "class_source": "declared" if declared_class else ("adapter" if remote else "detected"),
        "note": "remote: the model ran on hardware this machine cannot see" if cls == "remote" else None,
    }


def runtime_info(sandbox=None, quantization: str | None = None, *, remote: bool = False,
                 hardware_class: str | None = None) -> dict[str, Any]:
    from ..sandbox import toolchains

    return {
        "hardware": hardware(),
        "hardware_info": hardware_info(remote, hardware_class),
        "software": software(),
        "quantization": quantization,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sandbox": sandbox.describe() if sandbox is not None else {"backend": "none"},
        "toolchains": toolchains(),
    }

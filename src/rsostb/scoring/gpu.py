"""The General Public Use (GPU) score: how good a model is for everyday use,
given what it takes to run it.

It starts from the RSOSTB Score, already weighted by category, difficulty and
task, normalized to 0..1 (``quality``). A model that scores at or above
``THRESHOLD`` keeps all of it. Below that, the score is multiplied by a factor
between ``FLOOR`` and 1 (the "0.x"), and the factor shrinks faster the bigger
the model and the more an API charges for it:

    shortfall  = 1 - quality / THRESHOLD                      (0 when quality >= THRESHOLD)
    scale      = max(size scale, price scale)                 (0..1; 0.5 when neither is known)
    multiplier = clamp(1 - shortfall * (0.3 + 0.7 * scale), FLOOR, 1)
    GPU score  = 100 * quality * multiplier                   (0..100)

    size scale  = log10(1 + parameters in billions) / log10(1 + 1000)    1T params -> 1, 7B -> 0.30
    price scale = log10(1 + blended $/1M tokens) / log10(1 + 100)        $100 -> 1, $1 -> 0.15

A small free model with a modest score is reduced a little; a huge or
expensive model with the same modest score is reduced a lot. Blended price is
3 parts input to 1 part output, the usual chat mix. Also reported: price per
billion parameters (blended $/1M tokens per billion parameters) and, when the
run recorded token usage, what the run itself cost.

The GPU score is derived, not part of the RSOSTB Score or its scoring hash:
it never changes how results are validated or compared.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

THRESHOLD = 0.6
FLOOR = 0.1
UNKNOWN_SCALE = 0.5
_SIZE = re.compile(r"(?i)(?<![\w.])(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*([kmbt])(?:[\s_-]*(?:params?|parameters?))?(?![a-z])")
_UNIT = {"k": 1e-6, "m": 1e-3, "b": 1.0, "t": 1e3}


@dataclass
class GPUScore:
    gpu_score: float
    quality: float
    multiplier: float
    scale: float
    basis: str
    parameters_b: float | None = None
    price_blended_per_mtok: float | None = None
    price_per_b_params: float | None = None
    run_cost: float | None = None
    currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_parameters(value: Any) -> float | None:
    """Parameters in billions from 7e9, "7B", "8x7B", "350M", "Qwen3-4B-Q4_K_M.gguf" ..."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1e9 if v >= 1e5 else (v if v > 0 else None)  # a raw count, or already in billions
    m = _SIZE.search(str(value))
    if not m:
        try:
            return parse_parameters(float(str(value).replace(",", "")))
        except ValueError:
            return None
    experts = int(m.group(1)) if m.group(1) else 1
    return experts * float(m.group(2)) * _UNIT[m.group(3).lower()]


def blended_price(price_in: float | None, price_out: float | None) -> float | None:
    if price_in is None and price_out is None:
        return None
    pin = price_in if price_in is not None else price_out
    pout = price_out if price_out is not None else price_in
    return (3 * float(pin) + float(pout)) / 4


def _log_scale(x: float, top: float) -> float:
    return max(0.0, min(1.0, math.log10(1 + x) / math.log10(1 + top)))


def run_cost(task_results: list[dict[str, Any]], price_in: float | None, price_out: float | None) -> float | None:
    """What the run cost at the given prices ($ per 1M tokens), from recorded usage."""
    if price_in is None and price_out is None:
        return None
    tin = tout = 0
    seen = False
    for tr in task_results:
        u = tr.get("usage") or {}
        i = u.get("prompt_tokens", u.get("input_tokens"))
        o = u.get("completion_tokens", u.get("output_tokens"))
        if isinstance(i, (int, float)) or isinstance(o, (int, float)):
            seen = True
            tin += int(i or 0)
            tout += int(o or 0)
    if not seen:
        return None
    return round((tin * float(price_in or 0) + tout * float(price_out or 0)) / 1e6, 6)


def gpu_score(normalized: float, *, parameters: Any = None, price_in: float | None = None,
              price_out: float | None = None, task_results: list[dict[str, Any]] | None = None,
              currency: str | None = None) -> GPUScore:
    quality = max(0.0, min(1.0, float(normalized or 0.0)))
    params_b = parse_parameters(parameters)
    blended = blended_price(price_in, price_out)
    scales, basis = [], []
    if params_b:
        scales.append(_log_scale(params_b, 1000))
        basis.append(f"{params_b:g}B parameters")
    if blended is not None:
        scales.append(_log_scale(blended, 100))
        basis.append(f"${blended:g} per 1M tokens (blended)")
    scale = max(scales) if scales else UNKNOWN_SCALE
    shortfall = max(0.0, 1 - quality / THRESHOLD)
    multiplier = max(FLOOR, min(1.0, 1 - shortfall * (0.3 + 0.7 * scale)))
    return GPUScore(
        gpu_score=round(100 * quality * multiplier, 2),
        quality=round(quality, 4),
        multiplier=round(multiplier, 4),
        scale=round(scale, 4),
        basis=", ".join(basis) if basis else "size and price unknown",
        parameters_b=round(params_b, 4) if params_b else None,
        price_blended_per_mtok=round(blended, 6) if blended is not None else None,
        price_per_b_params=round(blended / params_b, 6) if blended is not None and params_b else None,
        run_cost=run_cost(task_results or [], price_in, price_out),
        currency=(currency or "USD") if blended is not None else None,
    )


def gpu_for_results(doc: dict[str, Any], *, parameters: Any = None, price_in: float | None = None,
                    price_out: float | None = None) -> GPUScore:
    """The GPU score of a results document. Arguments override what the results recorded."""
    model = doc.get("model") or {}
    pricing = model.get("pricing") or {}
    return gpu_score(
        (doc.get("scores") or {}).get("normalized") or 0.0,
        parameters=parameters if parameters is not None else (model.get("parameters") or model.get("name")),
        price_in=price_in if price_in is not None else pricing.get("input_per_mtok"),
        price_out=price_out if price_out is not None else pricing.get("output_per_mtok"),
        task_results=doc.get("task_results"),
        currency=pricing.get("currency") or "USD",
    )

"""Local Hugging Face models.

``transformers`` models (``pip install 'RSOSTB[transformers]'``), and GGUF
models — ``org/repo:file.gguf``, a repo that only has ``.gguf`` files, or a
local ``.gguf`` path — through llama.cpp (``pip install llama-cpp-python``):
a GGUF repo has no tokenizer ``transformers`` can load, so it failed with
"Couldn't instantiate the backend tokenizer".
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from .base import AdapterError, Generation, ModelAdapter, sampling

#: The file used from a GGUF repo when none is named, best first.
GGUF_PREFERENCE = ("Q4_K_M", "Q5_K_M", "Q8_0", "Q6_K", "Q4_0", "Q4_K_S", "BF16", "F16")
_REPO_FILE = re.compile(r"^(?P<repo>[\w.\-]+/[\w.\-]+)(?::|/)(?P<file>[^:]+\.gguf)$", re.I)


def pick_gguf(files: list[str]) -> str | None:
    """The .gguf to use from a repo: the preferred quantization, else the first."""
    ggufs = sorted(f for f in files if f.lower().endswith(".gguf") and "mmproj" not in f.lower())
    for quant in GGUF_PREFERENCE:
        hit = next((f for f in ggufs if re.search(rf"(?i)[-_.]{re.escape(quant)}\.gguf$", f)), None)
        if hit:
            return hit
    return ggufs[0] if ggufs else None


def gguf_source(model: str, revision: str | None = None, gguf_file: str | None = None) -> tuple[str | None, str] | None:
    """``(repo or None for a local file, file)`` when *model* is a GGUF model, else None."""
    if model.lower().endswith(".gguf") and os.path.isfile(os.path.expanduser(model)):
        return None, os.path.expanduser(model)
    m = _REPO_FILE.match(model)
    if m:
        return m.group("repo"), m.group("file")
    if model.count("/") != 1 or os.path.exists(model):
        return None if not gguf_file else (model, gguf_file)
    if gguf_file:
        return model, gguf_file
    try:
        from huggingface_hub import list_repo_files  # type: ignore

        files = list_repo_files(model, revision=revision)
    except Exception:  # noqa: BLE001 - offline, private, or no hub: decide by the name
        return (model, "") if model.lower().endswith("gguf") else None
    weights = [f for f in files if f.endswith((".safetensors", ".bin", ".pt", ".pth"))]
    if weights or "config.json" in files:
        return None
    chosen = pick_gguf(files)
    return (model, chosen) if chosen else None


class HFLocalAdapter(ModelAdapter):
    name = "hf-local"
    loads_model = True

    def __init__(self, model: str | None = None, *, revision: str | None = None, device_map: str = "auto",
                 dtype: str = "auto", trust_remote_code: bool = False, gguf_file: str | None = None,
                 n_ctx: int = 8192, gpu_layers: int = -1, **options: Any) -> None:
        super().__init__(model, **options)
        self.revision, self.device_map, self.dtype = revision, device_map, dtype
        self.trust_remote_code = trust_remote_code
        self.gguf_file, self.n_ctx, self.gpu_layers = gguf_file, int(n_ctx), int(gpu_layers)
        self._tok = self._model = None
        self._llama = None
        self._gguf: tuple[str | None, str] | None = None
        self._gguf_checked = False

    def _gguf_target(self) -> tuple[str | None, str] | None:
        if not self._gguf_checked:
            self._gguf = gguf_source(self.model, self.revision, self.gguf_file)
            self._gguf_checked = True
        return self._gguf

    def _load_gguf(self, repo: str | None, filename: str) -> None:
        try:
            from llama_cpp import Llama  # type: ignore
        except ImportError as exc:
            what = f"{repo}:{filename}" if repo else filename
            raise AdapterError(
                f"{self.model} is a GGUF model, which runs on llama.cpp, not transformers. Install it with "
                "`pip install 'RSOSTB[gguf]'` (llama-cpp-python; build it for your GPU as its docs describe), or run it another way: "
                f'`benchmake -M "{what}"` (HyperNix multilama), or load it on a T1 server and use '
                "--adapter hypernix-t1.") from exc
        if repo is None:
            self._llama = Llama(model_path=filename, n_ctx=self.n_ctx, n_gpu_layers=self.gpu_layers, verbose=False)
            return
        if not filename:
            raise AdapterError(f"{repo} looks like a GGUF repo; name the file: --model {repo}:<file>.gguf "
                               "or --adapter-option gguf_file=<file>.gguf")
        self._llama = Llama.from_pretrained(repo_id=repo, filename=filename, n_ctx=self.n_ctx,
                                            n_gpu_layers=self.gpu_layers, verbose=False)

    def prepare(self) -> None:
        self._load()

    def _load(self):
        if self._model is not None or self._llama is not None:
            return
        target = self._gguf_target()
        if target is not None:
            self._load_gguf(*target)
            return
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        except ImportError as exc:
            raise AdapterError("hf-local needs: pip install 'RSOSTB[transformers]'") from exc
        self._tok = AutoTokenizer.from_pretrained(self.model, revision=self.revision,
                                                  trust_remote_code=self.trust_remote_code)
        self._model = AutoModelForCausalLM.from_pretrained(self.model, revision=self.revision, device_map=self.device_map,
                                                           torch_dtype=self.dtype,
                                                           trust_remote_code=self.trust_remote_code)

    def chat(self, messages, **kwargs):
        self._load()
        s = sampling(kwargs)
        if self._llama is not None:
            return self._chat_gguf(messages, s)
        tok, model = self._tok, self._model
        if getattr(tok, "chat_template", None):
            ids = tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
        else:
            text = "\n\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\n\nassistant:"
            ids = tok(text, return_tensors="pt").input_ids
        ids = ids.to(model.device)
        temp = float(s.get("temperature", 0.0))
        gen_kwargs = {"max_new_tokens": int(s.get("max_tokens", 1024)), "do_sample": temp > 0}
        if temp > 0:
            gen_kwargs.update(temperature=temp, top_p=float(s.get("top_p", 1.0)))
        if "seed" in s:
            import torch  # type: ignore

            torch.manual_seed(int(s["seed"]))
        t0 = time.monotonic()
        out = model.generate(ids, **gen_kwargs)
        text = tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=True)
        return Generation(text=text, latency=time.monotonic() - t0,
                          usage={"input_tokens": int(ids.shape[-1]), "output_tokens": int(out.shape[-1] - ids.shape[-1])})

    def _chat_gguf(self, messages, s: dict[str, Any]) -> Generation:
        kw: dict[str, Any] = {"max_tokens": int(s.get("max_tokens", 1024)),
                              "temperature": float(s.get("temperature", 0.0))}
        if "top_p" in s:
            kw["top_p"] = float(s["top_p"])
        if "stop" in s:
            kw["stop"] = list(s["stop"])
        if "seed" in s:
            kw["seed"] = int(s["seed"])
        t0 = time.monotonic()
        try:
            out = self._llama.create_chat_completion(messages=messages, **kw)
        except Exception as exc:  # noqa: BLE001 - llama.cpp errors (context overflow, ...) fail the task
            raise AdapterError(f"llama.cpp: {type(exc).__name__}: {exc}") from exc
        choice = (out.get("choices") or [{}])[0]
        usage = out.get("usage") or {}
        return Generation(text=str((choice.get("message") or {}).get("content") or ""),
                          latency=time.monotonic() - t0, finish_reason=choice.get("finish_reason"),
                          usage={"input_tokens": usage.get("prompt_tokens"),
                                 "output_tokens": usage.get("completion_tokens")})

    def describe(self):
        d = {"name": self.model, "adapter": self.name, "kind": "model", "provider": "Hugging Face (local)",
             "revision": self.revision}
        target = self._gguf_target()
        if target is not None:
            repo, filename = target
            d["provider"] = "Hugging Face (local, llama.cpp)"
            if repo and filename and ":" not in self.model:
                d["name"] = f"{repo}:{filename}"
            quant = re.search(r"(?i)[-_.](I?Q\d[\w]*|BF16|F16|F32)\.gguf$", filename or "")
            if quant:
                d["quantization"] = quant.group(1)
        return d

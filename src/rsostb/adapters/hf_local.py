"""Local Hugging Face ``transformers`` models (``pip install 'rsostbtest-pro[transformers]'``)."""
from __future__ import annotations

import time
from typing import Any

from .base import AdapterError, Generation, ModelAdapter, sampling


class HFLocalAdapter(ModelAdapter):
    name = "hf-local"

    def __init__(self, model: str | None = None, *, revision: str | None = None, device_map: str = "auto",
                 dtype: str = "auto", trust_remote_code: bool = False, **options: Any) -> None:
        super().__init__(model, **options)
        self.revision, self.device_map, self.dtype = revision, device_map, dtype
        self.trust_remote_code = trust_remote_code
        self._tok = self._model = None

    def _load(self):
        if self._model is not None:
            return
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        except ImportError as exc:
            raise AdapterError("hf-local needs: pip install 'rsostbtest-pro[transformers]'") from exc
        self._tok = AutoTokenizer.from_pretrained(self.model, revision=self.revision,
                                                  trust_remote_code=self.trust_remote_code)
        self._model = AutoModelForCausalLM.from_pretrained(self.model, revision=self.revision, device_map=self.device_map,
                                                           torch_dtype=self.dtype,
                                                           trust_remote_code=self.trust_remote_code)

    def chat(self, messages, **kwargs):
        self._load()
        s = sampling(kwargs)
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

    def describe(self):
        return {"name": self.model, "adapter": self.name, "kind": "model", "provider": "Hugging Face (local)",
                "revision": self.revision}

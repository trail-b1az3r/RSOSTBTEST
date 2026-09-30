"""HyperNix integration.

Two adapters let RSOSTBTEST-pro benchmark models served or trained with
HyperNix (https://github.com/trail-b1az3r/HyperNix-pip):

* ``hypernix-t1`` — a HyperNix **T1 API** server's governed inference
  endpoint (``POST /inference/chat``). Needs only the standard library; if the
  ``hypernix`` package is installed, its official ``hypernix.t1sdk`` client is
  used instead (mTLS, retries, key sealing). Fallback down the plan's cascade
  is always disabled (``allow_fallback: false``) and a response that reports
  a substituted model is treated as an error, so results always describe the
  model that was asked for.
* ``hypernix`` — an in-process HyperNix **oven** (``hypernix.old_oven`` or
  ``hypernix.neo_oven``) loaded from a Hugging Face repo id, a local snapshot,
  or a brewed model folder. Requires ``pip install 'rsostbtest-pro[hypernix]'``.

See docs/HYPERNIX.md.
"""
from __future__ import annotations

import os
import time
from typing import Any

from .base import AdapterError, Generation, ModelAdapter, env_secret, http_json, is_local_url, sampling

DEFAULT_T1_URL = "http://127.0.0.1:8000"
KEY_ENV_VARS = ("RSOSTB_HYPERNIX_T1_KEY", "HYPERNIX_T1_KEY", "T1_KEY")


def hypernix_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("hypernix")
    except Exception:
        return None


class HyperNixT1Adapter(ModelAdapter):
    name = "hypernix-t1"

    def __init__(self, model: str | None = None, *, base_url: str | None = None, api_key_env: str | None = None,
                 timeout: float = 300.0, retries: int = 2, use_sdk: bool | None = None, **options: Any) -> None:
        super().__init__(model, **options)
        self.base_url = (base_url or os.environ.get("HYPERNIX_T1_URL") or DEFAULT_T1_URL).rstrip("/")
        self.api_key_env = api_key_env or next((k for k in KEY_ENV_VARS if os.environ.get(k)), KEY_ENV_VARS[1])
        self.timeout, self.retries = timeout, retries
        self.requires_network = not is_local_url(self.base_url)
        self._client = None
        want_sdk = use_sdk if use_sdk is not None else True
        if want_sdk:
            try:
                from hypernix.t1sdk import T1Client  # type: ignore

                self._client = T1Client(self.base_url, credential=env_secret(self.api_key_env), timeout=timeout)
            except Exception:
                self._client = None
        self._status: dict[str, Any] | None = None

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            try:
                return self._client.call("POST", path, body=body, auth=True)
            except Exception as exc:  # T1Error hierarchy
                raise AdapterError(f"T1 {path} failed: {exc}") from exc
        key = env_secret(self.api_key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return http_json(self.base_url + path, body, headers, timeout=self.timeout, retries=self.retries)

    def chat(self, messages, **kwargs):
        s = sampling(kwargs)
        body: dict[str, Any] = {"model": self.model, "messages": messages, "allow_fallback": False}
        for k in ("temperature", "max_tokens", "top_p", "stop"):
            if k in s:
                body[k] = s[k] if k != "stop" else list(s[k])
        t0 = time.monotonic()
        data = self._post("/inference/chat", body)
        if data.get("substituted"):
            raise AdapterError(f"T1 server substituted {data.get('model')!r} for {self.model!r}; refusing to score it")
        usage = {"input_tokens": data.get("input_tokens"), "output_tokens": data.get("output_tokens"),
                 "cost": data.get("cost"), "currency": data.get("currency"), "backend": data.get("backend")}
        return Generation(text=str(data.get("content", "")), latency=time.monotonic() - t0, usage=usage,
                          model=data.get("model"), finish_reason=data.get("finish_reason"))

    def server_status(self) -> dict[str, Any]:
        if self._status is None:
            try:
                if self._client is not None:
                    self._status = self._client.call("GET", "/status")
                else:
                    self._status = http_json(self.base_url + "/status", None, {}, timeout=10, retries=0, method="GET")
            except Exception:
                self._status = {}
        return self._status

    def describe(self):
        st = self.server_status()
        return {"name": self.model, "adapter": self.name, "kind": "model", "provider": "HyperNix T1",
                "version": st.get("t1_api_version") or st.get("version")}


class HyperNixOvenAdapter(ModelAdapter):
    """In-process HyperNix oven.

    Options: ``repo_id`` (defaults to the model name), ``local_dir``,
    ``revision``, ``device``, ``dtype``, ``oven`` (``old`` or ``neo``),
    ``brewed`` (path to a brewed model folder, NeoOven only), ``top_k``.
    """

    name = "hypernix"

    def __init__(self, model: str | None = None, *, repo_id: str | None = None, local_dir: str | None = None,
                 revision: str | None = None, device: str | None = None, dtype: str = "float32",
                 oven: str = "neo", brewed: str | None = None, top_k: int = 40, **options: Any) -> None:
        super().__init__(model, **options)
        self.repo_id = repo_id or model or "ray0rf1re/hyper-nix.1"
        self.local_dir, self.revision, self.device, self.dtype = local_dir, revision, device, dtype
        self.oven_kind, self.brewed, self.top_k = oven, brewed, top_k
        self._oven = None

    def _load(self):
        if self._oven is not None:
            return self._oven
        try:
            if self.oven_kind == "old":
                from hypernix import old_oven as mod  # type: ignore
            else:
                from hypernix import neo_oven as mod  # type: ignore
        except ImportError as exc:
            raise AdapterError("the 'hypernix' adapter needs: pip install 'rsostbtest-pro[hypernix]'") from exc
        if self.brewed:
            self._oven = mod.preheat_brewed(self.brewed, device=self.device, dtype=self.dtype)
        else:
            self._oven = mod.preheat(self.repo_id, local_dir=self.local_dir, revision=self.revision,
                                     token=os.environ.get("HF_TOKEN"), device=self.device, dtype=self.dtype)
        return self._oven

    def chat(self, messages, **kwargs):
        oven = self._load()
        s = sampling(kwargs)
        t0 = time.monotonic()
        text = oven.chat(messages, max_new_tokens=int(s.get("max_tokens", 1024)),
                         temperature=float(s.get("temperature", 0.0)), top_k=self.top_k,
                         top_p=float(s.get("top_p", 1.0)), seed=s.get("seed"))
        return Generation(text=str(text), latency=time.monotonic() - t0)

    def describe(self):
        return {"name": self.model, "adapter": self.name, "kind": "model", "provider": "HyperNix",
                "revision": self.revision, "version": hypernix_version(),
                "quantization": None if self.dtype == "float32" else self.dtype}

"""HyperNix integration.

Two adapters let RSOSTBTEST-pro benchmark models served or trained with
HyperNix (https://github.com/trail-b1az3r/HyperNix-pip):

* ``hypernix-t1`` — a HyperNix **T1 API** server's governed inference
  endpoint (``POST /inference/chat``). Needs only the standard library; if the
  ``hypernix`` package is installed, its official ``hypernix.t1sdk`` client is
  used instead (mTLS, retries, key sealing). Fallback down the plan's cascade
  is always disabled (``allow_fallback: false``) and a response that reports
  a substituted model is treated as an error, so results always describe the
  model that was asked for. Without ``--base-url`` / ``HYPERNIX_T1_URL`` it
  connects to the server ``waiter serv -A -I <server> -K <key>`` saved, and
  without a key variable it sends waiter's saved key (to that server only).
  T1 serves ``/inference`` from one of two backends — ``hypernix`` (the server's own runner, for the model it has
  loaded) or ``lmstudio`` — and names it in ``backend_name``; every task
  records it, and ``backend=hypernix`` (or ``lmstudio``) requires it.
* ``hypernix`` — an in-process HyperNix **oven** (``hypernix.old_oven`` or
  ``hypernix.neo_oven``) loaded from a Hugging Face repo id, a local snapshot,
  or a brewed model folder. Requires ``pip install 'RSOSTB[hypernix]'``.

See docs/HYPERNIX.md.
"""
from __future__ import annotations

import json
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import (
    AdapterError,
    Generation,
    ModelAdapter,
    RequestTimeout,
    env_secret,
    http_json,
    is_local_url,
    is_timeout,
    probe,
    sampling,
)

DEFAULT_T1_URL = "http://127.0.0.1:8000"
KEY_ENV_VARS = ("RSOSTB_HYPERNIX_T1_KEY", "HYPERNIX_T1_KEY", "T1_KEY")
#: The backends a T1 server can answer /inference from (``backend_name``).
T1_BACKENDS = ("hypernix", "lmstudio")
#: How HyperNix's waiter marks a password-locked config (``waiter serv -e``).
_WAITER_LOCK_PREFIX = "RVLOCK1:"


@dataclass
class WaiterServer:
    """The T1 server and key ``waiter serv -A -I <server> -K <key>`` saved."""

    url: str
    key: str | None
    path: Path

    def __repr__(self) -> str:  # never the key, not even masked
        return f"WaiterServer(url={self.url!r}, path={str(self.path)!r}, key={'set' if self.key else 'none'})"


def waiter_config_path() -> Path | None:
    """Where waiter keeps this machine's T1 connection: ``RSOSTB_WAITER_CONFIG``
    (``off`` to ignore waiter), else ``~/.hypernix/waiter/waiter.config.jsonl``."""
    raw = os.environ.get("RSOSTB_WAITER_CONFIG")
    if raw is None:
        return Path.home() / ".hypernix" / "waiter" / "waiter.config.jsonl"
    raw = raw.strip()
    return None if raw.lower() in ("", "0", "off", "none", "false") else Path(raw).expanduser()


def _open_sealed_waiter_config(path: Path, raw: str) -> dict[str, Any] | None:
    """A config saved with ``-E`` (waiter's per-machine key) or ``-e``
    (password, from ``HNX_WAITER_PASSWORD``), opened by HyperNix itself."""
    try:
        from hypernix.waiter.local_config import WaiterConfigStore  # type: ignore
    except Exception:
        return None
    if not raw.startswith(_WAITER_LOCK_PREFIX) and not (path.parent / ".master.key").is_file():
        return None  # encrypted on another machine; trying would create a master key here
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = WaiterConfigStore(path, password_provider=lambda: os.environ.get("HNX_WAITER_PASSWORD")).load()
    except Exception:
        return None
    return cfg.to_dict() if cfg is not None else None


def _waiter_base_url(cfg: dict[str, Any]) -> str:
    """The server URL waiter connects to, built the way ``waiter`` builds it."""
    server = str(cfg["server"]).strip()
    if not server.startswith(("http://", "https://")):
        server = f"{'http' if cfg.get('local_only') else 'https'}://{server}"
    port = cfg.get("port")
    if port and f":{port}" not in server:
        server = f"{server}:{port}"
    return server.rstrip("/")


def waiter_server() -> WaiterServer | None:
    """The T1 server (and key) saved by ``waiter serv -A -I <server> -K <key>``
    on this machine, or None. Never raises: a config that cannot be read is
    no config."""
    path = waiter_config_path()
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        cfg = _open_sealed_waiter_config(path, raw)
    if not isinstance(cfg, dict) or not cfg.get("server"):
        return None
    return WaiterServer(_waiter_base_url(cfg), str(cfg.get("key") or "") or None, path)


def same_server(a: str, b: str) -> bool:
    """Whether two base URLs name the same T1 server (loopback names and
    default ports are equivalent)."""
    def norm(url: str) -> tuple:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            host = "loopback"
        try:
            port = p.port or {"http": 80, "https": 443}.get(p.scheme)
        except ValueError:
            port = None
        return p.scheme, host, port, p.path.rstrip("/")
    return norm(a) == norm(b)


def hypernix_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("hypernix")
    except Exception:
        return None


class HyperNixT1Adapter(ModelAdapter):
    name = "hypernix-t1"

    def __init__(self, model: str | None = None, *, base_url: str | None = None, api_key_env: str | None = None,
                 timeout: float = 300.0, retries: int = 2, use_sdk: bool | None = None,
                 backend: str | None = None, **options: Any) -> None:
        super().__init__(model, **options)
        backend = (backend or "").strip().lower() or None
        if backend in ("any", "auto"):
            backend = None
        if backend is not None and backend not in T1_BACKENDS:
            raise AdapterError(f"backend must be one of {', '.join(T1_BACKENDS)} (or omitted); got {backend!r}")
        #: When set, every answer must come from this T1 backend.
        self.backend = backend
        self._preflight_done = False
        # Server: --base-url, $HYPERNIX_T1_URL, the server `waiter serv` saved, the default.
        # Key: the key variable, else waiter's saved key, and only for the server it was saved for.
        self._waiter = waiter_server()
        self.base_url = (base_url or os.environ.get("HYPERNIX_T1_URL")
                         or (self._waiter.url if self._waiter else None) or DEFAULT_T1_URL).rstrip("/")
        self.api_key_env = api_key_env or next((k for k in KEY_ENV_VARS if os.environ.get(k)), KEY_ENV_VARS[1])
        self._waiter_key = None
        if (not env_secret(self.api_key_env) and self._waiter and self._waiter.key
                and same_server(self._waiter.url, self.base_url)):
            self._waiter_key = self._waiter.key
        self.timeout, self.retries = timeout, retries
        self.requires_network = not is_local_url(self.base_url)
        self._client = None
        want_sdk = use_sdk if use_sdk is not None else True
        if want_sdk:
            try:
                from hypernix.t1sdk import T1Client  # type: ignore

                self._client = T1Client(self.base_url, credential=self._key(), timeout=timeout)
            except Exception:
                self._client = None
        self._status: dict[str, Any] | None = None

    def _key(self) -> str | None:
        return env_secret(self.api_key_env) or self._waiter_key

    def _no_key_hint(self) -> str:
        """Why no key was sent, and how to give one."""
        others = ", ".join(k for k in KEY_ENV_VARS if k != self.api_key_env)
        hint = (f"no T1 key was sent: set {self.api_key_env} (or {others}), "
                "or save one with `waiter serv -A -I <server> -K <key>`")
        w = self._waiter
        if w is not None and w.key and not same_server(w.url, self.base_url):
            hint += (f"; waiter's saved key is for {w.url}, not {self.base_url}, so it was not used "
                     f"(pass --base-url {w.url} to use it)")
        elif w is not None and not w.key:
            hint += f"; waiter's config ({w.path}) has a server but no key"
        return hint

    def _failed(self, path: str, exc: Exception) -> AdapterError:
        msg = f"T1 {path} failed: {exc}"
        text = str(exc)
        if is_timeout(exc):
            # The SDK's "timed out", or T1's own 503 when its model backend did not answer in time.
            return RequestTimeout(msg)
        if not self._key() and ("AUTH_" in text or "credential" in text or text.startswith(("HTTP 401", "HTTP 403"))):
            msg += f" ({self._no_key_hint()})"
        return AdapterError(msg)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            try:
                return self._client.call("POST", path, body=body, auth=True)
            except Exception as exc:  # T1Error hierarchy
                raise self._failed(path, exc) from exc
        key = self._key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            return http_json(self.base_url + path, body, headers, timeout=self.timeout, retries=self.retries)
        except AdapterError as exc:
            if key or isinstance(exc, RequestTimeout):
                raise
            raise self._failed(path, exc) from exc

    def _get(self, path: str) -> dict[str, Any]:
        if self._client is not None:
            try:
                return self._client.call("GET", path, auth=True)
            except Exception as exc:
                raise self._failed(path, exc) from exc
        key = self._key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            return http_json(self.base_url + path, None, headers, timeout=min(self.timeout, 30.0), retries=0,
                             method="GET")
        except AdapterError as exc:
            if key or isinstance(exc, RequestTimeout):
                raise
            raise self._failed(path, exc) from exc

    def backends(self) -> list[dict[str, Any]]:
        """``GET /inference/backends``: what the server can answer from, probed."""
        return list((self._get("/inference/backends") or {}).get("backends") or [])

    def _preflight(self) -> None:
        """With ``backend`` required, fail once and clearly before a run
        turns every task into the same error. Older servers without the
        listing are checked per reply instead."""
        self._preflight_done = True
        if self.backend is None:
            return
        try:
            rows = self.backends()
        except AdapterError:
            return
        row = next((r for r in rows if r.get("name") == self.backend), None)
        if row is None:
            raise AdapterError(f"T1 server at {self.base_url} does not offer the {self.backend!r} backend "
                               f"(it lists: {', '.join(r.get('name', '?') for r in rows) or 'nothing'}); "
                               "it may predate the HyperNix runner backend")
        if not row.get("reachable"):
            raise AdapterError(f"T1 backend {self.backend!r} is not answering: {row.get('detail') or 'unreachable'}")
        if self.backend == "hypernix" and row.get("model_id") and row["model_id"] != self.model:
            raise AdapterError(f"the T1 HyperNix runner is serving {row['model_id']!r}, not {self.model!r}; "
                               f"load it with POST /runner/load before benchmarking")

    def chat(self, messages, **kwargs):
        if not self._preflight_done:
            self._preflight()
        s = sampling(kwargs)
        body: dict[str, Any] = {"model": self.model, "messages": messages, "allow_fallback": False}
        for k in ("temperature", "max_tokens", "top_p", "stop"):
            if k in s:
                body[k] = s[k] if k != "stop" else list(s[k])
        t0 = time.monotonic()
        data = self._post("/inference/chat", body)
        if data.get("substituted"):
            raise AdapterError(f"T1 server substituted {data.get('model')!r} for {self.model!r}; refusing to score it")
        backend_name = data.get("backend_name") or None
        if self.backend is not None and backend_name != self.backend:
            served = backend_name or "an unnamed backend (the server predates backend_name)"
            raise AdapterError(f"T1 answered from {served}, but backend={self.backend!r} was required; "
                               "refusing to score it")
        usage = {"input_tokens": data.get("input_tokens"), "output_tokens": data.get("output_tokens"),
                 "cost": data.get("cost"), "currency": data.get("currency"), "backend": data.get("backend"),
                 "backend_name": backend_name}
        return Generation(text=str(data.get("content", "")), latency=time.monotonic() - t0, usage=usage,
                          model=data.get("model"), finish_reason=data.get("finish_reason"))

    def server_status(self) -> dict[str, Any]:
        """``GET /status``, for the server version in the results. Asked with a
        10 s timeout and no retries: it is metadata, and with the request
        timeout and the SDK's retries a stuck server held the run before the
        first task for up to 15 minutes, printing nothing."""
        if self._status is None:
            try:
                transport = getattr(self._client, "transport", None)
                if self._client is not None and hasattr(transport, "timeout"):
                    saved, transport.timeout = transport.timeout, 10.0
                    try:
                        self._status = self._client.call("GET", "/status", idempotent=False)
                    finally:
                        transport.timeout = saved
                else:
                    self._status = http_json(self.base_url + "/status", None, {}, timeout=10, retries=0, method="GET")
            except Exception:
                self._status = {}
        return self._status

    def health(self) -> str | None:
        """Whether T1 answers, and what it says about the backends behind it."""
        answered, detail = probe(self.base_url + "/health")
        if not answered:
            return f"T1 at {self.base_url} is not answering at all ({detail}): restart the T1 server"
        msg = f"T1 at {self.base_url} answers, so the model server behind it is slow or busy"
        key = self._key()
        try:
            rows = (http_json(self.base_url + "/inference/backends", None,
                              {"Authorization": f"Bearer {key}"} if key else {},
                              timeout=20, retries=0, method="GET") or {}).get("backends") or []
        except AdapterError:
            rows = []
        parts = []
        for r in rows:
            part = f"{r.get('name', '?')}: {'up' if r.get('reachable') else 'not answering'}"
            if r.get("model_id"):
                part += f", {r['model_id']}"
            if not r.get("reachable") and r.get("detail"):
                part += f" ({str(r['detail'])[:120]})"
            parts.append(part)
        return msg + (f"; T1 reports its backends as {'; '.join(parts)}" if parts else "")

    def describe(self):
        st = self.server_status()
        provider = "HyperNix T1" + (f" ({self.backend} backend)" if self.backend else "")
        return {"name": self.model, "adapter": self.name, "kind": "model", "provider": provider,
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
            raise AdapterError("the 'hypernix' adapter needs: pip install 'RSOSTB[hypernix]'") from exc
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

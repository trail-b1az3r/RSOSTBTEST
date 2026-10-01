"""One-line reports of Hugging Face Hub failures.

The Hub explains most refusals in its response body (``server_message``):
which plan is needed, which permission is missing. That explanation is the
useful part, so it is always kept, whatever the length of the rest.
"""
from __future__ import annotations

HINTS = {
    401: "the token was rejected; create a new token and update the secret / HF_TOKEN",
    403: "the token cannot write to this repo; use a write token (or a fine-grained token with write access to it)",
}
GRADIO_402 = ("Hugging Face needs a paid plan (PRO, or Team/Enterprise for an organization) to create Gradio or "
              "Docker Spaces; static Spaces are free for every account. Publish the static Space instead: "
              "`rsostb space publish --sdk static`.")


def hub_status(exc: BaseException) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def hub_error(exc: BaseException) -> str:
    """``Type: first line of the message — the Hub's own explanation``, on one line."""
    lines = str(exc).strip().splitlines()
    msg = f"{type(exc).__name__}: {lines[0] if lines else ''}".rstrip(": ")
    server = " ".join(str(getattr(exc, "server_message", None) or "").split())
    if server and server not in msg:
        msg += f" — {server}"
    return msg[:1500]


def hub_hint(exc: BaseException, *, space_sdk: str | None = None) -> str | None:
    status = hub_status(exc)
    if status == 402 and space_sdk not in (None, "static"):
        return GRADIO_402
    return HINTS.get(status)

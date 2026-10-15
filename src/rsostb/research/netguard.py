"""Live fetching with SSRF protection. Off unless explicitly enabled.

Every hop (the first request and each redirect) must pass:

* scheme ``https`` (``http`` only when the policy allows it), no user info in
  the URL, the default port unless the policy lists the port;
* a host on the allowlist (exact, or a subdomain of a ``*.`` entry);
* every address the host resolves to is public: loopback, private,
  link-local (cloud metadata), multicast, reserved, unspecified, carrier-NAT
  and IPv4-mapped forms of those are refused;
* the connection goes to the address that was checked (no second lookup,
  so DNS rebinding cannot swap it), with TLS verified against the host name.

Responses are capped in size and time and must be text, HTML, JSON or XML.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit


class FetchBlocked(Exception):
    """The request was refused by policy. The message says why."""


@dataclass
class FetchPolicy:
    allowlist: list[str] = field(default_factory=list)
    allow_http: bool = False
    allowed_ports: tuple[int, ...] = ()
    max_bytes: int = 512_000
    timeout: float = 10.0
    max_redirects: int = 3
    content_types: tuple[str, ...] = ("text/", "application/json", "application/xml", "application/xhtml+xml")
    user_agent: str = "RSOSTB-research/1.0 (+benchmark; offline-first)"

    @classmethod
    def from_config(cls, cfg: dict | None) -> FetchPolicy:
        cfg = cfg or {}
        return cls(allowlist=[str(h).lower() for h in cfg.get("allowlist") or []],
                   allow_http=bool(cfg.get("allow_http", False)),
                   allowed_ports=tuple(int(p) for p in cfg.get("allowed_ports") or ()),
                   max_bytes=int(cfg.get("max_bytes", 512_000)), timeout=float(cfg.get("timeout", 10.0)),
                   max_redirects=int(cfg.get("max_redirects", 3)))


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower().rstrip(".")
        if entry.startswith("*."):
            base = entry[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == entry:
            return True
    return False


def address_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return address_is_public(str(ip.ipv4_mapped))
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved \
            or ip.is_unspecified or not ip.is_global:
        return False
    return ip not in ipaddress.ip_network("100.64.0.0/10")


def check_url(url: str, policy: FetchPolicy, resolve=socket.getaddrinfo) -> tuple[str, str, int, str, str]:
    """Validate one URL. Returns (scheme, host, port, path+query, checked address)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or (scheme == "http" and not policy.allow_http):
        raise FetchBlocked(f"scheme {scheme or '(none)'!r} is not allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise FetchBlocked("URLs with user info are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise FetchBlocked("URL has no host")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise FetchBlocked("IP-address hosts are not allowed; use an allowlisted host name")
    except ValueError:
        pass
    if not host_allowed(host, policy.allowlist):
        raise FetchBlocked(f"host {host!r} is not on the research allowlist")
    default = 443 if scheme == "https" else 80
    try:
        port = parts.port or default
    except ValueError:
        raise FetchBlocked("invalid port") from None
    if port != default and port not in policy.allowed_ports:
        raise FetchBlocked(f"port {port} is not allowed")
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchBlocked(f"cannot resolve {host}: {exc}") from None
    addrs = sorted({i[4][0] for i in infos})
    if not addrs:
        raise FetchBlocked(f"{host} has no address")
    bad = [a for a in addrs if not address_is_public(a)]
    if bad:
        raise FetchBlocked(f"{host} resolves to a non-public address ({bad[0]})")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, host, port, path, addrs[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, **kw):
        super().__init__(host, **kw)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, **kw):
        super().__init__(host, **kw)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def fetch(url: str, policy: FetchPolicy, resolve=socket.getaddrinfo, connect=None) -> dict:
    """Fetch an allowlisted URL. Returns {"url", "status", "content_type", "text", "truncated"}."""
    if not policy.allowlist:
        raise FetchBlocked("live research is disabled (empty allowlist)")
    current = url
    for _hop in range(policy.max_redirects + 1):
        scheme, host, port, path, addr = check_url(current, policy, resolve)
        if connect is not None:
            conn = connect(scheme, host, port, addr, policy)
        elif scheme == "https":
            conn = _PinnedHTTPS(host, addr, port=port, timeout=policy.timeout, context=ssl.create_default_context())
        else:
            conn = _PinnedHTTP(host, addr, port=port, timeout=policy.timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": policy.user_agent, "Accept": "text/html, text/plain, "
                                               "application/json;q=0.9, */*;q=0.1", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise FetchBlocked("redirect without a Location")
                current = urljoin(current, loc)
                continue
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if not any(ctype.startswith(c) for c in policy.content_types):
                raise FetchBlocked(f"content type {ctype or '(none)'} is not allowed")
            body = resp.read(policy.max_bytes + 1)
            charset = "utf-8"
            for part in (resp.getheader("Content-Type") or "").split(";")[1:]:
                k, _, v = part.strip().partition("=")
                if k.lower() == "charset" and v:
                    charset = v.strip('"')
            try:
                text = body[:policy.max_bytes].decode(charset, errors="replace")
            except LookupError:
                text = body[:policy.max_bytes].decode("utf-8", errors="replace")
            return {"url": current, "status": resp.status, "content_type": ctype, "text": text,
                    "truncated": len(body) > policy.max_bytes}
        finally:
            conn.close()
    raise FetchBlocked(f"more than {policy.max_redirects} redirects")

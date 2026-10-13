"""Which addresses Attest's browser may visit.

In accounts mode the server drives a browser to addresses that strangers give it, so an address is untrusted input
and the browser is a tool that must not be aimable at the server's own network. Two layers:

* `parse_site_url` - the syntax: https, a real public-looking domain name, no credentials, no odd ports.
* `is_public_ip` / `resolve_public` - the network: the name must resolve to public addresses only. Private ranges,
  loopback, link-local (including cloud metadata at 169.254.169.254), carrier-grade NAT, multicast, and addresses that
  merely *wrap* a private one (::ffff:10.0.0.1, 6to4, NAT64) are all refused.

Resolving once is not enough (a name can answer differently the second time: DNS rebinding), so the browser layer
re-checks every request and the address it actually connected to; see pipeline/safe_browse.py.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Names that are meant for private networks even though they look like domains.
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".private",
                      ".home.arpa", ".localdomain", ".test", ".invalid", ".example", ".onion")


class UnsafeAddress(ValueError):
    """The address is not one Attest will visit. The message is safe to show to the person who typed it."""


@dataclass(frozen=True)
class SiteUrl:
    scheme: str
    host: str
    port: int
    base_path: str  # "" or "/app" - never a trailing slash

    @property
    def origin(self) -> str:
        default = (self.scheme == "https" and self.port == 443) or (self.scheme == "http" and self.port == 80)
        return f"{self.scheme}://{self.host}" + ("" if default else f":{self.port}")

    @property
    def url(self) -> str:
        return self.origin + self.base_path


def parse_site_url(raw: str, *, allow_insecure: bool = False, allowed_ports: tuple[int, ...] = (443,)) -> SiteUrl:
    if not isinstance(raw, str) or len(raw) > 300 or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise UnsafeAddress("enter the address of your site, for example https://app.example.com")
    u = urlsplit(raw)
    scheme = u.scheme.lower()
    if scheme not in ("https", "http") or (scheme == "http" and not allow_insecure):
        raise UnsafeAddress("the address must start with https://")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise UnsafeAddress("the address must not contain a username or password")
    if u.query or u.fragment:
        raise UnsafeAddress("give the address without ? or # parts; the pages to audit are set separately")
    if "\\" in raw or "%" in u.netloc:
        raise UnsafeAddress("the address contains characters that are not allowed")
    try:
        port = u.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UnsafeAddress("the port in the address is not valid") from None
    if port not in allowed_ports:
        raise UnsafeAddress(f"port {port} is not allowed; use one of {', '.join(map(str, allowed_ports))}")
    host = (u.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeAddress("the address has no host name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeAddress("use your site's domain name, not an IP address")
    try:
        host = host.encode("idna").decode("ascii")  # international names become their punycode form
    except UnicodeError:
        raise UnsafeAddress("the host name is not valid") from None
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253 or not all(_LABEL.match(lb) for lb in labels) or labels[-1].isdigit():
        raise UnsafeAddress("the host name is not a public domain name")
    if host.endswith(_INTERNAL_SUFFIXES) or host in {s.lstrip(".") for s in _INTERNAL_SUFFIXES}:
        raise UnsafeAddress("that host name is for private networks, not the public internet")
    path = u.path.rstrip("/")
    if path and (not path.startswith("/") or ".." in path.split("/") or "//" in path):
        raise UnsafeAddress("the path in the address is not valid")
    return SiteUrl(scheme, host, port, path)


def is_public_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for an ordinary public internet address. Addresses that wrap another address are judged by what they wrap."""
    a = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:  # ::ffff:10.0.0.1
            return is_public_ip(a.ipv4_mapped)
        if a.sixtofour is not None:  # 2002:0a00:0001::  (6to4 wraps an IPv4)
            return is_public_ip(a.sixtofour)
        if a.teredo is not None:
            return all(is_public_ip(x) for x in a.teredo)
        if a in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 wraps the last 32 bits
            return is_public_ip(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF))
    return a.is_global and not a.is_multicast and not a.is_reserved and not a.is_loopback and not a.is_link_local


async def resolve_public(host: str, port: int = 443, *, timeout: float = 5.0) -> list[str]:
    """Resolve `host` and return its addresses, but only if EVERY one is public. One private answer poisons the lot
    (an attacker's DNS can return a public and a private address and hope we pick the private one)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout)
    except (TimeoutError, socket.gaierror, OSError):
        raise UnsafeAddress(f"could not look up {host}") from None
    ips = sorted({i[4][0].split("%")[0] for i in infos})
    if not ips:
        raise UnsafeAddress(f"{host} has no address")
    bad = [ip for ip in ips if not is_public_ip(ip)]
    if bad:
        raise UnsafeAddress(f"{host} points at a private or reserved address, which Attest will not visit")
    return ips

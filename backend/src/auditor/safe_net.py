"""Fetching things from addresses strangers gave us, without being aimable at our own network.

`SafeNet.get_text` is used to read the ownership-proof file on someone's site. It
  * validates the address (https, public domain name, allowed port),
  * resolves it and refuses unless EVERY answer is public,
  * connects to the address it checked (not a second lookup), keeping the real name for TLS and the Host header, so a
    name cannot answer differently the second time,
  * follows only redirects that stay on the same host, re-checking each hop,
  * stops reading after a size cap and gives up after a timeout.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx

from .netguard import UnsafeAddress, parse_site_url, resolve_public

Resolver = Callable[[str, int], Awaitable[list[str]]]
TxtLookup = Callable[[str], Awaitable[list[str]]]


class FetchError(RuntimeError):
    """The fetch failed in a way that is worth telling the person (not found, too slow, redirected elsewhere...)."""


@dataclass(frozen=True)
class Fetched:
    status: int
    text: str
    url: str


async def default_txt(name: str) -> list[str]:
    try:
        ans = await dns.asyncresolver.Resolver().resolve(name, "TXT", lifetime=5.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException:
        raise FetchError(f"could not look up {name}") from None
    return ["".join(part.decode("utf-8", "replace") for part in rr.strings) for rr in ans]


class SafeNet:
    def __init__(self, *, allow_insecure: bool = False, allowed_ports: tuple[int, ...] = (443,), resolver: Resolver = resolve_public,
                 transport: httpx.AsyncBaseTransport | None = None, txt: TxtLookup = default_txt):
        self.allow_insecure, self.allowed_ports = allow_insecure, allowed_ports
        self._resolver, self._transport, self._txt = resolver, transport, txt

    async def txt_records(self, name: str) -> list[str]:
        return await self._txt(name)

    async def get_text(self, url: str, *, max_bytes: int = 64 * 1024, timeout: float = 8.0, max_redirects: int = 3) -> Fetched:
        first = urlsplit(url)
        for _ in range(max_redirects + 1):
            parts = urlsplit(url)
            site = parse_site_url(f"{parts.scheme}://{parts.netloc}", allow_insecure=self.allow_insecure, allowed_ports=self.allowed_ports)
            if site.host != parse_site_url(f"{first.scheme}://{first.netloc}", allow_insecure=self.allow_insecure,
                                           allowed_ports=self.allowed_ports).host:
                raise FetchError("the address redirects to a different site")
            ips = await self._resolver(site.host, site.port)
            target, headers, ext = url, {"Accept": "text/plain,*/*;q=0.1", "User-Agent": "attest-verifier/1.0"}, {}
            if self._transport is None:  # real network: connect to the address we just checked, keep the name for TLS and Host
                ip = ips[0]
                host = f"[{ip}]" if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address) else ip
                target = f"{site.scheme}://{host}:{site.port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
                headers["Host"] = site.host if site.port in (80, 443) else f"{site.host}:{site.port}"
                ext = {"sni_hostname": site.host}
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout, follow_redirects=False) as http:
                try:
                    async with http.stream("GET", target, headers=headers, extensions=ext) as r:
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            url = urljoin(url, r.headers["location"])
                            continue
                        body = bytearray()
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) >= max_bytes:
                                break
                        return Fetched(r.status_code, bytes(body[:max_bytes]).decode("utf-8", "replace"), url)
                except httpx.TimeoutException:
                    raise FetchError("the site took too long to answer") from None
                except httpx.HTTPError as e:
                    raise FetchError(f"could not reach the site ({type(e).__name__})") from None
        raise FetchError("too many redirects")


__all__ = ["FetchError", "Fetched", "SafeNet", "UnsafeAddress"]

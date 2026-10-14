"""A small proxy that every request of the audit browser goes through, so the browser cannot be aimed at our network.

Why a proxy and not per-request checks inside the browser tooling: a page can reach the network in many ways (redirects,
iframes, workers, WebSockets, prefetch) and tooling that intercepts requests misses some of them. A proxy sees every
connection the browser makes, and it does the name lookup itself. That removes two attacks at once:

  * the policy is applied to the address we will actually connect to, at the moment we connect (no second lookup, so
    DNS rebinding has nothing to rebind);
  * the browser never learns or chooses an address, so a redirect to `127.0.0.1` or to a name that resolves privately
    is simply a refused connection.

It speaks only CONNECT (HTTPS and WSS). Accounts mode only audits https sites, so plain-HTTP forwarding is refused
rather than half-implemented. Each run gets its own proxy with its own random credentials, so nothing else on the
machine can use it as a relay, and its own limits (tunnels, bytes, idle time).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from typing import Self

from ..netguard import UnsafeAddress, is_public_ip, resolve_public

HEAD_LIMIT = 8192
ALLOWED_PORTS = (80, 443, 8080, 8443)  # a browser has no business opening a tunnel to mail, SSH or database ports


class SafeProxy:
    def __init__(self, *, resolve: Callable[[str, int], Awaitable[list[str]]] = resolve_public,
                 is_public: Callable[[str], bool] = is_public_ip, allowed_ports: tuple[int, ...] = ALLOWED_PORTS,
                 max_tunnels: int = 3000, max_bytes: int = 1 << 30, idle_s: float = 60.0, connect_timeout_s: float = 10.0,
                 trusted: dict[str, str] | None = None):
        self._resolve, self._is_public, self.allowed_ports = resolve, is_public, allowed_ports
        self.max_tunnels, self.max_bytes, self.idle_s, self.connect_timeout_s = max_tunnels, max_bytes, idle_s, connect_timeout_s
        # Tests only: host -> address to connect to, skipping the public-address rule for that host.
        self._trusted = {h.lower(): ip for h, ip in (trusted or {}).items()}
        self.violations: list[str] = []
        self.tunnels = 0
        self.bytes = 0
        self.username = "attest"
        self.password = secrets.token_urlsafe(24)
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    # ---- lifecycle
    async def start(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def playwright_settings(self) -> dict:
        """Passed to chromium.launch(proxy=...). `<-loopback>` makes Chromium send even localhost traffic through us,
        where it is refused (by default it would bypass the proxy and reach the machine's own services)."""
        return {"server": f"http://127.0.0.1:{self.port}", "username": self.username, "password": self.password, "bypass": "<-loopback>"}

    # ---- one connection
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        finally:
            writer.close()
            if task:
                self._tasks.discard(task)

    async def _reply(self, writer, code: int, text: str, extra: str = "") -> None:
        body = text.encode()
        writer.write(f"HTTP/1.1 {code} {text}\r\n{extra}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _serve(self, reader, writer) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
        if len(head) > HEAD_LIMIT:
            return await self._reply(writer, 431, "Request Header Fields Too Large")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
        if not self._authorised(headers.get("proxy-authorization", "")):
            return await self._reply(writer, 407, "Proxy Authentication Required", 'Proxy-Authenticate: Basic realm="attest"\r\n')
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            self.violations.append("plain http request refused")
            return await self._reply(writer, 403, "Only https is allowed")
        host, port = self._split(parts[1])
        if host is None:
            return await self._reply(writer, 400, "Bad target")
        if port not in self.allowed_ports:
            self.violations.append(f"blocked port {port} on {host}")
            return await self._reply(writer, 403, f"Port {port} is not allowed")
        self.tunnels += 1
        if self.tunnels > self.max_tunnels:
            self.violations.append("too many connections from one run")
            return await self._reply(writer, 429, "Too many connections")
        try:
            ips = await self._targets(host, port)
        except UnsafeAddress:
            self.violations.append(f"blocked {host}: private or reserved address")
            return await self._reply(writer, 403, "That address is not allowed")
        upstream = await self._connect(ips, port)
        if upstream is None:
            return await self._reply(writer, 502, "Could not connect")
        up_reader, up_writer = upstream
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        finally:
            up_writer.close()

    def _authorised(self, value: str) -> bool:
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "basic":
            return False
        want = base64.b64encode(f"{self.username}:{self.password}".encode())
        return hmac.compare_digest(token.strip().encode(), want)

    @staticmethod
    def _split(target: str) -> tuple[str | None, int]:
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            return None, 0
        host = host.strip("[]").rstrip(".").lower()
        return (host if host and all(32 < ord(c) < 127 for c in host) else None), int(port)

    async def _targets(self, host: str, port: int) -> list[str]:
        """The addresses we may connect to for this host, or UnsafeAddress. Literals are judged directly; names are
        resolved here, once, and ALL answers must be public."""
        if host in self._trusted:
            return [self._trusted[host]]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return await self._resolve(host, port)
        if not self._is_public(host):
            raise UnsafeAddress("private literal")
        return [host]

    async def _connect(self, ips: list[str], port: int):
        for ip in ips:
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port), self.connect_timeout_s)
            except (OSError, TimeoutError):
                continue
        return None

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await asyncio.wait_for(src.read(65536), self.idle_s)
                if not data:
                    break
                self.bytes += len(data)
                if self.bytes > self.max_bytes:
                    self.violations.append("too much data from one run")
                    break
                dst.write(data)
                await dst.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                dst.write_eof()
            except (OSError, RuntimeError):
                dst.close()

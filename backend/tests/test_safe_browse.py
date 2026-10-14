"""The audit browser against hostile pages, behind the real proxy. A 'secret' server stands in for the server's private
network: it must see NOTHING, however the page tries to reach it."""

import datetime
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright

from auditor.netguard import UnsafeAddress
from auditor.pipeline.prober import _open
from auditor.pipeline.safe_browse import (
    GUARD,
    BlockedByGuard,
    BrowserGuard,
    context_options,
    launch_browser,
)
from auditor.pipeline.safe_proxy import SafeProxy

pytestmark = pytest.mark.timeout(120)


def make_cert(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ok.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("ok.test"), x509.DNSName("internal.test")]), False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "c.pem", tmp_path / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return str(c), str(k)


class Server:
    """A small HTTPS server that records every request path it receives."""

    def __init__(self, cert, routes=None):
        outer, self.hits, self.routes = self, [], routes or {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                status, ctype, body, headers = outer.routes.get(self.path, (404, "text/plain", b"not found", {}))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*cert)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def cert(tmp_path):
    return make_cert(tmp_path)


@pytest.fixture
def secret(cert):
    s = Server(cert, {"/secret": (200, "text/plain", b"internal-only", {})})
    yield s
    s.close()


TRUST_TEST_CERT = ["--ignore-certificate-errors"]


def proxy_for(*open_ports, resolver=None, **kw):
    """ok.test is the site under test (trusted: it connects to the local server). Every other name resolves like a real
    resolver would: internal.test answers with a private address, which must be refused. `open_ports` are the ports the
    proxy permits, so a refusal in these tests is down to the ADDRESS rule, not the port rule."""
    async def private(host, port):
        raise UnsafeAddress(f"{host} points at a private or reserved address")
    return SafeProxy(resolve=resolver or private, trusted={"ok.test": "127.0.0.1"}, allowed_ports=(443, *open_ports), **kw)


async def audit(proxy: SafeProxy, url: str, wait_ms: int = 900):
    """Open one page exactly as the pipeline does, behind the guard. Returns (guard, whether the run was stopped)."""
    stopped = False
    async with proxy:
        guard = BrowserGuard(proxy)
        async with async_playwright() as pw:
            token = GUARD.set(guard)
            try:
                browser = await launch_browser(pw, args=TRUST_TEST_CERT)
                try:
                    ctx, page, _ = await _open(browser, url)
                    await page.wait_for_timeout(wait_ms)  # let every attempted request play out
                    await guard.raise_if_tripped()
                    await ctx.close()
                except BlockedByGuard:
                    stopped = True
                finally:
                    await browser.close()
            finally:
                GUARD.reset(token)
    return guard, stopped


async def test_a_page_cannot_reach_private_addresses_by_any_route(secret, cert):
    site = Server(cert, {"/pixel": (200, "image/gif", b"GIF89a", {})})
    p = secret.port
    site.routes["/"] = (200, "text/html", f"""<!doctype html><html lang=en><title>t</title><body>
    <img src="https://127.0.0.1:{p}/secret?img-literal">
    <img src="https://internal.test:{p}/secret?img-private-name">
    <img src="https://localhost:{p}/secret?img-localhost">
    <img src="https://[::1]:{p}/secret?img-ipv6">
    <img src="https://ok.test:{site.port}/pixel">
    <iframe src="https://127.0.0.1:{p}/secret?iframe"></iframe>
    <script>
      fetch("https://127.0.0.1:{p}/secret?fetch").catch(()=>0);
      fetch("https://169.254.169.254/latest/meta-data/").catch(()=>0);
      fetch("https://10.0.0.1/admin").catch(()=>0);
      fetch("http://127.0.0.1:{p}/secret?plain-http").catch(()=>0);
      new Image().src = "https://internal.test:{p}/secret?image-ctor";
      try {{ new WebSocket("wss://127.0.0.1:{p}/ws"); }} catch (e) {{}}
      navigator.sendBeacon("https://127.0.0.1:{p}/secret?beacon", "x");
    </script></body></html>""".encode(), {})
    try:
        guard, stopped = await audit(proxy_for(site.port, p), f"https://ok.test:{site.port}/")
        assert secret.hits == [], f"the private server was reached: {secret.hits}"
        assert "/pixel" in site.hits, "an ordinary request to the audited site must still work"
        assert stopped and len(guard.violations) >= 5, guard.violations
        assert any("private or reserved" in v for v in guard.violations)
    finally:
        site.close()


async def test_a_redirect_to_a_private_address_is_not_followed(secret, cert):
    site = Server(cert, {"/hop": (302, "text/plain", b"", {"Location": f"https://127.0.0.1:{secret.port}/secret?redirect"}),
                         "/hop-name": (302, "text/plain", b"", {"Location": f"https://internal.test:{secret.port}/secret?redirect-name"})})
    site.routes["/"] = (200, "text/html", b'<!doctype html><html lang=en><title>t</title><img src="/hop"><img src="/hop-name">', {})
    try:
        guard, stopped = await audit(proxy_for(site.port, secret.port), f"https://ok.test:{site.port}/")
        assert secret.hits == [], f"a redirect reached the private server: {secret.hits}"
        assert stopped and guard.violations
    finally:
        site.close()


async def test_the_browser_never_does_its_own_name_lookup(cert):
    """rebind.test exists only in the proxy's resolver. The page loads, so the proxy resolved it and connected to the
    address it got, once, and the browser had no say. There is no second lookup for an attacker to answer differently."""
    site = Server(cert, {"/": (200, "text/html", b"<!doctype html><html lang=en><title>t</title><h1>hello</h1>", {})})
    asked: list[str] = []

    async def resolver(host, port):
        asked.append(host)
        return ["127.0.0.1"]
    try:
        async with async_playwright() as pw, proxy_for(site.port, resolver=resolver) as proxy:
            token = GUARD.set(BrowserGuard(proxy))
            try:
                browser = await launch_browser(pw, args=TRUST_TEST_CERT)
                ctx, page, _ = await _open(browser, f"https://rebind.test:{site.port}/")
                assert await page.inner_text("h1") == "hello"
                await ctx.close()
                await browser.close()
            finally:
                GUARD.reset(token)
        assert asked == ["rebind.test"]  # looked up by the proxy, exactly once
    finally:
        site.close()


async def test_ports_that_are_not_for_web_traffic_are_refused(secret, cert):
    site = Server(cert, {"/": (200, "text/html", b"<!doctype html><html lang=en><title>t</title><h1>x</h1>", {})})
    try:
        guard, stopped = await audit(SafeProxy(trusted={"ok.test": "127.0.0.1"}), f"https://ok.test:{site.port}/")  # default ports only
        assert stopped and any(f"blocked port {site.port}" in v for v in guard.violations), guard.violations
        assert site.hits == []
    finally:
        site.close()


async def test_a_page_cannot_register_a_service_worker(cert):
    site = Server(cert, {"/sw.js": (200, "application/javascript", b"self.addEventListener('fetch', e => {})", {})})
    site.routes["/"] = (200, "text/html", b"<!doctype html><html lang=en><title>t</title><h1>x</h1>", {})
    try:
        async with async_playwright() as pw, proxy_for(site.port) as proxy:
            token = GUARD.set(BrowserGuard(proxy))
            try:
                assert context_options() == {"service_workers": "block", "accept_downloads": False}
                browser = await launch_browser(pw, args=TRUST_TEST_CERT)
                ctx, page, _ = await _open(browser, f"https://ok.test:{site.port}/")
                # The page's register() call still resolves, but Playwright never lets the worker run (0, where it would be 1).
                await page.evaluate("() => navigator.serviceWorker.register('/sw.js').then(() => 'ok', () => 'refused')")
                await page.wait_for_timeout(600)
                assert len(ctx.service_workers) == 0
                await ctx.close()
                await browser.close()
            finally:
                GUARD.reset(token)
        assert context_options() == {}  # and without a guard, nothing changes
    finally:
        site.close()


async def test_without_a_guard_nothing_changes(cert):
    site = Server(cert, {"/": (200, "text/html", b"<!doctype html><html lang=en><title>t</title><h1>hi</h1>", {})})
    try:
        async with async_playwright() as pw:
            assert GUARD.get() is None
            browser = await launch_browser(pw, args=TRUST_TEST_CERT)
            ctx, page, _ = await _open(browser, f"https://127.0.0.1:{site.port}/")  # direct, including loopback
            assert await page.inner_text("h1") == "hi"
            await ctx.close()
            await browser.close()
    finally:
        site.close()


# ---- the proxy on its own, with a raw socket: it must be a hard door
async def raw(proxy: SafeProxy, request: bytes) -> bytes:
    import asyncio

    r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
    w.write(request)
    await w.drain()
    try:
        return await asyncio.wait_for(r.read(4096), 5)
    finally:
        w.close()


def auth(proxy: SafeProxy) -> str:
    import base64

    return "Proxy-Authorization: Basic " + base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode() + "\r\n"


async def test_the_proxy_needs_credentials_and_only_speaks_connect():
    async with SafeProxy() as px:
        assert (await raw(px, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 407")
        wrong = b"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic d3Jvbmc6Y3JlZHM=\r\n\r\n"
        assert (await raw(px, wrong)).startswith(b"HTTP/1.1 407")
        assert (await raw(px, f"GET http://example.com/ HTTP/1.1\r\n{auth(px)}\r\n".encode())).startswith(b"HTTP/1.1 403")
        for target in ("127.0.0.1:443", "localhost:443", "[::1]:443", "169.254.169.254:443", "10.0.0.1:443", "[::ffff:127.0.0.1]:443"):
            r = await raw(px, f"CONNECT {target} HTTP/1.1\r\n{auth(px)}\r\n".encode())
            assert r.startswith(b"HTTP/1.1 403"), (target, r[:40])
        assert (await raw(px, f"CONNECT example.com:22 HTTP/1.1\r\n{auth(px)}\r\n".encode())).startswith(b"HTTP/1.1 403")
        assert (await raw(px, f"CONNECT nonsense HTTP/1.1\r\n{auth(px)}\r\n".encode())).startswith(b"HTTP/1.1 400")
        assert len(px.violations) >= 8


async def test_the_proxy_survives_garbage_and_oversized_requests():
    async with SafeProxy() as px:
        for junk in (b"", b"\x00\xff\x00\xff\r\n\r\n", b"CONNECT\r\n\r\n", b"A" * 20000 + b"\r\n\r\n", b"CONNECT a:b:c:443 HTTP/1.1\r\n\r\n"):
            try:
                await raw(px, junk)
            except (TimeoutError, ConnectionError, OSError):
                pass
        assert (await raw(px, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 407")  # still serving


async def test_a_run_has_a_connection_budget():
    async def public(host, port):
        return ["127.0.0.1"]
    async with SafeProxy(resolve=public, max_tunnels=2) as px:
        for _ in range(4):
            await raw(px, f"CONNECT example.com:443 HTTP/1.1\r\n{auth(px)}\r\n".encode())  # connection refused upstream, still counted
        assert "too many connections from one run" in px.violations

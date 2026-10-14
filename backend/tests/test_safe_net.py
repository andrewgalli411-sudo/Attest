"""The safe fetcher: checks the address, pins the connection, follows only same-site redirects, caps size and time."""

import httpx
import pytest

from auditor.netguard import UnsafeAddress
from auditor.safe_net import FetchError, SafeNet

PUBLIC = {"app.example.com": ["93.184.216.34"], "other.example.org": ["8.8.8.8"]}


def net(handler, resolver=None, **kw):
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return handler(req)

    async def resolve(host, port):
        if resolver:
            return await resolver(host, port)
        if host not in PUBLIC:
            raise UnsafeAddress(f"could not look up {host}")
        return PUBLIC[host]

    return SafeNet(transport=httpx.MockTransport(h), resolver=resolve, **kw), seen


async def test_reads_a_small_text_file():
    n, seen = net(lambda r: httpx.Response(200, text="attest-verification=abc\n"))
    got = await n.get_text("https://app.example.com/.well-known/attest-verification.txt")
    assert (got.status, got.text.strip()) == (200, "attest-verification=abc") and len(seen) == 1


async def test_follows_a_redirect_on_the_same_site_and_reports_where_it_ended():
    def h(r):
        return httpx.Response(301, headers={"location": "/final.txt"}) if r.url.path == "/start" else httpx.Response(200, text="done")
    n, seen = net(h)
    got = await n.get_text("https://app.example.com/start")
    assert got.text == "done" and got.url == "https://app.example.com/final.txt" and len(seen) == 2


async def test_refuses_a_redirect_to_another_site_without_visiting_it():
    n, seen = net(lambda r: httpx.Response(302, headers={"location": "https://other.example.org/x"}))
    with pytest.raises(FetchError, match="different site"):
        await n.get_text("https://app.example.com/x")
    assert [r.url.host for r in seen] == ["app.example.com"]  # the other site was never contacted


async def test_a_redirect_to_a_private_name_or_address_is_refused():
    for target in ("https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest", "https://10.0.0.1/"):
        n, seen = net(lambda r, t=target: httpx.Response(302, headers={"location": t}))
        with pytest.raises((UnsafeAddress, FetchError)):
            await n.get_text("https://app.example.com/x")
        assert len(seen) == 1, target


async def test_endless_redirects_stop():
    n, seen = net(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(FetchError, match="too many redirects"):
        await n.get_text("https://app.example.com/x", max_redirects=3)
    assert len(seen) == 4


async def test_a_name_that_resolves_private_is_never_contacted():
    async def private(host, port):
        raise UnsafeAddress(f"{host} points at a private or reserved address, which Attest will not visit")
    n, seen = net(lambda r: httpx.Response(200, text="secret"), resolver=private)
    with pytest.raises(UnsafeAddress, match="private"):
        await n.get_text("https://app.example.com/x")
    assert seen == []


@pytest.mark.parametrize("url", ["http://app.example.com/x", "https://localhost/x", "https://127.0.0.1/x", "https://app.example.com:22/x",
                                 "https://u:p@app.example.com/x", "file:///etc/passwd", "gopher://app.example.com/x"])
async def test_hostile_addresses_are_refused_before_any_lookup_or_request(url):
    n, seen = net(lambda r: httpx.Response(200, text="x"))
    with pytest.raises(UnsafeAddress):
        await n.get_text(url)
    assert seen == []


async def test_big_responses_are_cut_off():
    n, _ = net(lambda r: httpx.Response(200, content=b"a" * 5_000_000))
    got = await n.get_text("https://app.example.com/x", max_bytes=1000)
    assert len(got.text) == 1000


async def test_slow_or_broken_sites_give_a_readable_error():
    def slow(r):
        raise httpx.ReadTimeout("slow", request=r)
    n, _ = net(slow)
    with pytest.raises(FetchError, match="too long"):
        await n.get_text("https://app.example.com/x")

    def broken(r):
        raise httpx.ConnectError("refused", request=r)
    n, _ = net(broken)
    with pytest.raises(FetchError, match="could not reach"):
        await n.get_text("https://app.example.com/x")


async def test_the_connection_is_pinned_to_the_address_that_was_checked(monkeypatch):
    """Without an injected transport the request goes to the checked IP, with the real name kept for TLS and Host."""
    import auditor.safe_net as sn

    captured: dict = {}

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            captured.update(url=str(request.url), host=request.headers["host"], sni=request.extensions.get("sni_hostname"))
            return httpx.Response(200, text="ok", request=request)

    real = httpx.AsyncClient

    def through_the_spy(*a, **k):  # the real-network branch, but routed to the spy instead of the internet
        k["transport"] = Spy()
        return real(*a, **k)
    monkeypatch.setattr(sn.httpx, "AsyncClient", through_the_spy)

    async def resolve(host, port):
        return ["93.184.216.34"]
    await SafeNet(resolver=resolve).get_text("https://app.example.com/.well-known/x.txt")
    assert captured["url"] == "https://93.184.216.34/.well-known/x.txt"  # the checked address, not a second lookup
    assert captured["host"] == "app.example.com" and captured["sni"] == "app.example.com"


async def test_txt_lookups_go_through_the_injected_resolver():
    async def txt(name):
        return ["v=spf1", "attest-verification=abc"] if name == "_attest-challenge.app.example.com" else []
    n = SafeNet(txt=txt)
    assert "attest-verification=abc" in await n.txt_records("_attest-challenge.app.example.com")
    assert await n.txt_records("nothing.example.com") == []

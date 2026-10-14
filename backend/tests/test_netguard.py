"""Which addresses Attest will visit: syntax, IP classification, DNS answers, and a property test that nothing odd slips through."""

import socket

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from auditor.netguard import UnsafeAddress, is_public_ip, parse_site_url, resolve_public

GOOD = {
    "https://app.example.com": "https://app.example.com",
    "https://App.Example.COM/": "https://app.example.com",
    "https://a.b.co/app/": "https://a.b.co/app",
    "https://münchen.de": "https://xn--mnchen-3ya.de",
    "https://example.com.": "https://example.com",
    "https://my-site.vercel.app": "https://my-site.vercel.app",
}
BAD = [
    "", " ", "app.example.com", "//app.example.com", "http://app.example.com", "ftp://app.example.com", "javascript:alert(1)",
    "data:text/html,hi", "file:///etc/passwd", "https://", "https:///x", "https://localhost", "https://localhost.", "https://LOCALHOST",
    "https://intranet", "https://db.internal", "https://x.local", "https://x.home.arpa", "https://a.test", "https://a.onion",
    "https://127.0.0.1", "https://0x7f.0.0.1", "https://2130706433", "https://[::1]", "https://[::ffff:10.0.0.1]", "https://10.1.2.3",
    "https://169.254.169.254", "https://user@app.example.com", "https://user:pw@app.example.com", "https://app.example.com@evil.com",
    "https://app.example.com:22", "https://app.example.com:8443", "https://app.example.com:0", "https://app.example.com:99999",
    "https://app.example.com/?a=1", "https://app.example.com/#x", "https://app.example.com/../x", "https://app.example.com//x",
    "https://app.example.com\\evil.com", "https://app.example.com%2f.evil.com", "https://-bad.example.com", "https://bad-.example.com",
    "https://a..example.com", "https://" + "a" * 64 + ".example.com", "https://exa mple.com", "https://x.com\n.evil.com",
    "https://x.com\x00", "https://" + "a." * 130 + "com", "https://1.2.3.4.5", "https://example.123",
]


@pytest.mark.parametrize("raw, want", GOOD.items())
def test_ordinary_site_addresses_are_accepted_and_normalised(raw, want):
    assert parse_site_url(raw).url == want


@pytest.mark.parametrize("raw", BAD)
def test_everything_else_is_refused_with_a_message_a_person_can_read(raw):
    with pytest.raises(UnsafeAddress) as e:
        parse_site_url(raw)
    assert str(e.value) and "Traceback" not in str(e.value)


def test_the_operator_can_widen_ports_and_allow_http_for_development_only():
    assert parse_site_url("http://app.example.com", allow_insecure=True, allowed_ports=(80,)).url == "http://app.example.com"
    assert parse_site_url("https://app.example.com:8443", allowed_ports=(443, 8443)).url == "https://app.example.com:8443"
    with pytest.raises(UnsafeAddress):
        parse_site_url("http://app.example.com")  # insecure stays off by default


@given(st.text(max_size=120))
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_whatever_you_type_the_parser_either_refuses_cleanly_or_returns_a_public_looking_name(raw):
    try:
        site = parse_site_url(raw)
    except UnsafeAddress:
        return
    assert site.scheme == "https" and "." in site.host and site.port == 443
    assert all(c.isalnum() or c in ".-" for c in site.host) and "@" not in site.url and " " not in site.url
    assert not site.host.endswith((".local", ".internal", ".localhost"))


PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
PRIVATE = ["10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.0.1", "127.0.0.1", "127.1.2.3", "0.0.0.0",
           "169.254.169.254", "169.254.0.1", "100.64.0.1", "100.100.100.200", "192.0.0.1", "198.18.0.1", "224.0.0.1", "240.0.0.1",
           "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
           "::ffff:169.254.169.254", "2002:0a00:0001::", "2002:7f00:0001::", "64:ff9b::a00:1", "64:ff9b::7f00:1"]


@pytest.mark.parametrize("ip", PUBLIC)
def test_public_addresses_are_public(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize("ip", PRIVATE)
def test_private_reserved_and_wrapped_addresses_are_not(ip):
    assert not is_public_ip(ip), ip


def fake_dns(monkeypatch, answers):
    def getaddrinfo(host, port, *a, **k):
        if isinstance(answers, Exception):
            raise answers
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def test_a_name_must_resolve_to_public_addresses_only(monkeypatch):
    fake_dns(monkeypatch, ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
    assert await resolve_public("app.example.com") == ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]
    for bad in (["10.0.0.5"], ["93.184.216.34", "10.0.0.5"], ["8.8.8.8", "169.254.169.254"], ["::1"], ["8.8.8.8", "::ffff:127.0.0.1"], []):
        fake_dns(monkeypatch, bad)  # even ONE private answer poisons the name: an attacker's DNS can mix them
        with pytest.raises(UnsafeAddress):
            await resolve_public("evil.example.com")
    fake_dns(monkeypatch, socket.gaierror("nope"))
    with pytest.raises(UnsafeAddress, match="could not look up"):
        await resolve_public("nonexistent.example.com")

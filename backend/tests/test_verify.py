"""Proving you own the site: a file or a DNS record, checked through the safe fetcher."""

import httpx
import pytest
from fakegithub import FakeGitHub
from sqlalchemy import select
from test_accounts import H, browser, sign_in
from test_projects import make, new_project

import auditor.api.projects as projects_api
from auditor.models import ProjectRecord
from auditor.netguard import UnsafeAddress
from auditor.safe_net import FetchError, SafeNet

PUBLIC = {"app.example.com": ["93.184.216.34"]}


@pytest.fixture
def gh():
    g = FakeGitHub()
    g.add_user("alice", 101)
    g.add_user("bob", 202)
    g.add_installation(1, "alice", ["alice/web"])
    return g


@pytest.fixture(autouse=True)
def no_cooldown(monkeypatch):
    monkeypatch.setattr(projects_api, "VERIFY_COOLDOWN_S", 0.0)


def wire(app, *, file=None, txt=None, private=False):
    """Replace the app's network with a scripted one."""
    async def resolve(host, port):
        if private:
            raise UnsafeAddress(f"{host} points at a private or reserved address, which Attest will not visit")
        return PUBLIC[host]

    async def lookup(name):
        if isinstance(txt, Exception):
            raise txt
        return txt or []

    handler = file or (lambda r: httpx.Response(404))
    app.state.deps.net = SafeNet(transport=httpx.MockTransport(handler), resolver=resolve, txt=lookup)


async def attach(c):
    return (await c.post("/api/projects", headers=H, json=new_project())).json()


def token(p):
    return p["verification"]["file_contents"]


async def test_the_file_proves_ownership(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh)
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app, file=lambda r: httpx.Response(200, text=f"# hello\r\n{token(p)}\r\n") if r.url.path == "/.well-known/attest-verification.txt" else httpx.Response(404))
        r = (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()
        assert r["verified"] is True and r["method"] == "file"
        assert (await c.get(f"/api/projects/{p['id']}")).json()["verified"] is True
        assert (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json() == {"verified": True, "method": "already", "checks": []}
    with sf() as db:
        assert db.scalar(select(ProjectRecord)).verified_at is not None


async def test_a_dns_record_proves_ownership_too(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh)
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app, txt=["v=spf1 -all", token(p)])
        r = (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()
        assert r["verified"] is True and r["method"] == "dns"
        assert {c["method"]: c["ok"] for c in r["checks"]} == {"file": False, "dns": True}


@pytest.mark.parametrize("content", ["", "attest-verification=wrong", "attest-verification=", "xattest-verification=TOKEN", "TOKEN is here", "<html>TOKEN</html>"])
async def test_a_file_without_exactly_your_code_proves_nothing(sf, tmp_path, gh, content):
    app = make(sf, tmp_path, gh)
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app, file=lambda r: httpx.Response(200, text=content.replace("TOKEN", token(p))))
        r = (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()
        assert r["verified"] is False and r["method"] is None
        assert "does not contain your code" in next(x for x in r["checks"] if x["method"] == "file")["detail"]
    with sf() as db:
        assert db.scalar(select(ProjectRecord)).verified_at is None


async def test_it_says_what_it_saw_when_nothing_works(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh)
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app)  # 404 and no TXT records
        r = (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()
        by = {x["method"]: x["detail"] for x in r["checks"]}
        assert r["verified"] is False and "answered 404" in by["file"] and by["dns"] == "no TXT record found"
        wire(app, txt=["something-else"])
        by = {x["method"]: x["detail"] for x in (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()["checks"]}
        assert "none with your code" in by["dns"]
        wire(app, txt=FetchError("could not look up _attest-challenge.app.example.com"))
        by = {x["method"]: x["detail"] for x in (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()["checks"]}
        assert "could not look up" in by["dns"]


async def test_a_site_that_points_at_a_private_address_is_never_asked(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh)
    hits = []
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app, file=lambda r: hits.append(r) or httpx.Response(200, text=token(p)), private=True)
        r = (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()
        assert r["verified"] is False and "private or reserved" in next(x for x in r["checks"] if x["method"] == "file")["detail"]
        assert hits == []  # nothing was sent to the internal address


async def test_you_cannot_verify_someone_elses_project_and_checks_are_rate_limited(sf, tmp_path, gh, monkeypatch):
    app = make(sf, tmp_path, gh)
    async with browser(app) as alice, browser(app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        p = await attach(alice)
        wire(app, file=lambda r: httpx.Response(200, text=token(p)))
        assert (await bob.post(f"/api/projects/{p['id']}/verify", headers=H)).status_code == 404
        with sf() as db:
            assert db.scalar(select(ProjectRecord)).verified_at is None  # bob's attempt proved nothing
        monkeypatch.setattr(projects_api, "VERIFY_COOLDOWN_S", 30.0)
        wire(app)
        assert (await alice.post(f"/api/projects/{p['id']}/verify", headers=H)).status_code == 200
        again = await alice.post(f"/api/projects/{p['id']}/verify", headers=H)
        assert again.status_code == 429 and again.headers["retry-after"]


async def test_verification_is_per_address(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh)
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = await attach(c)
        wire(app, file=lambda r: httpx.Response(200, text=token(p)))
        assert (await c.post(f"/api/projects/{p['id']}/verify", headers=H)).json()["verified"] is True
        moved = (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"site_url": "https://other.example.org"})).json()
        assert moved["verified"] is False  # a new address must be proven again, with the same code

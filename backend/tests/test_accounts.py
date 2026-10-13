"""Accounts mode: sign in with GitHub (a fake one), sessions, CSRF, and above all that nobody can see anyone else's data."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import Account, AuditRun, HumanDecision, UserSession
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(300)

USERS = {"alice": {"id": 101, "login": "alice", "name": "Alice A", "avatar_url": "https://avatars.example/a.png"},
         "bob": {"id": 202, "login": "bob", "name": "Bob B", "avatar_url": None}}
H = {"X-Requested-With": "attest"}


def fake_github(request: httpx.Request) -> httpx.Response:
    """Just enough of GitHub: the OAuth code exchange and /user."""
    if request.url.path == "/login/oauth/access_token":
        code = parse_qs(request.content.decode())["code"][0]
        who = code.removeprefix("code-")
        if who not in USERS:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(200, json={"access_token": f"gho_secret_{who}"})
    if request.url.path == "/user":
        who = request.headers["authorization"].removeprefix("Bearer gho_secret_")
        return httpx.Response(200, json=USERS[who]) if who in USERS else httpx.Response(401, json={})
    return httpx.Response(404, json={})


def settings(**kw) -> Settings:
    return Settings(auth_mode="accounts", secrets_key=Fernet.generate_key().decode(), github_client_id="cid",
                    github_client_secret="csec", public_url="http://t", allowed_hosts=["t"], **kw)


@pytest.fixture
def app(sf, tmp_path):
    a = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path), config=settings())
    a.state.github_transport = httpx.MockTransport(fake_github)
    return a


def browser(app) -> httpx.AsyncClient:
    """One cookie jar = one person's browser."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", follow_redirects=False)


async def sign_in(c: httpx.AsyncClient, who: str, nxt: str = "/") -> httpx.Response:
    r = await c.get("/api/auth/github/login", params={"next": nxt})
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["http://t/api/auth/github/callback"]
    return await c.get("/api/auth/github/callback", params={"code": f"code-{who}", "state": q["state"][0]})


async def test_signing_in_creates_an_account_and_a_hashed_session(app, sf):
    async with browser(app) as c:
        assert (await c.get("/api/runs")).status_code == 401  # nothing without signing in
        assert (await c.get("/api/auth/config")).json() == {"mode": "accounts", "signed_in": False, "login": None}
        r = await sign_in(c, "alice", "/runs")
        assert r.status_code == 302 and r.headers["location"] == "http://t/runs"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "attest_session=" in cookie
        me = (await c.get("/api/me")).json()
        assert me["login"] == "alice" and me["mode"] == "accounts" and me["name"] == "Alice A"
        assert (await c.get("/api/auth/config")).json()["signed_in"] is True
        token = c.cookies["attest_session"]
    with sf() as db:
        row = db.scalar(select(UserSession))
        assert token not in (row.token_hash, row.sealed_github_token)  # only a hash of the cookie is stored
        assert row.sealed_github_token.startswith("v1:") and "gho_" not in row.sealed_github_token  # the GitHub token is encrypted
        assert db.scalar(select(Account)).github_id == 101


async def test_signing_in_twice_reuses_the_account(app, sf):
    async with browser(app) as c1, browser(app) as c2:
        await sign_in(c1, "alice")
        USERS["alice"]["name"] = "Alice Renamed"
        try:
            await sign_in(c2, "alice")
        finally:
            USERS["alice"]["name"] = "Alice A"
        assert (await c2.get("/api/me")).json()["name"] == "Alice Renamed"
    with sf() as db:
        assert len(db.scalars(select(Account)).all()) == 1 and len(db.scalars(select(UserSession)).all()) == 2


async def test_a_forged_or_replayed_sign_in_is_refused(app, sf):
    async with browser(app) as c:
        r = await c.get("/api/auth/github/login")
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        assert (await c.get("/api/auth/github/callback", params={"code": "code-alice", "state": "wrong"})).status_code == 400
        assert (await c.get("/api/auth/github/callback", params={"code": "code-nobody", "state": state})).status_code == 400
        assert (await c.get("/api/auth/github/callback", params={"state": state})).status_code == 400  # no code
    async with browser(app) as fresh:  # a callback with no matching state cookie at all (someone else's link)
        assert (await fresh.get("/api/auth/github/callback", params={"code": "code-alice", "state": state})).status_code == 400
    with sf() as db:
        assert db.scalars(select(UserSession)).all() == []


@pytest.mark.parametrize("nxt", ["//evil.example", "https://evil.example/x", "/\\evil.example", "javascript:alert(1)", "evil", "/a\nb"])
async def test_sign_in_can_never_be_used_as_an_open_redirect(app, nxt):
    async with browser(app) as c:
        r = await sign_in(c, "alice", nxt)
        assert r.status_code == 302 and r.headers["location"] == "http://t/"


async def test_sessions_expire_and_logout_ends_them(app, sf):
    async with browser(app) as c:
        await sign_in(c, "alice")
        assert (await c.get("/api/runs")).status_code == 200
        with sf() as db:  # time passes
            row = db.scalar(select(UserSession))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        assert (await c.get("/api/runs")).status_code == 401
    async with browser(app) as c:
        await sign_in(c, "alice")
        stolen = c.cookies["attest_session"]
        assert (await c.post("/api/auth/logout", headers=H)).status_code == 204
        assert (await c.get("/api/runs")).status_code == 401
        c.cookies.set("attest_session", stolen)  # replaying the old cookie after logout gets nothing
        assert (await c.get("/api/runs")).status_code == 401
        c.cookies.set("attest_session", "x" * 500)
        assert (await c.get("/api/runs")).status_code == 401


async def test_changes_need_the_csrf_header(app):
    async with browser(app) as c:
        await sign_in(c, "alice")
        r = await c.post("/api/auth/logout")  # what a cross-site form would send: cookies, but no custom header
        assert r.status_code == 403 and "X-Requested-With" in r.json()["detail"]
        assert (await c.post("/api/runs", json={"demo": True})).status_code == 403
        assert (await c.get("/api/runs")).status_code == 200  # reading needs no header
        assert (await c.post("/api/auth/logout", headers=H)).status_code == 204


async def test_nobody_can_see_or_touch_anyone_elses_run(app, sf):
    async with browser(app) as alice, browser(app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        rid = (await alice.post("/api/runs", json={"demo": True, "pages": [{"path": "/", "label": "profile"}]}, headers=H)).json()["id"]
        for _ in range(480):
            d = (await alice.get(f"/api/runs/{rid}/dashboard")).json()
            if d["run"]["status"] in ("complete", "failed"):
                break
            await asyncio.sleep(0.5)
        assert d["run"]["status"] == "complete"
        fid = d["groups"][0]["findings"][0]["id"]
        label = next(f["id"] for g in d["groups"] for f in g["findings"] if f["rule_id"] == "label")
        ev = next(e["id"] for e in (await alice.get(f"/api/findings/{label}")).json()["evidence"] if e["url"])
        assert (await alice.get(f"/api/evidence/{ev}")).status_code == 200

        with sf() as db:
            owner = db.scalar(select(AuditRun).where(AuditRun.id == rid)).owner_id
            assert owner == db.scalar(select(Account).where(Account.login == "alice")).id

        missing = (await bob.get("/api/runs/does-not-exist/dashboard"))
        reads = [f"/api/runs/{rid}/dashboard", f"/api/runs/{rid}/pull-requests", f"/api/runs/{rid}/report",
                 f"/api/findings/{fid}", f"/api/evidence/{ev}", f"/api/runs/{rid}/events"]
        for path in reads:
            r = await bob.get(path)
            assert r.status_code == 404, (path, r.status_code)
            assert "alice" not in r.text and rid not in r.text and fid not in r.text, path
        assert (await bob.get(f"/api/runs/{rid}/dashboard")).json() == {"detail": "run not found"} == missing.json()  # same as a missing id
        for method, path, body in (("POST", f"/api/runs/{rid}/cancel", None), ("POST", f"/api/runs/{rid}/publish", None),
                                   ("POST", f"/api/findings/{fid}/decision", {"action": "accept_pr"})):
            r = await bob.request(method, path, json=body, headers=H)
            assert r.status_code == 404, (path, r.status_code)
        assert (await bob.get("/api/runs")).json() == []  # his list does not include hers
        assert (await bob.get("/api/status")).status_code == 404  # global counters are for the operator only
        with sf() as db:
            assert db.scalars(select(HumanDecision)).all() == []  # bob's attempt wrote nothing

        # alice still has everything, and her decision is recorded under her own login
        assert len((await alice.get("/api/runs")).json()) == 1
        assert (await alice.post(f"/api/findings/{label}/decision", json={"action": "accept_pr", "reviewer": "someone-else"}, headers=H)).status_code == 201
        with sf() as db:
            assert db.scalar(select(HumanDecision)).reviewer == "alice"  # a caller cannot claim to be somebody else


def test_accounts_mode_refuses_to_start_half_configured(sf, tmp_path):
    for missing in ("secrets_key", "github_client_id", "github_client_secret"):
        cfg = settings()
        cfg = cfg.model_copy(update={missing: ""})
        with pytest.raises(RuntimeError, match=missing.upper()):
            create_app(sf, config=cfg, work_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        create_app(sf, config=settings().model_copy(update={"secrets_key": "short"}), work_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="AUTH_MODE"):
        create_app(sf, config=Settings(auth_mode="open"), work_dir=str(tmp_path))


async def test_a_shared_server_never_runs_operators_projects_or_custom_commands(sf, tmp_path):
    pf = tmp_path / "projects.json"
    pf.write_text('{"projects": {"web": {"repo_path": "/tmp", "dev_command": "echo {port}"}}}')
    cfg = settings(projects_file=str(pf), allow_custom_runs=True)
    a = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path), config=cfg)
    a.state.github_transport = httpx.MockTransport(fake_github)
    assert a.state.deps.projects == {} and a.state.deps.cfg.allow_custom_runs is False
    async with browser(a) as c:
        await sign_in(c, "alice")
        assert (await c.get("/api/config")).json()["projects"] == []
        assert (await c.post("/api/runs", json={"project": "web"}, headers=H)).status_code == 404
        r = await c.post("/api/runs", json={"repo_path": "/tmp", "dev_command": "touch /tmp/pwned {port}"}, headers=H)
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert not (tmp_path / "pwned").exists()


async def test_local_mode_is_unchanged(sf, tmp_path):
    a = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                   config=Settings(allowed_hosts=["t"]))
    async with browser(a) as c:
        assert (await c.get("/api/auth/config")).json() == {"mode": "local", "signed_in": True, "login": None}
        assert (await c.get("/api/me")).json()["mode"] == "local"
        assert (await c.get("/api/auth/github/login")).status_code == 404  # sign-in does not exist in local mode
        assert (await c.get("/api/status")).status_code == 200


def test_the_session_cookie_gets_the_secure_flag_on_https(sf, tmp_path):
    from auditor.api.accounts import _safe_next, _secure

    assert _secure(Settings(public_url="https://attest.example")) and not _secure(Settings(public_url="http://localhost:3000"))
    assert _safe_next("/ok?x=1") == "/ok?x=1" and re.fullmatch(r"/", _safe_next(None))

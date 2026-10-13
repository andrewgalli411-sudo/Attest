"""Attaching projects: a GitHub repository plus a site, owned by an account, with secrets sealed."""

import httpx
import pytest
from cryptography.fernet import Fernet
from fakegithub import FakeGitHub
from sqlalchemy import select
from test_accounts import H, browser, sign_in

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import AuditRun, ProjectRecord, RunStatus
from auditor.storage.evidence_store import EvidenceStore

SECRET_COOKIE = "s3cr3t-session-value-xyz"


@pytest.fixture
def gh():
    g = FakeGitHub()
    g.add_user("alice", 101)
    g.add_user("bob", 202)
    g.add_installation(1, "alice", ["alice/web", "alice/api"])
    g.add_installation(2, "bob", ["bob/shop"])
    return g


def make(sf, tmp_path, gh, **kw):
    cfg = Settings(auth_mode="accounts", secrets_key=Fernet.generate_key().decode(), github_client_id="cid",
                   github_client_secret="csec", public_url="http://t", allowed_hosts=["t"], github_app_slug="attest-app", **kw)
    app = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path), config=cfg)
    app.state.github_transport = gh.transport
    return app


@pytest.fixture
def app(sf, tmp_path, gh):
    return make(sf, tmp_path, gh)


def new_project(**kw):
    return {"github_repo": "alice/web", "installation_id": 1, "site_url": "https://app.example.com", **kw}


async def test_you_see_only_repositories_you_can_reach_through_the_app(app, gh):
    async with browser(app) as alice, browser(app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        r = (await alice.get("/api/github/repos")).json()
        assert sorted(x["full_name"] for x in r["repos"]) == ["alice/api", "alice/web"]
        assert all(x["installation_id"] == 1 and x["attached"] is False for x in r["repos"])
        assert r["install_url"] == "https://github.com/apps/attest-app/installations/new"
        assert [x["full_name"] for x in (await bob.get("/api/github/repos")).json()["repos"]] == ["bob/shop"]


async def test_attaching_a_project_seals_the_login_and_never_returns_it(app, sf):
    async with browser(app) as c:
        await sign_in(c, "alice")
        r = await c.post("/api/projects", headers=H, json=new_project(
            pages=[{"path": "/", "label": "home"}, {"path": "/dashboard"}], login_cookies=[{"name": "session", "value": SECRET_COOKIE}]))
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["name"] == "web" and p["github_repo"] == "alice/web" and p["default_branch"] == "main"
        assert p["site_url"] == "https://app.example.com" and p["verified"] is False and p["allow_pr"] is False
        assert [x["path"] for x in p["pages"]] == ["/", "/dashboard"] and p["pages"][1]["label"] == "/dashboard"
        assert p["has_login"] is True and p["login_cookie_names"] == ["session"]
        assert p["verification"]["file_path"] == "/.well-known/attest-verification.txt"
        assert p["verification"]["dns_name"] == "_attest-challenge.app.example.com"
        assert p["verification"]["file_contents"] == p["verification"]["dns_value"] and "attest-verification=" in p["verification"]["file_contents"]
        everything = r.text + (await c.get(f"/api/projects/{p['id']}")).text + (await c.get("/api/projects")).text + (await c.get("/api/config")).text
        assert SECRET_COOKIE not in everything, "a login value must never come back out of the API"
        assert next(x for x in (await c.get("/api/github/repos")).json()["repos"] if x["full_name"] == "alice/web")["attached"] is True
    with sf() as db:
        row = db.scalar(select(ProjectRecord))
        assert row.sealed_auth.startswith("v1:") and SECRET_COOKIE not in row.sealed_auth  # encrypted at rest
        assert row.verification_token and row.verified_at is None


async def test_you_cannot_attach_a_repository_you_cannot_reach(app, sf):
    async with browser(app) as c:
        await sign_in(c, "alice")
        for body in (new_project(github_repo="bob/shop", installation_id=2),        # someone else's repository and installation
                     new_project(github_repo="bob/shop", installation_id=1),        # their repo claimed under your installation
                     new_project(github_repo="alice/web", installation_id=2),       # your repo claimed under their installation
                     new_project(github_repo="alice/nope", installation_id=1),      # a repository that is not installed
                     new_project(installation_id=999)):                             # an installation that does not exist
            r = await c.post("/api/projects", headers=H, json=body)
            assert r.status_code == 403 and "install the app" in r.json()["detail"], (body, r.text)
    with sf() as db:
        assert db.scalars(select(ProjectRecord)).all() == []


@pytest.mark.parametrize("url", [
    "http://app.example.com", "https://localhost", "https://127.0.0.1", "https://10.0.0.5", "https://[::1]", "https://intranet",
    "https://db.internal", "https://user:pw@app.example.com", "https://app.example.com:8080", "https://app.example.com/?x=1",
    "ftp://app.example.com", "javascript:alert(1)", "https://app.example.com/../etc", "", "   ", "app.example.com"])
async def test_hostile_or_private_site_addresses_are_refused(app, sf, url):
    async with browser(app) as c:
        await sign_in(c, "alice")
        r = await c.post("/api/projects", headers=H, json=new_project(site_url=url))
        assert r.status_code == 422, (url, r.status_code, r.text)
    with sf() as db:
        assert db.scalars(select(ProjectRecord)).all() == []


@pytest.mark.parametrize("bad", [
    {"github_repo": "not-a-repo"}, {"github_repo": "a/b/c"}, {"installation_id": 0}, {"name": ""}, {"name": "x" * 61},
    {"pages": [{"path": "http://evil.example/"}]}, {"pages": [{"path": "//evil.example"}]}, {"pages": [{"path": "/" + "a" * 600}]},
    {"login_cookies": [{"name": "a b", "value": "x"}]}, {"login_cookies": [{"name": "s", "value": "a;b"}]},
    {"login_cookies": [{"name": "s", "value": "x\r\nSet-Cookie: y"}]}, {"login_cookies": [{"name": f"c{i}", "value": "v"} for i in range(6)]},
    {"preview_url_template": "https://{evil}.example.app"}, {"preview_url_template": "http://pr-{pr}.example.app"}])
async def test_malformed_project_input_is_refused(app, sf, bad):
    async with browser(app) as c:
        await sign_in(c, "alice")
        r = await c.post("/api/projects", headers=H, json=new_project(**bad))
        assert r.status_code in (409, 422) and r.status_code != 500, (bad, r.status_code, r.text)
    with sf() as db:
        assert db.scalars(select(ProjectRecord)).all() == []


async def test_projects_belong_to_their_owner(app, sf):
    async with browser(app) as alice, browser(app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        pid = (await alice.post("/api/projects", headers=H, json=new_project())).json()["id"]
        assert (await bob.get("/api/projects")).json() == []
        for method, path, body in (("GET", f"/api/projects/{pid}", None), ("PATCH", f"/api/projects/{pid}", {"name": "stolen"}),
                                   ("DELETE", f"/api/projects/{pid}", None)):
            r = await bob.request(method, path, json=body, headers=H)
            assert r.status_code == 404 and r.json() == {"detail": "project not found"}, (method, r.status_code)
        assert (await bob.get("/api/projects/nope")).json() == {"detail": "project not found"}  # same as a missing one
        assert (await bob.get("/api/config")).json()["projects"] == []
        assert [p["name"] for p in (await alice.get("/api/config")).json()["projects"]] == ["web"]
    with sf() as db:
        assert db.scalar(select(ProjectRecord)).name == "web"  # untouched


async def test_changing_the_address_means_proving_ownership_again(app, sf):
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = (await c.post("/api/projects", headers=H, json=new_project())).json()
        from datetime import UTC, datetime
        with sf() as db:
            row = db.get(ProjectRecord, p["id"])
            row.verified_at = datetime.now(UTC)
            db.commit()
        assert (await c.get(f"/api/projects/{p['id']}")).json()["verified"] is True
        same = await c.patch(f"/api/projects/{p['id']}", headers=H, json={"site_url": "https://APP.example.com/", "name": "renamed"})
        assert same.json()["verified"] is True and same.json()["name"] == "renamed"  # the same address stays verified
        moved = (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"site_url": "https://other.example.org"})).json()
        assert moved["verified"] is False and moved["site_url"] == "https://other.example.org"
        assert (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"site_url": "https://localhost"})).status_code == 422
        assert (await c.get(f"/api/projects/{p['id']}")).json()["site_url"] == "https://other.example.org"  # the bad edit changed nothing


async def test_login_cookies_can_be_replaced_and_removed(app):
    async with browser(app) as c:
        await sign_in(c, "alice")
        p = (await c.post("/api/projects", headers=H, json=new_project())).json()
        assert p["has_login"] is False
        r = (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"login_cookies": [{"name": "sid", "value": SECRET_COOKIE}]})).json()
        assert r["has_login"] is True and r["login_cookie_names"] == ["sid"] and SECRET_COOKIE not in str(r)
        keep = (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"allow_pr": True})).json()
        assert keep["has_login"] is True and keep["allow_pr"] is True  # omitted means unchanged
        gone = (await c.patch(f"/api/projects/{p['id']}", headers=H, json={"login_cookies": []})).json()
        assert gone["has_login"] is False and gone["login_cookie_names"] == []


async def test_limits_and_duplicates(sf, tmp_path, gh):
    app = make(sf, tmp_path, gh, max_projects_per_account=2)
    async with browser(app) as c:
        await sign_in(c, "alice")
        assert (await c.post("/api/projects", headers=H, json=new_project())).status_code == 201
        dup = await c.post("/api/projects", headers=H, json=new_project())
        assert dup.status_code == 409 and "already have" in dup.json()["detail"]
        assert (await c.post("/api/projects", headers=H, json=new_project(github_repo="alice/api", name="api"))).status_code == 201
        third = await c.post("/api/projects", headers=H, json=new_project(github_repo="alice/api", name="other"))
        assert third.status_code == 409 and "up to 2" in third.json()["detail"]


async def test_deleting_keeps_history_but_not_while_a_run_is_going(app, sf):
    async with browser(app) as c:
        await sign_in(c, "alice")
        pid = (await c.post("/api/projects", headers=H, json=new_project())).json()["id"]
        with sf() as db:
            owner = db.get(ProjectRecord, pid).owner_id
            run = AuditRun(owner_id=owner, project_id=pid, target_url="project:web", status=RunStatus.detecting)
            db.add(run)
            db.commit()
            rid = run.id
        busy = await c.delete(f"/api/projects/{pid}", headers=H)
        assert busy.status_code == 409 and "in progress" in busy.json()["detail"]
        with sf() as db:
            db.get(AuditRun, rid).status = RunStatus.complete
            db.commit()
        assert (await c.delete(f"/api/projects/{pid}", headers=H)).status_code == 204
        assert (await c.get(f"/api/projects/{pid}")).status_code == 404
        assert [r["id"] for r in (await c.get("/api/runs")).json()] == [rid]  # the past run is still there
    with sf() as db:
        assert db.get(AuditRun, rid).project_id is None


async def test_an_expired_github_sign_in_sends_you_back_to_sign_in(app, gh):
    async with browser(app) as c:
        await sign_in(c, "alice")
        gh.expired_tokens.add("gho_secret_alice")
        for r in (await c.get("/api/github/repos"), await c.post("/api/projects", headers=H, json=new_project())):
            assert r.status_code == 401 and r.json() == {"detail": "sign in required"}


async def test_project_endpoints_need_sign_in_and_the_csrf_header(app):
    async with browser(app) as anon:
        for method, path in (("GET", "/api/projects"), ("POST", "/api/projects"), ("GET", "/api/github/repos")):
            assert (await anon.request(method, path, json={} if method == "POST" else None)).status_code == 401
    async with browser(app) as c:
        await sign_in(c, "alice")
        assert (await c.post("/api/projects", json=new_project())).status_code == 403  # no X-Requested-With


async def test_local_mode_has_no_project_database(sf, tmp_path):
    a = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                   config=Settings(allowed_hosts=["t"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
        for method, path in (("GET", "/api/projects"), ("GET", "/api/github/repos")):
            assert (await c.request(method, path)).status_code == 404

"""Auditing a LIVE site: the production build of the demo app served over HTTPS, its source handed over as a GitHub
tarball, the whole thing run through the API as a signed-in account. Nothing is built or run on Attest's side."""

import asyncio
import io
import mimetypes
import ssl
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fakegithub import FakeGitHub
from sqlalchemy import select
from test_accounts import H, browser, sign_in
from test_github_repo import private_key_pem
from test_safe_browse import Server, make_cert

from auditor.config import Settings
from auditor.demo import SEED_AUTH, prepare_demo_repo
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import Account, AuditRun, Finding, FixAttempt, ProjectRecord
from auditor.pipeline.safe_browse import BrowserGuard
from auditor.pipeline.safe_proxy import SafeProxy
from auditor.scm.github_app import GitHubAppAuth
from auditor.scm.github_repo import GitHubRepoClient
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(600)
HOST = "app.example.com"


class StaticSite:
    """Serves a built site over HTTPS (the demo's `dist/`), like a real deployment would, and records what it was asked for."""

    def __init__(self, root: Path, cert):
        outer, self.hits, self.root = self, [], root

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.hits.append(path)
                f = (outer.root / path.lstrip("/")).resolve()
                if not f.is_file() or outer.root.resolve() not in f.parents:
                    f = outer.root / "index.html"  # single-page app: unknown paths get the shell
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
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


def tarball(src: Path, top: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if p.is_file() and not {"node_modules", "dist", ".git"} & set(rel.parts):
                t.add(p, arcname=f"{top}/{rel}")
    return buf.getvalue()


@pytest.fixture(scope="module")
def demo_build(tmp_path_factory):
    """The demo app, built for production once for all the tests in this file."""
    root = tmp_path_factory.mktemp("demo")
    repo, _ = prepare_demo_repo(root / "src")
    subprocess.run(["pnpm", "build"], cwd=repo, check=True, capture_output=True, timeout=180)
    return repo, tarball(repo, "alice-web-1234567"), root


@pytest.fixture
def site(demo_build, tmp_path):
    repo, _, _ = demo_build
    cert = make_cert(tmp_path)
    s = StaticSite(repo / "dist", cert)
    yield s
    s.close()


class World:
    """Everything one test needs: fake GitHub, the app, and handles to look inside."""

    def __init__(self, sf, tmp_path, site, demo_build, gh_extra=None):
        _, self.tar, _ = demo_build
        self.site, self.tmp_path, self.sf = site, tmp_path, sf
        self.gh = FakeGitHub()
        for who, gid, inst in (("alice", 101, 1), ("bob", 202, 2)):
            self.gh.add_user(who, gid)
            self.gh.add_installation(inst, who, [f"{who}/web"])
            self.gh.repos[f"{who}/web"] = {"sha": ("a" if who == "alice" else "b") * 40, "tarball": self.tar}
        pem = private_key_pem()
        settings = {"auth_mode": "accounts", "secrets_key": Fernet.generate_key().decode(), "github_client_id": "cid",
                    "github_client_secret": "csec", "public_url": "http://t", "allowed_hosts": ["t"], "github_app_id": "1",
                    "github_app_private_key": pem, "allowed_site_ports": [443, site.port], "max_concurrent_runs": 3, **(gh_extra or {})}
        cfg = Settings(**settings)
        self.guards: list[BrowserGuard] = []

        def guard():
            g = BrowserGuard(SafeProxy(trusted={HOST: "127.0.0.1"}, allowed_ports=(443, site.port)),
                             browser_args=["--ignore-certificate-errors"])
            self.guards.append(g)
            return g

        def github(inst, repo):
            auth = GitHubAppAuth("1", pem, inst, api_base="https://api.github.com", transport=self.gh.transport)
            return GitHubRepoClient(repo, auth, transport=self.gh.transport)

        self.app = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path), config=cfg,
                              guard_factory=guard, github_factory=github)
        self.app.state.github_transport = self.gh.transport

    async def attach(self, c, who="alice", *, verified=True, name=None, **kw):
        body = {"github_repo": f"{who}/web", "installation_id": 1 if who == "alice" else 2, "site_url": f"https://{HOST}:{self.site.port}",
                "login_cookies": [{"name": c_["name"], "value": c_["value"]} for c_ in SEED_AUTH["cookies"]], "name": name or "web", **kw}
        r = await c.post("/api/projects", headers=H, json=body)
        assert r.status_code == 201, r.text
        p = r.json()
        if verified:
            from datetime import UTC, datetime
            with self.sf() as db:
                db.get(ProjectRecord, p["id"]).verified_at = datetime.now(UTC)
                db.commit()
        return p

    async def run(self, c, project="web", **kw):
        r = await c.post("/api/runs", headers=H, json={"project": project, **kw})
        assert r.status_code == 202, r.text
        rid = r.json()["id"]
        for _ in range(600):
            d = (await c.get(f"/api/runs/{rid}/dashboard")).json()
            if d["run"]["status"] in ("complete", "failed", "cancelled"):
                return rid, d
            await asyncio.sleep(0.5)
        raise AssertionError("run did not finish")


@pytest.fixture
def world(sf, tmp_path, site, demo_build):
    return World(sf, tmp_path, site, demo_build)


async def test_a_live_site_is_audited_and_fixes_are_proposed_without_building_anything(world, sf):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)
        rid, d = await world.run(c)
        assert d["run"]["status"] == "complete", d
        assert d["meta"]["mode"] == "remote" and d["meta"]["project"] == "web"
        assert d["headline"] == "3 issues - 2 fixes ready for review - 1 need you"
        assert d["counts"]["total"] == 3 and d["counts"]["awaiting_proof"] == 2 and d["counts"]["fixed"] == 0 and d["counts"]["needs_human"] == 1
        assert d["score_before"] and d["score_after"] is None, "nothing is proved yet, so there is no 'after' score"
        stages = {s["key"]: s for s in d["stages"]}
        assert [stages[k]["state"] for k in ("detect", "triage", "fix", "prove", "regress")] == ["done", "done", "done", "waiting", "waiting"]
        assert "preview deployment" in stages["prove"]["detail"]

        by = {f["rule_id"]: f for g in d["groups"] for f in g["findings"]}
        assert by["label"]["status"] == "fix_authored" and by["html-has-lang"]["status"] == "fix_authored"
        assert by["color-contrast"]["status"] == "needs_human"

        # the fix for the label was written from source found by matching the page to the repository (no React dev info on a deployed site)
        fd = (await c.get(f"/api/findings/{by['label']['id']}")).json()
        assert fd["source"].startswith("src/App.jsx:") and fd["fix"]["diff"] and "aria-label" in fd["fix"]["diff"] + "htmlFor"
        assert fd["proof"] is None and fd["regression"] is None  # not proved yet, and not pretending to be
        assert fd["mode"] == "remote" and fd["status"] == "fix_authored"

        prs = (await c.get(f"/api/runs/{rid}/pull-requests")).json()
        assert {r["rule_id"]: (r["proof"], r["decision"], r["remote"]) for r in prs["rows"]} == {
            "label": ("not_retested", "awaiting", "server_only"), "html-has-lang": ("not_retested", "awaiting", "server_only")}

    # what was audited, what was read, and what must NOT exist afterwards
    assert world.guards[0].violations == [] and "/" in world.site.hits
    assert world.gh.token_requests >= 1
    assert [m for m, _ in world.gh.calls if m not in ("GET", "POST")] == [], "a read-only audit must not write to GitHub"
    assert not (Path(world.app.state.deps.scratch_root) / rid).exists(), "the scratch copy of the customer's source must be deleted"
    with sf() as db:
        attempts = db.scalars(select(FixAttempt).join(Finding, Finding.id == FixAttempt.finding_id).where(Finding.run_id == rid)).all()
        assert len(attempts) == 2 and all(a.repo == "alice/web" and a.diff and a.edits for a in attempts)
        run = db.get(AuditRun, rid)
        assert run.config["mode"] == "remote" and run.config["base_sha"] == "a" * 40 and run.project_id
        assert "demo_session" not in str(run.config) and "path" not in str(sorted(run.config)).replace("pages", "")  # no secrets, no server paths


async def test_an_unverified_or_missing_or_foreign_project_cannot_be_audited(world, sf):
    async with browser(world.app) as alice, browser(world.app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        await world.attach(alice, verified=False)
        r = await alice.post("/api/runs", headers=H, json={"project": "web"})
        assert r.status_code == 409 and "prove you own this site" in r.json()["detail"]
        assert (await alice.post("/api/runs", headers=H, json={"project": "nope"})).status_code == 404
        assert (await bob.post("/api/runs", headers=H, json={"project": "web"})).status_code == 404  # bob has no such project
    with sf() as db:
        assert db.scalars(select(AuditRun)).all() == []  # nothing was started
    assert world.site.hits == [] and world.gh.calls and not any("tarball" in p for _, p in world.gh.calls)


async def test_two_accounts_auditing_identical_source_never_share_work(world, sf):
    """Same site, same source, same moment in time: each account must get its own fix attempts (they are keyed on a base commit)."""
    async with browser(world.app) as alice, browser(world.app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        await world.attach(alice)
        await world.attach(bob, "bob")
        (ra, da), (rb, db_) = await world.run(alice), await world.run(bob)
        assert da["run"]["status"] == db_["run"]["status"] == "complete"
        with sf() as db:
            rows = db.execute(select(FixAttempt, Finding).join(Finding, Finding.id == FixAttempt.finding_id)).all()
            assert len(rows) == 4
            assert {(f.run_id, a.repo) for a, f in rows} == {(ra, "alice/web"), (rb, "bob/web")}
            assert len({a.base_sha for a, _ in rows}) == 2 and len({a.idempotency_key for a, _ in rows}) == 4
        # and neither can see the other's findings
        bob_finding = next(f["id"] for g in db_["groups"] for f in g["findings"])
        assert (await alice.get(f"/api/findings/{bob_finding}")).status_code == 404


async def test_a_site_that_reaches_for_a_private_address_stops_the_run(world, sf, tmp_path, demo_build):
    (tmp_path / "s").mkdir()
    secret = Server(make_cert(tmp_path / "s"), {"/secret": (200, "text/plain", b"internal", {})})
    repo, _, _ = demo_build
    page = (repo / "dist" / "index.html").read_text()
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "index.html").write_text(page.replace("</body>", f'<img src="https://127.0.0.1:{secret.port}/secret"></body>'))
    for f in (repo / "dist" / "assets").iterdir():
        (hostile / "assets").mkdir(exist_ok=True)
        (hostile / "assets" / f.name).write_bytes(f.read_bytes())
    world.site.root = hostile
    world.guards.clear()
    try:
        async with browser(world.app) as c:
            await sign_in(c, "alice")
            await world.attach(c)
            rid, d = await world.run(c)
        assert d["run"]["status"] == "failed", d["run"]
        assert secret.hits == [], f"the private server was reached: {secret.hits}"
        row = next(r for r in (await _runs(world)) if r["id"] == rid)
        assert "private or reserved address" in row["result"], row
    finally:
        secret.close()


async def _runs(world):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        return (await c.get("/api/runs")).json()


async def test_the_server_says_so_when_it_is_not_connected_to_github(sf, tmp_path, site, demo_build):
    w = World(sf, tmp_path, site, demo_build, gh_extra={"github_app_id": "", "github_app_private_key": ""})
    async with browser(w.app) as c:
        await sign_in(c, "alice")
        await w.attach(c)
        r = await c.post("/api/runs", headers=H, json={"project": "web"})
        assert r.status_code == 503 and "GitHub App is not configured" in r.json()["detail"]


async def test_accounts_exist_for_both_people(world, sf):
    async with browser(world.app) as a, browser(world.app) as b:
        await sign_in(a, "alice")
        await sign_in(b, "bob")
    with sf() as db:
        assert sorted(x.login for x in db.scalars(select(Account))) == ["alice", "bob"]

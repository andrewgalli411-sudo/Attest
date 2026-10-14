"""Shared set-up for the live-site tests: a production build of the demo app served over HTTPS, a fake GitHub, and an app
wired to both. Registered as a pytest plugin from conftest.py, so its fixtures are available everywhere without imports."""

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
from test_accounts import H
from test_github_repo import private_key_pem
from test_safe_browse import make_cert

from auditor.config import Settings
from auditor.demo import SEED_AUTH, prepare_demo_repo
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import ProjectRecord
from auditor.pipeline.safe_browse import BrowserGuard
from auditor.pipeline.safe_proxy import SafeProxy
from auditor.scm.github_app import GitHubAppAuth
from auditor.scm.github_repo import GitHubRepoClient
from auditor.storage.evidence_store import EvidenceStore

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

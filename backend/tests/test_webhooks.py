"""The loop closes: a signed webhook announces a pull request's preview, Attest re-tests it, and says what it found."""

import asyncio
import hashlib
import hmac
import json
import subprocess

import pytest
from remote_world import HOST, WEBHOOK_SECRET, StaticSite
from sqlalchemy import select
from test_accounts import H, browser, sign_in
from test_safe_browse import make_cert

from auditor.demo import prepare_demo_repo
from auditor.models import FixAttempt, ProjectRecord, ProofJob, WebhookDelivery

pytestmark = pytest.mark.timeout(600)
PREVIEW_HOST = "pr-1--web.vercel.app"


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send(c, event: str, payload: dict, *, secret: str = WEBHOOK_SECRET, delivery: str | None = None, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery or f"d-{hashlib.sha1(body).hexdigest()[:20]}-{id(payload)}",
               "X-Hub-Signature-256": signature or sign(body, secret), "Content-Type": "application/json"}
    return await c.post("/api/github/webhook", content=body, headers=headers)


def deployment(branch: str, sha: str, url: str, *, repo="alice/web", state="success", installation=1) -> dict:
    return {"action": "created", "deployment": {"ref": branch, "sha": sha, "environment": "Preview"},
            "deployment_status": {"state": state, "environment_url": url}, "repository": {"full_name": repo},
            "installation": {"id": installation}}


async def published_pr(world, c, *, accept=("label", "html-has-lang")):
    """Audit alice's site, accept fixes, open the pull request. Returns (run id, branch, head sha, fix rows)."""
    await sign_in(c, "alice")
    await world.attach(c, allow_pr=True)
    rid, d = await world.run(c)
    rows = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
    for rule in accept:
        await c.post(f"/api/findings/{rows[rule]['finding_id']}/decision", headers=H, json={"action": "accept_pr"})
    out = (await c.post(f"/api/runs/{rid}/publish", headers=H)).json()
    branch = out["opened"][0]["branch"]
    git = world.gh.repos["alice/web"]["obj"]
    return rid, branch, git.refs[f"refs/heads/{branch}"], rows


def preview_of_branch(world, tmp_path, branch, *, patched=True):
    """What the customer's pipeline would deploy for the pull request: the repository with the branch's changes, built."""
    git = world.gh.repos["alice/web"]["obj"]
    repo, _ = prepare_demo_repo(tmp_path / ("patched" if patched else "plain"))
    if patched:
        for path in git.changed_paths(branch):
            (repo / path).write_text(git.read(branch, path)[1])
    subprocess.run(["pnpm", "build"], cwd=repo, check=True, capture_output=True, timeout=180)
    site = StaticSite(repo / "dist", make_cert(tmp_path / "pcert") if (tmp_path / "pcert").mkdir() is None else None)
    world.trusted[PREVIEW_HOST] = "127.0.0.1"
    world.ports.append(site.port)
    world.app.state.deps.cfg.allowed_site_ports.append(site.port)
    return site, f"https://{PREVIEW_HOST}:{site.port}"


async def wait_job(world, sf, statuses=("done", "skipped", "failed")):
    for _ in range(480):
        with sf() as db:
            job = db.scalar(select(ProofJob).order_by(ProofJob.created_at.desc()))
            if job is not None and job.status in statuses:
                return job
        await asyncio.sleep(0.5)
    raise AssertionError("the proof job did not finish")


async def test_the_whole_loop_a_webhook_proves_the_fixes_on_the_preview(world, sf, tmp_path):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        site, url = preview_of_branch(world, tmp_path, branch)
        try:
            r = await send(c, "deployment_status", deployment(branch, sha, url))
            assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
            job = await wait_job(world, sf)
            assert job.status == "done" and job.detail == "2 of 2 proved", job.detail

            d = (await c.get(f"/api/runs/{rid}/dashboard")).json()
            assert d["counts"]["fixed"] == 2 and d["counts"]["awaiting_proof"] == 0 and d["counts"]["regressions"] == 0
            assert d["headline"] == "3 issues - 2 fixed (proved) - 0 awaiting proof - 1 need you"
            assert d["score_after"]["score"] > d["score_before"]["score"]
            for rule in ("label", "html-has-lang"):
                f = (await c.get(f"/api/findings/{rows[rule]['finding_id']}")).json()
                assert f["status"] == "regress_ok"
                assert f["proof"]["verdict"] == "pass" and f["proof"]["relocated"] and f["proof"]["baseline_failed"]
                assert f["proof"]["relocation_score"] >= 0.85 and f["regression"]["regressions"] == 0
            pr = {r_["rule_id"]: r_["proof"] for r_ in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
            assert pr == {"label": "proved", "html-has-lang": "proved"}

            # the verdict is posted where the reviewer is looking
            (comment,) = world.gh.repos["alice/web"]["obj"].comments
            assert comment["number"] == 1 and "re-tested this pull request's preview" in comment["body"] and url in comment["body"]
            assert comment["body"].count("**passed**") == 2 and "does not establish" in comment["body"]
            assert not any(w in comment["body"].lower() for w in ("compliant", "certified", "conforms"))
        finally:
            site.close()


async def test_a_preview_where_the_fix_did_not_take_is_reported_as_still_failing(world, sf, tmp_path):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        site, url = preview_of_branch(world, tmp_path, branch, patched=False)  # the deployment does NOT contain the change
        try:
            assert (await send(c, "deployment_status", deployment(branch, sha, url))).status_code == 202
            job = await wait_job(world, sf)
            assert job.status == "done" and job.detail == "0 of 2 proved"
            f = (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()
            assert f["status"] == "prove_failed" and f["proof"]["verdict"] == "fail"
            (comment,) = world.gh.repos["alice/web"]["obj"].comments
            assert comment["body"].count("**still fails**") == 2
            d = (await c.get(f"/api/runs/{rid}/dashboard")).json()
            assert d["counts"]["fixed"] == 0 and d["score_after"] is None or d["score_after"]["score"] == d["score_before"]["score"]
        finally:
            site.close()


async def test_a_deployment_of_a_different_commit_proves_nothing(world, sf, tmp_path):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        site, url = preview_of_branch(world, tmp_path, branch)
        try:
            assert (await send(c, "deployment_status", deployment(branch, "f" * 40, url))).status_code == 202
            job = await wait_job(world, sf)
            assert job.status == "skipped" and "different commit" in job.detail
            assert (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()["status"] == "pr_open"
            assert world.gh.repos["alice/web"]["obj"].comments == []
        finally:
            site.close()


async def test_a_preview_that_reaches_for_a_private_address_is_stopped(world, sf, tmp_path):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        # a name on an allowed platform that the network guard resolves to a private address
        r = await send(c, "deployment_status", deployment(branch, sha, "https://sneaky-preview.vercel.app"))
        assert r.status_code == 202 and r.json()["status"] == "accepted"
        job = await wait_job(world, sf)
        assert job.status == "failed" and "private or reserved" in job.detail, job.detail
        assert (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()["status"] == "pr_open"


# ---- the door: nothing is believed until the signature checks out
async def test_unsigned_or_wrongly_signed_events_do_nothing(world, sf):
    async with browser(world.app) as c:
        payload = deployment("attest/x", "a" * 40, "https://x.vercel.app")
        body = json.dumps(payload).encode()
        good_headers = {"X-GitHub-Event": "deployment_status", "X-GitHub-Delivery": "delivery-0001", "Content-Type": "application/json"}
        assert (await c.post("/api/github/webhook", content=body, headers=good_headers)).status_code == 401  # no signature
        for bad in ("sha256=" + "0" * 64, "sha1=" + "0" * 40, "sha256=zz", "", sign(body, "wrong-secret"), sign(body + b" ", WEBHOOK_SECRET)):
            r = await c.post("/api/github/webhook", content=body, headers={**good_headers, "X-Hub-Signature-256": bad})
            assert r.status_code == 401, bad
    with sf() as db:
        assert db.scalars(select(WebhookDelivery)).all() == [] and db.scalars(select(ProofJob)).all() == []


async def test_malformed_but_signed_events_are_refused_cleanly(world):
    async with browser(world.app) as c:
        counter = iter(range(10_000))  # every delivery has its own id, like GitHub's

        def post(body: bytes, **h):
            hdr = {"X-GitHub-Event": "deployment_status", "X-GitHub-Delivery": f"delivery-{next(counter):06d}",
                   "X-Hub-Signature-256": sign(body), "Content-Type": "application/json", **h}
            return c.post("/api/github/webhook", content=body, headers=hdr)
        assert (await post(b"not json")).status_code == 400
        assert (await post(b"[1,2,3]")).status_code == 400
        assert (await post(b"{}", **{"X-GitHub-Delivery": "x"})).status_code == 400  # a delivery id must look like one
        assert (await post(b"{}", **{"X-GitHub-Event": "Bad Event!"})).status_code == 400
        assert (await post(b"{}", **{"X-GitHub-Event": "star"})).json()["status"] == "ignored"
        assert (await post(b"{}", **{"X-GitHub-Event": "ping"})).json() == {"status": "pong"}
        big = json.dumps({"x": "a" * 1_200_000}).encode()
        assert (await post(big)).status_code == 413  # over the webhook limit, refused before reading it
        assert (await c.post("/api/runs", content=b"x" * 70_000, headers={"Content-Length": "70000"})).status_code in (401, 403, 413)


async def test_a_replayed_delivery_is_acknowledged_once_and_does_nothing_twice(world, sf, tmp_path):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        site, url = preview_of_branch(world, tmp_path, branch)
        try:
            first = await send(c, "deployment_status", deployment(branch, sha, url), delivery="delivery-aaaa-1111")
            replay = await send(c, "deployment_status", deployment(branch, sha, url), delivery="delivery-aaaa-1111")
            same_deployment = await send(c, "deployment_status", deployment(branch, sha, url), delivery="delivery-bbbb-2222")
            assert first.json()["status"] == "accepted" and replay.json() == {"status": "duplicate"}
            assert same_deployment.json()["status"] == "duplicate" and "already" in same_deployment.json()["detail"]
            await wait_job(world, sf)
            with sf() as db:
                assert len(db.scalars(select(ProofJob)).all()) == 1
            assert len(world.gh.repos["alice/web"]["obj"].comments) == 1
        finally:
            site.close()


async def test_webhooks_are_absent_until_a_secret_is_configured(sf, tmp_path, demo_build, site):
    from remote_world import World

    w = World(sf, tmp_path, site, demo_build, gh_extra={"github_webhook_secret": ""})
    async with browser(w.app) as c:
        r = await send(c, "ping", {})
        assert r.status_code == 404 and "not enabled" in r.json()["detail"]


# ---- which deployments are even considered
async def test_deployments_that_are_not_ours_or_not_allowed_are_ignored(world, sf):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        cases = {
            "an address outside the project and the preview platforms": deployment(branch, sha, "https://evil-site.example.org"),
            "a private address": deployment(branch, sha, "https://127.0.0.1"),
            "plain http": deployment(branch, sha, "http://x.vercel.app"),
            "a branch Attest did not create": deployment("main", sha, "https://x.vercel.app"),
            "a branch that looks like ours but is not": deployment("attest/../../x", sha, "https://x.vercel.app"),
            "someone else's repository": deployment(branch, sha, "https://x.vercel.app", repo="mallory/web"),
            "the wrong installation": deployment(branch, sha, "https://x.vercel.app", installation=999),
            "a deployment that is not successful": deployment(branch, sha, "https://x.vercel.app", state="pending"),
            "a missing preview address": deployment(branch, sha, ""),
        }
        for why, payload in cases.items():
            r = await send(c, "deployment_status", payload)
            assert r.status_code == 202 and r.json()["status"] == "ignored", (why, r.text)
    with sf() as db:
        assert db.scalars(select(ProofJob)).all() == [], "no browser job may start for any of these"


async def test_merging_or_closing_and_uninstalling_are_recorded(world, sf):
    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        r = await send(c, "pull_request", {"action": "closed", "pull_request": {"merged": True, "head": {"ref": branch}}, "repository": {"full_name": "alice/web"}})
        assert r.json()["status"] == "recorded" and "marked merged" in r.json()["detail"]
        shown = {x["rule_id"]: x["remote"] for x in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
        assert shown["label"] == "merged"
        assert (await send(c, "pull_request", {"action": "opened", "pull_request": {"head": {"ref": branch}}})).json()["status"] == "ignored"
        with sf() as db:
            assert {a.status for a in db.scalars(select(FixAttempt)) if a.pr_url} == {"merged"}

        r = await send(c, "installation", {"action": "deleted", "installation": {"id": 1}})
        assert "1 project" in r.json()["detail"]
        with sf() as db:
            assert db.scalar(select(ProjectRecord)).allow_pr is False  # we stop trying to write to a repository we no longer have
        assert HOST


async def test_a_newer_deployment_replaces_the_earlier_result(world, sf, tmp_path):
    from auditor.models import RegressionCheck, ReproveResult

    async with browser(world.app) as c:
        rid, branch, sha, rows = await published_pr(world, c)
        site, url = preview_of_branch(world, tmp_path, branch)
        try:
            assert (await send(c, "deployment_status", deployment(branch, sha, url))).json()["status"] == "accepted"
            await wait_job(world, sf)
            git = world.gh.repos["alice/web"]["obj"]  # the author pushes again: the branch moves to a new commit
            newer = "c" * 40
            git.objects[newer] = dict(git.objects[sha])
            git.refs[f"refs/heads/{branch}"] = newer
            assert (await send(c, "deployment_status", deployment(branch, newer, url))).json()["status"] == "accepted"
            for _ in range(240):
                with sf() as db:
                    if len([j for j in db.scalars(select(ProofJob)) if j.status == "done"]) == 2:
                        break
                await asyncio.sleep(0.5)
            with sf() as db:
                assert len(db.scalars(select(ReproveResult)).all()) == 2, "one current result per fix, not one per deployment"
                assert len(db.scalars(select(RegressionCheck)).all()) == 2
            assert len(git.comments) == 2  # each deployment gets its own comment on the pull request
            assert (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()["status"] == "regress_ok"
        finally:
            site.close()

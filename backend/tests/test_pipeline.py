import asyncio
import json

import pytest
from fakes import FakeJudge
from sqlalchemy import func, select

from auditor.models import (
    AuditorRuling,
    AuditRun,
    EvidenceBundle,
    JudgeProposal,
    Level,
    PageState,
    RulingAction,
    RunEvent,
    RunStatus,
)
from auditor.pipeline.events import EventBus
from auditor.pipeline.orchestrator import run_audit
from auditor.review import compute_rollup, dataset_rows, latest_ruling, record_ruling
from auditor.storage.evidence_store import EvidenceStore


def _start(sf, tmp_path, url, flows=None, **kw):
    with sf() as db:
        run = AuditRun(target_url=url, flows=flows)
        db.add(run)
        db.commit()
        rid = run.id
    bus = EventBus(sf)
    store = EvidenceStore(tmp_path / "ev")
    return rid, bus, store


async def _go(sf, tmp_path, url, judge=None, **kw):
    rid, bus, store = _start(sf, tmp_path, url, kw.pop("flows", None))
    await run_audit(rid, sf, store, bus, judge_client=judge, **kw)
    return rid, bus, store


async def test_full_pipeline(sf, tmp_path, site_url):
    judge = FakeJudge("good")
    rid, bus, store = await _go(sf, tmp_path, site_url + "/index.html", judge)
    with sf() as db:
        run = db.get(AuditRun, rid)
        assert run.status == RunStatus.awaiting_review
        pages = db.scalars(select(PageState).where(PageState.run_id == rid)).all()
        urls = {p.url.split("/")[-1] for p in pages}
        assert urls == {"index.html", "good.html"}  # robots-disallowed, pdf, external, self-fragment excluded
        for p in pages:
            assert p.status == "probed"
            for ref in (p.screenshot_ref, p.dom_snapshot_ref, p.ax_snapshot_ref):
                assert store.get(ref)  # artifacts really on disk
        bundles = db.scalars(select(EvidenceBundle)).all()
        assert bundles and len({(b.page_state_id, b.criterion_id) for b in bundles}) == len(bundles)
        crit = {b.criterion_id for b in bundles}
        assert {"1.4.3", "2.1.1", "2.1.2", "2.4.7", "4.1.2", "3.3.2"} <= crit
        # every bundle judged exactly once
        counts = dict(db.execute(select(JudgeProposal.bundle_id, func.count()).group_by(JudgeProposal.bundle_id)).all())
        assert set(counts) == {b.id for b in bundles} and set(counts.values()) == {1}
        # event log is gap-free, ordered, ends with awaiting_review
        evs = db.scalars(select(RunEvent).where(RunEvent.run_id == rid).order_by(RunEvent.seq)).all()
        assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
        assert evs[-1].type == "run.awaiting_review" and evs[0].type == "stage.started"
        assert not [e for e in evs if e.type == "error"]
        # contrast failure on index.html surfaced against 1.4.3
        idx = next(p for p in pages if p.url.endswith("index.html"))
        b = db.scalar(select(EvidenceBundle).where(EvidenceBundle.page_state_id == idx.id,
                                                   EvidenceBundle.criterion_id == "1.4.3"))
        assert any(v["rule"] == "color-contrast" for v in b.axe_result["violations"])


async def test_rerun_uses_judge_cache(sf, tmp_path, site_url):
    judge = FakeJudge("good")
    rid, bus, store = await _go(sf, tmp_path, site_url + "/good.html", judge, max_depth=0)
    first = len(judge.calls)
    assert first > 0
    # same evidence in a second run -> cached proposals, zero new API calls
    rid2, bus2, store2 = await _go(sf, tmp_path, site_url + "/good.html", judge, max_depth=0)
    assert len(judge.calls) == first
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(JudgeProposal)) == 2 * first


async def test_judge_failures_do_not_kill_run(sf, tmp_path, site_url):
    class Boom:
        messages = None
        async def create(self, **kw):
            raise RuntimeError("api down")
    boom = Boom()
    boom.messages = boom
    rid, bus, _ = await _go(sf, tmp_path, site_url + "/good.html", boom, max_depth=0)
    with sf() as db:
        assert db.get(AuditRun, rid).status == RunStatus.awaiting_review
        assert db.scalar(select(func.count()).select_from(JudgeProposal)) == 0
        assert db.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.type == "error")) > 0


async def test_unreachable_target_recorded_not_crashed(sf, tmp_path):
    rid, bus, _ = await _go(sf, tmp_path, "http://127.0.0.1:9/")  # nothing listens
    with sf() as db:
        assert db.get(AuditRun, rid).status == RunStatus.awaiting_review
        skipped = [e for e in db.scalars(select(RunEvent)).all() if e.type == "stage.finished"
                   and e.payload.get("stage") == "crawl"][0].payload["skipped"]
        assert skipped and skipped[0]["reason"]


async def test_flow_reaches_state_and_bad_flow_is_recorded(sf, tmp_path, site_url):
    flows = [
        {"name": "fill", "label": "form filled", "url": site_url + "/good.html", "steps": [
            {"action": "fill", "selector": "#n", "value": "Ada"}, {"action": "press", "key": "Tab"}]},
        {"name": "broken", "url": site_url + "/good.html", "steps": [{"action": "click", "selector": "#nope"}]},
        {"name": "invalid", "url": site_url + "/good.html", "steps": [{"action": "explode"}]},
    ]
    rid, bus, _ = await _go(sf, tmp_path, site_url + "/good.html", FakeJudge("good"), flows=flows, max_depth=0)
    with sf() as db:
        states = {p.flow_name: p for p in db.scalars(select(PageState).where(PageState.run_id == rid))}
        assert states["fill"].status == "probed" and states["fill"].state_label == "form filled"
        assert states["broken"].status == "error" and states["invalid"].status == "error"
        assert db.get(AuditRun, rid).status == RunStatus.awaiting_review


async def test_review_rollup_and_dataset(sf, tmp_path, site_url):
    rid, *_ = await _go(sf, tmp_path, site_url + "/index.html", FakeJudge("good"), max_depth=0)
    with sf() as db:
        b = db.scalar(select(EvidenceBundle).where(EvidenceBundle.criterion_id == "1.4.3"))
        # before any ruling everything is unreviewed -> not_evaluated, never silently 'supports'
        roll = {r.criterion_id: r for r in compute_rollup(db, rid)}
        assert roll["1.4.3"].level == Level.not_evaluated and roll["1.4.3"].unreviewed_count >= 1

        prop = db.scalar(select(JudgeProposal).where(JudgeProposal.bundle_id == b.id))
        r1 = record_ruling(db, b.id, RulingAction.accept)
        assert r1.proposal_id == prop.id and r1.final_level == prop.proposed_level
        r2 = record_ruling(db, b.id, RulingAction.edit, final_level=Level.does_not_support,
                           final_remark="manual", rationale="seen in browser")
        assert latest_ruling(db, b.id).id == r2.id  # append-only history, latest wins
        assert db.scalar(select(func.count()).select_from(AuditorRuling)) == 2
        with pytest.raises(ValueError):
            record_ruling(db, b.id, RulingAction.reject)  # reject needs a level

        roll = {r.criterion_id: r for r in compute_rollup(db, rid)}
        assert roll["1.4.3"].level == Level.does_not_support and roll["1.4.3"].unreviewed_count == 0
        rows = list(dataset_rows(db, rid))
        assert len(rows) == 2 and rows[0]["agreed"] is True and rows[1]["agreed"] is False
        json.dumps(rows)  # JSON-serialisable for .jsonl export


def test_evidence_store_blocks_traversal(tmp_path):
    st = EvidenceStore(tmp_path)
    with pytest.raises(ValueError):
        st.put("r", "p", "../../escape.txt", b"x")
    for bad in [("..", "p", "f"), ("r", "..", "f"), ("r", "p", "a/b"), ("r", "p", ""), ("r/x", "p", "f")]:
        with pytest.raises(ValueError):
            st.put(*bad, b"x")
    with pytest.raises(ValueError):
        st.get("../outside")
    ref, sha = st.put("r", "p", "ok.png", b"data")
    assert st.get(ref) == b"data" and len(sha) == 64


def test_concurrent_event_emit_is_gap_free(sf):
    with sf() as db:
        run = AuditRun(target_url="x")
        db.add(run)
        db.commit()
        rid = run.id
    bus = EventBus(sf)

    async def spam():
        await asyncio.gather(*[asyncio.to_thread(bus.emit, rid, "log", {"i": i}) for i in range(1)])
        for i in range(50):
            bus.emit(rid, "log", {"i": i})

    asyncio.run(spam())
    with sf() as db:
        seqs = [e["seq"] for e in bus.replay(db, rid)]
        assert seqs == list(range(1, 52))
        assert [e["seq"] for e in bus.replay(db, rid, after_seq=40)] == list(range(41, 52))

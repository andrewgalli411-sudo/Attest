"""Run state machine: crawl -> probe -> persist bundles -> judge -> awaiting_review."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..models import (
    AuditRun,
    Criterion,
    EvidenceBundle,
    JudgeProposal,
    Level,
    PageState,
    RunStatus,
)
from ..storage.evidence_store import EvidenceStore
from .bundles import sc_evidence
from .crawler import crawl
from .events import EventBus
from .judge.judge import build_evidence_items, build_input, judge_bundle
from .prober import probe_page

PROBE_CONCURRENCY = 2
JUDGE_CONCURRENCY = 4


def _set_status(sf: sessionmaker, run_id: str, status: RunStatus, **kw) -> None:
    with sf() as db:
        run = db.get(AuditRun, run_id)
        run.status = status
        for k, v in kw.items():
            setattr(run, k, v)
        db.commit()


async def run_audit(
    run_id: str, sf: sessionmaker, store: EvidenceStore, bus: EventBus, *,
    judge_client=None, judge_model: str = "claude-sonnet-5-5",
    max_pages: int = 25, max_depth: int = 2, respect_robots: bool = True,
) -> None:
    try:
        await _run(run_id, sf, store, bus, judge_client, judge_model, max_pages, max_depth, respect_robots)
    except Exception as e:  # a run never dies silently
        _set_status(sf, run_id, RunStatus.failed, finished_at=datetime.now(UTC))
        bus.emit(run_id, "error", {"stage": "run", "error": f"{type(e).__name__}: {e}"})
        raise


async def _run(run_id, sf, store, bus, judge_client, judge_model, max_pages, max_depth, respect_robots):
    with sf() as db:
        run = db.get(AuditRun, run_id)
        seed, flows = run.target_url, list(run.flows or [])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            # ---- crawl
            _set_status(sf, run_id, RunStatus.crawling)
            bus.emit(run_id, "stage.started", {"stage": "crawl"})
            pages, skipped = await crawl(browser, seed, max_pages=max_pages, max_depth=max_depth,
                                         respect_robots=respect_robots)
            targets: list[dict] = [{"url": p.url, "flow": None, "label": None, "steps": None} for p in pages]
            targets += [{"url": f.get("url") or seed, "flow": f["name"], "label": f.get("label", f["name"]),
                         "steps": f["steps"]} for f in flows]
            with sf() as db:
                for t in targets:
                    ps = PageState(run_id=run_id, url=t["url"], flow_name=t["flow"],
                                   state_label=t["label"], steps=t["steps"])
                    db.add(ps)
                    db.flush()
                    t["id"] = ps.id
                db.commit()
            for t in targets:
                bus.emit(run_id, "page.discovered", {"page_state_id": t["id"], "url": t["url"]})
            bus.emit(run_id, "stage.finished", {"stage": "crawl", "pages": len(targets), "skipped": skipped})

            # ---- probe
            _set_status(sf, run_id, RunStatus.probing)
            bus.emit(run_id, "stage.started", {"stage": "probe"})
            sem = asyncio.Semaphore(PROBE_CONCURRENCY)

            async def probe_one(t: dict) -> None:
                async with sem:
                    bus.emit(run_id, "probe.started", {"page_state_id": t["id"], "url": t["url"]})
                    try:
                        res = await probe_page(browser, t["url"], t["steps"], capture=True)
                    except Exception as e:
                        res = {"url": t["url"], "error": f"{type(e).__name__}: {e}", "axe": None,
                               "probes": [], "artifacts": {}}
                    _persist_page(sf, store, run_id, t["id"], res)
                    bus.emit(run_id, "probe.finished", {"page_state_id": t["id"], "error": res["error"]})

            await asyncio.gather(*[probe_one(t) for t in targets])
            bus.emit(run_id, "stage.finished", {"stage": "probe"})
        finally:
            await browser.close()

    # ---- judge
    _set_status(sf, run_id, RunStatus.judging)
    bus.emit(run_id, "stage.started", {"stage": "judge"})
    if judge_client is not None:
        await _judge_all(run_id, sf, bus, judge_client, judge_model)
    else:
        bus.emit(run_id, "log", {"msg": "no judge client configured; bundles left unjudged"})
    bus.emit(run_id, "stage.finished", {"stage": "judge"})

    _set_status(sf, run_id, RunStatus.awaiting_review, finished_at=datetime.now(UTC))
    bus.emit(run_id, "run.awaiting_review", {})


def _persist_page(sf, store: EvidenceStore, run_id: str, page_id: str, res: dict) -> None:
    refs: dict[str, str] = {}
    for name, data in (res.get("artifacts") or {}).items():
        ref, _sha = store.put(run_id, page_id, name, data)
        refs[name] = ref
    with sf() as db:
        ps = db.get(PageState, page_id)
        ps.url = res.get("final_url") or ps.url
        ps.screenshot_ref = refs.get("screenshot.png")
        ps.dom_snapshot_ref = refs.get("dom.html")
        ps.ax_snapshot_ref = refs.get("ax.json")
        nothing_ran = res["axe"] is None and all(p.get("error") for p in res["probes"])
        ps.status = "error" if nothing_ran else "probed"
        ps.status_detail = res["error"] if nothing_ran else None
        valid_scs = set(db.scalars(select(Criterion.sc_id)))
        for sc, ev in sc_evidence(res).items():
            if sc not in valid_scs:  # e.g. axe tags a SC outside our A/AA 2.1 catalog
                continue
            db.add(EvidenceBundle(
                page_state_id=page_id, criterion_id=sc, axe_result=ev["axe"],
                probe_transcripts=ev["probes"], screenshot_ref=refs.get("screenshot.png"),
                dom_ref=refs.get("dom.html"), ax_ref=refs.get("ax.json"),
                selectors=_selectors(ev),
            ))
        db.commit()


def _selectors(ev: dict) -> list[str]:
    out: list[str] = []
    for kind in ("violations", "incomplete"):
        for rule in ev["axe"][kind]:
            out += [str(n["target"]) for n in rule["nodes"]]
    for p in ev["probes"]:
        out += [o["selector"] for o in p["observations"] if "selector" in o]
    return sorted(set(out))[:50]


async def _judge_all(run_id, sf, bus, client, model) -> None:
    with sf() as db:
        rows = db.execute(
            select(EvidenceBundle.id).join(PageState).where(PageState.run_id == run_id)
            .order_by(EvidenceBundle.id)
        ).all()
    ids = [r[0] for r in rows]
    sem = asyncio.Semaphore(JUDGE_CONCURRENCY)

    async def one(bundle_id: str) -> None:
        async with sem:
            with sf() as db:
                b = db.get(EvidenceBundle, bundle_id)
                crit = db.get(Criterion, b.criterion_id)
                page = db.get(PageState, b.page_state_id)
                sc_ev = {"axe": b.axe_result, "probes": b.probe_transcripts}
                items = build_evidence_items(sc_ev)
                payload = build_input(
                    {"sc_id": crit.sc_id, "title": crit.title, "level": crit.level},
                    page.url, page.state_label, items)
            try:
                from .judge.judge import input_hash

                h = input_hash(payload, model)
                with sf() as db:  # identical evidence + model + prompt -> reuse, no new API call
                    cached = db.scalar(select(JudgeProposal).where(
                        JudgeProposal.input_hash == h, JudgeProposal.valid.is_(True)))
                if cached is not None:
                    bus.emit(run_id, "judge.cached", {"bundle_id": bundle_id})
                    prop = None
                    new = JudgeProposal(
                        bundle_id=bundle_id, proposed_level=cached.proposed_level, remark=cached.remark,
                        confidence=cached.confidence, evidence_cited=cached.evidence_cited,
                        reasoning=cached.reasoning, needs_human=cached.needs_human, valid=True,
                        model_id=cached.model_id, prompt_version=cached.prompt_version, input_hash=h,
                        usage={"cached_from": cached.id})
                else:
                    prop = await judge_bundle(client, model, payload, items)
                    new = JudgeProposal(
                        bundle_id=bundle_id, proposed_level=Level(prop.level), remark=prop.remark,
                        confidence=prop.confidence, evidence_cited=prop.evidence_cited,
                        reasoning=(prop.reasoning + (" [guardrails: " + "; ".join(prop.guardrail_notes) + "]"
                                                     if prop.guardrail_notes else "")),
                        needs_human=prop.needs_human, valid=prop.valid, model_id=prop.model_id,
                        prompt_version=prop.prompt_version, input_hash=prop.input_hash,
                        usage=prop.usage)
                with sf() as db:
                    db.add(new)
                    db.commit()
                bus.emit(run_id, "judge.proposed", {"bundle_id": bundle_id, "level": new.proposed_level.value})
            except Exception as e:  # API/network failure: recorded, bundle stays unjudged
                bus.emit(run_id, "error", {"stage": "judge", "bundle_id": bundle_id,
                                           "error": f"{type(e).__name__}: {e}"})

    await asyncio.gather(*[one(i) for i in ids])

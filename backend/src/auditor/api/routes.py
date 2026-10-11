from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import select

from ..demo import DEV_COMMAND, SEED_AUTH, prepare_demo_repo
from ..disclaimers import DRAFT_BANNER
from ..models import (
    AuditRun,
    Criterion,
    Finding,
    FindingEvidence,
    FixAttempt,
    HumanDecision,
    PageScore,
    PageState,
    RegressionCheck,
    ReproveResult,
    RunStatus,
    TriageVerdict,
)
from ..remediate import RemediationConfig, run_remediation
from ..report.html import run_report
from .auth import require_auth
from .schemas import (
    ConfigOut,
    Counts,
    Dashboard,
    DecisionIn,
    DecisionOut,
    ErrorOut,
    EvidenceOut,
    FindingDetail,
    FindingRow,
    FixOut,
    Group,
    PageOut,
    ProjectInfo,
    ProofOut,
    RegressionOut,
    RunOut,
    ScoreOut,
    StartRun,
    StatusOut,
    TriageOut,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], responses={
    code: {"model": ErrorOut, "description": why}
    for code, why in ((401, "missing or invalid API token"), (403, "not allowed on this server"), (404, "not found"),
                      (409, "conflicts with the current state"), (411, "Content-Length required"),
                      (413, "request body too large"), (421, "host not allowed"), (429, "a run is already in progress"))})
FIXED = {"proved", "regress_ok", "regress_fail"}
TERMINAL = {RunStatus.complete, RunStatus.failed, RunStatus.cancelled, RunStatus.reviewed, RunStatus.exported}
KEEPALIVE_S = 15


def deps(request: Request):
    return request.app.state.deps


def _run_out(run: AuditRun) -> RunOut:
    return RunOut(id=run.id, status=run.status.value, target=run.target_url, created_at=run.created_at.isoformat(),
                  headline=(run.summary or {}).get("headline"))


@router.get("/status", response_model=StatusOut)
def status(d=Depends(deps)):
    """Operational counters. Authenticated, unlike /health."""
    return StatusOut(active_runs=d.active_runs(), max_concurrent_runs=d.cfg.max_concurrent_runs,
                     event_subscribers=sum(len(v) for v in d.bus._subs.values()))


@router.get("/config", response_model=ConfigOut)
def get_config(d=Depends(deps)):
    """What this server will let a caller do. Never includes commands, paths, or secrets."""
    return ConfigOut(
        demo_enabled=d.cfg.demo_enabled, allow_custom_runs=d.cfg.allow_custom_runs, auth_required=bool(d.cfg.api_token),
        projects=[ProjectInfo(name=n, pages=p.pages, can_publish=p.allow_push) for n, p in sorted(d.projects.items())])


def _pages(specs) -> list[dict] | None:
    return None if specs is None else [{"path": s.path, "label": s.label or s.path} for s in specs]


def _resolve(body: StartRun, d) -> tuple[str, RemediationConfig | None]:
    """Turn a request into a run configuration, or raise. Callers can never inject commands."""
    if body.project:
        proj = d.projects.get(body.project)
        if proj is None:
            raise HTTPException(404, f"unknown project {body.project!r}")
        if not Path(proj.repo_path).is_dir():
            raise HTTPException(409, f"project {body.project!r} points at a repository that does not exist on the server")
        cfg = RemediationConfig(
            repo_path=Path(proj.repo_path), dev_command=proj.dev_command, check_commands=proj.check_commands,
            pages=_pages(body.pages) or proj.pages, auth={"cookies": proj.auth_cookies} if proj.auth_cookies else None,
            base_branch=proj.base_branch, allow_push=bool(body.publish and proj.allow_push),
            only_rules=body.only_rules, max_fixes=body.max_fixes, with_probes=body.with_probes)
        if body.publish and not proj.allow_push:
            raise HTTPException(403, f"project {body.project!r} is not allowed to publish (allow_push is false)")
        return f"project:{body.project}", cfg
    if body.demo:
        if not d.cfg.demo_enabled:
            raise HTTPException(403, "the demo app is disabled on this server")
        if body.publish:
            raise HTTPException(403, "the demo app cannot publish")
        return "demo", None  # the demo repo is prepared inside the run
    if body.repo_path:
        if not d.cfg.allow_custom_runs:
            raise HTTPException(403, "custom repositories and commands are disabled; ask an operator to define a project")
        if body.publish:
            raise HTTPException(403, "custom runs cannot publish")
        return body.repo_path, RemediationConfig(
            repo_path=Path(body.repo_path), dev_command=body.dev_command, check_commands=body.check_commands,
            pages=_pages(body.pages) or [{"path": "/", "label": "home"}],
            auth={"cookies": body.auth_cookies} if body.auth_cookies else None, base_branch=body.base_branch,
            only_rules=body.only_rules, max_fixes=body.max_fixes, with_probes=body.with_probes)
    raise RequestValidationError([{"type": "value_error", "loc": ["body"], "msg": "give a project name (or demo=true)", "input": None}])


@router.post("/runs", response_model=RunOut, status_code=202)
async def start_run(body: StartRun, d=Depends(deps)):
    target, cfg = _resolve(body, d)
    if d.active_runs() >= d.cfg.max_concurrent_runs:
        raise HTTPException(429, "a run is already in progress; wait for it to finish or cancel it",
                            headers={"Retry-After": "30"})
    with d.sf() as db:
        run = AuditRun(target_url=target)
        db.add(run)
        db.commit()
        db.refresh(run)
        out = _run_out(run)
    if not d.claim_slot(out.id):  # another worker won the race between our check and our insert
        d.withdraw(out.id)
        raise HTTPException(429, "a run is already in progress; wait for it to finish or cancel it",
                            headers={"Retry-After": "30"})
    d.spawn(out.id, _execute(d, out.id, body, cfg))
    return out


async def _execute(d, run_id: str, body: StartRun, cfg: RemediationConfig | None) -> None:
    """Background task. Every way this can end is recorded on the run; nothing is left 'running'."""
    try:
        if cfg is None:  # demo
            repo, _ = await asyncio.to_thread(prepare_demo_repo, Path(d.work_dir) / "demos" / run_id)
            cfg = RemediationConfig(repo_path=repo, dev_command=DEV_COMMAND, check_commands=["pnpm build"],
                                    pages=_pages(body.pages) or [{"path": "/", "label": "profile"}], auth=SEED_AUTH,
                                    only_rules=body.only_rules, max_fixes=body.max_fixes, with_probes=body.with_probes)
        cfg.triage_model, cfg.fix_model = d.cfg.triage_model, d.cfg.fix_model
        cfg.send_screenshots_to_model = d.cfg.send_screenshots_to_model
        cfg.model_token_budget = d.cfg.model_token_budget
        cfg.max_findings_per_page = d.cfg.max_findings_per_page
        cfg.refusal_fallback = d.cfg.refusal_fallback
        await asyncio.wait_for(run_remediation(run_id, d.sf, d.store, d.bus, d.client_factory(), cfg),
                               timeout=d.cfg.run_timeout_s)
    except TimeoutError:
        d.finish(run_id, RunStatus.failed, f"the run exceeded its time limit of {d.cfg.run_timeout_s}s")
    except asyncio.CancelledError:
        d.finish(run_id, RunStatus.cancelled, "cancelled")
        raise
    except Exception as e:  # noqa: BLE001 - run_remediation already recorded its own failures
        d.log.warning("run %s failed: %s", run_id, e)
        d.finish(run_id, None, f"{type(e).__name__}: {e}")  # only records if the run is still non-terminal


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, d=Depends(deps)):
    task = d.tasks.get(run_id)
    with d.sf() as db:
        run = _get_run(db, run_id)
        if task is None or run.status in TERMINAL:
            raise HTTPException(409, "this run is not running")
    task.cancel()
    await asyncio.wait({task}, timeout=30)  # wait for cleanup (dev server, branch restore) to finish
    with d.sf() as db:
        return _run_out(_get_run(db, run_id))


@router.get("/runs", response_model=list[RunOut])
def list_runs(d=Depends(deps)):
    with d.sf() as db:
        return [_run_out(r) for r in db.scalars(select(AuditRun).order_by(AuditRun.created_at.desc()).limit(50))]


def _get_run(db, run_id: str) -> AuditRun:
    run = db.get(AuditRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.get("/runs/{run_id}/dashboard", response_model=Dashboard)
def dashboard(run_id: str, d=Depends(deps)):
    with d.sf() as db:
        run = _get_run(db, run_id)
        findings = db.scalars(select(Finding).where(Finding.run_id == run_id)).all()
        pages = db.scalars(select(PageState).where(PageState.run_id == run_id)).all()
        scores = db.scalars(select(PageScore).where(PageScore.run_id == run_id)).all()
        titles = {c.sc_id: c.title for c in db.scalars(select(Criterion))}
        tiers = {v.finding_id: v.tier.value for v in db.scalars(
            select(TriageVerdict).join(Finding, Finding.id == TriageVerdict.finding_id).where(Finding.run_id == run_id))}
        page_label = {p.id: p.state_label or urlsplit(p.url).path for p in pages}
        regress = sum(rc.regressions_count for rc in db.scalars(
            select(RegressionCheck).join(FixAttempt, FixAttempt.id == RegressionCheck.fix_attempt_id)
            .join(Finding, Finding.id == FixAttempt.finding_id).where(Finding.run_id == run_id)))
        summary = run.summary or {}
        run_out = _run_out(run)

    def row(f: Finding) -> FindingRow:
        return FindingRow(id=f.id, rule_id=f.rule_id, sc_ids=f.sc_ids, severity=f.severity.value, status=f.status.value,
                          page=page_label.get(f.page_state_id, ""), impact_text=f.impact_text, tier=tiers.get(f.id))

    by_sc: dict[str, list[Finding]] = {}
    for f in findings:
        for sc in f.sc_ids or ["other"]:
            by_sc.setdefault(sc, []).append(f)
    sev_rank = ["critical", "serious", "moderate", "minor"]
    groups = [
        Group(sc=sc, title=titles.get(sc, "Other"), total=len(fs), fixed=sum(f.status.value in FIXED for f in fs),
              needs_human=sum(f.status.value == "needs_human" for f in fs),
              findings=[row(f) for f in sorted(fs, key=lambda f: (sev_rank.index(f.severity.value), f.rule_id))])
        for sc, fs in sorted(by_sc.items())]
    fixed = sum(f.status.value in FIXED for f in findings)
    human = sum(f.status.value == "needs_human" for f in findings)
    incon = sum(f.status.value in ("inconclusive", "prove_failed") for f in findings)
    done = run.status in TERMINAL
    counts = Counts(total=len(findings), fixed=fixed, needs_human=human, inconclusive=incon, regressions=regress,
                    in_progress=0 if done else len(findings) - fixed - human - incon)
    headline = summary.get("headline") or f"{len(findings)} issues - {fixed} fixed - {human} need you"
    base = {s.page_state_id: s.score for s in scores if s.phase == "baseline"}
    after = {s.page_state_id: s.score for s in scores if s.phase == "after"}
    return Dashboard(
        run=run_out, headline=headline, counts=counts, severity={k: sum(f.severity.value == k for f in findings) for k in sev_rank},
        score_before=ScoreOut(**summary["score_before"]) if summary.get("score_before") else None,
        score_after=ScoreOut(**summary["score_after"]) if summary.get("score_after") else None, groups=groups,
        pages=[PageOut(id=p.id, label=page_label[p.id], path=urlsplit(p.url).path or "/",
                       score_before=base.get(p.id), score_after=after.get(p.id)) for p in pages],
        notice=DRAFT_BANNER, model_kind=summary.get("model_kind"))


@router.get("/findings/{finding_id}", response_model=FindingDetail)
def finding_detail(finding_id: str, d=Depends(deps)):
    with d.sf() as db:
        f = db.get(Finding, finding_id)
        if f is None:
            raise HTTPException(404, "finding not found")
        ps = db.get(PageState, f.page_state_id)
        evs = db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == finding_id)
                         .order_by(FindingEvidence.created_at, FindingEvidence.id)).all()
        v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == finding_id)
                      .order_by(TriageVerdict.created_at.desc()))
        fa = db.scalar(select(FixAttempt).where(FixAttempt.finding_id == finding_id))
        rr = db.scalar(select(ReproveResult).where(ReproveResult.finding_id == finding_id))
        rg = db.scalar(select(RegressionCheck).join(FixAttempt, FixAttempt.id == RegressionCheck.fix_attempt_id)
                       .where(FixAttempt.finding_id == finding_id))
        diff_ev = next((e for e in evs if e.kind == "diff"), None)
        return FindingDetail(
            id=f.id, run_id=f.run_id, rule_id=f.rule_id, sc_ids=f.sc_ids, severity=f.severity.value,
            status=f.status.value, page=ps.state_label or urlsplit(ps.url).path, impact_text=f.impact_text,
            triage=None if v is None else TriageOut(
                tier=v.tier.value, reason=v.reason_code, pr_tier=v.pr_tier, fix_plan=v.fix_plan,
                confidence=v.confidence, valid=v.valid, gate_notes=v.gate_notes),
            fix=None if fa is None or not fa.diff else FixOut(
                diff=fa.diff, branch=fa.branch, tier=fa.tier_pr, pr_url=fa.pr_url,
                explanation=((diff_ev.payload or {}).get("explanation", "") if diff_ev else "")),
            proof=None if rr is None else ProofOut(
                verdict=rr.verdict, notes=rr.notes, baseline_failed=rr.baseline_failed, relocated=rr.relocated,
                relocation_score=rr.relocation_score, relocation_method=rr.relocation_method, runs=rr.runs_n),
            regression=None if rg is None else RegressionOut(
                verdict=rg.verdict, regressions=rg.regressions_count, rules_checked=len(rg.criteria_checked)),
            evidence=[EvidenceOut(id=e.id, stage=e.stage, kind=e.kind, payload=e.payload,
                                  url=f"/api/evidence/{e.id}" if e.blob_ref else None) for e in evs],
            notice=DRAFT_BANNER)


@router.get("/evidence/{evidence_id}")
def evidence_blob(evidence_id: str, d=Depends(deps)):
    with d.sf() as db:
        e = db.get(FindingEvidence, evidence_id)
        if e is None or not e.blob_ref:
            raise HTTPException(404, "no such evidence")
        ref = e.blob_ref
    try:
        data = d.store.get(ref)
    except (OSError, ValueError):
        raise HTTPException(404, "evidence file missing") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/runs/{run_id}/report", response_class=HTMLResponse)
def report(run_id: str, d=Depends(deps)):
    with d.sf() as db:
        _get_run(db, run_id)
    return HTMLResponse(run_report(d.sf, d.store, run_id), headers={
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"})


@router.post("/findings/{finding_id}/decision", response_model=DecisionOut, status_code=201)
def decide(finding_id: str, body: DecisionIn, d=Depends(deps)):
    with d.sf() as db:
        if db.get(Finding, finding_id) is None:
            raise HTTPException(404, "finding not found")
        v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == finding_id)
                      .order_by(TriageVerdict.created_at.desc()))
        h = HumanDecision(finding_id=finding_id, verdict_id=v.id if v else None, action=body.action,
                          reviewer=body.reviewer, note=body.note)
        db.add(h)
        db.commit()
        return DecisionOut(id=h.id, finding_id=finding_id, action=h.action)


# ------------------------------------------------------------------------------------------ SSE
def _sse(seq: int, type_: str, payload: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(payload)}\n\n"


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = Query(0, ge=0), d=Depends(deps)):
    """Server-sent events: replays from Last-Event-ID / ?after, then tails live; closes when the run ends."""
    with d.sf() as db:
        _get_run(db, run_id)
    last = after
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        last = max(last, int(header))

    async def gen() -> AsyncIterator[str]:
        q = d.bus.subscribe(run_id)  # subscribe BEFORE replaying so nothing is missed in between
        try:
            # Flush something immediately: clients learn the stream is live and buffering proxies release it.
            yield "retry: 3000\n: connected\n\n"
            seen = last
            with d.sf() as db:
                for ev in d.bus.replay(db, run_id, seen):
                    seen = ev["seq"]
                    yield _sse(ev["seq"], ev["type"], ev["payload"])
            while True:
                with d.sf() as db:
                    status = db.get(AuditRun, run_id).status
                if status in TERMINAL and q.empty():
                    with d.sf() as db:  # final drain: events committed after our last replay
                        for ev in d.bus.replay(db, run_id, seen):
                            seen = ev["seq"]
                            yield _sse(ev["seq"], ev["type"], ev["payload"])
                    yield "event: end\ndata: {}\n\n"
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev["seq"] > seen:
                    seen = ev["seq"]
                    yield _sse(ev["seq"], ev["type"], ev["payload"])
        finally:
            d.bus.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

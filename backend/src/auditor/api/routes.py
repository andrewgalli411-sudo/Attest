from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
    FindingLocator,
    FixAttempt,
    HumanDecision,
    PageScore,
    PageState,
    ProjectRecord,
    RegressionCheck,
    ReproveResult,
    RunEvent,
    RunStatus,
    TriageVerdict,
)
from ..pipeline.safe_browse import GUARD
from ..ratelimit import limited
from ..remediate import RemediationConfig, run_remediation
from ..remote import RemoteConfig, run_remote_audit
from ..report.html import run_report
from .auth import Principal, require_auth
from .publish import PublishRefused, publish_accepted
from .publish_remote import publish_remote
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
    PublishOut,
    PullRequestsOut,
    RegressionOut,
    RunOut,
    ScoreOut,
    SettingsOut,
    StartRun,
    StatusOut,
    TriageOut,
)
from .views import build_meta, build_pull_requests, build_stages

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


def _run_out(run: AuditRun, error: str | None = None) -> RunOut:
    summary = run.summary or {}
    cfg = run.config or {}
    status = run.status.value
    headline = summary.get("headline")
    if headline:
        result = headline
    elif run.status == RunStatus.failed:
        result = error or "The run failed."
    elif run.status == RunStatus.cancelled:
        result = "Cancelled. Nothing pushed, repo restored."
    else:
        result = None
    sb, sa = summary.get("score_before") or {}, summary.get("score_after") or {}
    return RunOut(
        id=run.id, status=status, target=run.target_url, created_at=run.created_at.isoformat(), headline=headline,
        project=run.target_url.removeprefix("project:") if run.target_url.startswith("project:") else
        ("demo" if run.target_url == "demo" else None),
        base_branch=cfg.get("base_branch"), page_count=len(cfg["pages"]) if cfg.get("pages") else None,
        score_before=sb.get("score"), score_after=sa.get("score"), findings=summary.get("total_findings"), fixed=summary.get("proved"),
        needs_human=summary.get("needs_human"), result=result)


def _last_errors(db, run_ids: list[str]) -> dict[str, str]:
    """The reason each given run failed: its latest 'error' event."""
    out: dict[str, str] = {}
    if not run_ids:
        return out
    for e in db.scalars(select(RunEvent).where(RunEvent.run_id.in_(run_ids), RunEvent.type == "error").order_by(RunEvent.seq)):
        out[e.run_id] = str((e.payload or {}).get("error") or "")
    return out


@router.get("/status", response_model=StatusOut)
def status(d=Depends(deps), who: Principal = Depends(require_auth)):
    """Operational counters across every account, so only the local operator may read them. Authenticated, unlike /health."""
    if not who.is_local:
        raise HTTPException(404, "not found")
    return StatusOut(active_runs=d.active_runs(), max_concurrent_runs=d.cfg.max_concurrent_runs,
                     event_subscribers=sum(len(v) for v in d.bus._subs.values()))


@router.get("/config", response_model=ConfigOut)
def get_config(d=Depends(deps), who: Principal = Depends(require_auth)):
    """What this server will let a caller do. Never includes commands, paths, or secrets."""
    if who.is_local:
        projects = [ProjectInfo(name=n, pages=p.pages, can_publish=p.allow_push, base_branch=p.base_branch)
                    for n, p in sorted(d.projects.items())]
    else:  # accounts mode: the projects this person attached, and only theirs
        with d.sf() as db:
            projects = [ProjectInfo(name=p.name, pages=p.pages, can_publish=p.allow_pr, base_branch=p.default_branch, id=p.id,
                                    site_url=p.site_url, github_repo=p.github_repo, verified=p.verified_at is not None)
                        for p in db.scalars(select(ProjectRecord).where(ProjectRecord.owner_id == who.account_id)
                                            .order_by(ProjectRecord.created_at))]
    return ConfigOut(
        demo_enabled=d.cfg.demo_enabled, allow_custom_runs=d.cfg.allow_custom_runs, auth_required=bool(d.cfg.api_token),
        projects=projects,
        settings=SettingsOut(
            triage_model=d.cfg.triage_model, fix_model=d.cfg.fix_model, model_token_budget=d.cfg.model_token_budget,
            refusal_fallback=d.cfg.refusal_fallback, send_screenshots_to_model=d.cfg.send_screenshots_to_model,
            max_concurrent_runs=d.cfg.max_concurrent_runs, run_timeout_s=d.cfg.run_timeout_s,
            max_findings_per_page=d.cfg.max_findings_per_page, model_configured=bool(d.cfg.anthropic_api_key)))


def _pages(specs) -> list[dict] | None:
    return None if specs is None else [{"path": s.path, "label": s.label or s.path} for s in specs]


def _resolve(body: StartRun, d, who: Principal | None = None) -> tuple[str, RemediationConfig | RemoteConfig | None]:
    """Turn a request into a run configuration, or raise. Callers can never inject commands."""
    if body.project and who is not None and not who.is_local:
        return _resolve_remote(body, d, who)
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


@router.post("/runs", response_model=RunOut, status_code=202, dependencies=[Depends(limited("runs", "rl_runs_per_min"))])
async def start_run(body: StartRun, d=Depends(deps), who: Principal = Depends(require_auth)):
    target, cfg = _resolve(body, d, who)
    if not who.is_local:
        allowance = _enforce_quota(d, who)
        if hasattr(cfg, "model_token_budget") and allowance is not None:
            cfg.model_token_budget = min(cfg.model_token_budget, allowance)  # a run cannot spend more than the month has left
    if d.active_runs() >= d.cfg.max_concurrent_runs:
        raise HTTPException(429, "a run is already in progress; wait for it to finish or cancel it",
                            headers={"Retry-After": "30"})
    with d.sf() as db:
        run = AuditRun(target_url=target, owner_id=who.account_id, project_id=getattr(cfg, "project_id", None))
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


def _enforce_quota(d, who: Principal) -> int | None:
    """Fair use on a shared server. Raises 429 with a plain reason, or returns how many model tokens the person has left
    this month (None if there is no cap)."""
    now = datetime.now(UTC)
    with d.sf() as db:
        mine = db.scalars(select(AuditRun).where(AuditRun.owner_id == who.account_id)).all()
    if d.cfg.max_runs_per_account > 0:
        live = [r for r in mine if r.status not in TERMINAL and (r.id in d.tasks or d._last_seen(r) >= now - timedelta(seconds=75))]
        if len(live) >= d.cfg.max_runs_per_account:
            raise HTTPException(429, f"you already have {len(live)} run(s) in progress; wait for it to finish or cancel it",
                                headers={"Retry-After": "30"})
    if d.cfg.max_runs_per_day > 0:
        recent = sorted(r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC) for r in mine
                        if (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)) >= now - timedelta(hours=24))
        if len(recent) >= d.cfg.max_runs_per_day:
            wait = max(60, int((recent[0] + timedelta(hours=24) - now).total_seconds()))
            raise HTTPException(429, f"daily limit reached ({d.cfg.max_runs_per_day} runs in 24 hours); the oldest frees up in about "
                                f"{max(1, wait // 60)} minutes", headers={"Retry-After": str(wait)})
    if d.cfg.monthly_model_token_cap > 0:
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        used = sum(r.model_tokens or 0 for r in mine if (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)) >= month)
        left = d.cfg.monthly_model_token_cap - used
        if left <= 0:
            raise HTTPException(429, "this month's language-model allowance is used up; it resets at the start of next month")
        return left
    return None


def _resolve_remote(body: StartRun, d, who: Principal) -> tuple[str, RemoteConfig]:
    """A signed-in person's own project: audit the live site, read the repository through the GitHub App."""
    with d.sf() as db:
        p = db.scalar(select(ProjectRecord).where(ProjectRecord.owner_id == who.account_id, ProjectRecord.name == body.project))
        if p is None:
            raise HTTPException(404, f"unknown project {body.project!r}")
        if p.verified_at is None:
            raise HTTPException(409, "prove you own this site first (Projects > Verify); Attest only audits sites you control")
        if not (d.cfg.github_app_id and d.cfg.github_app_private_key):
            raise HTTPException(503, "this server is not connected to GitHub yet (the GitHub App is not configured)")
        auth = d.vault.open(p.sealed_auth) if p.sealed_auth else None
        pages = _pages(body.pages) or p.pages
        return f"project:{p.name}", RemoteConfig(
            project_id=p.id, repo=p.github_repo, installation_id=p.installation_id, base_branch=p.default_branch,
            site_url=p.site_url, pages=pages, scratch_root=Path(d.scratch_root), auth=auth, with_probes=body.with_probes,
            triage_model=d.cfg.triage_model, fix_model=d.cfg.fix_model, send_screenshots_to_model=d.cfg.send_screenshots_to_model,
            model_token_budget=d.cfg.model_token_budget, max_findings_per_page=d.cfg.max_findings_per_page,
            refusal_fallback=d.cfg.refusal_fallback, only_rules=body.only_rules, max_fixes=body.max_fixes)


async def _execute_remote(d, run_id: str, rc: RemoteConfig) -> None:
    guard = d.guard_factory()  # one proxy per run: its own credentials, its own limits
    async with guard.proxy:
        token = GUARD.set(guard)
        try:
            github = d.github_factory(rc.installation_id, rc.repo)
            await asyncio.wait_for(run_remote_audit(run_id, d.sf, d.store, d.bus, d.client_factory(), rc, github),
                                   timeout=d.cfg.run_timeout_s)
        finally:
            GUARD.reset(token)


async def _execute(d, run_id: str, body: StartRun, cfg: RemediationConfig | RemoteConfig | None) -> None:
    """Background task. Every way this can end is recorded on the run; nothing is left 'running'."""
    try:
        if isinstance(cfg, RemoteConfig):
            await _execute_remote(d, run_id, cfg)
            return
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
async def cancel_run(run_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    task = d.tasks.get(run_id)
    with d.sf() as db:
        run = _get_run(db, run_id, who)
        if task is None or run.status in TERMINAL:
            raise HTTPException(409, "this run is not running")
    task.cancel()
    await asyncio.wait({task}, timeout=30)  # wait for cleanup (dev server, branch restore) to finish
    with d.sf() as db:
        return _run_out(_get_run(db, run_id, who))


@router.get("/runs", response_model=list[RunOut])
def list_runs(d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        q = select(AuditRun).order_by(AuditRun.created_at.desc()).limit(50)
        if not who.is_local:
            q = q.where(AuditRun.owner_id == who.account_id)
        runs = db.scalars(q).all()
        errors = _last_errors(db, [r.id for r in runs if r.status == RunStatus.failed])
        return [_run_out(r, errors.get(r.id)) for r in runs]


def _get_run(db, run_id: str, who: Principal) -> AuditRun:
    """The run, if it exists AND belongs to the caller. Someone else's run answers exactly like a missing one (404), so
    ids cannot be probed to learn what exists."""
    run = db.get(AuditRun, run_id)
    if run is None or not who.can_see(run.owner_id):
        raise HTTPException(404, "run not found")
    return run


def _get_finding(db, finding_id: str, who: Principal) -> Finding:
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404, "finding not found")
    run = db.get(AuditRun, f.run_id)
    if run is None or not who.can_see(run.owner_id):
        raise HTTPException(404, "finding not found")
    return f


@router.get("/runs/{run_id}/dashboard", response_model=Dashboard)
def dashboard(run_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        run = _get_run(db, run_id, who)
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
        meta = build_meta(db, run, len(pages), False)
        stages = build_stages(db, run)

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
    awaiting = sum(f.status.value in ("fix_authored", "pr_open") for f in findings)
    counts = Counts(total=len(findings), fixed=fixed, awaiting_proof=awaiting, needs_human=human, inconclusive=incon,
                    regressions=regress, in_progress=0 if done else len(findings) - fixed - human - incon - awaiting)
    headline = summary.get("headline") or f"{len(findings)} issues - {fixed} fixed - {human} need you"
    base = {s.page_state_id: s.score for s in scores if s.phase == "baseline"}
    after = {s.page_state_id: s.score for s in scores if s.phase == "after"}
    return Dashboard(
        run=run_out, meta=meta, stages=stages, headline=headline, counts=counts, severity={k: sum(f.severity.value == k for f in findings) for k in sev_rank},
        score_before=ScoreOut(**summary["score_before"]) if summary.get("score_before") else None,
        score_after=ScoreOut(**summary["score_after"]) if summary.get("score_after") else None, groups=groups,
        pages=[PageOut(id=p.id, label=page_label[p.id], path=urlsplit(p.url).path or "/",
                       score_before=base.get(p.id), score_after=after.get(p.id)) for p in pages],
        notice=DRAFT_BANNER, model_kind=summary.get("model_kind"))


@router.get("/findings/{finding_id}", response_model=FindingDetail)
def finding_detail(finding_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        f = _get_finding(db, finding_id, who)
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
        loc = db.get(FindingLocator, finding_id)
        fp = (loc.fingerprint if loc else None) or {}
        selector = (" ".join(map(str, loc.axe_target)) if loc and loc.axe_target else None) or fp.get("stable_css") or fp.get("css_path")
        hit = next((e.payload for e in evs if e.kind == "source_hit"), None) or {}
        src = (hit.get("hit") or {}) if hit.get("unique") else {}
        detail = next((e.payload for e in evs if e.kind == "detector_detail"), None) or {}
        dec = db.scalar(select(HumanDecision.action).where(HumanDecision.finding_id == finding_id,
                                                           HumanDecision.action.in_(("accept_pr", "reject_pr")))
                        .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc()))
        titles = {c.sc_id: c.title for c in db.scalars(select(Criterion).where(Criterion.sc_id.in_(f.sc_ids or [])))}
        return FindingDetail(
            selector=selector, source=f"{src['file']}:{src['line']}" if src.get("file") and src.get("line") else None,
            sc_titles=titles, decision=dec, instances=int(detail.get("instances") or 1),
            mode=(db.get(AuditRun, f.run_id).config or {}).get("mode", "local"),
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
def evidence_blob(evidence_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        e = db.get(FindingEvidence, evidence_id)
        if e is None or not e.blob_ref:
            raise HTTPException(404, "no such evidence")
        _get_finding(db, e.finding_id, who)  # evidence belongs to a finding, which belongs to a run, which has an owner
        ref = e.blob_ref
    try:
        data = d.store.get(ref)
    except (OSError, ValueError):
        raise HTTPException(404, "evidence file missing") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/runs/{run_id}/report", response_class=HTMLResponse)
def report(run_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        _get_run(db, run_id, who)
    return HTMLResponse(run_report(d.sf, d.store, run_id), headers={
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"})


@router.post("/findings/{finding_id}/decision", response_model=DecisionOut, status_code=201)
def decide(finding_id: str, body: DecisionIn, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        _get_finding(db, finding_id, who)
        v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == finding_id)
                      .order_by(TriageVerdict.created_at.desc()))
        h = HumanDecision(finding_id=finding_id, verdict_id=v.id if v else None, action=body.action,
                          reviewer=body.reviewer if who.is_local else who.login, note=body.note)
        db.add(h)
        db.commit()
        return DecisionOut(id=h.id, finding_id=finding_id, action=h.action)


@router.get("/runs/{run_id}/pull-requests", response_model=PullRequestsOut)
def pull_requests(run_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    with d.sf() as db:
        run = _get_run(db, run_id, who)
        rows = build_pull_requests(db, run)
        project = run.target_url.removeprefix("project:") if run.target_url.startswith("project:") else None
        proj = d.projects.get(project) if project else None
        can = bool(proj and proj.allow_push and run.status in TERMINAL)
    return PullRequestsOut(
        run_id=run_id, project=project, can_publish=can, rows=rows,
        publishable=sum(r.decision == "accepted" and r.remote != "open" for r in rows),
        published=sum(r.remote == "open" for r in rows))


@router.post("/runs/{run_id}/publish", response_model=PublishOut, dependencies=[Depends(limited("publish", "rl_publish_per_min"))])
async def publish(run_id: str, d=Depends(deps), who: Principal = Depends(require_auth)):
    """Push the fixes a person accepted (and that were proved) and open pull requests for them."""
    with d.sf() as db:
        run = _get_run(db, run_id, who)
        target, status = run.target_url, run.status
        remote = (run.config or {}).get("mode") == "remote"
    if remote:  # a live-site run: the pull request is opened through the GitHub API, with nothing cloned or pushed here
        if status not in TERMINAL:
            raise HTTPException(409, "wait for the run to finish before publishing")
        with d.sf() as db:
            return await publish_remote(d, who, db.get(AuditRun, run_id))
    if not target.startswith("project:"):
        raise HTTPException(403, "only runs on an operator-defined project can publish")
    proj = d.projects.get(target.removeprefix("project:"))
    if proj is None or not proj.allow_push:
        raise HTTPException(403, "this project is not allowed to publish (allow_push is false)")
    if status not in TERMINAL or d.active_runs() > 0:
        raise HTTPException(409, "wait for runs to finish before publishing")
    try:
        out = await asyncio.to_thread(publish_accepted, d.sf, proj, run_id)
    except PublishRefused as e:
        raise HTTPException(e.status, e.detail) from None
    if not out.opened and out.errors:
        raise HTTPException(502, "; ".join(out.errors))
    return out


# ------------------------------------------------------------------------------------------ SSE
def _sse(seq: int, type_: str, payload: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(payload)}\n\n"


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = Query(0, ge=0), d=Depends(deps),
                 who: Principal = Depends(require_auth)):
    """Server-sent events: replays from Last-Event-ID / ?after, then tails live; closes when the run ends."""
    with d.sf() as db:
        _get_run(db, run_id, who)
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

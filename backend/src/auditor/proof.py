"""Proving a pull request's fixes on the pull request's own preview deployment.

Attest never builds the customer's code, so a fix it proposes is unproved until something built it. That something is the
customer's own pipeline: when their preview deployment for the pull request is ready, GitHub tells us (a signed webhook)
and this module re-audits it, with the same standard of proof local mode uses:

  * the unpatched LIVE site is audited in the same harness first (the control: the problem must still reproduce there);
  * the SAME element is re-located on the preview (not "some element that passes"), and the rule must pass on it in
    every run;
  * every rule is re-checked on each page and nothing new may appear (the regression guard).

Anything that cannot be established is "could not be re-tested", never a pass. The result is recorded on the finding
and posted as a comment on the pull request. All browsing goes through the network guard.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from playwright.async_api import async_playwright
from sqlalchemy import delete, select

from .api.views import build_pull_requests
from .findings.score import run_score
from .models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    FixAttempt,
    PageScore,
    PageState,
    ProjectRecord,
    ProofJob,
    RegressionCheck,
    ReproveResult,
)
from .pipeline.safe_browse import GUARD, launch_browser
from .remediate import _record_regression, _record_reprove, _score_after
from .reprove.regress import diff_snapshots, snapshot_state, weak_key
from .reprove.reprove import check_on_build, decide
from .scm.pr_stage import STANDING_LINE

log = logging.getLogger("auditor.proof")
PROOF_TIMEOUT_S = 900


def _finish(d, job_id: str, status: str, detail: str) -> None:
    with d.sf() as db:
        j = db.get(ProofJob, job_id)
        j.status, j.detail, j.finished_at = status, detail[:2000], datetime.now(UTC)
        db.commit()


async def prove_preview(d, job_id: str) -> None:
    """Run one proof job to completion. Never raises: every outcome lands on the job row."""
    async with d.proof_sem:
        try:
            await _prove(d, job_id)
        except Exception as e:  # noqa: BLE001 - a proof that cannot run is reported, not thrown into the server
            log.warning("proof job %s failed: %s", job_id, e)
            _finish(d, job_id, "failed", f"{type(e).__name__}: {e}")


async def _prove(d, job_id: str) -> None:
    with d.sf() as db:
        job = db.get(ProofJob, job_id)
        job.status = "running"
        db.commit()
        run = db.get(AuditRun, job.run_id)
        project = db.get(ProjectRecord, run.project_id) if run and run.project_id else None
        if project is None:
            return _finish(d, job_id, "skipped", "the project no longer exists")
        proj = {"installation_id": project.installation_id, "repo": project.github_repo, "site_url": project.site_url,
                "auth": d.vault.open(project.sealed_auth) if project.sealed_auth else None}
        branch, sha, preview, run_id = job.branch, job.deployment_sha, job.preview_url, job.run_id
        items = []
        for a in db.scalars(select(FixAttempt).join(Finding, Finding.id == FixAttempt.finding_id)
                            .where(Finding.run_id == run_id, FixAttempt.branch == branch, FixAttempt.pr_url.is_not(None))):
            f = db.get(Finding, a.finding_id)
            loc = db.get(FindingLocator, f.id)
            if loc is None or (loc.fingerprint or {}).get("unlocatable"):
                continue
            items.append({"fid": f.id, "rule": f.rule_id, "sc": f.sc_ids, "page": f.page_state_id, "fp": loc.fingerprint,
                          "ignore": list(a.expected_dom_changes or []), "attempt": a.id})
        pages = {p.id: {"id": p.id, "url": p.url, "steps": p.steps, "label": p.state_label} for p in db.scalars(
            select(PageState).where(PageState.run_id == run_id))}
        pr_number = _pr_number(db, run)
    if not items:
        return _finish(d, job_id, "skipped", "no fix on this branch can be re-tested")

    gh = d.github_factory(proj["installation_id"], proj["repo"])
    head = await gh.branch_sha(branch)
    if head != sha:  # the preview shows a different commit than the one Attest wrote (someone pushed since): do not claim anything
        return _finish(d, job_id, "skipped", "the preview is of a different commit than Attest's, so its result would not describe Attest's change")

    guard = d.guard_factory()
    by_page: dict[str, list[dict]] = {}
    for it in items:
        by_page.setdefault(it["page"], []).append(it)
    outcomes: dict[str, object] = {}
    regress: dict[str, object] = {}
    scores: dict[str, tuple[int, dict]] = {}
    cfg = SimpleNamespace(auth=proj["auth"], with_probes=False)
    async with guard.proxy:
        token = GUARD.set(guard)
        try:
            async with async_playwright() as pw:
                browser = await launch_browser(pw)
                try:
                    for pid, its in by_page.items():
                        pg = pages[pid]
                        kw = {"page_url": pg["url"], "steps": pg["steps"], "auth": proj["auth"]}
                        before = await snapshot_state(browser, proj["site_url"], with_probes=False, **kw)
                        control = {it["fid"]: await check_on_build(browser, proj["site_url"], stored_fp=it["fp"], rule_id=it["rule"],
                                                                   ignore_attrs=it["ignore"], runs=2, **kw) for it in its}
                        after = {it["fid"]: await check_on_build(browser, preview, stored_fp=it["fp"], rule_id=it["rule"],
                                                                 ignore_attrs=it["ignore"], runs=2, **kw) for it in its}
                        snap_after = await snapshot_state(browser, preview, with_probes=False, **kw)
                        scores[pid] = await _score_after(browser, preview, pg, cfg)
                        for it in its:
                            outcomes[it["fid"]] = decide(control[it["fid"]], after[it["fid"]])
                        regress[pid] = diff_snapshots(before, snap_after, fixed_keys=[weak_key(it["rule"], it["fp"]) for it in its])
                finally:
                    await browser.close()
            await guard.raise_if_tripped()
        finally:
            GUARD.reset(token)

    _clear_old_proof(d, items)
    for pid, its in by_page.items():
        for it in its:
            _record_reprove(d.sf, d.store, d.bus, run_id, pid, it, outcomes[it["fid"]], preview)
        _record_regression(d.sf, d.store, d.bus, run_id, pid, its, regress[pid])
    _upsert_after_scores(d, run_id, scores)
    _refresh_summary(d, run_id)

    comment = _comment(items, outcomes, regress, preview)
    if pr_number:
        await gh.comment(pr_number, comment)
    _finish(d, job_id, "done", f"{sum(1 for o in outcomes.values() if o.verdict == 'pass')} of {len(outcomes)} proved")


def _pr_number(db, run: AuditRun) -> int | None:
    import re

    for r in build_pull_requests(db, run):
        m = re.search(r"/pull/(\d+)", r.pr_url or "")
        if m:
            return int(m.group(1))
    return None


def _clear_old_proof(d, items: list[dict]) -> None:
    """A newer preview replaces the previous result: keep the latest proof only."""
    with d.sf() as db:
        fids = [i["fid"] for i in items]
        attempts = [i["attempt"] for i in items]
        db.execute(delete(ReproveResult).where(ReproveResult.finding_id.in_(fids)))
        db.execute(delete(RegressionCheck).where(RegressionCheck.fix_attempt_id.in_(attempts)))
        db.execute(delete(FindingEvidence).where(FindingEvidence.finding_id.in_(fids), FindingEvidence.kind.in_(
            ("reprove_shot_before", "reprove_shot_after", "reprove_shot_after_annotated", "reprove_result", "regress_report"))))
        db.commit()


def _upsert_after_scores(d, run_id: str, scores: dict[str, tuple[int, dict]]) -> None:
    """One 'after' row per page. Several pull requests may each improve a page; the best measured score stands."""
    with d.sf() as db:
        for pid, (score, breakdown) in scores.items():
            row = db.scalar(select(PageScore).where(PageScore.page_state_id == pid, PageScore.phase == "after"))
            if row is None:
                db.add(PageScore(run_id=run_id, page_state_id=pid, phase="after", score=score,
                                 breakdown={**breakdown, "measured_on": "pull request preview"}))
            elif score > row.score:
                row.score, row.breakdown = score, {**breakdown, "measured_on": "pull request preview"}
        db.commit()


def _refresh_summary(d, run_id: str) -> None:
    """Bring the run's headline and scores up to date with what has been proved so far."""
    with d.sf() as db:
        run = db.get(AuditRun, run_id)
        findings = db.scalars(select(Finding).where(Finding.run_id == run_id)).all()
        proved = sum(f.status.value in ("proved", "regress_ok") for f in findings)
        ready = sum(f.status.value in ("fix_authored", "pr_open") for f in findings)
        human = sum(f.status.value == "needs_human" for f in findings)
        base = {s.page_state_id: s.score for s in db.scalars(select(PageScore).where(PageScore.run_id == run_id, PageScore.phase == "baseline"))}
        after = {s.page_state_id: s.score for s in db.scalars(select(PageScore).where(PageScore.run_id == run_id, PageScore.phase == "after"))}
        summary = dict(run.summary or {})
        summary.update(
            proved=proved, awaiting_proof=ready, needs_human=human,
            headline=f"{len(findings)} issues - {proved} fixed (proved) - {ready} awaiting proof - {human} need you",
            score_after=run_score([after.get(pid, s) for pid, s in base.items()]) if after else None)
        run.summary = summary
        db.commit()


def _comment(items: list[dict], outcomes: dict, regress: dict, preview: str) -> str:
    lines = ["### Attest re-tested this pull request's preview", f"Preview: {preview}", ""]
    word = {"pass": "**passed**", "fail": "**still fails**", "inconclusive": "**could not be re-tested**"}
    for it in items:
        o = outcomes[it["fid"]]
        lines.append(f"- `{it['rule']}` (WCAG {', '.join(it['sc']) or 'n/a'}): {word.get(o.verdict, o.verdict)}. {o.notes}")
    news = sum(r.regressions_count for r in regress.values())
    lines += ["", f"Regression check: {news} new finding{'s' if news != 1 else ''} on the pages re-checked.", "", "---",
              "This is an automated re-test of one preview deployment, produced by Attest. " + STANDING_LINE]
    return "\n".join(lines)

"""The closed loop: drive -> detect -> triage -> fix -> re-prove -> regression-guard (-> PRs).

Invariants enforced here (each has a test):
  * only findings present in BOTH baseline passes, with ONE unambiguous source location, can be auto-fixed;
  * every fix is applied anchor-exactly, verified by the repo's own checks, and rolled back on failure;
  * a fix counts as proved only if an unpatched control still fails AND the same element is re-located on
    the patched build AND the rule passes there, in every run;
  * the regression guard compares whole pages before/after the patch;
  * nothing leaves the machine (push / PR / issue) unless the provider was created with allow_push.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .findings import locator as loc
from .findings.detect import RawFinding, detect_state, persist_findings
from .findings.score import page_score, run_score
from .fix import author as author_mod
from .fix import source_map
from .fix.journal import Journal, get_or_create_attempt, idempotency_key
from .fix.verify import apply_and_verify
from .models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    FindingStatus,
    FixAttempt,
    PageScore,
    PageState,
    RegressionCheck,
    ReproveResult,
    RunStatus,
    Severity,
    TriageVerdict,
)
from .pipeline.events import EventBus
from .pipeline.prober import _open
from .reprove.devserver import dev_server
from .reprove.regress import diff_snapshots, snapshot_state, weak_key
from .reprove.reprove import check_on_build, decide, rebase_url
from .scm.base import RepoProvider
from .scm.local_git import LocalGitProvider
from .scm.pr_stage import FixSummary, build_pr, build_work_order
from .storage.evidence_store import EvidenceStore
from .triage.triage import run_triage

log = logging.getLogger(__name__)
TIER_BRANCH = {"PR1": "pr1-mechanical", "PR2": "pr2-review"}
MAX_FIX_ATTEMPTS = 2


@dataclass
class RemediationConfig:
    repo_path: Path
    dev_command: str
    check_commands: list[str] = field(default_factory=list)
    pages: list[dict] = field(default_factory=lambda: [{"path": "/", "label": "home"}])
    auth: dict | None = None
    base_branch: str = "main"
    allow_push: bool = False
    triage_model: str = "claude-opus-5-5"
    fix_model: str = "claude-opus-5-5"
    with_probes: bool = False
    send_screenshots_to_model: bool = False  # privacy: off unless an operator opts in
    only_rules: list[str] | None = None
    max_fixes: int | None = None
    max_issues: int = 25
    reprove_runs: int = 2

    def public(self) -> dict:
        """Run config safe to store (no auth values)."""
        return {"repo_path": str(self.repo_path), "dev_command": self.dev_command,
                "check_commands": self.check_commands, "pages": self.pages, "base_branch": self.base_branch,
                "allow_push": self.allow_push, "with_probes": self.with_probes, "only_rules": self.only_rules,
                "max_fixes": self.max_fixes, "auth_keys": sorted((self.auth or {}).keys())}


def _status(sf: sessionmaker, run_id: str, status: RunStatus, **kw) -> None:
    with sf() as db:
        run = db.get(AuditRun, run_id)
        run.status = status
        for k, v in kw.items():
            setattr(run, k, v)
        db.commit()


def _evidence(sf, finding_id, stage, kind, payload=None, store=None, run_id=None, page_id=None, blob=None):
    ref = sha = None
    if blob is not None:
        ref, sha = store.put(run_id, page_id, f"{finding_id}-{kind}.png", blob)
    with sf() as db:
        db.add(FindingEvidence(finding_id=finding_id, stage=stage, kind=kind, blob_ref=ref, sha256=sha,
                               payload=payload))
        db.commit()


def _set_finding(sf, finding_id, status: FindingStatus) -> None:
    with sf() as db:
        db.get(Finding, finding_id).status = status
        db.commit()


# ------------------------------------------------------------------------------------------ run
async def run_remediation(
    run_id: str, sf: sessionmaker, store: EvidenceStore, bus: EventBus, client, cfg: RemediationConfig,
    provider: RepoProvider | None = None,
) -> dict:
    provider = provider or LocalGitProvider(cfg.repo_path, allow_push=cfg.allow_push)
    began = False  # force-restore is only safe once we know the tree was clean when we started
    try:
        if not Path(cfg.repo_path).is_dir():
            raise RuntimeError(f"repo path does not exist: {cfg.repo_path}")
        if not (Path(cfg.repo_path) / ".git").exists():
            raise RuntimeError(f"not a git repository: {cfg.repo_path}")
        base_sha = provider.head_sha()
        if not provider.is_clean():
            raise RuntimeError("target repo has uncommitted changes; refusing to run")
        began = True
        summary = await _run(run_id, sf, store, bus, client, cfg, provider, base_sha)
        _status(sf, run_id, RunStatus.complete, finished_at=datetime.now(UTC), summary=summary)
        bus.emit(run_id, "run.summary", summary)
        return summary
    except Exception as e:  # a run never dies silently
        _status(sf, run_id, RunStatus.failed, finished_at=datetime.now(UTC))
        bus.emit(run_id, "error", {"stage": "run", "error": f"{type(e).__name__}: {e}"})
        raise
    finally:
        if began:  # never `checkout -f` over changes that are the user's, not ours
            try:
                provider.restore(cfg.base_branch)  # leave the repo where we found it
            except Exception as e:  # noqa: BLE001
                log.warning("could not restore %s: %s", cfg.base_branch, e)


async def _run(run_id, sf, store, bus, client, cfg, provider, base_sha) -> dict:
    with sf() as db:
        db.get(AuditRun, run_id).config = cfg.public()
        db.commit()
    repo = cfg.repo_path
    pages: list[dict] = []  # {id, label, path, steps, url}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            # ============================ 1. DRIVE + DETECT (baseline, two passes) ============================
            _status(sf, run_id, RunStatus.detecting)
            bus.emit(run_id, "stage.started", {"stage": "detect"})
            with dev_server(repo, cfg.dev_command) as base_url:
                for pc in cfg.pages:
                    pages.append(await _detect_page(sf, store, bus, browser, run_id, base_url, pc, cfg))
                await _map_sources(sf, store, browser, repo, run_id, pages, cfg)
            bus.emit(run_id, "stage.finished", {"stage": "detect"})

            # ============================ 2. TRIAGE ============================
            _status(sf, run_id, RunStatus.triaging)
            bus.emit(run_id, "stage.started", {"stage": "triage"})
            await run_triage(sf, store, client, cfg.triage_model, run_id, cfg.send_screenshots_to_model)
            with sf() as db:
                for f in db.scalars(select(Finding).where(Finding.run_id == run_id)):
                    bus.emit(run_id, "finding.triaged", {"finding_id": f.id, "status": f.status.value})
            bus.emit(run_id, "stage.finished", {"stage": "triage"})

            # ============================ 3. FIX ============================
            _status(sf, run_id, RunStatus.fixing)
            bus.emit(run_id, "stage.started", {"stage": "fix"})
            tiers = await _author_fixes(sf, store, bus, client, cfg, provider, run_id, base_sha)
            bus.emit(run_id, "stage.finished", {"stage": "fix", "tiers": {k: len(v["findings"]) for k, v in tiers.items()}})

            # ============================ 4+5. RE-PROVE + REGRESSION ============================
            _status(sf, run_id, RunStatus.reproving)
            bus.emit(run_id, "stage.started", {"stage": "reprove"})
            after_scores = await _reprove_tiers(sf, store, bus, browser, cfg, provider, run_id, base_sha, pages, tiers)
            bus.emit(run_id, "stage.finished", {"stage": "reprove"})
        finally:
            await browser.close()

    # ============================ 6. PRs / WORK ORDERS ============================
    pr_urls = _stage_prs(sf, store, bus, provider, cfg, run_id, tiers)
    return _summarise(sf, run_id, pages, after_scores, pr_urls, client, tiers)


# ------------------------------------------------------------------------------------------ detect
async def _detect_page(sf, store, bus, browser, run_id, base_url, pc, cfg) -> dict:
    url = rebase_url(base_url + pc["path"], base_url)
    steps = pc.get("steps")
    label = pc.get("label", pc["path"])
    state_ref = {"flow_name": label, "label": label, "steps": steps or []}
    with sf() as db:
        ps = PageState(run_id=run_id, url=url, flow_name=label, state_label=label, steps=steps, status="probed")
        db.add(ps)
        db.commit()
        pid = ps.id
    first = await detect_state(browser, url, steps=steps, auth=cfg.auth, state_ref=state_ref,
                               with_probes=cfg.with_probes)
    second = await detect_state(browser, url, steps=steps, auth=cfg.auth, state_ref=state_ref,
                                with_probes=cfg.with_probes, with_evidence=False)
    ids = persist_findings(sf, store, run_id, pid, state_ref, first, second)
    for fid in ids:
        bus.emit(run_id, "finding.caught", {"finding_id": fid, "page_state_id": pid})
    score, breakdown = page_score((f.rule_id, f.severity) for f in first)
    with sf() as db:
        db.add(PageScore(run_id=run_id, page_state_id=pid, phase="baseline", score=score, breakdown=breakdown))
        db.commit()
    bus.emit(run_id, "score.updated", {"page_state_id": pid, "phase": "baseline", "score": score})
    return {"id": pid, "label": label, "path": pc["path"], "steps": steps, "url": url}


async def _map_sources(sf, store, browser, repo: Path, run_id, pages, cfg) -> None:
    """Resolve each element-level finding to its source location while the baseline build is running."""
    for pg in pages:
        with sf() as db:
            rows = db.execute(select(Finding.id, FindingLocator.fingerprint).join(FindingLocator).where(
                Finding.page_state_id == pg["id"])).all()
        ctx, page, _ = await _open(browser, pg["url"], pg["steps"], cfg.auth)
        try:
            await loc.install(page)
            for fid, fp in rows:
                el = None
                if not fp.get("unlocatable"):
                    rel = await loc.relocate(page, fp)
                    el = rel.handle if rel.found else None
                res = await source_map.resolve(repo, fp, el)
                payload = {"unique": res.unique, "reason": res.reason,
                           "hit": res.hit.__dict__ if res.hit else None}
                _evidence(sf, fid, "caught", "source_hit", payload)
        finally:
            await ctx.close()


# ------------------------------------------------------------------------------------------ fix
async def _author_fixes(sf, store, bus, client, cfg, provider, run_id, base_sha) -> dict:
    """Returns {tier: {"branch", "findings": [finding_id], "attempts": {finding_id: attempt_id}}}."""
    with sf() as db:
        rows = db.execute(
            select(Finding, TriageVerdict).join(TriageVerdict, TriageVerdict.finding_id == Finding.id).where(
                Finding.run_id == run_id, Finding.status == FindingStatus.triaged,
                TriageVerdict.tier == "auto_fix", TriageVerdict.valid.is_(True))
            .order_by(Finding.severity, Finding.id)).all()
    picked = [(f.id, v.id, v.pr_tier, v.fix_plan) for f, v in rows
              if not cfg.only_rules or f.rule_id in cfg.only_rules]
    if cfg.max_fixes is not None:
        picked = picked[: cfg.max_fixes]
    tiers: dict[str, dict] = {}
    for fid, vid, pr_tier, plan in picked:
        tier = pr_tier or "PR2"
        t = tiers.get(tier)
        if t is None:
            branch = f"a11y/{run_id[:8]}-{TIER_BRANCH[tier]}"
            if provider.branch_exists(branch):
                provider.checkout(branch)
            else:
                provider.create_branch(branch, base_sha)
            t = tiers[tier] = {"branch": branch, "findings": [], "attempts": {}}
        elif provider.current_branch() != t["branch"]:
            provider.checkout(t["branch"])
        ok = await _fix_one(sf, store, bus, client, cfg, provider, run_id, base_sha, fid, vid, tier, plan, t)
        if ok:
            t["findings"].append(fid)
    for t in tiers.values():
        t["tip"] = None
    # record each branch tip
    for tier, t in tiers.items():
        provider.checkout(t["branch"])
        t["tip"] = provider.head_sha()
    provider.restore(cfg.base_branch)
    for t in tiers.values():  # a tier branch with no verified fix must not be left behind in the user's repo
        if not t["findings"] and provider.branch_exists(t["branch"]):
            provider.delete_branch(t["branch"])
    return {k: v for k, v in tiers.items() if v["findings"]}


async def _fix_one(sf, store, bus, client, cfg, provider, run_id, base_sha, fid, vid, tier, plan, t) -> bool:
    with sf() as db:
        f = db.get(Finding, fid)
        ev = {e.kind: e for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == fid))}
        rule, dedupe, sev, impact = f.rule_id, f.dedupe_key, f.severity.value, f.impact_text
        src = ev["source_hit"].payload
        snippet = (ev["dom_snippet"].payload or {}).get("html", "")
    hit = source_map.SourceHit(**src["hit"])
    key = idempotency_key(dedupe, rule, base_sha, author_mod.PROMPT_VERSION)
    attempt_id, _created = get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr=tier,
                                                repo=str(cfg.repo_path), base_sha=base_sha, key=key)
    journal = Journal(sf, attempt_id)
    if journal.done("committed"):  # resumed run: this fix is already on the branch
        t["attempts"][fid] = attempt_id
        return True

    finding_ctx = {"rule": rule, "severity": sev, "impact": impact, "dom": snippet}
    err = None
    for attempt in range(MAX_FIX_ATTEMPTS):
        file_text = (cfg.repo_path / hit.file).read_text()
        authored = await author_mod.author_fix(client, cfg.fix_model, finding_ctx, hit, plan, file_text, err)
        if not authored.valid:
            err = "; ".join(authored.notes)
            continue
        journal.record("authored", detail={"edits": authored.edits})
        res = apply_and_verify(cfg.repo_path, authored.edits, {hit.file}, cfg.check_commands, rule_id=rule)
        if not res.ok:
            err = res.error
            journal.record("verified", "failed", {"error": err, "checks": res.checks})
            continue
        journal.record("applied")
        journal.record("verified", detail={"checks": res.checks})
        msg = f"a11y: fix {rule} ({hit.file}:{hit.line})"
        sha = provider.commit(msg, [hit.file])
        diff = provider.show_diff(sha) if hasattr(provider, "show_diff") else res.diff
        journal.record("committed", detail={"sha": sha})
        with sf() as db:
            fa = db.get(FixAttempt, attempt_id)
            fa.branch, fa.edits, fa.diff = t["branch"], authored.edits, diff
            fa.expected_dom_changes, fa.verify_log, fa.status = authored.expected_dom_changes, {"checks": res.checks}, "committed"
            db.commit()
        _evidence(sf, fid, "fix", "diff", {"diff": diff, "explanation": authored.explanation,
                                           "edits": authored.edits, "expected_dom_changes": authored.expected_dom_changes,
                                           "commit": sha})
        _set_finding(sf, fid, FindingStatus.fix_authored)
        bus.emit(run_id, "finding.fix_authored", {"finding_id": fid, "tier": tier, "commit": sha})
        t["attempts"][fid] = attempt_id
        return True
    # could not produce a verified fix: hand it to a human, with the reason on record
    _evidence(sf, fid, "fix", "fix_failed", {"reason": err})
    _set_finding(sf, fid, FindingStatus.needs_human)
    bus.emit(run_id, "finding.needs_human", {"finding_id": fid, "reason": f"fix could not be verified: {err}"})
    return False


# ------------------------------------------------------------------------------------------ re-prove
async def _reprove_tiers(sf, store, bus, browser, cfg, provider, run_id, base_sha, pages, tiers) -> dict:
    after_scores: dict[str, int] = {}
    pg_by_id = {p["id"]: p for p in pages}
    for t in tiers.values():
        with sf() as db:
            items = []
            for fid in t["findings"]:
                f = db.get(Finding, fid)
                fp = db.get(FindingLocator, fid)
                fa = db.get(FixAttempt, t["attempts"][fid])
                items.append({"fid": fid, "rule": f.rule_id, "page": f.page_state_id, "fp": fp.fingerprint,
                              "ignore": list(fa.expected_dom_changes or []), "attempt": fa.id})
        by_page: dict[str, list[dict]] = {}
        for it in items:
            by_page.setdefault(it["page"], []).append(it)

        control, before_snaps = {}, {}
        provider.restore(base_sha)
        with dev_server(cfg.repo_path, cfg.dev_command) as url:
            for pid, its in by_page.items():
                pg = pg_by_id[pid]
                kw = {"page_url": pg["url"], "steps": pg["steps"], "auth": cfg.auth}
                before_snaps[pid] = await snapshot_state(browser, url, with_probes=cfg.with_probes, **kw)
                for it in its:
                    control[it["fid"]] = await check_on_build(
                        browser, url, stored_fp=it["fp"], rule_id=it["rule"], ignore_attrs=it["ignore"],
                        runs=cfg.reprove_runs, **kw)

        provider.restore(t["tip"])
        with dev_server(cfg.repo_path, cfg.dev_command) as url:
            for pid, its in by_page.items():
                pg = pg_by_id[pid]
                kw = {"page_url": pg["url"], "steps": pg["steps"], "auth": cfg.auth}
                after = {}
                for it in its:
                    after[it["fid"]] = await check_on_build(
                        browser, url, stored_fp=it["fp"], rule_id=it["rule"], ignore_attrs=it["ignore"],
                        runs=cfg.reprove_runs, **kw)
                snap_after = await snapshot_state(browser, url, with_probes=cfg.with_probes, **kw)
                score = await _score_after(sf, browser, url, run_id, pg, cfg)
                after_scores[pid] = score
                for it in its:
                    out = decide(control[it["fid"]], after[it["fid"]])
                    _record_reprove(sf, store, bus, run_id, pid, it, out, url)
                rep = diff_snapshots(before_snaps[pid], snap_after,
                                     fixed_keys=[weak_key(it["rule"], it["fp"]) for it in its])
                _record_regression(sf, store, bus, run_id, pid, its, rep)
        provider.restore(cfg.base_branch)
    return after_scores


async def _score_after(sf, browser, url, run_id, pg, cfg) -> int:
    found = await detect_state(browser, rebase_url(pg["url"], url), steps=pg["steps"], auth=cfg.auth,
                               with_probes=cfg.with_probes, with_evidence=False)
    score, breakdown = _score_findings(found)
    with sf() as db:
        db.add(PageScore(run_id=run_id, page_state_id=pg["id"], phase="after", score=score, breakdown=breakdown))
        db.commit()
    return score


def _score_findings(found: list[RawFinding]) -> tuple[int, dict]:
    return page_score((f.rule_id, f.severity) for f in found)


def _record_reprove(sf, store, bus, run_id, pid, it, out, url) -> None:
    fid = it["fid"]
    before_ref = after_ref = None
    if out.before_png:
        before_ref, _ = store.put(run_id, pid, f"{fid}-reprove-before.png", out.before_png)
    if out.after_png:
        after_ref, _ = store.put(run_id, pid, f"{fid}-reprove-after.png", out.after_png)
    annotated_ref = None
    if out.after_annotated:
        annotated_ref, _ = store.put(run_id, pid, f"{fid}-reprove-after-annotated.png", out.after_annotated)
    with sf() as db:
        db.add(ReproveResult(
            finding_id=fid, fix_attempt_id=it["attempt"], build_url=url, relocated=out.relocated,
            relocation_score=out.relocation_score, relocation_method=out.relocation_method,
            baseline_failed=out.baseline_failed, rule_pass=out.rule_pass, runs_n=out.runs_n,
            before_ref=before_ref, after_ref=after_ref, verdict=out.verdict, notes=out.notes))
        db.commit()
    for kind, ref in (("reprove_shot_before", before_ref), ("reprove_shot_after", after_ref),
                      ("reprove_shot_after_annotated", annotated_ref)):
        if ref:
            with sf() as db:
                db.add(FindingEvidence(finding_id=fid, stage="proof", kind=kind, blob_ref=ref))
                db.commit()
    _evidence(sf, fid, "proof", "reprove_result", {
        "verdict": out.verdict, "notes": out.notes, "baseline_failed": out.baseline_failed,
        "relocated": out.relocated, "relocation_score": out.relocation_score,
        "relocation_method": out.relocation_method, "runs_n": out.runs_n})
    _set_finding(sf, fid, {"pass": FindingStatus.proved, "fail": FindingStatus.prove_failed}.get(
        out.verdict, FindingStatus.inconclusive))
    bus.emit(run_id, "finding.proved", {"finding_id": fid, "verdict": out.verdict, "notes": out.notes})


def _record_regression(sf, store, bus, run_id, pid, its, rep) -> None:
    for it in its:
        with sf() as db:
            db.add(RegressionCheck(
                fix_attempt_id=it["attempt"], page_state_id=pid, criteria_checked=rep.criteria_checked,
                new_findings=rep.new_findings, regressions_count=rep.regressions_count, verdict=rep.verdict))
            f = db.get(Finding, it["fid"])
            if f.status == FindingStatus.proved:  # only a proved fix can be marked regression-clean
                f.status = FindingStatus.regress_ok if rep.verdict == "pass" else FindingStatus.regress_fail
            db.commit()
        _evidence(sf, it["fid"], "regression", "regress_report", {
            "verdict": rep.verdict, "regressions_count": rep.regressions_count, "new_findings": rep.new_findings,
            "lost_passes": rep.lost_passes, "criteria_checked": len(rep.criteria_checked)})
    bus.emit(run_id, "finding.regressed", {"page_state_id": pid, "verdict": rep.verdict,
                                           "regressions": rep.regressions_count})


# ------------------------------------------------------------------------------------------ PRs
def _page_name(ps: PageState) -> str:
    """Reviewer-facing page name: the label and path, never the ephemeral dev-server host:port."""
    path = urlsplit(ps.url).path or "/"
    return f"{ps.state_label} ({path})" if ps.state_label and ps.state_label != path else path


def _fix_summaries(sf, run_id, finding_ids) -> list[FixSummary]:
    out = []
    with sf() as db:
        for fid in finding_ids:
            f = db.get(Finding, fid)
            ps = db.get(PageState, f.page_state_id)
            ev = {e.kind: e for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == fid))}
            fixp = (ev["diff"].payload if "diff" in ev else {}) or {}
            rr = (ev["reprove_result"].payload if "reprove_result" in ev else None)
            rg = (ev["regress_report"].payload if "regress_report" in ev else None)
            out.append(FixSummary(
                finding_id=fid, rule_id=f.rule_id, sc_ids=f.sc_ids, severity=f.severity.value, page_url=_page_name(ps),
                impact_text=f.impact_text or "", explanation=fixp.get("explanation", ""), diff=fixp.get("diff", ""),
                proof=None if rr is None else {"verdict": rr["verdict"], "relocation_score": rr["relocation_score"],
                                               "runs_n": rr["runs_n"]},
                regression=None if rg is None else {"regressions_count": rg["regressions_count"],
                                                    "verdict": rg["verdict"]}))
    return out


def _stage_prs(sf, store, bus, provider, cfg, run_id, tiers) -> dict:
    """Builds PR/issue text always (stored as evidence for review); publishes only if allowed."""
    urls: dict[str, str | None] = {}
    for tier, t in tiers.items():
        fixes = _fix_summaries(sf, run_id, t["findings"])
        title, body = build_pr(tier, run_id, fixes)
        store.put(run_id, "prs", f"{tier}.md", f"# {title}\n\n{body}\n".encode())
        urls[tier] = None
        if cfg.allow_push:
            provider.push(t["branch"])
            urls[tier] = provider.open_pr(t["branch"], cfg.base_branch, title, body)
            with sf() as db:
                for aid in t["attempts"].values():
                    fa = db.get(FixAttempt, aid)
                    fa.pr_url, fa.status = urls[tier], "pr_open"
                db.commit()
            bus.emit(run_id, "finding.pr_opened", {"tier": tier, "url": urls[tier]})
    with sf() as db:
        humans = db.scalars(select(Finding).where(Finding.run_id == run_id,
                                                  Finding.status == FindingStatus.needs_human)).all()
        ids = [f.id for f in humans][: cfg.max_issues]
    issue_urls = []
    for i, s in enumerate(_fix_summaries(sf, run_id, ids)):
        with sf() as db:
            v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == s.finding_id)
                          .order_by(TriageVerdict.created_at.desc()))
            reason = v.reason_code if v else "needs_human"
        title, body = build_work_order(s, reason)
        store.put(run_id, "issues", f"{i:03d}-{s.finding_id}.md", f"# {title}\n\n{body}\n".encode())
        if cfg.allow_push:
            issue_urls.append(provider.open_issue(title, body, ["accessibility"]))
    urls["issues"] = issue_urls
    return urls


# ------------------------------------------------------------------------------------------ summary
def _summarise(sf, run_id, pages, after_scores, pr_urls, client, tiers) -> dict:
    with sf() as db:
        counts = dict(db.execute(select(Finding.status, func.count()).where(Finding.run_id == run_id)
                                 .group_by(Finding.status)).all())
        counts = {k.value: v for k, v in counts.items()}
        total = sum(counts.values())
        base = {s.page_state_id: s.score for s in db.scalars(select(PageScore).where(
            PageScore.run_id == run_id, PageScore.phase == "baseline"))}
        sev = dict(db.execute(select(Finding.severity, func.count()).where(Finding.run_id == run_id)
                              .group_by(Finding.severity)).all())
        regress = db.scalar(select(func.coalesce(func.sum(RegressionCheck.regressions_count), 0))
                            .join(FixAttempt, FixAttempt.id == RegressionCheck.fix_attempt_id)
                            .join(Finding, Finding.id == FixAttempt.finding_id).where(Finding.run_id == run_id))
    auto_fixed = sum(counts.get(k, 0) for k in ("proved", "regress_ok", "regress_fail"))
    needs_human = counts.get("needs_human", 0)
    final = {pid: after_scores.get(pid, s) for pid, s in base.items()}
    return {
        "headline": f"{total} issues - {auto_fixed} fixed in PRs (proved) - {needs_human} need you",
        "total_findings": total, "by_status": counts,
        "by_severity": {k.value: v for k, v in sev.items()},
        "proved": auto_fixed, "needs_human": needs_human,
        "inconclusive": counts.get("inconclusive", 0) + counts.get("prove_failed", 0),
        "regressions": int(regress or 0),
        "score_before": run_score(list(base.values())), "score_after": run_score(list(final.values())),
        "prs": {k: v for k, v in pr_urls.items() if k != "issues"}, "issues_opened": len(pr_urls.get("issues", [])),
        "branches": {k: v["branch"] for k, v in tiers.items()},
        "model_kind": "scripted (not a language model)" if getattr(client, "scripted", False) else "language model",
        "notice": "Automated first pass. Every change is a pull request for a human to review.",
    }


__all__ = ["RemediationConfig", "Severity", "run_remediation"]

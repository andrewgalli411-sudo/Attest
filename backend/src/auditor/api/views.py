"""Read models for the web app: pipeline stages, run metadata, and the pull-request list. Pure functions of the database."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select

from ..models import (
    AuditRun,
    Finding,
    FixAttempt,
    FixJournal,
    HumanDecision,
    PageState,
    RegressionCheck,
    ReproveResult,
    RunEvent,
    RunStatus,
    TriageVerdict,
)
from .schemas import PullRequestRow, RunMeta, StageOut

TERMINAL = {RunStatus.complete, RunStatus.failed, RunStatus.cancelled, RunStatus.reviewed, RunStatus.exported}
PUBLISHABLE = {"regress_ok"}  # proved on the patched build and no regression
REMOTE_PUBLISHABLE = {"fix_authored", "proved", "regress_ok"}  # live-site runs: a proposal may become a pull request before it is proved
PROVED = {"proved", "regress_ok"}


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    return None if a is None or b is None else max(0.0, (b - a).total_seconds())


def build_meta(db, run: AuditRun, page_count: int, publish: bool) -> RunMeta:
    cfg = run.config or {}
    summary = run.summary or {}
    base = db.scalar(select(FixAttempt.base_sha).join(Finding, Finding.id == FixAttempt.finding_id)
                     .where(Finding.run_id == run.id, FixAttempt.base_sha != "").limit(1))
    return RunMeta(
        mode=cfg.get("mode", "local"), project=run.target_url.removeprefix("project:") if run.target_url.startswith("project:") else None,
        base_branch=cfg.get("base_branch"), base_sha=(base or None) and base[:7], started_at=run.created_at.isoformat(),
        duration_s=_secs(run.created_at, run.finished_at), page_count=page_count, publish=bool(cfg.get("allow_push", publish)),
        model_tokens_used=summary.get("model_tokens_used"), model_token_budget=summary.get("model_token_budget"))


def _fmt(s: float | None) -> str:
    if s is None:
        return ""
    s = round(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def build_stages(db, run: AuditRun) -> list[StageOut]:
    """Five stages for the progress card. The backend has four stage events; 'prove' and 'regress' both live inside
    'reprove', so the reprove duration is shown once, on 'prove'."""
    started: dict[str, datetime] = {}
    finished: dict[str, datetime] = {}
    for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type.in_(("stage.started", "stage.finished")))
                        .order_by(RunEvent.seq)):
        (started if e.type == "stage.started" else finished)[str((e.payload or {}).get("stage"))] = e.ts
    done_run = run.status in TERMINAL
    ok = run.status in (RunStatus.complete, RunStatus.reviewed, RunStatus.exported)

    findings = db.scalars(select(Finding).where(Finding.run_id == run.id)).all()
    ids = [f.id for f in findings]
    verdicts = {}
    for v in db.scalars(select(TriageVerdict).where(TriageVerdict.finding_id.in_(ids)).order_by(TriageVerdict.created_at)) if ids else []:
        verdicts[v.finding_id] = v
    auto = sum(v.tier.value == "auto_fix" for v in verdicts.values())
    attempts = db.scalars(select(FixAttempt).where(FixAttempt.finding_id.in_(ids))).all() if ids else []
    edits = sum(1 for a in attempts if a.diff)
    reproves = db.scalars(select(ReproveResult).where(ReproveResult.finding_id.in_(ids))).all() if ids else []
    ok_n, fail_n = sum(r.verdict == "pass" for r in reproves), sum(r.verdict == "fail" for r in reproves)
    inc_n = sum(r.verdict == "inconclusive" for r in reproves)
    regs = db.scalars(select(RegressionCheck).join(FixAttempt, FixAttempt.id == RegressionCheck.fix_attempt_id)
                      .where(FixAttempt.finding_id.in_(ids))).all() if ids else []
    new_n = sum(r.regressions_count for r in regs)
    rules = max((len(r.criteria_checked or []) for r in regs), default=0)
    with_probes = bool((run.config or {}).get("with_probes"))
    remote = (run.config or {}).get("mode") == "remote"

    def state(key: str) -> str:
        if remote and key == "reprove":
            return "waiting"  # proof of a live-site fix comes from its pull request's preview deployment, later
        if key in finished or (done_run and ok and key in started):
            return "done"
        if key in started:
            return "stopped" if done_run else "current"
        return "done" if (done_run and ok) else "waiting"

    def dur(key: str) -> float | None:
        return _secs(started.get(key), finished.get(key))

    detect_d = f"{'axe-core + 6 probes' if with_probes else 'axe-core'} · {len(findings)} finding{'s' if len(findings) != 1 else ''}"
    stages = [
        ("detect", "Detect", "detect", detect_d),
        ("triage", "Triage", "triage", f"{auto} auto-fix · {len(verdicts) - auto} to a person"),
        ("fix", "Fix", "fix", f"{edits} edit{'s' if edits != 1 else ''} passed safety + build"),
        ("prove", "Prove", "reprove", f"{ok_n} pass · {fail_n} failed · {inc_n} not re-tested"),
        ("regress", "Regress", "reprove", f"{new_n} new findings" + (f" · {rules} rules" if rules else "")),
    ]
    out = []
    for key, title, ev, detail in stages:
        st = state(ev)
        if key == "regress" and st == "current":
            st = "waiting"  # the re-prove stage covers both; only one bar pulses at a time
        secs = dur(ev) if key != "regress" else None
        shown = detail if st == "done" else ""
        if remote and key in ("prove", "regress"):
            shown = "starts when your preview deployment is ready" if key == "prove" else ""
        if shown and secs is not None:
            shown += f" · {_fmt(secs)}"
        out.append(StageOut(key=key, title=title, state=st, detail=shown, seconds=secs))
    return out


# ------------------------------------------------------------------------------------------ pull requests
def _diff_stats(diff: str) -> tuple[str | None, int, int]:
    file = None
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = re.sub(r"^b/", "", line[4:].strip())
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return file, added, removed


def _pr_number(url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


def build_pull_requests(db, run: AuditRun) -> list[PullRequestRow]:
    rows: list[PullRequestRow] = []
    attempts = db.execute(select(FixAttempt, Finding).join(Finding, Finding.id == FixAttempt.finding_id)
                          .where(Finding.run_id == run.id, FixAttempt.diff != "").order_by(FixAttempt.created_at)).all()
    for fa, f in attempts:
        ps = db.get(PageState, f.page_state_id)
        d = db.scalar(select(HumanDecision).where(HumanDecision.finding_id == f.id,
                                                  HumanDecision.action.in_(("accept_pr", "reject_pr")))
                      .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc()))
        status = f.status.value
        if status == "regress_ok":
            proof = "proved"
        elif status in ("regress_fail",):
            proof = "regression"
        elif status in ("prove_failed",):
            proof = "fix_failed"
        else:
            proof = "not_retested"
        publishable = status in (REMOTE_PUBLISHABLE if (run.config or {}).get("mode") == "remote" else PUBLISHABLE)
        decision = ("held_back" if not publishable else "accepted" if d and d.action == "accept_pr"
                    else "rejected" if d and d.action == "reject_pr" else "awaiting")
        file, added, removed = _diff_stats(fa.diff)
        remote = "open" if fa.pr_url else ("server_only" if publishable else "not_publishable")
        rows.append(PullRequestRow(
            finding_id=f.id, rule_id=f.rule_id, sc_ids=f.sc_ids, severity=f.severity.value,
            page=(ps.state_label or urlsplit(ps.url).path) if ps else "", tier=fa.tier_pr, branch=fa.branch, file=file,
            added=added, removed=removed, proof=proof, decision=decision, remote=remote, pr_url=fa.pr_url,
            pr_number=_pr_number(fa.pr_url)))
    return rows


def committed_sha(db, fix_attempt_id: str) -> str | None:
    j = db.scalar(select(FixJournal).where(FixJournal.fix_attempt_id == fix_attempt_id, FixJournal.step == "committed"))
    return ((j.detail or {}).get("sha") if j else None) or None

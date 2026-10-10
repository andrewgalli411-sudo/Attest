import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    FindingStatus,
    FixAttempt,
    FixJournal,
    PageState,
    ReproveResult,
    Severity,
    Tier,
    TriageVerdict,
)


def _seed(db):
    run = AuditRun(target_url="http://x")
    db.add(run)
    db.flush()
    ps = PageState(run_id=run.id, url="http://x/")
    db.add(ps)
    db.flush()
    f = Finding(run_id=run.id, page_state_id=ps.id, rule_id="label", sc_ids=["3.3.2"],
                severity=Severity.critical, source="axe", dedupe_key="k1")
    db.add(f)
    db.flush()
    return run, ps, f


def test_full_evidence_chain_roundtrips(sf):
    with sf() as db:
        run, ps, f = _seed(db)
        db.add(FindingLocator(finding_id=f.id, fingerprint={"tag": "input"}, state_ref={"steps": []}))
        for stage, kind in [("caught", "shot_clip"), ("explained", "impact"), ("plan", "fix_plan"),
                            ("fix", "diff"), ("proof", "reprove_shot_after"), ("regression", "report")]:
            db.add(FindingEvidence(finding_id=f.id, stage=stage, kind=kind, payload={"k": kind}))
        v = TriageVerdict(finding_id=f.id, tier=Tier.auto_fix, reason_code="missing_label", confidence=0.9)
        db.add(v)
        db.flush()
        fa = FixAttempt(finding_id=f.id, verdict_id=v.id, tier_pr="PR1", idempotency_key="idem-1",
                        edits=[{"file": "a.tsx", "anchor_text": "x", "replacement": "y"}])
        db.add(fa)
        db.flush()
        db.add(ReproveResult(finding_id=f.id, fix_attempt_id=fa.id, verdict="pass", relocated=True,
                             relocation_score=0.97, rule_pass=True, baseline_failed=True, runs_n=2))
        f.status = FindingStatus.proved
        db.commit()
    with sf() as db:
        f = db.scalar(select(Finding))
        assert f.status == FindingStatus.proved and f.locator.fingerprint == {"tag": "input"}
        stages = [e.stage for e in db.scalars(select(FindingEvidence).order_by(FindingEvidence.created_at))]
        assert set(stages) == {"caught", "explained", "plan", "fix", "proof", "regression"}


def test_duplicate_finding_in_same_run_is_rejected(sf):
    with sf() as db:
        run, ps, f = _seed(db)
        db.commit()
        db.add(Finding(run_id=run.id, page_state_id=ps.id, rule_id="label", sc_ids=[],
                       severity=Severity.minor, source="axe", dedupe_key="k1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_idempotency_key_and_journal_steps_are_unique(sf):
    with sf() as db:
        run, ps, f = _seed(db)
        v = TriageVerdict(finding_id=f.id, tier=Tier.auto_fix, reason_code="x")
        db.add(v)
        db.flush()
        fa = FixAttempt(finding_id=f.id, verdict_id=v.id, tier_pr="PR1", idempotency_key="same")
        db.add(fa)
        db.flush()
        db.add(FixJournal(fix_attempt_id=fa.id, step="authored"))
        db.commit()
        db.add(FixJournal(fix_attempt_id=fa.id, step="authored"))  # re-running a step must not double-log
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(FixAttempt(finding_id=f.id, verdict_id=v.id, tier_pr="PR1", idempotency_key="same"))
        with pytest.raises(IntegrityError):  # same fix cannot be opened twice
            db.commit()

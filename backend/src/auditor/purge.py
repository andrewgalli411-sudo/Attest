"""Retention: delete finished runs older than N days, with every row and every evidence file they own.

Evidence contains screenshots and HTML of audited (possibly logged-in) pages, so it should not live forever.
  python -m auditor.purge --days 30 [--dry-run]
Only terminal runs are ever deleted. Deletion is one transaction per run: a run is removed completely or not at all.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from .api.routes import TERMINAL
from .models import (
    AuditorRuling,
    AuditRun,
    CriterionRollup,
    EvidenceBundle,
    ExportArtifact,
    Finding,
    FindingEvidence,
    FindingLocator,
    FixAttempt,
    FixJournal,
    HumanDecision,
    JudgeProposal,
    PageScore,
    PageState,
    ProofJob,
    RegressionCheck,
    ReproveResult,
    RunEvent,
    TriageVerdict,
    UserSession,
    WebhookDelivery,
)
from .storage.evidence_store import EvidenceStore


def _ids(db, col, where):
    return list(db.scalars(select(col).where(where)))


def _delete_run(db, run_id: str) -> None:
    """Children before parents; every foreign key is honoured (Postgres enforces them)."""
    pages = _ids(db, PageState.id, PageState.run_id == run_id)
    findings = _ids(db, Finding.id, Finding.run_id == run_id)
    attempts = _ids(db, FixAttempt.id, FixAttempt.finding_id.in_(findings)) if findings else []
    bundles = _ids(db, EvidenceBundle.id, EvidenceBundle.page_state_id.in_(pages)) if pages else []
    if attempts:
        db.execute(delete(RegressionCheck).where(RegressionCheck.fix_attempt_id.in_(attempts)))
        db.execute(delete(ReproveResult).where(ReproveResult.fix_attempt_id.in_(attempts)))
        db.execute(delete(FixJournal).where(FixJournal.fix_attempt_id.in_(attempts)))
    if findings:
        db.execute(delete(HumanDecision).where(HumanDecision.finding_id.in_(findings)))
        db.execute(delete(FixAttempt).where(FixAttempt.finding_id.in_(findings)))
        db.execute(delete(TriageVerdict).where(TriageVerdict.finding_id.in_(findings)))
        db.execute(delete(FindingEvidence).where(FindingEvidence.finding_id.in_(findings)))
        db.execute(delete(FindingLocator).where(FindingLocator.finding_id.in_(findings)))
        db.execute(delete(Finding).where(Finding.id.in_(findings)))
    if bundles:
        db.execute(delete(AuditorRuling).where(AuditorRuling.bundle_id.in_(bundles)))
        db.execute(delete(JudgeProposal).where(JudgeProposal.bundle_id.in_(bundles)))
        db.execute(delete(EvidenceBundle).where(EvidenceBundle.id.in_(bundles)))
    db.execute(delete(ProofJob).where(ProofJob.run_id == run_id))
    db.execute(delete(PageScore).where(PageScore.run_id == run_id))
    db.execute(delete(CriterionRollup).where(CriterionRollup.run_id == run_id))
    db.execute(delete(ExportArtifact).where(ExportArtifact.run_id == run_id))
    db.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
    db.execute(delete(PageState).where(PageState.run_id == run_id))
    db.execute(delete(AuditRun).where(AuditRun.id == run_id))


def purge_runs(sf: sessionmaker, store: EvidenceStore, older_than: timedelta, *, dry_run: bool = False,
               now: datetime | None = None) -> list[str]:
    cutoff = (now or datetime.now(UTC)) - older_than
    with sf() as db:
        old = list(db.scalars(select(AuditRun).where(AuditRun.created_at < cutoff)))
        victims = [r.id for r in old if r.status in TERMINAL]
    if dry_run:
        return victims
    done: list[str] = []
    for rid in victims:
        with sf() as db, db.begin():
            _delete_run(db, rid)
        d = (store.root / rid).resolve()
        if store.root in d.parents:  # never delete anything outside the evidence root
            shutil.rmtree(d, ignore_errors=True)
        done.append(rid)
    return done


def purge_housekeeping(sf: sessionmaker, *, now: datetime | None = None, deliveries_days: int = 30) -> dict[str, int]:
    """Things that only ever pile up: expired sign-in sessions, and the ids of webhook deliveries we have already handled
    (kept long enough to recognise a redelivery, not forever)."""
    now = now or datetime.now(UTC)
    with sf() as db, db.begin():
        sessions = db.execute(delete(UserSession).where(UserSession.expires_at < now)).rowcount
        deliveries = db.execute(delete(WebhookDelivery).where(WebhookDelivery.received_at < now - timedelta(days=deliveries_days))).rowcount
    return {"expired_sessions": sessions, "old_webhook_deliveries": deliveries}


def main() -> None:
    from .config import settings
    from .db import SessionLocal

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, required=True, help="delete finished runs older than this many days")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.days < 1:
        ap.error("--days must be at least 1")
    ids = purge_runs(SessionLocal, EvidenceStore(settings.evidence_dir), timedelta(days=a.days), dry_run=a.dry_run)
    print(f"{'would delete' if a.dry_run else 'deleted'} {len(ids)} run(s)")
    if not a.dry_run:
        print("housekeeping:", purge_housekeeping(SessionLocal))


if __name__ == "__main__":
    main()

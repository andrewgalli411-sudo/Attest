"""Idempotent, resumable fix pipeline bookkeeping (fix_attempt + fix_journal)."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..models import FixAttempt, FixJournal

STEPS = ["authored", "applied", "verified", "committed", "pushed", "pr_opened", "build_ready",
         "reproved", "regressed"]


def idempotency_key(dedupe_key: str, rule_id: str, base_sha: str, prompt_version: str) -> str:
    return hashlib.sha256(f"{dedupe_key}|{rule_id}|{base_sha}|{prompt_version}".encode()).hexdigest()


def get_or_create_attempt(sf: sessionmaker, *, finding_id: str, verdict_id: str, tier_pr: str, repo: str,
                          base_sha: str, key: str) -> tuple[str, bool]:
    """Returns (attempt_id, created). A second call with the same key returns the same attempt."""
    with sf() as db:
        found = db.scalar(select(FixAttempt).where(FixAttempt.idempotency_key == key))
        if found:
            return found.id, False
        try:
            fa = FixAttempt(finding_id=finding_id, verdict_id=verdict_id, tier_pr=tier_pr, repo=repo,
                            base_sha=base_sha, idempotency_key=key)
            db.add(fa)
            db.commit()
            return fa.id, True
        except IntegrityError:  # lost a race with a concurrent creator
            db.rollback()
            return db.scalar(select(FixAttempt.id).where(FixAttempt.idempotency_key == key)), False


class Journal:
    def __init__(self, sf: sessionmaker, attempt_id: str):
        self.sf, self.attempt_id = sf, attempt_id

    def done(self, step: str) -> bool:
        with self.sf() as db:
            row = db.scalar(select(FixJournal).where(FixJournal.fix_attempt_id == self.attempt_id,
                                                     FixJournal.step == step))
            return row is not None and row.outcome == "ok"

    def record(self, step: str, outcome: str = "ok", detail: dict | None = None) -> None:
        with self.sf() as db:
            row = db.scalar(select(FixJournal).where(FixJournal.fix_attempt_id == self.attempt_id,
                                                     FixJournal.step == step))
            if row is None:
                db.add(FixJournal(fix_attempt_id=self.attempt_id, step=step, outcome=outcome, detail=detail))
            else:  # a retried step overwrites its previous outcome (still one row per step)
                row.outcome, row.detail = outcome, detail
            db.commit()

    def completed(self) -> list[str]:
        with self.sf() as db:
            rows = db.scalars(select(FixJournal).where(FixJournal.fix_attempt_id == self.attempt_id,
                                                       FixJournal.outcome == "ok")).all()
        have = {r.step for r in rows}
        return [s for s in STEPS if s in have]

    def next_step(self) -> str | None:
        have = set(self.completed())
        return next((s for s in STEPS if s not in have), None)

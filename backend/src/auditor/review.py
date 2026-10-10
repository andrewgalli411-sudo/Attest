"""Auditor rulings (append-only labeled data), run-level rollup, and dataset export."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AuditorRuling,
    Criterion,
    CriterionRollup,
    EvidenceBundle,
    JudgeProposal,
    Level,
    PageState,
    RulingAction,
)

_SEVERITY = [Level.does_not_support, Level.partially_supports, Level.supports, Level.not_applicable]


def latest_proposal(db: Session, bundle_id: str) -> JudgeProposal | None:
    return db.scalar(select(JudgeProposal).where(JudgeProposal.bundle_id == bundle_id)
                     .order_by(JudgeProposal.created_at.desc(), JudgeProposal.id.desc()))


def latest_ruling(db: Session, bundle_id: str) -> AuditorRuling | None:
    return db.scalar(select(AuditorRuling).where(AuditorRuling.bundle_id == bundle_id)
                     .order_by(AuditorRuling.created_at.desc(), AuditorRuling.id.desc()))


def record_ruling(
    db: Session, bundle_id: str, action: RulingAction, *, final_level: Level | None = None,
    final_remark: str | None = None, rationale: str | None = None, reviewer: str = "local",
    time_spent_ms: int | None = None,
) -> AuditorRuling:
    """Stores the ruling against the exact proposal the auditor saw."""
    prop = latest_proposal(db, bundle_id)
    if action == RulingAction.accept:
        if prop is None:
            raise ValueError("cannot accept: no judge proposal for this bundle")
        final_level, final_remark = prop.proposed_level, prop.remark
    elif final_level is None:
        raise ValueError(f"{action.value} requires final_level")
    r = AuditorRuling(
        bundle_id=bundle_id, proposal_id=prop.id if prop else None, action=action,
        final_level=final_level, final_remark=final_remark if final_remark is not None else (prop.remark if prop else ""),
        rationale=rationale, reviewer=reviewer, time_spent_ms=time_spent_ms,
    )
    db.add(r)
    db.commit()
    return r


def compute_rollup(db: Session, run_id: str) -> list[CriterionRollup]:
    """Worst-case across reviewed pages; unreviewed bundles are counted and block 'confirmed'."""
    bundles = db.execute(
        select(EvidenceBundle).join(PageState).where(PageState.run_id == run_id)
    ).scalars().all()
    per_sc: dict[str, list[tuple[Level | None, str]]] = {}
    for b in bundles:
        r = latest_ruling(db, b.id)
        per_sc.setdefault(b.criterion_id, []).append((r.final_level if r else None, r.final_remark if r else ""))

    existing = {x.criterion_id: x for x in db.scalars(select(CriterionRollup).where(CriterionRollup.run_id == run_id))}
    out = []
    for sc in db.scalars(select(Criterion.sc_id).order_by(Criterion.vpat_row_order)):
        rows = per_sc.get(sc, [])
        reviewed = [lv for lv, _ in rows if lv is not None]
        row = existing.get(sc) or CriterionRollup(run_id=run_id, criterion_id=sc)
        if row.overridden_by_auditor:  # auditor's manual rollup wins
            row.unreviewed_count = len(rows) - len(reviewed)
        else:
            row.unreviewed_count = len(rows) - len(reviewed)
            levels = set(reviewed)
            if not reviewed or Level.not_evaluated in levels:
                row.level = Level.not_evaluated
            elif Level.does_not_support in levels and (levels - {Level.does_not_support}):
                row.level = Level.partially_supports
            else:
                row.level = next(lv for lv in _SEVERITY if lv in levels)
            row.remark = " | ".join(sorted({m for lv, m in rows if lv is not None and m}))[:2000]
        db.merge(row)
        out.append(row)
    db.commit()
    return out


def dataset_rows(db: Session, run_id: str | None = None):
    """Labeled judge-training rows: proposal vs final ruling, with the evidence it saw."""
    q = select(AuditorRuling, EvidenceBundle, JudgeProposal).join(
        EvidenceBundle, EvidenceBundle.id == AuditorRuling.bundle_id).outerjoin(
        JudgeProposal, JudgeProposal.id == AuditorRuling.proposal_id)
    if run_id:
        q = q.join(PageState, PageState.id == EvidenceBundle.page_state_id).where(PageState.run_id == run_id)
    for ruling, bundle, prop in db.execute(q.order_by(AuditorRuling.created_at)):
        yield {
            "bundle_id": bundle.id, "criterion": bundle.criterion_id,
            "evidence": {"axe": bundle.axe_result, "probes": bundle.probe_transcripts,
                         "screenshot_ref": bundle.screenshot_ref},
            "proposal": None if prop is None else {
                "level": prop.proposed_level.value, "remark": prop.remark, "confidence": prop.confidence,
                "cited": prop.evidence_cited, "model": prop.model_id, "prompt_version": prop.prompt_version},
            "ruling": {"action": ruling.action.value, "level": ruling.final_level.value,
                       "remark": ruling.final_remark, "rationale": ruling.rationale},
            "agreed": prop is not None and prop.proposed_level == ruling.final_level,
        }

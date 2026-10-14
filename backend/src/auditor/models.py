from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Level(str, enum.Enum):
    supports = "supports"
    partially_supports = "partially_supports"
    does_not_support = "does_not_support"
    not_applicable = "not_applicable"
    not_evaluated = "not_evaluated"


class RunStatus(str, enum.Enum):
    queued = "queued"
    crawling = "crawling"
    probing = "probing"
    judging = "judging"
    detecting = "detecting"
    triaging = "triaging"
    fixing = "fixing"
    reproving = "reproving"
    complete = "complete"
    cancelled = "cancelled"
    awaiting_review = "awaiting_review"
    reviewed = "reviewed"
    exported = "exported"
    failed = "failed"


class RulingAction(str, enum.Enum):
    accept = "accept"
    edit = "edit"
    reject = "reject"


class Criterion(Base):
    __tablename__ = "criterion"
    sc_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    level: Mapped[str] = mapped_column(String(3))  # A | AA
    wcag_version: Mapped[str] = mapped_column(String(8))  # first introduced: 2.0 | 2.1
    principle: Mapped[str] = mapped_column(String(32))
    vpat_row_order: Mapped[int] = mapped_column(Integer)


class Account(Base):
    """A person who signed in with GitHub. Everything they create is owned by this row."""

    __tablename__ = "account"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    github_id: Mapped[int] = mapped_column(Integer, unique=True)
    login: Mapped[str] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Base):
    """Server-side session. Only the SHA-256 of the cookie token is stored, so a database leak cannot be replayed."""

    __tablename__ = "user_session"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    # The user's GitHub token (for listing the repositories they can reach), encrypted; dies with the session.
    sealed_github_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectRecord(Base):
    """A site a person attached: a GitHub repository (read and changed through the Attest GitHub App, never cloned and
    run on our servers) plus the live address Attest audits in a browser."""

    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    github_repo: Mapped[str] = mapped_column(String(200))  # "owner/name"
    installation_id: Mapped[int] = mapped_column(Integer)
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    site_url: Mapped[str] = mapped_column(Text)  # normalised origin (+ optional base path), https
    preview_url_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages: Mapped[list] = mapped_column(JSON, default=list)  # [{"path": "/", "label": "home"}]
    sealed_auth: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted {"cookies": [...]}; never returned
    allow_pr: Mapped[bool] = mapped_column(default=False)  # may Attest open pull requests on this repository?
    verification_token: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WebhookDelivery(Base):
    """GitHub's delivery id for every webhook we accepted, so a replayed or redelivered event is ignored."""

    __tablename__ = "webhook_delivery"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event: Mapped[str] = mapped_column(String(50))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProofJob(Base):
    """Re-auditing one pull request's preview deployment. Unique per (run, branch, deployed commit), so a repeated event is one job."""

    __tablename__ = "proof_job"
    __table_args__ = (UniqueConstraint("run_id", "branch", "deployment_sha"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    branch: Mapped[str] = mapped_column(String(200))
    deployment_sha: Mapped[str] = mapped_column(String(64))
    preview_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | skipped | failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditRun(Base):
    __tablename__ = "audit_run"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # None only in local mode (one implicit owner). In accounts mode every run has an owner.
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("account.id"), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    auth_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    flows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # remediation settings (no secrets)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.queued)
    tool_versions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stamped every few seconds by whichever process owns the run; a stale stamp means the owner died.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    page_states: Mapped[list[PageState]] = relationship(back_populates="run")


class PageState(Base):
    __tablename__ = "page_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    flow_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    dom_snapshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    ax_snapshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="discovered")
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    run: Mapped[AuditRun] = relationship(back_populates="page_states")


class EvidenceBundle(Base):
    __tablename__ = "evidence_bundle"
    __table_args__ = (UniqueConstraint("page_state_id", "criterion_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    page_state_id: Mapped[str] = mapped_column(ForeignKey("page_state.id"), index=True)
    criterion_id: Mapped[str] = mapped_column(ForeignKey("criterion.sc_id"), index=True)
    axe_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    probe_transcripts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    screenshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    dom_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    ax_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    selectors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class JudgeProposal(Base):
    """Immutable; re-judging appends a new row."""

    __tablename__ = "judge_proposal"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("evidence_bundle.id"), index=True)
    proposed_level: Mapped[Level] = mapped_column(Enum(Level))
    remark: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_cited: Mapped[list] = mapped_column(JSON, default=list)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_human: Mapped[bool] = mapped_column(default=True)
    valid: Mapped[bool] = mapped_column(default=True)
    model_id: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_response_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditorRuling(Base):
    """Append-only; latest per bundle is current. Labeled data for future judge training."""

    __tablename__ = "auditor_ruling"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("evidence_bundle.id"), index=True)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("judge_proposal.id"), nullable=True)
    action: Mapped[RulingAction] = mapped_column(Enum(RulingAction))
    final_level: Mapped[Level] = mapped_column(Enum(Level))
    final_remark: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[str] = mapped_column(String(100), default="local")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_spent_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CriterionRollup(Base):
    __tablename__ = "criterion_rollup"
    __table_args__ = (UniqueConstraint("run_id", "criterion_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    criterion_id: Mapped[str] = mapped_column(ForeignKey("criterion.sc_id"))
    level: Mapped[Level] = mapped_column(Enum(Level), default=Level.not_evaluated)
    remark: Mapped[str] = mapped_column(Text, default="")
    overridden_by_auditor: Mapped[bool] = mapped_column(default=False)
    unreviewed_count: Mapped[int] = mapped_column(Integer, default=0)


class RunEvent(Base):
    __tablename__ = "run_event"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExportArtifact(Base):
    __tablename__ = "export_artifact"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    format: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    rollup_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------------------------
# Remediation loop: the Finding is the unit that accumulates evidence from detect to regression.
# ---------------------------------------------------------------------------------------------


class Severity(str, enum.Enum):
    critical = "critical"
    serious = "serious"
    moderate = "moderate"
    minor = "minor"


class FindingStatus(str, enum.Enum):
    caught = "caught"
    triaged = "triaged"
    needs_human = "needs_human"  # terminal for the auto-fix path
    fix_authored = "fix_authored"
    pr_open = "pr_open"
    proved = "proved"
    prove_failed = "prove_failed"
    inconclusive = "inconclusive"  # e.g. element could not be relocated: never counted as a pass
    regress_ok = "regress_ok"
    regress_fail = "regress_fail"


class Tier(str, enum.Enum):
    auto_fix = "auto_fix"
    needs_human = "needs_human"


class Finding(Base):
    __tablename__ = "finding"
    __table_args__ = (UniqueConstraint("run_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    page_state_id: Mapped[str] = mapped_column(ForeignKey("page_state.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(96), index=True)  # axe id or probe:<name>:<kind>
    sc_ids: Mapped[list] = mapped_column(JSON, default=list)
    severity: Mapped[Severity] = mapped_column(Enum(Severity))
    source: Mapped[str] = mapped_column(String(8))  # axe | probe
    status: Mapped[FindingStatus] = mapped_column(Enum(FindingStatus), default=FindingStatus.caught)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    baseline_stable: Mapped[bool] = mapped_column(default=False)
    impact_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    locator: Mapped[FindingLocator] = relationship(back_populates="finding", uselist=False)


class FindingLocator(Base):
    """Immutable fingerprint captured when the Finding is caught; used to re-find the element."""

    __tablename__ = "finding_locator"
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), primary_key=True)
    fingerprint: Mapped[dict] = mapped_column(JSON)  # findings.locator.build() output
    axe_target: Mapped[list | None] = mapped_column(JSON, nullable=True)
    state_ref: Mapped[dict] = mapped_column(JSON, default=dict)  # flow name, steps, step_index
    finding: Mapped[Finding] = relationship(back_populates="locator")


class FindingEvidence(Base):
    """Append-only; the timeline UI scrolls these in order."""

    __tablename__ = "finding_evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), index=True)
    stage: Mapped[str] = mapped_column(String(16))  # caught|explained|plan|fix|proof|regression
    kind: Mapped[str] = mapped_column(String(32))
    blob_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TriageVerdict(Base):
    __tablename__ = "triage_verdict"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), index=True)
    tier: Mapped[Tier] = mapped_column(Enum(Tier))
    reason_code: Mapped[str] = mapped_column(String(48))
    pr_tier: Mapped[str | None] = mapped_column(String(8), nullable=True)  # PR1 | PR2 | None
    fix_plan: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_cited: Mapped[list] = mapped_column(JSON, default=list)
    gate_notes: Mapped[list] = mapped_column(JSON, default=list)
    valid: Mapped[bool] = mapped_column(default=True)
    model_id: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    input_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class HumanDecision(Base):
    """Auditor/reviewer outcome against a triage verdict or PR: the future training labels."""

    __tablename__ = "human_decision"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), index=True)
    verdict_id: Mapped[str | None] = mapped_column(ForeignKey("triage_verdict.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(24))  # accept_pr|reject_pr|override_tier|edit_fix
    reviewer: Mapped[str] = mapped_column(String(100), default="local")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FixAttempt(Base):
    __tablename__ = "fix_attempt"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), index=True)
    verdict_id: Mapped[str] = mapped_column(ForeignKey("triage_verdict.id"))
    tier_pr: Mapped[str] = mapped_column(String(8))  # PR1 | PR2 | issue
    repo: Mapped[str] = mapped_column(Text, default="")
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    branch: Mapped[str] = mapped_column(Text, default="")
    edits: Mapped[list] = mapped_column(JSON, default=list)  # {file, anchor_text, replacement}
    expected_dom_changes: Mapped[list] = mapped_column(JSON, default=list)  # attrs the fix adds
    diff: Mapped[str] = mapped_column(Text, default="")
    verify_log: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="authored")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FixJournal(Base):
    """Step log that makes the fix pipeline resumable and idempotent."""

    __tablename__ = "fix_journal"
    __table_args__ = (UniqueConstraint("fix_attempt_id", "step"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fix_attempt_id: Mapped[str] = mapped_column(ForeignKey("fix_attempt.id"), index=True)
    step: Mapped[str] = mapped_column(String(24))
    outcome: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReproveResult(Base):
    __tablename__ = "reprove_result"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"), index=True)
    fix_attempt_id: Mapped[str] = mapped_column(ForeignKey("fix_attempt.id"), index=True)
    build_url: Mapped[str] = mapped_column(Text, default="")
    relocated: Mapped[bool] = mapped_column(default=False)
    relocation_score: Mapped[float] = mapped_column(Float, default=0.0)
    relocation_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    baseline_failed: Mapped[bool] = mapped_column(default=False)  # control: unpatched still fails
    rule_pass: Mapped[bool] = mapped_column(default=False)
    runs_n: Mapped[int] = mapped_column(Integer, default=0)
    before_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))  # pass | fail | inconclusive
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RegressionCheck(Base):
    __tablename__ = "regression_check"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    fix_attempt_id: Mapped[str] = mapped_column(ForeignKey("fix_attempt.id"), index=True)
    page_state_id: Mapped[str] = mapped_column(ForeignKey("page_state.id"))
    criteria_checked: Mapped[list] = mapped_column(JSON, default=list)
    new_findings: Mapped[list] = mapped_column(JSON, default=list)
    regressions_count: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PageScore(Base):
    __tablename__ = "page_score"
    __table_args__ = (UniqueConstraint("page_state_id", "phase"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id"), index=True)
    page_state_id: Mapped[str] = mapped_column(ForeignKey("page_state.id"))
    phase: Mapped[str] = mapped_column(String(8))  # baseline | after
    score: Mapped[int] = mapped_column(Integer)
    breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)

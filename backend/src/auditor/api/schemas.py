from __future__ import annotations

from pydantic import BaseModel, Field


class StartRun(BaseModel):
    """What a caller may ask for. Commands and repo paths come from server-side projects, not from here."""

    project: str | None = Field(None, description="name of an operator-defined project")
    demo: bool = False
    pages: list[dict] | None = Field(None, description="override the project's pages: [{path, label}]")
    only_rules: list[str] | None = None
    max_fixes: int | None = Field(None, ge=1, le=100)
    with_probes: bool = False
    publish: bool = Field(False, description="push branches and open PRs/issues; honoured only if the project allows it")
    # Only honoured when the server runs with ALLOW_CUSTOM_RUNS=1 (development):
    repo_path: str | None = None
    dev_command: str = "pnpm dev --port {port}"
    check_commands: list[str] = Field(default_factory=list)
    auth_cookies: list[dict] = Field(default_factory=list)
    base_branch: str = "main"


class ProjectInfo(BaseModel):
    name: str
    pages: list[dict]
    can_publish: bool


class ConfigOut(BaseModel):
    demo_enabled: bool
    allow_custom_runs: bool
    projects: list[ProjectInfo]
    auth_required: bool


class RunOut(BaseModel):
    id: str
    status: str
    target: str
    created_at: str
    headline: str | None = None


class FindingRow(BaseModel):
    id: str
    rule_id: str
    sc_ids: list[str]
    severity: str
    status: str
    page: str
    impact_text: str | None = None
    tier: str | None = None


class Group(BaseModel):
    sc: str
    title: str
    total: int
    fixed: int
    needs_human: int
    findings: list[FindingRow]


class PageOut(BaseModel):
    id: str
    label: str
    path: str
    score_before: int | None = None
    score_after: int | None = None


class Counts(BaseModel):
    total: int
    fixed: int
    needs_human: int
    in_progress: int
    inconclusive: int
    regressions: int


class Dashboard(BaseModel):
    run: RunOut
    headline: str
    counts: Counts
    severity: dict[str, int]
    score_before: ScoreOut | None = None
    score_after: ScoreOut | None = None
    groups: list[Group]
    pages: list[PageOut]
    notice: str
    model_kind: str | None = None


class EvidenceOut(BaseModel):
    id: str
    stage: str
    kind: str
    url: str | None = None
    payload: dict | None = None


class ScoreOut(BaseModel):
    score: int
    worst: int


class TriageOut(BaseModel):
    tier: str
    reason: str
    pr_tier: str | None = None
    fix_plan: str
    confidence: float
    valid: bool
    gate_notes: list[str]


class FixOut(BaseModel):
    diff: str
    branch: str
    tier: str
    pr_url: str | None = None
    explanation: str


class ProofOut(BaseModel):
    verdict: str
    notes: str | None = None
    baseline_failed: bool
    relocated: bool
    relocation_score: float
    relocation_method: str | None = None
    runs: int


class RegressionOut(BaseModel):
    verdict: str
    regressions: int
    rules_checked: int


class FindingDetail(BaseModel):
    id: str
    run_id: str
    rule_id: str
    sc_ids: list[str]
    severity: str
    status: str
    page: str
    impact_text: str | None
    triage: TriageOut | None
    fix: FixOut | None
    proof: ProofOut | None
    regression: RegressionOut | None
    evidence: list[EvidenceOut]
    notice: str


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(accept_pr|reject_pr|override_tier|edit_fix)$")
    note: str | None = None
    reviewer: str = "local"


class DecisionOut(BaseModel):
    id: str
    finding_id: str
    action: str

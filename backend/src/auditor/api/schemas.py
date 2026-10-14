from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/?#\[\]-]{0,499}$")  # one leading '/', no scheme, no spaces/controls


class PageSpec(BaseModel):
    """A page to audit, as a path on the audited app. Never a URL: the host always comes from the server."""

    path: str = Field(max_length=500)
    label: str | None = Field(None, max_length=100)

    @field_validator("path")
    @classmethod
    def _path_only(cls, v: str) -> str:
        if not _PATH.match(v) or "\\" in v or ".." in v.split("?")[0].split("/"):
            raise ValueError("path must start with a single '/', contain no scheme, host, backslash or '..'")
        return v

    @field_validator("label")
    @classmethod
    def _printable(cls, v: str | None) -> str | None:
        if v is not None and not v.isprintable():
            raise ValueError("label must be printable text")
        return v


class StartRun(BaseModel):
    """What a caller may ask for. Commands and repo paths come from server-side projects, not from here."""

    project: str | None = Field(None, description="name of an operator-defined project")
    demo: bool = False
    pages: list[PageSpec] | None = Field(None, max_length=25, description="override the project's pages: [{path, label}]")
    only_rules: list[str] | None = Field(None, max_length=50)
    max_fixes: int | None = Field(None, ge=1, le=100)
    with_probes: bool = False
    publish: bool = Field(False, description="push branches and open PRs/issues; honoured only if the project allows it")
    # Only honoured when the server runs with ALLOW_CUSTOM_RUNS=1 (development):
    repo_path: str | None = None
    dev_command: str = "pnpm dev --port {port}"
    check_commands: list[str] = Field(default_factory=list)
    auth_cookies: list[dict] = Field(default_factory=list)
    base_branch: str = "main"


class StatusOut(BaseModel):
    active_runs: int
    event_subscribers: int
    max_concurrent_runs: int


class ErrorOut(BaseModel):
    """Shape of every non-validation error: {"detail": "human-readable reason"}."""

    detail: str


class ProjectInfo(BaseModel):
    name: str
    pages: list[dict]
    can_publish: bool
    base_branch: str = "main"
    # Set for projects stored in the database (accounts mode); absent for operator projects.
    id: str | None = None
    site_url: str | None = None
    github_repo: str | None = None
    verified: bool | None = None


class SettingsOut(BaseModel):
    """Read-only view of how the operator configured this server. Never includes secrets, paths or commands."""

    triage_model: str
    fix_model: str
    model_token_budget: int
    refusal_fallback: bool
    send_screenshots_to_model: bool
    max_concurrent_runs: int
    run_timeout_s: int
    max_findings_per_page: int
    model_configured: bool


class ConfigOut(BaseModel):
    demo_enabled: bool
    allow_custom_runs: bool
    projects: list[ProjectInfo]
    auth_required: bool
    settings: SettingsOut


class RunOut(BaseModel):
    id: str
    status: str
    target: str
    created_at: str
    headline: str | None = None
    # Filled in from the stored run summary; absent while a run is still working or if it never got that far.
    project: str | None = None
    base_branch: str | None = None
    page_count: int | None = None
    score_before: int | None = None
    score_after: int | None = None
    findings: int | None = None
    fixed: int | None = None
    needs_human: int | None = None
    result: str | None = Field(None, description="one line: the headline, or why the run failed or was cancelled")


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
    awaiting_proof: int = 0  # a fix exists but has not been re-tested on a build yet (live-site runs)
    needs_human: int
    in_progress: int
    inconclusive: int
    regressions: int


class StageOut(BaseModel):
    key: str = Field(description="detect | triage | fix | prove | regress")
    title: str
    state: str = Field(description="done | current | waiting | stopped")
    detail: str
    seconds: float | None = None


class RunMeta(BaseModel):
    mode: str = "local"  # local: fixes proved on a build we ran | remote: a live site, proof waits for a preview deployment
    project: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    started_at: str
    duration_s: float | None = None
    page_count: int
    publish: bool = False
    model_tokens_used: int | None = None
    model_token_budget: int | None = None


class Dashboard(BaseModel):
    run: RunOut
    meta: RunMeta
    stages: list[StageOut]
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
    selector: str | None = None
    source: str | None = Field(None, description="file:line in the audited repo, when the element maps to exactly one place")
    sc_titles: dict[str, str] = Field(default_factory=dict)
    decision: str | None = Field(None, description="accept_pr | reject_pr: the latest decision a person recorded")
    instances: int = 1
    mode: str = "local"  # remote: a live-site run, so a fix was proposed from source but never built or run here


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(accept_pr|reject_pr|override_tier|edit_fix)$")
    note: str | None = None
    reviewer: str = "local"


class DecisionOut(BaseModel):
    id: str
    finding_id: str
    action: str


class PullRequestRow(BaseModel):
    finding_id: str
    rule_id: str
    sc_ids: list[str]
    severity: str
    page: str
    tier: str | None = None
    branch: str
    file: str | None = None
    added: int = 0
    removed: int = 0
    proof: str = Field(description="proved | not_retested | fix_failed | regression")
    decision: str = Field(description="awaiting | accepted | rejected | held_back")
    remote: str = Field(description="open | server_only | not_publishable")
    pr_url: str | None = None
    pr_number: int | None = None


class PullRequestsOut(BaseModel):
    run_id: str
    project: str | None = None
    can_publish: bool = Field(description="the project allows publishing and this run is finished")
    publishable: int = Field(description="accepted, proved fixes that are not on the remote yet")
    published: int
    rows: list[PullRequestRow]


class PublishedPr(BaseModel):
    tier: str
    branch: str
    url: str
    fixes: int


class PublishOut(BaseModel):
    opened: list[PublishedPr]
    errors: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------ projects (accounts mode)
class LoginCookie(BaseModel):
    """A cookie that signs Attest in to the site under test (a dev or test account, never a real person's)."""

    name: str = Field(pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$")
    value: str = Field(max_length=4096)

    @field_validator("value")
    @classmethod
    def _no_controls(cls, v: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in v) or ";" in v:
            raise ValueError("a cookie value cannot contain control characters or ';'")
        return v


class ProjectIn(BaseModel):
    github_repo: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$", description="owner/name")
    installation_id: int = Field(ge=1)
    site_url: str = Field(max_length=300)
    name: str | None = Field(None, min_length=1, max_length=60)
    pages: list[PageSpec] | None = Field(None, max_length=25)
    preview_url_template: str | None = Field(None, max_length=300)
    login_cookies: list[LoginCookie] | None = Field(None, max_length=5)
    allow_pr: bool = False


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    site_url: str | None = Field(None, max_length=300)
    pages: list[PageSpec] | None = Field(None, max_length=25)
    preview_url_template: str | None = Field(None, max_length=300)
    login_cookies: list[LoginCookie] | None = Field(None, max_length=5, description="[] removes the login; omit to keep it")
    allow_pr: bool | None = None


class VerificationHelp(BaseModel):
    file_path: str
    file_contents: str
    dns_name: str
    dns_value: str


class ProjectOut(BaseModel):
    id: str
    name: str
    github_repo: str
    default_branch: str
    site_url: str
    preview_url_template: str | None = None
    pages: list[dict]
    has_login: bool
    login_cookie_names: list[str] = Field(default_factory=list, description="names only; values are never returned")
    allow_pr: bool
    verified: bool
    verified_at: str | None = None
    verification: VerificationHelp
    created_at: str


class RepoOut(BaseModel):
    installation_id: int
    full_name: str
    default_branch: str
    private: bool
    homepage: str | None = None
    attached: bool = False


class ReposOut(BaseModel):
    install_url: str | None = None
    repos: list[RepoOut]


class VerifyCheck(BaseModel):
    method: str = Field(description="file | dns")
    ok: bool
    detail: str


class VerifyOut(BaseModel):
    verified: bool
    method: str | None = Field(None, description="which proof worked: file | dns | already")
    checks: list[VerifyCheck] = Field(default_factory=list)

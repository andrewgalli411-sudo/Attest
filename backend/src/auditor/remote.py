"""Audit a LIVE site and propose fixes to its GitHub repository, without ever building or running the customer's code.

    detect    drive the deployed site in a browser (behind the network guard) and find issues
    triage    decide what can be fixed automatically and what needs a person
    fix       read the source (a tarball unpacked into a scratch git repo that is never executed), write safe edits
    (stop)    fixes wait as proposals; proof comes later, from the pull request's own preview deployment

This reuses the detect/triage/fix code that local mode uses, unchanged: the scratch repository is simply a place where
fix-authoring can make commits. It is deleted when the run ends, so customer source never stays on our disk. What is
different from local mode, and said plainly everywhere it shows: nothing is built here, so a fix is not proved until the
pull request's preview deployment can be re-audited.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import select

from .budget import BudgetedClient
from .models import AuditRun, Finding, FixAttempt, RunStatus
from .pipeline.safe_browse import BlockedByGuard, launch_browser
from .remediate import (
    RemediationConfig,
    _author_fixes,
    _detect_page,
    _map_sources,
    _record_page_failure,
    _status,
    _summarise,
    record_tokens,
)
from .scm.github_repo import GitHubRepoClient
from .scm.local_git import LocalGitProvider
from .scm.workspace import extract, init_scratch_repo
from .triage.triage import run_triage


@dataclass
class RemoteConfig:
    project_id: str
    repo: str  # owner/name
    installation_id: int
    base_branch: str  # the repository's real default branch
    site_url: str  # origin (+ optional base path) of the live site
    pages: list[dict]
    scratch_root: Path
    auth: dict | None = None  # login cookies, decrypted only for the duration of the run
    with_probes: bool = False
    triage_model: str = "claude-opus-5-5"
    fix_model: str = "claude-opus-5-5"
    send_screenshots_to_model: bool = False
    model_token_budget: int = 1_500_000
    max_findings_per_page: int = 200
    refusal_fallback: bool = True
    only_rules: list[str] | None = None
    max_fixes: int | None = None
    notes: list[str] = field(default_factory=list)

    def public(self, base_sha: str) -> dict:
        """What is stored on the run. No secrets, and no server paths."""
        return {"mode": "remote", "repo": self.repo, "base_branch": self.base_branch, "base_sha": base_sha, "site_url": self.site_url,
                "pages": self.pages, "with_probes": self.with_probes, "only_rules": self.only_rules, "max_fixes": self.max_fixes,
                "installation_id": self.installation_id, "auth_keys": sorted((self.auth or {}).keys())}


async def run_remote_audit(run_id: str, sf, store, bus, client, rc: RemoteConfig, github: GitHubRepoClient) -> dict:
    client = BudgetedClient(client, rc.model_token_budget, refusal_fallback=rc.refusal_fallback)
    workdir = rc.scratch_root / run_id
    try:
        _status(sf, run_id, RunStatus.detecting)
        bus.emit(run_id, "stage.started", {"stage": "detect"})
        sha = await github.head_sha(rc.base_branch)
        repo = workdir / "repo"
        stats = await asyncio.to_thread(extract, await github.archive(sha), repo)
        scratch_sha = await asyncio.to_thread(init_scratch_repo, repo, label=f"{rc.repo}@{sha[:10]}")
        bus.emit(run_id, "log", {"msg": f"read {stats['files']} source files from {rc.repo}@{sha[:7]}"})
        with sf() as db:
            db.get(AuditRun, run_id).config = rc.public(sha)
            db.commit()
        # The scratch repo's own branch is always "main"; the real branch name only matters when we publish.
        cfg = RemediationConfig(repo_path=repo, dev_command="(not run)", check_commands=[], pages=rc.pages, auth=rc.auth,
                                base_branch="main", allow_push=False, triage_model=rc.triage_model, fix_model=rc.fix_model,
                                with_probes=rc.with_probes, send_screenshots_to_model=rc.send_screenshots_to_model,
                                model_token_budget=rc.model_token_budget, max_findings_per_page=rc.max_findings_per_page,
                                refusal_fallback=rc.refusal_fallback, only_rules=rc.only_rules, max_fixes=rc.max_fixes)
        provider = LocalGitProvider(repo, allow_push=False)

        pages: list[dict] = []
        async with async_playwright() as pw:
            browser = await launch_browser(pw)
            try:
                for pc in rc.pages:
                    try:
                        pages.append(await _detect_page(sf, store, bus, browser, run_id, rc.site_url, pc, cfg))
                    except BlockedByGuard:
                        raise  # a page that reaches for a private address ends the run; it is not just a bad page
                    except Exception as e:  # noqa: BLE001 - isolate the failure to this page and carry on
                        _record_page_failure(sf, bus, run_id, rc.site_url, pc, e)
                if not pages:
                    raise RuntimeError("no page could be audited; see the page errors for each reason")
                await _map_sources(sf, store, browser, repo, run_id, pages, cfg)
            finally:
                await browser.close()
        bus.emit(run_id, "stage.finished", {"stage": "detect"})

        _status(sf, run_id, RunStatus.triaging)
        bus.emit(run_id, "stage.started", {"stage": "triage"})
        await run_triage(sf, store, client, rc.triage_model, run_id, rc.send_screenshots_to_model)
        with sf() as db:
            for f in db.scalars(select(Finding).where(Finding.run_id == run_id)):
                bus.emit(run_id, "finding.triaged", {"finding_id": f.id, "status": f.status.value})
        bus.emit(run_id, "stage.finished", {"stage": "triage"})

        _status(sf, run_id, RunStatus.fixing)
        bus.emit(run_id, "stage.started", {"stage": "fix"})
        tiers = await _author_fixes(sf, store, bus, client, cfg, provider, run_id, scratch_sha)
        bus.emit(run_id, "stage.finished", {"stage": "fix", "tiers": {k: len(v["findings"]) for k, v in tiers.items()}})
        _own_attempts_only(sf, run_id, tiers)
        with sf() as db:  # a fix attempt remembers which repository it is for (not the scratch folder it was made in)
            for t in tiers.values():
                for aid in t["attempts"].values():
                    db.get(FixAttempt, aid).repo = rc.repo
            db.commit()

        summary = _summarise(sf, run_id, pages, {}, {}, client, tiers)
        ready = sum(len(t["findings"]) for t in tiers.values())
        summary.update(mode="remote", awaiting_proof=ready, score_after=None, prs={}, branches={},
                       headline=f"{summary['total_findings']} issues - {ready} fixes ready for review - {summary['needs_human']} need you",
                       notice="Automated first pass. Fixes are proposals: nothing was built here, so none is proved until its "
                              "pull request's preview deployment is re-audited. A person reviews and merges every change.")
        _status(sf, run_id, RunStatus.complete, finished_at=datetime.now(UTC), summary=summary)
        bus.emit(run_id, "run.summary", summary)
        return summary
    except Exception as e:  # a run never dies silently
        _status(sf, run_id, RunStatus.failed, finished_at=datetime.now(UTC))
        bus.emit(run_id, "error", {"stage": "run", "error": f"{type(e).__name__}: {e}"})
        raise
    finally:
        record_tokens(sf, run_id, client)
        shutil.rmtree(workdir, ignore_errors=True)  # customer source does not stay on our disk


def _own_attempts_only(sf, run_id: str, tiers: dict) -> None:
    """Safety net for a shared server: every fix attempt this run is using must belong to one of this run's findings.
    (Attempts are looked up by an idempotency key; if two runs ever produced the same key, one would silently borrow the
    other's work. Fail loudly instead.)"""
    ids = [aid for t in tiers.values() for aid in t["attempts"].values()]
    if not ids:
        return
    with sf() as db:
        foreign = db.scalars(select(FixAttempt.id).join(Finding, Finding.id == FixAttempt.finding_id)
                             .where(FixAttempt.id.in_(ids), Finding.run_id != run_id)).all()
    if foreign:
        raise RuntimeError("internal error: a fix attempt belongs to another run; refusing to continue")

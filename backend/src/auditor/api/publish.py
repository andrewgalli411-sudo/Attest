"""Publish the fixes a person accepted. Outward-facing, so every guard is explicit and nothing is published by accident.

A tier branch holds every fix of that tier, accepted or not, so it cannot be pushed as-is. Instead each tier's accepted
fixes are cherry-picked, one commit each, onto a fresh branch cut from the run's base commit.
"""

from __future__ import annotations

import hashlib
import threading

from sqlalchemy import select

from ..models import AuditRun, Finding, FixAttempt
from ..remediate import _fix_summaries
from ..scm.base import RepoError
from ..scm.local_git import LocalGitProvider
from ..scm.pr_stage import build_pr
from .schemas import PublishedPr, PublishOut
from .views import PUBLISHABLE, build_pull_requests, committed_sha

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _repo_lock(path: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


class PublishRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def publish_accepted(sf, project, run_id: str) -> PublishOut:
    """Blocking (git + gh). Call from a worker thread."""
    with sf() as db:
        run = db.get(AuditRun, run_id)
        rows = [r for r in build_pull_requests(db, run) if r.decision == "accepted" and r.remote != "open"]
        picked: dict[str, list[tuple[str, str, str]]] = {}  # tier -> [(finding_id, attempt_id, sha)]
        for r in rows:
            fa = db.scalar(select(FixAttempt).where(FixAttempt.finding_id == r.finding_id))
            sha = committed_sha(db, fa.id)
            f = db.get(Finding, r.finding_id)
            if sha is None or f.status.value not in PUBLISHABLE:
                continue  # belt and braces: only proved, regression-clean, committed fixes
            picked.setdefault(r.tier or "PR2", []).append((r.finding_id, fa.id, sha))
        base_sha = db.scalar(select(FixAttempt.base_sha).join(Finding, Finding.id == FixAttempt.finding_id)
                             .where(Finding.run_id == run_id, FixAttempt.base_sha != "").limit(1))
        base_branch = (run.config or {}).get("base_branch") or project.base_branch
    if not picked:
        raise PublishRefused(409, "nothing to publish: accept at least one proved fix first")
    if not base_sha:
        raise PublishRefused(409, "this run has no recorded base commit")

    provider = LocalGitProvider(project.repo_path, allow_push=True)
    lock = _repo_lock(str(project.repo_path))
    if not lock.acquire(blocking=False):
        raise PublishRefused(409, "another publish is already running on this repository")
    out = PublishOut(opened=[])
    try:
        if not provider.is_clean():
            raise PublishRefused(409, "the repository has uncommitted changes; commit or stash them first")
        original = provider.current_branch()
        try:
            for tier, items in sorted(picked.items()):
                tag = hashlib.sha256("".join(sorted(s for _, _, s in items)).encode()).hexdigest()[:6]
                branch = f"a11y/{run_id[:8]}-{tier.lower()}-{tag}"  # same set of fixes -> same branch -> no duplicate PR
                try:
                    if provider.branch_exists(branch):
                        provider.delete_branch(branch)
                    provider.create_branch(branch, base_sha)
                    for _, _, sha in items:
                        provider.cherry_pick(sha)
                    fixes = _fix_summaries(sf, run_id, [i for i, _, _ in items])
                    title, body = build_pr(tier, run_id, fixes)
                    provider.push(branch)
                    url = provider.open_pr(branch, base_branch, title, body)
                except RepoError as e:
                    out.errors.append(f"{tier}: {e}")
                    provider.restore(original)
                    continue
                with sf() as db:
                    for _, aid, _ in items:
                        fa = db.get(FixAttempt, aid)
                        fa.pr_url, fa.status = url, "pr_open"
                    db.commit()
                out.opened.append(PublishedPr(tier=tier, branch=branch, url=url, fixes=len(items)))
                provider.restore(original)
        finally:
            provider.restore(original)
    finally:
        lock.release()
    return out

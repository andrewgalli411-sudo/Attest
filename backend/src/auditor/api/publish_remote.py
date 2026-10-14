"""Open pull requests on a person's own repository for the fixes they accepted, through the GitHub API only.

Nothing is cloned or pushed. For each tier of accepted fixes:
  1. download the base commit's source again (read-only, hostile-archive rules) and re-apply each stored edit with the
     same applier and the same safety policy that wrote it, so a stale or tampered edit cannot get through at the last step;
  2. make one real commit per fix with the Git Data API, preserving each file's mode and refusing links;
  3. create a branch `attest/<run>-<tier>-<hash>` and open a pull request against the repository's default branch.

Repeating the request is safe: the same set of fixes maps to the same branch, and an existing branch or pull request is
reused, never duplicated. Attest never merges anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from ..fix.verify import EditError, apply_edits
from ..models import AuditRun, Finding, FindingEvidence, FindingStatus, FixAttempt, ProjectRecord
from ..remediate import _fix_summaries
from ..scm.base import RepoError
from ..scm.pr_stage import build_pr
from ..scm.workspace import WorkspaceError, extract
from .auth import Principal
from .schemas import PublishedPr, PublishOut
from .views import REMOTE_PUBLISHABLE, build_pull_requests

NOTE = (
    "Attest does not build your site, so these changes have not been tested yet. When this pull request's preview "
    "deployment is ready, Attest re-audits the same elements there and reports the result here."
)
NOTES = [NOTE]


async def publish_remote(d, who: Principal, run: AuditRun) -> PublishOut:
    run_id = run.id
    with d.sf() as db:
        project = db.get(ProjectRecord, run.project_id) if run.project_id else None
        if project is None or project.owner_id != who.account_id:
            raise HTTPException(409, "the project for this run no longer exists")
        if not project.allow_pr:
            raise HTTPException(403, "pull requests are switched off for this project; turn them on in the project's settings first")
        proj = {"installation_id": project.installation_id, "repo": project.github_repo, "branch": project.default_branch}
        base_sha = (run.config or {}).get("base_sha", "")
        rows = [r for r in build_pull_requests(db, run) if r.decision == "accepted" and r.remote != "open"]
        picked: dict[str, list[dict]] = {}
        for r in rows:
            f = db.get(Finding, r.finding_id)
            fa = db.scalar(select(FixAttempt).where(FixAttempt.finding_id == r.finding_id))
            hit = next((e.payload for e in db.scalars(select(FindingEvidence).where(
                FindingEvidence.finding_id == r.finding_id, FindingEvidence.kind == "source_hit")) if e.payload), {})
            if fa is None or not fa.edits or f.status.value not in REMOTE_PUBLISHABLE or not (hit.get("hit") or {}).get("file"):
                continue
            picked.setdefault(r.tier or "PR2", []).append({"finding_id": f.id, "attempt_id": fa.id, "rule": f.rule_id, "edits": fa.edits,
                                                           "file": hit["hit"]["file"], "line": hit["hit"].get("line")})
    if not picked:
        raise HTTPException(409, "nothing to publish: accept at least one proposed fix first")
    if not base_sha:
        raise HTTPException(409, "this run has no recorded base commit")

    lock = d.publish_locks.setdefault(run_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "this run is already being published")
    async with lock:
        gh = d.github_factory(proj["installation_id"], proj["repo"])
        out = PublishOut(opened=[])
        work = Path(d.scratch_root) / f"publish-{run_id}-{secrets.token_hex(4)}"
        try:
            base_tree = await gh.commit_tree(base_sha)
            for tier, items in sorted(picked.items()):
                try:
                    done = await _publish_tier(d, gh, run_id, tier, items, base_sha, base_tree, proj["branch"], work / tier.lower(), out)
                except (RepoError, WorkspaceError) as e:
                    out.errors.append(f"{tier}: {e}")
                    continue
                if done:
                    out.opened.append(done)
        except RepoError as e:
            raise HTTPException(502, str(e)) from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not out.opened and out.errors:
        raise HTTPException(502, "; ".join(out.errors))
    return out


async def _publish_tier(d, gh, run_id, tier, items, base_sha, base_tree, base_branch, ws: Path, out: PublishOut) -> PublishedPr | None:
    tag = hashlib.sha256("".join(sorted(i["finding_id"] for i in items)).encode()).hexdigest()[:6]
    branch = f"attest/{run_id[:8]}-{tier.lower()}-{tag}"  # the same set of fixes always maps to the same branch
    applied = items
    if await gh.branch_sha(branch) is None:
        from ..scm.workspace import (
            extract as _extract,
        )

        await asyncio.to_thread(_extract, await gh.archive(base_sha), ws)
        parent_commit, parent_tree, applied = base_sha, base_tree, []
        for it in items:
            try:
                apply_edits(ws, it["edits"], {it["file"]}, rule_id=it["rule"])  # same applier, same safety policy, again
            except EditError as e:
                out.errors.append(f"{it['rule']}: could not be applied cleanly ({e})")
                continue
            files = {f: (ws / f).read_text() for f in {e["file"] for e in it["edits"]}}
            parent_commit, parent_tree = await gh.commit_files(parent_commit, parent_tree, files,
                                                               f"a11y: fix {it['rule']} ({it['file']}:{it['line'] or '?'})")
            applied.append(it)
        if not applied:
            return None
        await gh.create_branch(branch, parent_commit)
    found = await gh.find_pr(branch)
    if found:
        url, number = found
    else:
        fixes = _fix_summaries(d.sf, run_id, [i["finding_id"] for i in applied])
        title, body = build_pr(tier, run_id, fixes, NOTES)
        url, number = await gh.open_pr(branch, base_branch, title, body)
    with d.sf() as db:
        for it in applied:
            fa = db.get(FixAttempt, it["attempt_id"])
            fa.pr_url, fa.status, fa.branch = url, "pr_open", branch
            db.get(Finding, it["finding_id"]).status = FindingStatus.pr_open
        db.commit()
    d.bus.emit(run_id, "finding.pr_opened", {"tier": tier, "url": url, "number": number})
    return PublishedPr(tier=tier, branch=branch, url=url, fixes=len(applied))


__all__ = ["extract", "publish_remote"]

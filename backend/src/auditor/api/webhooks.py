"""GitHub webhooks: how Attest learns that a pull request's preview deployment is ready.

Nothing here is trusted until the signature checks out: GitHub signs the exact bytes of each delivery with a shared secret
(HMAC-SHA256), and we compare in constant time before reading a single field. Each delivery id is accepted once, so a
replayed or redelivered event does nothing the second time. The preview address inside the payload is then treated as
untrusted anyway (see preview.py) because whoever configured that repository's deployments chose it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..models import AuditRun, Finding, FixAttempt, ProjectRecord, ProofJob, WebhookDelivery
from ..netguard import UnsafeAddress
from ..preview import preview_allowed
from ..proof import PROOF_TIMEOUT_S, prove_preview
from .auth import check_host

router = APIRouter(prefix="/api")
DELIVERY = re.compile(r"^[A-Za-z0-9-]{8,100}$")
BRANCH = re.compile(r"^attest/[A-Za-z0-9._/-]{1,150}$")  # only branches Attest itself created


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not header.startswith("sha256=") or len(header) != 71:
        return False
    return hmac.compare_digest(header[7:], hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())


@router.post("/github/webhook", status_code=202)
async def github_webhook(request: Request):
    check_host(request)
    d = request.app.state.deps
    if not d.cfg.github_webhook_secret:
        raise HTTPException(503, "webhooks are not configured on this server")
    body = await request.body()  # its size was capped (and a Content-Length required) before we got here
    if not signature_ok(d.cfg.github_webhook_secret, body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(401, "bad signature")
    delivery, event = request.headers.get("x-github-delivery", ""), request.headers.get("x-github-event", "")
    if not DELIVERY.match(delivery) or not re.fullmatch(r"[a-z_]{1,50}", event):
        raise HTTPException(400, "missing or malformed delivery headers")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "the body is not JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "the body is not an object")
    with d.sf() as db:
        try:
            db.add(WebhookDelivery(id=delivery, event=event))
            db.commit()
        except IntegrityError:
            return {"status": "duplicate"}  # already handled: acknowledge, do nothing
    if event == "ping":
        return {"status": "pong"}
    handler = {"deployment_status": _deployment_status, "pull_request": _pull_request, "installation": _installation}.get(event)
    return handler(d, payload) if handler else {"status": "ignored", "detail": f"event {event!r} is not used"}


def _repo(payload: dict) -> str:
    r = (payload.get("repository") or {}).get("full_name", "")
    return r.lower() if isinstance(r, str) else ""


def _deployment_status(d, payload: dict) -> dict:
    ds, dep = payload.get("deployment_status") or {}, payload.get("deployment") or {}
    if ds.get("state") != "success":
        return {"status": "ignored", "detail": "the deployment is not successful (yet)"}
    url, ref, sha = ds.get("environment_url") or "", dep.get("ref") or "", dep.get("sha") or ""
    if not (isinstance(url, str) and isinstance(ref, str) and isinstance(sha, str)) or not url:
        return {"status": "ignored", "detail": "no preview address in the event"}
    if not BRANCH.match(ref) or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return {"status": "ignored", "detail": "not a branch Attest created"}
    repo = _repo(payload)
    install_id = (payload.get("installation") or {}).get("id")
    with d.sf() as db:
        row = db.execute(select(AuditRun.id, ProjectRecord).join(Finding, Finding.run_id == AuditRun.id)
                         .join(FixAttempt, FixAttempt.finding_id == Finding.id).join(ProjectRecord, ProjectRecord.id == AuditRun.project_id)
                         .where(FixAttempt.branch == ref, FixAttempt.pr_url.is_not(None))).first()
        if row is None or row[1].github_repo.lower() != repo or (install_id is not None and install_id != row[1].installation_id):
            return {"status": "ignored", "detail": "no pull request of ours matches that deployment"}
        run_id, project = row
        try:
            preview, why = preview_allowed(url, site_url=project.site_url, template=project.preview_url_template,
                                           suffixes=d.cfg.preview_host_suffixes, allow_insecure=d.cfg.allow_insecure_sites,
                                           ports=tuple(d.cfg.allowed_site_ports))
        except UnsafeAddress as e:
            return {"status": "ignored", "detail": str(e)}
        job = ProofJob(run_id=run_id, branch=ref, deployment_sha=sha, preview_url=preview)
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            return {"status": "duplicate", "detail": "this deployment is already being (or was) re-tested"}
        job_id = job.id
    task = asyncio.create_task(asyncio.wait_for(prove_preview(d, job_id), PROOF_TIMEOUT_S))
    d.proof_tasks.add(task)
    task.add_done_callback(d.proof_tasks.discard)
    return {"status": "accepted", "job": job_id, "preview": why}


def _pull_request(d, payload: dict) -> dict:
    pr = payload.get("pull_request") or {}
    ref = ((pr.get("head") or {}).get("ref")) or ""
    if payload.get("action") != "closed" or not isinstance(ref, str) or not BRANCH.match(ref):
        return {"status": "ignored"}
    state = "merged" if pr.get("merged") else "closed"
    with d.sf() as db:
        n = db.execute(update(FixAttempt).where(FixAttempt.branch == ref, FixAttempt.pr_url.is_not(None)).values(status=state)).rowcount
        db.commit()
    return {"status": "recorded", "detail": f"{n} fix(es) marked {state}"}


def _installation(d, payload: dict) -> dict:
    """If the app is uninstalled or suspended, stop trying to write to those repositories."""
    inst = (payload.get("installation") or {}).get("id")
    if payload.get("action") not in ("deleted", "suspend") or not isinstance(inst, int):
        return {"status": "ignored"}
    with d.sf() as db:
        n = db.execute(update(ProjectRecord).where(ProjectRecord.installation_id == inst).values(allow_pr=False)).rowcount
        db.commit()
    return {"status": "recorded", "detail": f"pull requests switched off for {n} project(s)"}

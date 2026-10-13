"""Projects an account has attached: a GitHub repository plus the live site to audit. Accounts mode only."""

from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, update

from ..models import AuditRun, ProjectRecord, UserSession
from ..netguard import UnsafeAddress, parse_site_url
from ..scm.github_user import GitHubAuthExpired, GitHubError, GitHubUser
from .auth import Principal, require_auth
from .schemas import ProjectIn, ProjectOut, ProjectPatch, RepoOut, ReposOut, VerificationHelp

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
TERMINAL = {"complete", "failed", "cancelled", "reviewed", "exported"}
VERIFY_PATH = "/.well-known/attest-verification.txt"


def _deps(request: Request):
    return request.app.state.deps


def _accounts_only(d, who: Principal) -> None:
    if who.is_local or d.cfg.auth_mode != "accounts":
        raise HTTPException(404, "projects are managed in projects.json on this server")


def _site(d, raw: str):
    try:
        return parse_site_url(raw, allow_insecure=d.cfg.allow_insecure_sites, allowed_ports=tuple(d.cfg.allowed_site_ports))
    except UnsafeAddress as e:
        raise HTTPException(422, str(e)) from None


def _template(d, raw: str | None) -> str | None:
    """A preview address pattern such as https://pr-{pr}--myapp.example.app. Checked with the placeholders filled in."""
    if raw is None or raw == "":
        return None
    probe = raw.replace("{pr}", "1").replace("{sha}", "abc1234").replace("{branch}", "branch")
    if "{" in probe or "}" in probe:
        raise HTTPException(422, "the preview address may only use {pr}, {sha} and {branch}")
    _site(d, probe)
    return raw


def _cookies(vault, items) -> str | None:
    return vault.seal({"cookies": [{"name": c.name, "value": c.value} for c in items]}) if items else None


def _out(d, p: ProjectRecord) -> ProjectOut:
    names: list[str] = []
    if p.sealed_auth:
        names = [c["name"] for c in d.vault.open(p.sealed_auth).get("cookies", [])]
    host = parse_site_url(p.site_url, allow_insecure=True, allowed_ports=(80, 443, 8080, 8443, *d.cfg.allowed_site_ports)).host
    return ProjectOut(
        id=p.id, name=p.name, github_repo=p.github_repo, default_branch=p.default_branch, site_url=p.site_url,
        preview_url_template=p.preview_url_template, pages=p.pages, has_login=bool(names), login_cookie_names=names,
        allow_pr=p.allow_pr, verified=p.verified_at is not None, verified_at=p.verified_at.isoformat() if p.verified_at else None,
        verification=VerificationHelp(file_path=VERIFY_PATH, file_contents=f"attest-verification={p.verification_token}",
                                      dns_name=f"_attest-challenge.{host}", dns_value=f"attest-verification={p.verification_token}"),
        created_at=p.created_at.isoformat())


def get_project(db, project_id: str, who: Principal) -> ProjectRecord:
    p = db.get(ProjectRecord, project_id)
    if p is None or p.owner_id != who.account_id:  # someone else's project answers exactly like a missing one
        raise HTTPException(404, "project not found")
    return p


async def _github(request: Request, who: Principal) -> GitHubUser:
    d = _deps(request)
    with d.sf() as db:
        row = db.get(UserSession, who.session_id) if who.session_id else None
        sealed = row.sealed_github_token if row else None
    if not sealed:
        raise HTTPException(401, "sign in required")
    return GitHubUser(d.vault.open(sealed), api_base=d.cfg.github_api_base,
                      transport=getattr(request.app.state, "github_transport", None))


@router.get("/github/repos", response_model=ReposOut)
async def list_repos(request: Request, d=Depends(_deps), who: Principal = Depends(require_auth)):
    """The repositories this person can reach through an installation of the Attest GitHub App."""
    _accounts_only(d, who)
    gh = await _github(request, who)
    try:
        repos = await gh.repos()
    except GitHubAuthExpired:
        raise HTTPException(401, "sign in required") from None
    except GitHubError as e:
        raise HTTPException(502, str(e)) from None
    finally:
        await gh.aclose()
    with d.sf() as db:
        attached = {(p.installation_id, p.github_repo.lower()) for p in db.scalars(
            select(ProjectRecord).where(ProjectRecord.owner_id == who.account_id))}
    slug = d.cfg.github_app_slug
    return ReposOut(
        install_url=f"{d.cfg.github_web_base}/apps/{quote(slug)}/installations/new" if slug else None,
        repos=[RepoOut(installation_id=r.installation_id, full_name=r.full_name, default_branch=r.default_branch,
                       private=r.private, homepage=r.homepage, attached=(r.installation_id, r.full_name.lower()) in attached)
               for r in repos])


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(d=Depends(_deps), who: Principal = Depends(require_auth)):
    _accounts_only(d, who)
    with d.sf() as db:
        rows = db.scalars(select(ProjectRecord).where(ProjectRecord.owner_id == who.account_id).order_by(ProjectRecord.created_at)).all()
        return [_out(d, p) for p in rows]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectIn, request: Request, d=Depends(_deps), who: Principal = Depends(require_auth)):
    _accounts_only(d, who)
    site = _site(d, body.site_url)
    template = _template(d, body.preview_url_template)
    with d.sf() as db:
        n = db.scalar(select(func.count()).select_from(ProjectRecord).where(ProjectRecord.owner_id == who.account_id))
    if n >= d.cfg.max_projects_per_account:
        raise HTTPException(409, f"you can attach up to {d.cfg.max_projects_per_account} projects")

    # Never trust the browser's claim about the repository or the installation: ask GitHub, as this person.
    gh = await _github(request, who)
    try:
        repos = await gh.repos()
    except GitHubAuthExpired:
        raise HTTPException(401, "sign in required") from None
    except GitHubError as e:
        raise HTTPException(502, str(e)) from None
    finally:
        await gh.aclose()
    repo = next((r for r in repos if r.installation_id == body.installation_id and r.full_name.lower() == body.github_repo.lower()), None)
    if repo is None:
        raise HTTPException(403, "you do not have access to that repository through the Attest GitHub App; install the app on it first")

    name = body.name or repo.full_name.split("/")[1]
    with d.sf() as db:
        if db.scalar(select(ProjectRecord.id).where(ProjectRecord.owner_id == who.account_id, ProjectRecord.name == name)):
            raise HTTPException(409, f"you already have a project called {name!r}")
        p = ProjectRecord(
            owner_id=who.account_id, name=name, github_repo=repo.full_name, installation_id=repo.installation_id,
            default_branch=repo.default_branch, site_url=site.url, preview_url_template=template,
            pages=[{"path": s.path, "label": s.label or s.path} for s in (body.pages or [])] or [{"path": "/", "label": "home"}],
            sealed_auth=_cookies(d.vault, body.login_cookies), allow_pr=body.allow_pr,
            verification_token=secrets.token_urlsafe(24))
        db.add(p)
        db.commit()
        db.refresh(p)
        return _out(d, p)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def read_project(project_id: str, d=Depends(_deps), who: Principal = Depends(require_auth)):
    _accounts_only(d, who)
    with d.sf() as db:
        return _out(d, get_project(db, project_id, who))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, body: ProjectPatch, d=Depends(_deps), who: Principal = Depends(require_auth)):
    _accounts_only(d, who)
    with d.sf() as db:
        p = get_project(db, project_id, who)
        if body.name is not None and body.name != p.name:
            if db.scalar(select(ProjectRecord.id).where(ProjectRecord.owner_id == who.account_id, ProjectRecord.name == body.name)):
                raise HTTPException(409, f"you already have a project called {body.name!r}")
            p.name = body.name
        if body.site_url is not None:
            site = _site(d, body.site_url)
            if site.url != p.site_url:
                p.site_url, p.verified_at = site.url, None  # a different address has to be proven to be yours again
        if body.pages is not None:
            p.pages = [{"path": s.path, "label": s.label or s.path} for s in body.pages] or [{"path": "/", "label": "home"}]
        if "preview_url_template" in body.model_fields_set:
            p.preview_url_template = _template(d, body.preview_url_template)
        if body.login_cookies is not None:
            p.sealed_auth = _cookies(d.vault, body.login_cookies)
        if body.allow_pr is not None:
            p.allow_pr = body.allow_pr
        db.commit()
        db.refresh(p)
        return _out(d, p)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, d=Depends(_deps), who: Principal = Depends(require_auth)):
    _accounts_only(d, who)
    with d.sf() as db:
        p = get_project(db, project_id, who)
        busy = db.scalars(select(AuditRun.status).where(AuditRun.project_id == p.id)).all()
        if any(s.value not in TERMINAL for s in busy):
            raise HTTPException(409, "a run is still in progress on this project; cancel it first")
        db.execute(update(AuditRun).where(AuditRun.project_id == p.id).values(project_id=None))  # past runs stay
        db.delete(p)
        db.commit()

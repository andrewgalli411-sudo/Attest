"""Sign in with GitHub (the GitHub App's user authorisation), sessions, and "who am I"."""

from __future__ import annotations

import base64
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import delete, select

from ..models import Account, UserSession
from .auth import SESSION_COOKIE, Principal, check_host, hash_token, require_auth, session_principal

STATE_COOKIE = "attest_oauth"
STATE_TTL_S = 600

router = APIRouter(prefix="/api")


class AuthConfig(BaseModel):
    mode: str
    signed_in: bool = False
    login: str | None = None


class Me(BaseModel):
    mode: str
    login: str
    name: str | None = None
    avatar_url: str | None = None


def _secure(cfg) -> bool:
    return cfg.public_url.startswith("https://")


def _safe_next(value: str | None) -> str:
    """Where to go after sign-in: a path on this site only. Anything else (another site, a protocol-relative URL, a
    backslash trick) becomes "/", so the sign-in flow can never be used as an open redirect."""
    v = value or "/"
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(c) < 32 for c in v) or len(v) > 300:
        return "/"
    return v


def _pack(state: str, nxt: str) -> str:
    return f"{state}.{base64.urlsafe_b64encode(nxt.encode()).decode()}"


def _unpack(value: str) -> tuple[str, str]:
    state, _, nxt = value.partition(".")
    try:
        return state, _safe_next(base64.urlsafe_b64decode(nxt.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        return state, "/"


def _client(request: Request) -> httpx.AsyncClient:
    transport = getattr(request.app.state, "github_transport", None)  # tests replace GitHub with a fake
    return httpx.AsyncClient(transport=transport, timeout=20)


@router.get("/auth/config", response_model=AuthConfig)
def auth_config(request: Request):
    """Public: lets the web app decide whether to show a sign-in page."""
    check_host(request)
    cfg = request.app.state.deps.cfg
    who = session_principal(request) if cfg.auth_mode == "accounts" else None
    return AuthConfig(mode=cfg.auth_mode, signed_in=who is not None or cfg.auth_mode != "accounts", login=who.login if who else None)


@router.get("/auth/github/login")
def github_login(request: Request, next: str | None = Query(None, max_length=300)):
    check_host(request)
    cfg = request.app.state.deps.cfg
    if cfg.auth_mode != "accounts":
        raise HTTPException(404, "sign-in is not enabled on this server")
    state = secrets.token_urlsafe(24)
    url = f"{cfg.github_web_base}/login/oauth/authorize?" + urlencode({
        "client_id": cfg.github_client_id, "redirect_uri": f"{cfg.public_url}/api/auth/github/callback", "state": state})
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, _pack(state, _safe_next(next)), max_age=STATE_TTL_S, httponly=True, samesite="lax",
                    secure=_secure(cfg), path="/api/auth")
    return resp


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str = Query("", max_length=200), state: str = Query("", max_length=200)):
    check_host(request)
    d = request.app.state.deps
    cfg = d.cfg
    if cfg.auth_mode != "accounts":
        raise HTTPException(404, "sign-in is not enabled on this server")
    cookie = request.cookies.get(STATE_COOKIE, "")
    want_state, nxt = _unpack(cookie) if cookie else ("", "/")
    if not code or not state or not want_state or not hmac.compare_digest(state.encode(), want_state.encode()):
        raise HTTPException(400, "sign-in could not be verified; start again")

    async with _client(request) as http:
        tok = await http.post(f"{cfg.github_web_base}/login/oauth/access_token", headers={"Accept": "application/json"}, data={
            "client_id": cfg.github_client_id, "client_secret": cfg.github_client_secret, "code": code,
            "redirect_uri": f"{cfg.public_url}/api/auth/github/callback"})
        access = (tok.json() if tok.status_code == 200 else {}).get("access_token")
        if not access:
            raise HTTPException(400, "GitHub did not accept the sign-in; start again")
        me = await http.get(f"{cfg.github_api_base}/user", headers={"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"})
        if me.status_code != 200:
            raise HTTPException(502, "could not read your GitHub profile")
        gh = me.json()

    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with d.sf() as db:
        acct = db.scalar(select(Account).where(Account.github_id == int(gh["id"])))
        if acct is None:
            acct = Account(github_id=int(gh["id"]), login=gh["login"])
            db.add(acct)
        acct.login, acct.name, acct.avatar_url, acct.last_seen_at = gh["login"], gh.get("name"), gh.get("avatar_url"), now
        db.flush()
        db.execute(delete(UserSession).where(UserSession.expires_at < now))  # housekeeping
        db.add(UserSession(token_hash=hash_token(token), account_id=acct.id, sealed_github_token=d.vault.seal(access),
                           expires_at=now + timedelta(seconds=cfg.session_ttl_s)))
        db.commit()

    resp = RedirectResponse(f"{cfg.public_url.rstrip('/')}{nxt}", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, max_age=cfg.session_ttl_s, httponly=True, samesite="lax", secure=_secure(cfg), path="/")
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


@router.post("/auth/logout", status_code=204)
def logout(request: Request, who: Principal = Depends(require_auth)):
    d = request.app.state.deps
    if who.session_id:
        with d.sf() as db:
            db.execute(delete(UserSession).where(UserSession.id == who.session_id))
            db.commit()
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/me", response_model=Me)
def me(request: Request, who: Principal = Depends(require_auth)):
    d = request.app.state.deps
    if who.is_local:
        return Me(mode="local", login="local")
    with d.sf() as db:
        a = db.get(Account, who.account_id)
        return Me(mode="accounts", login=a.login, name=a.name, avatar_url=a.avatar_url)

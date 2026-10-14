"""Access control for /api.

Two modes, chosen by configuration (never by the caller):

  local mode (AUTH_MODE=local, the default): one implicit owner.
    * token configured: every request needs `Authorization: Bearer <token>`.
    * no token: only loopback clients are served - for development on one machine.
  accounts mode (AUTH_MODE=accounts): people sign in with GitHub and every record belongs to an account. A session
    cookie identifies the caller, and any request that changes something must also carry `X-Requested-With: attest`
    (a cross-site form or image cannot set that header, which is what stops cross-site request forgery).

In both modes the Host header must be on the allow-list, which blocks DNS-rebinding attacks that would otherwise let
a web page in the user's browser reach a loopback-only API.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy import select

from ..models import Account, UserSession

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SESSION_COOKIE = "attest_session"
CSRF_HEADER = ("x-requested-with", "attest")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Principal:
    """Who is calling. `account_id` is None only in local mode, where there is a single implicit owner."""

    account_id: str | None
    login: str = "local"
    session_id: str | None = None

    @property
    def is_local(self) -> bool:
        return self.account_id is None

    def can_see(self, owner_id: str | None) -> bool:
        """Local mode sees everything (there is only one person). Accounts see only what they own."""
        return self.is_local or owner_id == self.account_id


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1]:8000
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def check_host(request: Request) -> None:
    cfg = request.app.state.deps.cfg
    if "*" not in cfg.allowed_hosts and _hostname(request.headers.get("host", "")) not in cfg.allowed_hosts:
        raise HTTPException(421, "host not allowed")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_principal(request: Request) -> Principal | None:
    """The signed-in account for this request's cookie, or None. Expired sessions are never honoured."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return None
    d = request.app.state.deps
    with d.sf() as db:
        row = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        if row is None:
            return None
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None
        acct = db.get(Account, row.account_id)
        if acct is None or acct.disabled_at is not None:  # an operator can switch an account off
            return None
        return Principal(account_id=acct.id, login=acct.login, session_id=row.id)


async def require_auth(request: Request) -> Principal:
    who = await _authenticate(request)
    request.state.principal = who  # for the rate limiter, which runs after authentication
    return who


async def _authenticate(request: Request) -> Principal:
    check_host(request)
    cfg = request.app.state.deps.cfg
    if cfg.auth_mode == "accounts":
        who = session_principal(request)
        if who is None:
            raise HTTPException(401, "sign in required")
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER[0], "").lower() != CSRF_HEADER[1]:
            raise HTTPException(403, "missing X-Requested-With header")
        return who
    if cfg.api_token:
        given = request.headers.get("authorization", "")
        scheme, _, token = given.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), cfg.api_token.encode()):
            raise HTTPException(401, "missing or invalid API token", headers={"WWW-Authenticate": "Bearer"})
        return Principal(None)
    client = request.client.host if request.client else ""
    if client not in LOOPBACK:
        raise HTTPException(401, "no API token is configured, so only local clients are served")
    return Principal(None)

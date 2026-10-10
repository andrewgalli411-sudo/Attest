"""Access control for /api.

Two modes, chosen by configuration (never by the caller):
  * token mode  (settings.api_token set): every request needs `Authorization: Bearer <token>`.
  * local mode  (no token): only loopback clients are served - for development on one machine.
In both modes the Host header must be on the allow-list, which blocks DNS-rebinding attacks that would
otherwise let a web page in the user's browser reach a loopback-only API.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1]:8000
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


async def require_auth(request: Request) -> None:
    cfg = request.app.state.deps.cfg
    if "*" not in cfg.allowed_hosts and _hostname(request.headers.get("host", "")) not in cfg.allowed_hosts:
        raise HTTPException(421, "host not allowed")
    if cfg.api_token:
        given = request.headers.get("authorization", "")
        scheme, _, token = given.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), cfg.api_token.encode()):
            raise HTTPException(401, "missing or invalid API token", headers={"WWW-Authenticate": "Bearer"})
        return
    client = request.client.host if request.client else ""
    if client not in LOOPBACK:
        raise HTTPException(401, "no API token is configured, so only local clients are served")

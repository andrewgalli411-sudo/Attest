"""Rate limiting for the endpoints that cost something (they call GitHub, start browsers, or spend model tokens).

A sliding window per (limit name, caller). It lives in this process, so with several workers the real allowance is
multiplied by the worker count; the deployment guide also asks for limiting at the reverse proxy. The table is bounded:
callers choose their own keys (an address, an account), so an attacker must not be able to grow it without limit.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

MAX_KEYS = 50_000


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_KEYS):
        self._clock, self._max = clock, max_keys
        self._hits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()

    def retry_after(self, name: str, key: str, limit: int, window_s: float = 60.0) -> float | None:
        """Record one use. Returns None if allowed, else the seconds to wait. limit <= 0 means unlimited."""
        if limit <= 0:
            return None
        now = self._clock()
        k = (name, key)
        q = self._hits.get(k)
        if q is None:
            q = self._hits[k] = deque()
            while len(self._hits) > self._max:  # forget the least recently used caller
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(k)
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= limit:
            return max(0.1, q[0] + window_s - now)
        q.append(now)
        return None


def client_ip(request: Request, hops: int) -> str:
    """The caller's address. X-Forwarded-For can say anything, so it is only used when the operator says how many proxies
    they control (`hops`); then the entry that many places from the RIGHT is the one our own outermost proxy saw, and
    anything further left was supplied by the caller and is ignored."""
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) < hops:
        return peer
    candidate = parts[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def limited(name: str, limit_attr: str, by: str = "account") -> Callable:
    """A FastAPI dependency: `by` is "account" (falls back to the address when local) or "address"."""

    async def check(request: Request) -> None:
        d = request.app.state.deps
        who = getattr(request.state, "principal", None)
        key = (who.account_id if (by == "account" and who is not None and who.account_id) else None) or client_ip(request, d.cfg.trusted_proxy_hops)
        wait = d.limiter.retry_after(name, key, getattr(d.cfg, limit_attr))
        if wait is not None:
            raise HTTPException(429, f"too many requests; try again in {math.ceil(wait)} seconds", headers={"Retry-After": str(math.ceil(wait))})

    return check

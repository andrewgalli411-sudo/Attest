"""Plain ASGI middleware (not BaseHTTPMiddleware, which interferes with streaming responses such as SSE).

* request size limit: a POST must declare a Content-Length within the limit (411 if it does not, 413 if it is too
  big), so nobody can make the API buffer an unbounded body;
* security headers on every response;
* a request id (validated, so a client cannot inject log lines) and one access-log line per request.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

access_log = logging.getLogger("auditor.access")
_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityMiddleware:
    def __init__(self, app, max_body: int = 64 * 1024, path_limits: dict[str, int] | None = None):
        self.app, self.max_body = app, max_body
        self.path_limits = path_limits or {}  # a few endpoints (GitHub webhooks) legitimately receive larger bodies

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        given = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = given if _ID.match(given) else uuid.uuid4().hex
        started, status = time.perf_counter(), [0]

        async def reject(code: int, detail: str):
            body = json.dumps({"detail": detail}).encode()
            await send({"type": "http.response.start", "status": code, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"x-request-id", rid.encode()), *SECURITY_HEADERS]})
            await send({"type": "http.response.body", "body": body})
            status[0] = code

        if scope["method"] in ("POST", "PUT", "PATCH"):
            cl = headers.get(b"content-length")
            if cl is None or b"transfer-encoding" in headers:
                await reject(411, "a Content-Length header is required")
                self._log(scope, rid, status[0], started)
                return
            try:
                too_big = int(cl) > self.path_limits.get(scope["path"], self.max_body)
            except ValueError:
                too_big = True
            if too_big:
                await reject(413, f"request body exceeds {self.path_limits.get(scope['path'], self.max_body)} bytes")
                self._log(scope, rid, status[0], started)
                return

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                present = {k.lower() for k, _ in message["headers"]}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in present]
                extra.append((b"x-request-id", rid.encode()))
                if scope["path"].startswith("/api/") and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message["headers"], *extra]}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            self._log(scope, rid, status[0], started)

    @staticmethod
    def _log(scope, rid: str, status: int, started: float) -> None:
        access_log.info("%s %s -> %s %.0fms rid=%s", scope["method"], scope["path"], status,
                        (time.perf_counter() - started) * 1000, rid)

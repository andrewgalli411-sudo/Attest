"""Keeping the audit browser away from the server's own network.

In accounts mode the browser opens pages that strangers chose, and those pages can ask it for anything: `fetch
("http://169.254.169.254/...")`, an <img> at an internal service, a redirect to `localhost`, a WebSocket to a database.
The defence is a proxy (see safe_proxy.py) that every connection the browser makes must pass through; it resolves names
itself and refuses anything that is not a public address, at the moment of connecting.

This module is the glue: a per-run `BrowserGuard` that owns the proxy, a context variable so the pipeline code does not
have to pass it around, and `launch_browser`, which starts Chromium behind it. Service workers and downloads are turned
off as well, so a page cannot do work outside the browser's normal request path.

Defence in depth: the deployment guide also asks for an egress firewall on audit workers.
"""

from __future__ import annotations

from contextvars import ContextVar

from .safe_proxy import SafeProxy


class BlockedByGuard(RuntimeError):
    """A page tried to reach somewhere Attest will not go."""


class BrowserGuard:
    def __init__(self, proxy: SafeProxy, *, browser_args: list[str] | None = None):
        self.proxy = proxy
        self.browser_args = list(browser_args or [])  # extra Chromium arguments (tests only)

    @property
    def violations(self) -> list[str]:
        return self.proxy.violations

    async def raise_if_tripped(self) -> None:
        if self.proxy.violations:
            raise BlockedByGuard("a page tried to reach a private or reserved address (" + "; ".join(self.proxy.violations[:3]) + ")")


GUARD: ContextVar[BrowserGuard | None] = ContextVar("attest_browser_guard", default=None)


def context_options() -> dict:
    """Extra browser-context settings that only matter when the guard is on."""
    return {"service_workers": "block", "accept_downloads": False} if GUARD.get() else {}


async def launch_browser(pw, **launch_kwargs):
    """Start Chromium, behind the current run's proxy if there is one."""
    guard = GUARD.get()
    if guard:
        launch_kwargs["proxy"] = guard.proxy.playwright_settings()
        launch_kwargs["args"] = [*launch_kwargs.get("args", []), *guard.browser_args]
    return await pw.chromium.launch(**launch_kwargs)

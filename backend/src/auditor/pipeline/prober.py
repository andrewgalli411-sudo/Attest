from __future__ import annotations

import asyncio
import json
import logging

from playwright.async_api import Browser, async_playwright

from .axe_runner import run_axe
from .flows import replay_steps
from .probes.base import PROBE_SCS, ProbeResult
from .probes.focus_visible import focus_visible
from .probes.form_labels import form_labels
from .probes.keyboard_nav import keyboard_nav
from .probes.keyboard_trap import keyboard_trap
from .probes.modal_focus import modal_focus
from .probes.name_role_value import name_role_value

log = logging.getLogger(__name__)

PROBES = [keyboard_nav, keyboard_trap, focus_visible, modal_focus, form_labels, name_role_value]
VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20_000
SETTLE_MS = 3_000
CHECK_CONCURRENCY = 3
HTML_TYPES = ("text/html", "application/xhtml+xml")


class PageUnauditable(Exception):
    pass


async def apply_auth(ctx, auth: dict | None, url: str) -> None:
    """Seeded-session auth: cookies and/or localStorage set before the first navigation.
    No credentials are stored or typed; the target app's dev build honours the seed."""
    if not auth:
        return
    if auth.get("cookies"):
        await ctx.add_cookies([{"url": url, **c} for c in auth["cookies"]])
    if auth.get("local_storage"):
        items = json.dumps(auth["local_storage"])
        await ctx.add_init_script(
            f"try {{ for (const [k, v] of Object.entries({items})) localStorage.setItem(k, v); }} catch (e) {{}}"
        )


async def _open(
    browser: Browser, url: str, steps: list[dict] | None = None, auth: dict | None = None
):
    """Fresh context per check so probes cannot contaminate each other."""
    if not url.startswith(("http://", "https://")):
        raise PageUnauditable(f"unsupported scheme: {url.split(':', 1)[0]!r}")
    # bypass_csp: an auditor must be able to inject axe regardless of the page's CSP.
    ctx = await browser.new_context(viewport=VIEWPORT, reduced_motion="reduce", bypass_csp=True)
    try:
        await apply_auth(ctx, auth, url)
        page = await ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        resp = await page.goto(url, wait_until="load")
        try:  # let client-side redirects / late scripts settle so we audit the final page
            await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
        except Exception:
            pass
        if resp is None or resp.status >= 400:
            raise PageUnauditable(f"HTTP {resp.status if resp else 'no response'}")
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype not in HTML_TYPES:
            raise PageUnauditable(f"non-HTML content-type: {ctype or 'missing'}")
        if steps:
            await replay_steps(page, steps)
        return ctx, page, page.url
    except BaseException:
        await ctx.close()
        raise


async def _guarded(
    browser: Browser, url: str, fn, steps: list[dict] | None = None, auth: dict | None = None
):
    """Run fn(page); fail loudly if the page navigated away mid-check (results would be stale)."""
    ctx, page, start_url = await _open(browser, url, steps, auth)
    try:
        out = await fn(page)
        if page.url != start_url:
            raise RuntimeError(f"page navigated during check: {start_url} -> {page.url}")
        return out
    finally:
        await ctx.close()


async def _capture(page) -> dict[str, bytes]:
    cdp = await page.context.new_cdp_session(page)
    try:
        tree = await cdp.send("Accessibility.getFullAXTree")
    finally:
        await cdp.detach()
    return {
        "screenshot.png": await page.screenshot(full_page=True, animations="disabled"),
        "dom.html": (await page.content()).encode(),
        "ax.json": json.dumps(tree, sort_keys=True).encode(),
    }


async def probe_page(
    browser: Browser, url: str, steps: list[dict] | None = None, capture: bool = False,
    auth: dict | None = None,
) -> dict:
    result: dict = {"url": url, "final_url": None, "error": None, "axe": None, "probes": []}
    try:  # fail fast: one bad navigation must not cost N timeouts
        ctx, _, final = await _open(browser, url, steps, auth)
        result["final_url"] = final
        await ctx.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}".split("\n")[0]
        result["probes"] = [
            ProbeResult(p.__name__, PROBE_SCS[p.__name__], "inconclusive", error=result["error"]).to_dict()
            for p in PROBES
        ]
        return result

    sem = asyncio.Semaphore(CHECK_CONCURRENCY)  # checks are isolated (own context) so can overlap

    async def do_axe() -> None:
        async with sem:
            try:
                result["axe"] = await _guarded(browser, url, run_axe, steps, auth)
            except Exception as e:
                result["error"] = f"axe: {type(e).__name__}: {e}".split("\n")[0]
                log.warning("axe failed for %s: %s", url, e)

    async def do_capture() -> None:
        async with sem:
            try:
                result["artifacts"] = await _guarded(browser, url, _capture, steps, auth)
            except Exception as e:
                result["artifacts"] = {}
                log.warning("capture failed for %s: %s", url, e)

    async def do_probe(probe) -> dict:
        async with sem:
            try:
                return (await _guarded(browser, url, probe, steps, auth)).to_dict()
            except Exception as e:
                log.warning("probe %s failed for %s: %s", probe.__name__, url, e)
                return ProbeResult(
                    probe.__name__, PROBE_SCS[probe.__name__], "inconclusive",
                    error=f"{type(e).__name__}: {e}".split("\n")[0],
                ).to_dict()

    jobs = [do_axe()] + ([do_capture()] if capture else []) + [do_probe(p) for p in PROBES]
    outs = await asyncio.gather(*jobs)
    result["probes"] = outs[-len(PROBES):]  # PROBES order, independent of completion order
    return result


async def probe_url(url: str) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            return await probe_page(browser, url)
        finally:
            await browser.close()

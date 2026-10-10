from __future__ import annotations

import asyncio
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from playwright.async_api import Browser

SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".mp3",
            ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".css", ".js", ".json", ".xml", ".ico")
UA = "wcag-auditor"


@dataclass(frozen=True)
class Discovered:
    url: str
    depth: int


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlsplit(url)
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def in_scope(url: str, origin: str) -> bool:
    p = urlsplit(url)
    return p.scheme in ("http", "https") and p.netloc.lower() == origin and not p.path.lower().endswith(SKIP_EXT)


async def _robots(seed: str) -> urllib.robotparser.RobotFileParser | None:
    p = urlsplit(seed)
    rp = urllib.robotparser.RobotFileParser(f"{p.scheme}://{p.netloc}/robots.txt")
    try:
        await asyncio.wait_for(asyncio.to_thread(rp.read), 10)
    except Exception:
        return None  # unreachable robots.txt -> treated as allow-all (RobotFileParser default)
    return rp


async def crawl(
    browser: Browser, seed: str, *, max_pages: int = 25, max_depth: int = 2,
    respect_robots: bool = True, nav_timeout_ms: int = 15_000,
) -> tuple[list[Discovered], list[dict]]:
    """Deterministic same-origin BFS. Returns (pages, skipped[{url, reason}])."""
    seed = normalize(seed)
    origin = urlsplit(seed).netloc.lower()
    rp = await _robots(seed) if respect_robots else None
    seen: set[str] = {seed}
    queue: list[Discovered] = [Discovered(seed, 0)]
    pages: list[Discovered] = []
    skipped: list[dict] = []
    ctx = await browser.new_context(bypass_csp=True)
    try:
        while queue and len(pages) < max_pages:
            item = queue.pop(0)
            if rp is not None and not rp.can_fetch(UA, item.url):
                skipped.append({"url": item.url, "reason": "robots.txt"})
                continue
            page = await ctx.new_page()
            page.set_default_timeout(nav_timeout_ms)
            try:
                resp = await page.goto(item.url, wait_until="load")
                ctype = ((resp.headers.get("content-type") if resp else "") or "").split(";")[0].lower()
                if resp is None or resp.status >= 400 or ctype not in ("text/html", "application/xhtml+xml"):
                    skipped.append({"url": item.url, "reason": f"HTTP {resp.status if resp else '?'} {ctype}"})
                    continue
                final = normalize(page.url)
                if final != item.url:  # redirect: dedupe on the landing URL
                    if final in seen and final != item.url and item.url != seed:
                        skipped.append({"url": item.url, "reason": f"redirect to seen {final}"})
                        continue
                    seen.add(final)
                    item = Discovered(final, item.depth)
                pages.append(item)
                if item.depth < max_depth:
                    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    for h in sorted({normalize(urljoin(page.url, h)) for h in hrefs}):
                        if h not in seen and in_scope(h, origin):
                            seen.add(h)
                            queue.append(Discovered(h, item.depth + 1))
            except Exception as e:
                skipped.append({"url": item.url, "reason": f"{type(e).__name__}: {str(e).splitlines()[0]}"})
            finally:
                await page.close()
    finally:
        await ctx.close()
    if queue:
        skipped += [{"url": q.url, "reason": "max_pages reached"} for q in queue]
    return pages, skipped

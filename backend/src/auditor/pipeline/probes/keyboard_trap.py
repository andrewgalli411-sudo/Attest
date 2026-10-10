from __future__ import annotations

from playwright.async_api import Page

from ._focus import active_selector, enumerate_interactive, reset_focus
from .base import ProbeResult

STUCK_LIMIT = 5


async def keyboard_trap(page: Page) -> ProbeResult:
    """SC 2.1.2 (No Keyboard Trap): focus must be able to leave every component via keyboard.

    Fail evidence: Tab revisits a closed set of elements (or stays on one) without leaving the
    document or reaching all interactive elements, AND Escape / Shift+Tab do not get out either.
    """
    inter, frames = await enumerate_interactive(page)
    expected = set(inter.expected)
    await reset_focus(page)
    if not expected:
        return ProbeResult("keyboard_trap", ["2.1.2"], "inconclusive", [{"kind": "no_focusables"}])

    seen: list[str] = []
    last, stuck = None, 0
    ended = "cap"
    for _ in range(min(8000, len(expected) * 2 + 20)):
        await page.keyboard.press("Tab")
        cur = await active_selector(page.main_frame, frames)
        if cur is None:
            ended = "left_document"
            break
        stuck = stuck + 1 if cur == last else 0
        last = cur
        if stuck >= STUCK_LIMIT or (cur in seen and cur != seen[-1] and len(set(seen)) < len(expected)):
            ended = "stuck" if stuck >= STUCK_LIMIT else "cycle"
            break
        if cur not in seen:
            seen.append(cur)
        elif len(set(seen)) >= len(expected):
            ended = "full_cycle"
            break

    obs = [{"kind": "tab_loop_ended", "reason": ended, "visited": len(set(seen)), "expected": len(expected)}]
    # A browser leaves the document after the last element; wrapping means the page intercepts Tab,
    # so "full_cycle" is a trap *candidate* and must prove an escape route like the others.
    if ended == "left_document":
        return ProbeResult("keyboard_trap", ["2.1.2"], "pass", obs)
    if ended == "cap":
        return ProbeResult("keyboard_trap", ["2.1.2"], "inconclusive", obs)

    # Candidate trap: try the documented escape routes before failing.
    trapped_set = set(seen)
    for name, keys in (("Escape", ["Escape", "Tab"]), ("Shift+Tab", ["Shift+Tab", "Shift+Tab"])):
        for k in keys:
            await page.keyboard.press(k)
        cur = await active_selector(page.main_frame, frames)
        escaped = cur is None or cur not in trapped_set
        obs.append({"kind": "escape_attempt", "keys": name, "escaped": escaped, "focus": cur})
        if escaped:
            return ProbeResult("keyboard_trap", ["2.1.2"], "pass", obs)
    obs.append({"kind": "trapped_elements", "selectors": sorted(trapped_set)})
    return ProbeResult("keyboard_trap", ["2.1.2"], "fail", obs)

from __future__ import annotations

from playwright.async_api import Page

from ._focus import active_selector, enumerate_interactive, reset_focus
from .base import ProbeResult

MIN_TABS = 100
MAX_TABS = 3000


async def keyboard_nav(page: Page) -> ProbeResult:
    """SC 2.1.1 (Keyboard): every visible interactive element reachable via Tab."""
    inter, frames = await enumerate_interactive(page)
    expected = inter.expected
    await reset_focus(page)

    cap = min(MAX_TABS, max(MIN_TABS, int(len(expected) * 1.5) + 10))
    sequence: list[str] = []
    seen: set[str] = set()
    ended = "cap"
    for _ in range(cap):
        await page.keyboard.press("Tab")
        cur = await active_selector(page.main_frame, frames)
        if cur is None:
            ended = "left_document"
            break
        if cur in seen:
            ended = "cycle"
            break
        seen.add(cur)
        sequence.append(cur)

    unreachable = sorted(s for s in expected if s not in seen)
    obs = (
        [
            {"kind": "focus_sequence", "selectors": sequence},
            {"kind": "expected_interactive", "count": len(expected)},
            {"kind": "tab_loop_ended", "reason": ended, "cap": cap},
        ]
        + [{"kind": "frame_skipped", "frame": i} for i in inter.skipped_frames]
        + [{"kind": "unreachable", "selector": s, "element": expected[s]} for s in unreachable]
    )
    if not expected or inter.skipped_frames or (ended == "cap" and unreachable):
        return ProbeResult("keyboard_nav", ["2.1.1"], "inconclusive", obs)
    return ProbeResult("keyboard_nav", ["2.1.1"], "fail" if unreachable else "pass", obs)

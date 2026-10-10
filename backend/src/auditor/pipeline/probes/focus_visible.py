from __future__ import annotations

from playwright.async_api import Page

from ._focus import enumerate_interactive, reset_focus
from .base import ProbeResult

MAX_STOPS = 150

_JS_STYLE = """
el => { const c = getComputedStyle(el);
  return {outline: c.outlineStyle + '|' + c.outlineWidth + '|' + c.outlineColor,
          shadow: c.boxShadow, border: c.borderTopColor + c.borderTopWidth + c.borderTopStyle,
          bg: c.backgroundColor, color: c.color, deco: c.textDecorationLine, tf: c.transform}; }
"""

_JS_STOP = """() => { const el = window.__deepActive(); (window.__stops = window.__stops || []).push(el);
  return [window.__sel(el), (%s)(el)]; }""" % _JS_STYLE

_JS_BASELINE = """() => { document.activeElement.blur(); return window.__stops.map(e => (%s)(e)); }""" % _JS_STYLE


def _indicator(base: dict, foc: dict) -> bool:
    o_style, o_w, _ = foc["outline"].split("|")
    if o_style not in ("none", "hidden") and o_w not in ("0px", "0"):
        return True
    if foc["shadow"] != "none" and foc["shadow"] != base["shadow"]:
        return True
    return any(foc[k] != base[k] for k in ("border", "bg", "color", "deco", "tf"))


async def focus_visible(page: Page) -> ProbeResult:
    """SC 2.4.7 (Focus Visible): keyboard focus produces a visible style change at every stop.

    Deterministic: compares computed styles focused vs. unfocused (no pixel diffs, which are
    noisy because of caret blink / animations). Main frame only; frames are reported.
    """
    inter, frames = await enumerate_interactive(page)
    obs: list[dict] = [{"kind": "frame_skipped", "frame": i} for i in range(1, len(frames))]
    main = {s for s in inter.expected if not s.startswith("frame[")}
    if not main:
        return ProbeResult("focus_visible", ["2.4.7"], "inconclusive", obs + [{"kind": "no_focusables"}])
    await reset_focus(page)
    await page.evaluate("() => { window.__stops = []; }")

    stops: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for _ in range(min(MAX_STOPS, len(main) + 5)):
        await page.keyboard.press("Tab")
        got = await page.evaluate(
            "() => (window.__deepActive() && window.__deepActive() !== document.body) ? true : false"
        )
        if not got:
            break
        sel, style = await page.evaluate(_JS_STOP)
        if sel in seen:
            break
        seen.add(sel)
        stops.append((sel, style))
    baselines = await page.evaluate(_JS_BASELINE)

    failing = []
    for (sel, foc), base in zip(stops, baselines, strict=True):
        if not _indicator(base, foc):
            failing.append({"kind": "no_visible_focus", "selector": sel, "focused": foc, "unfocused": base})
    obs += [{"kind": "stops_checked", "count": len(stops)}] + failing
    if not stops:
        return ProbeResult("focus_visible", ["2.4.7"], "inconclusive", obs)
    return ProbeResult("focus_visible", ["2.4.7"], "fail" if failing else "pass", obs)

from __future__ import annotations

from playwright.async_api import Page

from ._ax import ax_for_expr
from ._focus import enumerate_interactive
from .base import ProbeResult

MAX_ELEMENTS = 300
_NEEDS_NAME = {"button", "link", "checkbox", "radio", "textbox", "combobox", "menuitem", "tab",
               "switch", "slider", "searchbox", "spinbutton", "option"}
_GENERIC = {None, "generic", "none", "presentation", "StaticText", "group"}


async def name_role_value(page: Page) -> ProbeResult:
    """SC 4.1.2 (Name, Role, Value) + 2.5.3 (Label in Name) from the browser's AX tree."""
    inter, _ = await enumerate_interactive(page)
    main = [s for s in inter.expected if not s.startswith("frame[")]
    obs: list[dict] = [{"kind": "frame_skipped", "frame": i} for i in inter.skipped_frames]
    obs.append({"kind": "elements_checked", "count": min(len(main), MAX_ELEMENTS)})
    if not main:
        return ProbeResult("name_role_value", ["4.1.2", "2.5.3"], "inconclusive", obs)

    # window.__els is index-aligned with enumeration order of the main document.
    count = await page.evaluate("() => window.__els.length")
    fails = []
    for i in range(min(count, MAX_ELEMENTS)):
        ax = await ax_for_expr(page, f"window.__els[{i}]")
        info = await page.evaluate(
            """i => { const e = window.__els[i];
              return {sel: window.__sel(e), tag: e.tagName.toLowerCase(),
                      text: (e.innerText || e.value || '').trim().toLowerCase()}; }""", i)
        if info["sel"] not in inter.expected:  # element belongs to another frame/doc
            continue
        if ax["ignored"] or ax["role"] in _GENERIC:
            fails.append({"kind": "missing_role", "selector": info["sel"], "tag": info["tag"], "role": ax["role"]})
            continue
        if ax["role"] in _NEEDS_NAME and not ax["name"]:
            fails.append({"kind": "missing_name", "selector": info["sel"], "role": ax["role"]})
        elif info["text"] and ax["name"] and info["tag"] != "input" and info["text"] not in ax["name"].lower():
            fails.append({"kind": "label_not_in_name", "sc": "2.5.3", "selector": info["sel"],
                          "visible": info["text"], "name": ax["name"]})
    obs += fails
    return ProbeResult("name_role_value", ["4.1.2", "2.5.3"], "fail" if fails else "pass", obs)

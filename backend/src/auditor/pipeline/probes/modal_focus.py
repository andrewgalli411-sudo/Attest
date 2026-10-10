from __future__ import annotations

from playwright.async_api import Page

from .base import ProbeResult

_TRIGGERS = "[aria-haspopup=dialog],[data-toggle=modal],[data-bs-toggle=modal],[data-modal-trigger]"
_DIALOG = "[role=dialog],[role=alertdialog],dialog[open]"

_JS_STATE = """
() => { const d = [...document.querySelectorAll('%s')].find(e => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; });
  let a = document.activeElement;
  return {open: !!d, inside: !!(d && a && d.contains(a)), active: a ? (a.id || a.tagName) : null}; }
""" % _DIALOG


async def modal_focus(page: Page) -> ProbeResult:
    """SC 2.4.3 / 2.1.2 / 4.1.2: dialog takes focus, keeps it, closes on Escape, returns focus."""
    n = await page.evaluate(f"() => document.querySelectorAll('{_TRIGGERS}').length")
    if n == 0:
        return ProbeResult("modal_focus", ["2.4.3", "2.1.2", "4.1.2"], "inconclusive",
                           [{"kind": "no_dialog_triggers"}])
    obs, fails = [], []
    for i in range(min(n, 10)):
        await page.reload(wait_until="load")
        tag = f"trigger[{i}]"
        await page.evaluate(f"() => {{ const t = document.querySelectorAll('{_TRIGGERS}')[{i}]; window.__trig = t; t.focus(); }}")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        st = await page.evaluate(_JS_STATE)
        if not st["open"]:
            obs.append({"kind": "dialog_not_opened_by_keyboard", "trigger": tag})
            fails.append({"kind": "trigger_not_keyboard_operable", "sc": "2.1.1", "trigger": tag})
            continue
        if not st["inside"]:
            fails.append({"kind": "focus_not_moved_into_dialog", "sc": "2.4.3", "trigger": tag})
        for _ in range(12):  # focus must stay inside for modal dialogs
            await page.keyboard.press("Tab")
        st2 = await page.evaluate(_JS_STATE)
        modal = await page.evaluate(f"() => {{ const d = document.querySelector('{_DIALOG}'); return !!d && (d.getAttribute('aria-modal') === 'true' || d.tagName === 'DIALOG'); }}")
        if modal and st2["open"] and not st2["inside"]:
            fails.append({"kind": "focus_escaped_modal", "sc": "2.4.3", "trigger": tag})
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        st3 = await page.evaluate(_JS_STATE)
        if st3["open"]:
            fails.append({"kind": "escape_does_not_close", "sc": "2.1.2", "trigger": tag})
        else:
            returned = await page.evaluate("() => document.activeElement === window.__trig")
            if not returned:
                fails.append({"kind": "focus_not_returned_to_trigger", "sc": "2.4.3", "trigger": tag})
        obs.append({"kind": "trigger_checked", "trigger": tag})
    obs += fails
    return ProbeResult("modal_focus", ["2.4.3", "2.1.2", "4.1.2"], "fail" if fails else "pass", obs)

from __future__ import annotations

from playwright.async_api import Page

from ._ax import ax_for_expr
from .base import ProbeResult

_JS_COLLECT = """
() => {
  const sel = window.__sel || (e => e.tagName.toLowerCase());
  const all = [];
  const walk = root => {
    for (const e of root.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),select,textarea')) all.push(e);
    for (const e of root.querySelectorAll('*')) if (e.shadowRoot) walk(e.shadowRoot);
  };
  walk(document);
  window.__ctl = all;
  return all.map((e, i) => ({i, sel: sel(e), type: e.type || e.tagName.toLowerCase(),
    label: !!(e.labels && e.labels.length), aria: !!(e.getAttribute('aria-label') || e.getAttribute('aria-labelledby')),
    title: !!e.getAttribute('title'), placeholder: !!e.getAttribute('placeholder'), autocomplete: e.getAttribute('autocomplete')}));
}
"""

_JS_ERRORS = """
() => window.__ctl.filter(e => e.getAttribute('aria-invalid') === 'true').map(e => {
  const ids = (e.getAttribute('aria-errormessage') || e.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
  const text = ids.map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim();
  return {sel: window.__sel ? window.__sel(e) : '', described: ids.length > 0, text};
})
"""


async def form_labels(page: Page) -> ProbeResult:
    """SC 1.3.1 / 3.3.2 / 4.1.2 labels + 3.3.1 error association."""
    from ._focus import enumerate_interactive

    await enumerate_interactive(page)  # installs window.__sel
    controls = await page.evaluate(_JS_COLLECT)
    obs: list[dict] = [{"kind": "controls_checked", "count": len(controls)}]
    if not controls:
        return ProbeResult("form_labels", ["1.3.1", "3.3.2", "4.1.2", "3.3.1"], "inconclusive", obs)

    fails = []
    for c in controls:
        ax = await ax_for_expr(page, f"window.__ctl[{c['i']}]")
        if not ax["name"]:
            fails.append({"kind": "no_accessible_name", "sc": "3.3.2", "selector": c["sel"],
                          "placeholder_only": c["placeholder"]})
        elif not (c["label"] or c["aria"]):
            fails.append({"kind": "name_from_weak_source", "sc": "1.3.1", "selector": c["sel"],
                          "name": ax["name"], "source": "title/placeholder"})

    # Try submitting: custom validation scripts typically set aria-invalid + a message.
    await page.evaluate("""() => { const f = document.querySelector('form');
        if (f) { f.addEventListener('submit', e => e.preventDefault()); const b = f.querySelector('[type=submit],button:not([type])'); if (b) b.click(); } }""")
    await page.wait_for_timeout(150)
    for e in await page.evaluate(_JS_ERRORS):
        if not e["described"] or not e["text"]:
            fails.append({"kind": "error_not_associated", "sc": "3.3.1", "selector": e["sel"]})
    obs += fails
    return ProbeResult(
        "form_labels", ["1.3.1", "3.3.2", "4.1.2", "3.3.1"], "fail" if fails else "pass", obs
    )

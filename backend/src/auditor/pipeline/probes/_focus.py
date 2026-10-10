"""Shared focus helpers: enumerate interactive elements, read focus, walk Tab order."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Frame, Page

# Installs window.__sel and enumerates visible interactive elements (piercing open shadow roots).
JS_ENUM = """
() => {
  const sel = el => {
    const parts = [];
    while (el && el.nodeType === 1) {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      const root = el.getRootNode();
      el = el.parentElement || (root instanceof ShadowRoot ? (parts.unshift('>>>'), root.host) : null);
    }
    return parts.join(' > ').replace(/ > >>> > /g, ' >>> ');
  };
  window.__sel = sel;
  window.__deepActive = () => {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return el;
  };
  const visible = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const q = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[tabindex],' +
            '[role=button],[role=link],[role=checkbox],[role=menuitem],[role=tab],[onclick]';
  const out = [], els = [];
  window.__els = els;
  const walk = root => {
    for (const el of root.querySelectorAll(q)) {
      if (el.disabled || el.getAttribute('tabindex') === '-1' || el.closest('[inert]') || !visible(el)) continue;
      if (/^i?frame$/i.test(el.tagName)) continue;
      els.push(el);
      out.push([sel(el), el.tagName.toLowerCase() + (el.getAttribute('role') ? '[role=' + el.getAttribute('role') + ']' : '')]);
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  return out;
}
"""

# One round trip: [selector, tag] of deepest active element, or null when focus is on body.
_JS_STATE = """
() => {
  const el = window.__deepActive && window.__deepActive();
  if (!el || el === document.body || el === document.documentElement) return null;
  return [window.__sel(el), el.tagName.toLowerCase()];
}
"""


@dataclass
class Interactive:
    expected: dict[str, str] = field(default_factory=dict)
    skipped_frames: list[int] = field(default_factory=list)


async def enumerate_interactive(page: Page) -> tuple[Interactive, list[Frame]]:
    frames = list(page.frames)
    inter = Interactive()
    for i, fr in enumerate(frames):
        try:
            for sel, desc in await fr.evaluate(JS_ENUM):
                inter.expected[sel if i == 0 else f"frame[{i}]:{sel}"] = desc
        except Exception:  # detached/inaccessible frame: skip but report it
            inter.skipped_frames.append(i)
    return inter, frames


async def active_selector(frame: Frame, frames: list[Frame]) -> str | None:
    """Deepest focused element's selector (frame-prefixed), descending into iframes."""
    state = await frame.evaluate(_JS_STATE)
    if state is None:
        return None
    sel, tag = state
    if tag in ("iframe", "frame"):
        handle = await frame.evaluate_handle("() => window.__deepActive()")
        child = await handle.as_element().content_frame()  # type: ignore[union-attr]
        if child is not None and child in frames:
            inner = await active_selector(child, frames)
            if inner is not None:
                return inner
        return None
    idx = frames.index(frame)
    return sel if idx == 0 else f"frame[{idx}]:{sel}"


async def reset_focus(page: Page) -> None:
    await page.evaluate(
        "() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }"
    )

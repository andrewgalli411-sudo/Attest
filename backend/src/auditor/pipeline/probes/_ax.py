"""Accessible name/role via the browser's own accessibility tree (CDP)."""

from __future__ import annotations

from playwright.async_api import Page


async def ax_for_expr(page: Page, expr: str) -> dict:
    """Role/name/states for the element a JS expression evaluates to (works through shadow DOM)."""
    cdp = await page.context.new_cdp_session(page)
    try:
        ev = await cdp.send("Runtime.evaluate", {"expression": expr})
        oid = ev.get("result", {}).get("objectId")
        if not oid:
            return {"role": None, "name": None, "ignored": True}
        tree = await cdp.send("Accessibility.getPartialAXTree", {"objectId": oid, "fetchRelatives": False})
        node = tree["nodes"][0]
        props = {p["name"]: p["value"].get("value") for p in node.get("properties", [])}
        return {
            "role": (node.get("role") or {}).get("value"),
            "name": ((node.get("name") or {}).get("value") or "").strip(),
            "ignored": bool(node.get("ignored")),
            "props": props,
        }
    finally:
        await cdp.detach()

"""User-defined flows: replayable step lists that reach an application state."""

from __future__ import annotations

from playwright.async_api import Page

STEP_TIMEOUT_MS = 5_000
ACTIONS = {"goto", "click", "fill", "press", "wait_for", "select"}


class FlowError(Exception):
    pass


def validate_steps(steps: list[dict]) -> None:
    for i, s in enumerate(steps):
        if s.get("action") not in ACTIONS:
            raise FlowError(f"step {i}: unknown action {s.get('action')!r}")
        need = {"goto": ["url"], "click": ["selector"], "fill": ["selector", "value"],
                "press": ["key"], "wait_for": ["selector"], "select": ["selector", "value"]}
        for k in need[s["action"]]:
            if k not in s:
                raise FlowError(f"step {i} ({s['action']}): missing {k!r}")


async def replay_steps(page: Page, steps: list[dict]) -> None:
    validate_steps(steps)
    for i, s in enumerate(steps):
        a = s["action"]
        try:
            if a == "goto":
                await page.goto(s["url"], wait_until="load")
            elif a == "click":
                await page.click(s["selector"], timeout=STEP_TIMEOUT_MS)
            elif a == "fill":
                await page.fill(s["selector"], s["value"], timeout=STEP_TIMEOUT_MS)
            elif a == "select":
                await page.select_option(s["selector"], s["value"], timeout=STEP_TIMEOUT_MS)
            elif a == "press":
                await page.keyboard.press(s["key"])
            elif a == "wait_for":
                await page.wait_for_selector(s["selector"], timeout=STEP_TIMEOUT_MS)
        except Exception as e:
            raise FlowError(f"step {i} ({a}) failed: {type(e).__name__}: {str(e).splitlines()[0]}") from e
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

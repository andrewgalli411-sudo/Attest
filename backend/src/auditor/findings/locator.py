"""Durable element fingerprints and the relocation ladder used to prove "same element, twice".

A Finding stores a Locator when caught. After a fix, the DOM legitimately changes (new label,
new ids, wrapper elements), so relocation must tolerate intended edits yet never silently match
a *different* element. Rules: a candidate must be unique within its rung, score >= THRESHOLD, and
lookalike candidates must be separated by MARGIN (bbox proximity alone can never break a tie).
Ambiguous or missing -> not relocated; callers must treat that as "inconclusive", never "pass".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from playwright.async_api import ElementHandle, Page

THRESHOLD = 0.85
MARGIN = 0.15  # must exceed the bbox weight so position alone cannot decide between lookalikes
RUNGS = ("stable_css", "text_landmark", "ancestry", "positional", "fuzzy")
_JS = (Path(__file__).with_name("locator.js")).read_text()

# Attributes that identify WHAT an element is. If both fingerprints carry one and the values differ (or
# it vanished and was not an expected edit), the candidates are different elements no matter how similar
# their surroundings look. This is the guard against relocating onto a lookalike sibling.
IDENTITY_ATTRS = ("name", "type", "href", "src", "role", "data-testid", "data-test", "data-cy", "data-qa")
CONFLICT_CAP = 0.5

# score weights (sum 1.0). Accessible name/role are deliberately NOT scored: fixing them is the point.
W_TEXT, W_LANDMARK, W_ANCESTRY, W_HTML, W_BBOX = 0.25, 0.20, 0.20, 0.25, 0.10


@dataclass
class Relocation:
    found: bool
    method: str | None = None
    score: float = 0.0
    candidates: int = 0
    reason: str = ""
    handle: ElementHandle | None = field(default=None, repr=False)
    tried: list[dict] = field(default_factory=list)


async def install(page: Page) -> None:
    await page.evaluate(f"({_JS})()")


async def build(handle: ElementHandle, ignore_attrs: list[str] | None = None) -> dict:
    """Fingerprint an element. Call install(page) first."""
    return await handle.evaluate("(el, ig) => window.__loc.build(el, ig)", ignore_attrs or [])


def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _seq(a: list, b: list) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _bbox(a: dict, b: dict) -> float:
    vw, vh = a["viewport"]
    dx = abs((a["x"] + a["w"] / 2) - (b["x"] + b["w"] / 2)) / max(vw, 1)
    dy = abs((a["y"] + a["h"] / 2) - (b["y"] + b["h"] / 2)) / max(vh, 1)
    return max(0.0, 1.0 - (dx + dy))


def similarity(stored: dict, cand: dict) -> float:
    """0..1. Different tag is a hard zero. Both sides must be built with the same ignore list."""
    if stored["tag"] != cand["tag"]:
        return 0.0
    score = (
        W_TEXT * _ratio(stored["visible_text"], cand["visible_text"])
        + W_LANDMARK * _seq(stored["landmark_path"], cand["landmark_path"])
        + W_ANCESTRY * _seq(stored["ancestry"], cand["ancestry"])
        + W_HTML * _ratio(stored["html_norm"], cand["html_norm"])
        + W_BBOX * _bbox(stored["bbox"], cand["bbox"])
    )
    if _identity_conflict(stored.get("attrs"), cand.get("attrs")):
        return min(score, CONFLICT_CAP)
    return score


def _identity_conflict(stored: dict | None, cand: dict | None) -> bool:
    if stored is None or cand is None:  # legacy fingerprint without attrs: html_norm similarity only
        return False
    return any(k in stored and stored[k] != cand.get(k) for k in IDENTITY_ATTRS)


async def relocate(
    page: Page, stored: dict, ignore_attrs: list[str] | None = None, threshold: float = THRESHOLD
) -> Relocation:
    """Walk the ladder; return the first rung that yields one confident, unambiguous element."""
    await install(page)
    ig = ignore_attrs or []
    tried: list[dict] = []
    # The stored fingerprint's html_norm was built without `ig`; rebuild comparison by stripping
    # the same attributes from the stored string so intended edits don't count as a mismatch.
    stored = {**stored, "html_norm": _strip_attrs(stored["html_norm"], ig)}
    if "attrs" in stored:  # expected edits are not identity: drop them from the stored side too
        stored["attrs"] = {k: v for k, v in stored["attrs"].items() if k not in ig}
    for rung in RUNGS:
        cands = await page.evaluate("([s, l, ig]) => window.__loc.collect(s, l, ig)", [rung, stored, ig])
        if not cands:
            tried.append({"rung": rung, "n": 0})
            continue
        scored = sorted(((similarity(stored, c), i) for i, c in enumerate(cands)), reverse=True)
        top, idx = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        tried.append({"rung": rung, "n": len(cands), "top": round(top, 3), "second": round(second, 3)})
        if top < threshold:
            continue
        if len(cands) > 1 and top - second < MARGIN:
            return Relocation(False, rung, top, len(cands),
                              f"ambiguous: {len(cands)} lookalike candidates (margin {top - second:.2f})",
                              tried=tried)
        handle = (await page.evaluate_handle(f"window.__locEls[{idx}]")).as_element()
        return Relocation(True, rung, top, len(cands), "ok", handle, tried)
    return Relocation(False, None, 0.0, 0, "not found: no rung produced a confident candidate", tried=tried)


def _strip_attrs(html_norm: str, ignore: list[str]) -> str:
    if not ignore:
        return html_norm
    tag, _, rest = html_norm.partition(" ")
    kept = [a for a in rest.split(" ") if a and a.split("=", 1)[0] not in ignore]
    return tag + " " + " ".join(kept)

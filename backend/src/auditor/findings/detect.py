"""Detect: turn a driven page state into Findings that each carry their own evidence.

One pristine page is used for evidence (probes mutate focus/state, so they run in their own isolated
contexts, and their element-level observations are resolved back onto the pristine page).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from playwright.async_api import Browser, ElementHandle, Page
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..models import Finding, FindingEvidence, FindingLocator, FindingStatus
from ..pipeline.axe_runner import run_axe
from ..pipeline.prober import PROBES, PageUnauditable, _guarded, _open
from ..pipeline.probes._ax import ax_for_expr
from ..storage.evidence_store import EvidenceStore
from . import locator as loc
from .severity import axe_severity, probe_severity

log = logging.getLogger(__name__)
PAD = 16
MAX_CLIP = (1400, 700)  # whole-page elements (html/body) are clipped to the top of the page
CAPTION_PAD = 30  # extra room above the element for the accessibility caption chip
SNIPPET_MAX = 1500


@dataclass
class RawFinding:
    rule_id: str
    sc_ids: list[str]
    severity: object
    source: str  # axe | probe
    fingerprint: dict | None  # None -> element could not be located (nested frame/shadow target)
    axe_target: list | None
    detail: dict
    dedupe_key: str = ""
    evidence: dict[str, bytes] = field(default_factory=dict)  # kind -> blob
    snippet: str = ""


def identity(fp: dict) -> str:
    """Position-independent element identity, so the same element dedupes across runs and fixes."""
    key = fp["stable_css"] or json.dumps(
        [fp["tag"], fp["visible_text"], fp["landmark_path"], fp["ancestry"]], sort_keys=True)
    return key


def make_dedupe_key(rule_id: str, fp: dict | None, state_ref: dict, extra: str = "") -> str:
    ident = identity(fp) if fp else extra
    blob = json.dumps([rule_id, ident, state_ref.get("flow_name"), state_ref.get("label")], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


async def _ax_caption(page: Page, el: ElementHandle) -> str:
    """What assistive tech gets for this element, e.g. 'role: textbox | name: (none)'."""
    await el.evaluate("e => { window.__evEl = e; }")
    try:
        ax = await ax_for_expr(page, "window.__evEl")
    except Exception:  # noqa: BLE001 - caption is a nicety; never fail evidence capture over it
        return ""
    name = ax.get("name") or "(none)"
    cap = f"role: {ax.get('role') or 'none'} | accessible name: {name}"
    if await el.evaluate("e => e === document.documentElement"):
        # the page language changes nothing visible or in the role/name; state it so before/after differ
        lang = await el.evaluate("e => e.getAttribute('lang')")
        cap = f"page language: {lang or '(not declared)'}"
    return cap


_BOX_JS = """([b, caption]) => {
  const d = document.createElement('div'); d.id = '__a11y_box';
  d.style.cssText = `position:absolute;left:${b.x-4}px;top:${b.y-4}px;width:${b.w+8}px;height:${b.h+8}px;
    border:3px solid #e11d48;box-shadow:0 0 0 2px #fff;z-index:2147483647;pointer-events:none;box-sizing:border-box`;
  document.body.appendChild(d);
  if (caption) {
    const c = document.createElement('div'); c.id = '__a11y_cap'; c.textContent = caption;
    c.style.cssText = `position:absolute;left:${b.x-4}px;top:${Math.max(0, b.y-4-22)}px;z-index:2147483647;
      background:#e11d48;color:#fff;font:600 12px/18px system-ui,sans-serif;padding:2px 6px;border-radius:3px;
      white-space:nowrap;pointer-events:none`;
    document.body.appendChild(c);
  }
}"""
_CLEAR_JS = "() => { for (const id of ['__a11y_box', '__a11y_cap']) document.getElementById(id)?.remove(); }"


async def _evidence_for(page: Page, el: ElementHandle, fp: dict) -> tuple[dict[str, bytes], str]:
    """Clip + annotated full-page screenshots and a DOM snippet for one element.

    Both screenshots carry a caption with the element's role and accessible name: an accessibility fix
    usually changes nothing visible, so the caption is what makes a before/after pair show the difference."""
    await el.scroll_into_view_if_needed()
    await page.evaluate("() => window.scrollTo(0, window.scrollY)")  # settle
    fp = await loc.build(el)  # fresh doc coordinates after scrolling
    b = fp["bbox"]
    caption = await _ax_caption(page, el)
    clip = {"x": max(0, b["x"] - PAD), "y": max(0, b["y"] - PAD - CAPTION_PAD),
            "width": min(MAX_CLIP[0], max(1, b["w"] + 2 * PAD)),
            "height": min(MAX_CLIP[1], max(1, b["h"] + 2 * PAD + CAPTION_PAD))}
    clip["width"] = max(clip["width"], 7 * len(caption) + 24)  # keep the whole caption inside the clip
    out: dict[str, bytes] = {}
    await page.evaluate(_BOX_JS, [b, caption])
    try:
        out["shot_clip"] = await page.screenshot(clip=clip, full_page=True, animations="disabled")
        out["shot_full_annotated"] = await page.screenshot(full_page=True, animations="disabled")
    finally:
        await page.evaluate(_CLEAR_JS)
    snippet = await el.evaluate("e => (e.parentElement || e).outerHTML")
    return out, snippet[:SNIPPET_MAX]


async def _resolve_css(page: Page, css_path: str) -> ElementHandle | None:
    h = await page.evaluate_handle("p => window.__loc.resolve(p)", css_path)
    return h.as_element()


async def _axe_findings(page: Page, state_ref: dict, with_evidence: bool = True) -> list[RawFinding]:
    res = await run_axe(page)
    out: list[RawFinding] = []
    for v in res["violations"]:
        for node in v["nodes"]:
            tgt = node["target"]
            fp, ev, snip = None, {}, node["html"]
            if len(tgt) == 1 and isinstance(tgt[0], str):
                el = await page.query_selector(tgt[0])
                if el is not None:
                    fp = await loc.build(el)
                    if with_evidence:
                        ev, snip = await _evidence_for(page, el, fp)
            rf = RawFinding(
                rule_id=v["rule"], sc_ids=v["sc"], severity=axe_severity(v["impact"]), source="axe",
                fingerprint=fp, axe_target=tgt, detail={"axe_node": node, "impact": v["impact"]},
                evidence=ev, snippet=snip)
            rf.dedupe_key = make_dedupe_key(rf.rule_id, fp, state_ref, extra=json.dumps(tgt))
            out.append(rf)
    return out


async def _probe_findings(
    browser: Browser, page: Page, url: str, state_ref: dict, steps, auth, with_evidence: bool = True
) -> list[RawFinding]:
    out: list[RawFinding] = []
    for probe in PROBES:
        try:
            pr = await _guarded(browser, url, probe, steps, auth)
        except Exception as e:  # noqa: BLE001 - one broken probe must not abort detection
            log.warning("probe %s crashed on %s: %s", probe.__name__, url, e)
            continue
        if pr.verdict != "fail":
            continue
        for ob in pr.observations:
            kind = ob.get("kind")
            sel = ob.get("selector") or (ob.get("selectors") or [None])[0]
            if kind in (None, "focus_sequence", "expected_interactive", "tab_loop_ended", "frame_skipped",
                        "elements_checked", "controls_checked", "stops_checked", "trigger_checked",
                        "escape_attempt") or sel is None:
                continue
            fp, ev, snip = None, {}, ""
            if sel and not sel.startswith("frame["):
                el = await _resolve_css(page, sel)
                if el is not None:
                    fp = await loc.build(el)
                    if with_evidence:
                        ev, snip = await _evidence_for(page, el, fp)
            sc = ob.get("sc") or (pr.sc_ids[0] if pr.sc_ids else "")
            rf = RawFinding(
                rule_id=f"probe:{pr.probe}:{kind}", sc_ids=[sc] if sc else pr.sc_ids,
                severity=probe_severity(pr.probe, kind), source="probe", fingerprint=fp,
                axe_target=None, detail={"observation": ob, "probe": pr.probe}, evidence=ev, snippet=snip)
            rf.dedupe_key = make_dedupe_key(rf.rule_id, fp, state_ref, extra=sel)
            out.append(rf)
    return out


async def detect_state(
    browser: Browser, url: str, *, steps: list[dict] | None = None, auth: dict | None = None,
    state_ref: dict | None = None, with_probes: bool = True, with_evidence: bool = True,
) -> list[RawFinding]:
    """One detection pass over a state. Raises PageUnauditable if the state cannot be reached."""
    state_ref = state_ref or {"flow_name": None, "label": None, "steps": steps or []}
    ctx, page, _ = await _open(browser, url, steps, auth)
    try:
        await loc.install(page)
        found = await _axe_findings(page, state_ref, with_evidence)
        if with_probes:
            found += await _probe_findings(browser, page, url, state_ref, steps, auth, with_evidence)
        return found
    finally:
        await ctx.close()


def stable_keys(a: list[RawFinding], b: list[RawFinding]) -> set[str]:
    return {f.dedupe_key for f in a} & {f.dedupe_key for f in b}


def persist_findings(
    sf: sessionmaker, store: EvidenceStore, run_id: str, page_state_id: str, state_ref: dict,
    first: list[RawFinding], second: list[RawFinding] | None = None,
) -> list[str]:
    """Write Findings (+locator +evidence). `second` is the repeat pass used for baseline stability."""
    stable = stable_keys(first, second) if second is not None else set()
    ids: list[str] = []
    seen: set[str] = set()
    with sf() as db:
        for rf in first:
            if rf.dedupe_key in seen:
                continue  # same rule on the same element reported twice in one pass
            seen.add(rf.dedupe_key)
            exists = db.scalar(select(Finding).where(Finding.run_id == run_id,
                                                     Finding.dedupe_key == rf.dedupe_key))
            if exists:
                continue
            f = Finding(
                run_id=run_id, page_state_id=page_state_id, rule_id=rf.rule_id, sc_ids=rf.sc_ids,
                severity=rf.severity, source=rf.source, status=FindingStatus.caught,
                dedupe_key=rf.dedupe_key,
                baseline_stable=(rf.dedupe_key in stable) and rf.fingerprint is not None)
            db.add(f)
            db.flush()
            db.add(FindingLocator(
                finding_id=f.id, fingerprint=rf.fingerprint or {"unlocatable": True},
                axe_target=rf.axe_target, state_ref=state_ref))
            for kind, blob in rf.evidence.items():
                ref, sha = store.put(run_id, page_state_id, f"{f.id}-{kind}.png", blob)
                db.add(FindingEvidence(finding_id=f.id, stage="caught", kind=kind, blob_ref=ref, sha256=sha))
            db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="dom_snippet",
                                   payload={"html": rf.snippet}))
            db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="detector_detail",
                                   payload=json.loads(json.dumps(rf.detail, default=str))))
            ids.append(f.id)
        db.commit()
    return ids


__all__ = ["PageUnauditable", "RawFinding", "detect_state", "persist_findings", "stable_keys"]

"""Re-prove: show that the SAME element failed before and passes after, on the same harness.

Pass requires ALL of: the control run on the unpatched build still fails (so the baseline is real), the
element is relocated on the patched build (unique + similar enough), the rule still APPLIES to it, and the
rule passes on it in every run. Everything else is fail / inconclusive - never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Page

from ..findings import locator as loc
from ..findings.detect import _evidence_for
from ..pipeline.axe_runner import AXE_PATH
from ..pipeline.prober import _open

RUNS = 2

_AXE_ON_ELEMENT = """
async ([el, rule]) => {
  const r = await axe.run(el, {runOnly: {type: 'rule', values: [rule]}, resultTypes: ['violations', 'passes']});
  const hit = n => n.target && n.target.length;
  return {violations: r.violations.filter(v => v.id === rule).reduce((a, v) => a + v.nodes.length, 0),
          passes: r.passes.filter(v => v.id === rule).reduce((a, v) => a + v.nodes.length, 0)};
}
"""


def rebase_url(url: str, base: str) -> str:
    """Replace scheme+host+port of `url` with `base` (each dev-server run gets a new port)."""
    u, b = urlsplit(url), urlsplit(base)
    return urlunsplit((b.scheme, b.netloc, u.path, u.query, u.fragment))


def rebase_steps(steps: list[dict] | None, base: str) -> list[dict] | None:
    if not steps:
        return steps
    return [{**s, "url": rebase_url(s["url"], base)} if s.get("action") == "goto" and "url" in s else s
            for s in steps]


@dataclass
class RunCheck:
    relocated: bool
    method: str | None = None
    score: float = 0.0
    reason: str = ""
    applicable: bool = False  # rule produced a result (pass or violation) on the element
    fails: bool | None = None
    passes: bool | None = None


@dataclass
class BuildCheck:
    runs: list[RunCheck] = field(default_factory=list)
    clip: bytes | None = None
    annotated: bytes | None = None
    snippet: str = ""

    @property
    def relocated(self) -> bool:
        return bool(self.runs) and all(r.relocated for r in self.runs)

    @property
    def score(self) -> float:
        return min((r.score for r in self.runs), default=0.0)

    @property
    def method(self) -> str | None:
        return self.runs[0].method if self.runs else None

    @property
    def consistent(self) -> bool:
        return len({(r.fails, r.passes, r.applicable) for r in self.runs}) <= 1


@dataclass
class ReproveOutcome:
    verdict: str  # pass | fail | inconclusive
    notes: str
    baseline_failed: bool
    relocated: bool
    relocation_score: float
    relocation_method: str | None
    rule_pass: bool
    runs_n: int
    before_png: bytes | None
    after_png: bytes | None
    after_annotated: bytes | None


async def _check_once(page: Page, stored_fp: dict, rule_id: str, ignore_attrs: list[str], want_evidence: bool):
    await loc.install(page)
    rel = await loc.relocate(page, stored_fp, ignore_attrs)
    if not rel.found:
        return RunCheck(False, rel.method, rel.score, rel.reason), None
    await page.add_script_tag(content=AXE_PATH.read_text())
    res = await page.evaluate(_AXE_ON_ELEMENT, [rel.handle, rule_id])
    ev = None
    if want_evidence:
        fp_now = await loc.build(rel.handle)
        ev = await _evidence_for(page, rel.handle, fp_now)
    applicable = (res["violations"] + res["passes"]) > 0
    rc = RunCheck(True, rel.method, rel.score, rel.reason, applicable, res["violations"] > 0,
                  res["passes"] > 0 and res["violations"] == 0)
    return rc, ev


async def check_on_build(
    browser: Browser, base_url: str, *, page_url: str, steps: list[dict] | None, auth: dict | None,
    stored_fp: dict, rule_id: str, ignore_attrs: list[str], runs: int = RUNS,
) -> BuildCheck:
    """Drive to the stored state on one build and test the rule on the relocated element, `runs` times."""
    out = BuildCheck()
    url = rebase_url(page_url, base_url)
    st = rebase_steps(steps, base_url)
    for i in range(runs):
        ctx, page, _ = await _open(browser, url, st, auth)
        try:
            rc, ev = await _check_once(page, stored_fp, rule_id, ignore_attrs, want_evidence=(i == 0))
            out.runs.append(rc)
            if ev is not None and i == 0:
                out.clip, out.snippet = ev[0]["shot_clip"], ev[1]
                out.annotated = ev[0]["shot_full_annotated"]
        finally:
            await ctx.close()
    return out


def decide(control: BuildCheck, after: BuildCheck) -> ReproveOutcome:
    """Pure decision logic: kept separate so every branch is unit-testable without a browser."""
    baseline_failed = control.relocated and control.consistent and bool(control.runs[0].fails)
    common = {
        "baseline_failed": baseline_failed, "relocated": after.relocated, "relocation_score": after.score,
        "relocation_method": after.method, "runs_n": len(after.runs), "before_png": control.clip,
        "after_png": after.clip, "after_annotated": after.annotated}

    def out(verdict: str, notes: str, rule_pass: bool = False) -> ReproveOutcome:
        return ReproveOutcome(verdict=verdict, notes=notes, rule_pass=rule_pass, **common)

    if not control.relocated:
        return out("inconclusive", "control: could not re-locate the element on the unpatched build")
    if not control.consistent:
        return out("inconclusive", "control: unpatched build gave inconsistent results across runs (flaky)")
    if not baseline_failed:
        return out("inconclusive", "control: the unpatched build did not reproduce the failure; no valid 'before'")
    if not after.relocated:
        why = next((r.reason for r in after.runs if not r.relocated), "no runs")
        return out("inconclusive", f"relocation failed on the patched build: {why}")
    if not after.consistent:
        return out("inconclusive", "patched build gave inconsistent results across runs (flaky)")
    if any(r.fails for r in after.runs):
        return out("fail", "the originating rule still fails on the same element after the fix")
    if not all(r.applicable for r in after.runs):
        return out("inconclusive", "the rule no longer applies to the element (it may have been removed/replaced)")
    return out("pass", f"same element re-located (similarity {after.score:.2f}, via {after.method}); "
                       f"rule passes in {len(after.runs)}/{len(after.runs)} runs; unpatched control still fails",
               rule_pass=True)

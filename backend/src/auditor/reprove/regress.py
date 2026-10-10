"""Regression guard: did the fix introduce findings that were not there before?

Identity is attribute-insensitive on purpose (rule + tag + visible text + landmark + ancestry): a fix may
legitimately add an id/label to an element, and that must not make its *other* issues look new.
Comparison is multiset-based so identical siblings are counted, not merged.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from playwright.async_api import Browser

from ..findings import locator as loc
from ..findings.detect import _axe_findings, _probe_findings
from ..pipeline.axe_runner import run_axe
from ..pipeline.prober import _open
from .reprove import rebase_steps, rebase_url


@dataclass
class Snapshot:
    weak: Counter = field(default_factory=Counter)
    passes: dict[str, int] = field(default_factory=dict)  # rule -> passing node count
    rules_checked: list[str] = field(default_factory=list)


@dataclass
class RegressionReport:
    new_findings: list[str]
    regressions_count: int
    lost_passes: dict[str, int]
    verdict: str  # pass | fail
    criteria_checked: list[str]


def weak_key(rule_id: str, fp: dict | None, extra: str = "") -> str:
    if not fp or fp.get("unlocatable"):
        return json.dumps([rule_id, "unlocated", extra], sort_keys=True)
    return json.dumps([rule_id, fp["tag"], fp["visible_text"], fp["landmark_path"], fp["ancestry"]], sort_keys=True)


async def snapshot_state(
    browser: Browser, base_url: str, *, page_url: str, steps: list[dict] | None, auth: dict | None,
    with_probes: bool = True,
) -> Snapshot:
    url, st = rebase_url(page_url, base_url), rebase_steps(steps, base_url)
    ctx, page, _ = await _open(browser, url, st, auth)
    snap = Snapshot()
    try:
        await loc.install(page)
        state_ref = {"flow_name": None, "label": None, "steps": st or []}
        found = await _axe_findings(page, state_ref, with_evidence=False)
        if with_probes:
            found += await _probe_findings(browser, page, url, state_ref, st, auth, with_evidence=False)
        for f in found:
            snap.weak[weak_key(f.rule_id, f.fingerprint, json.dumps(f.axe_target))] += 1
        axe = await run_axe(page)
        snap.passes = {r["rule"]: len(r["nodes"]) for r in axe["passes"]}
        snap.rules_checked = sorted({r["rule"] for r in axe["passes"] + axe["violations"] + axe["incomplete"]})
    finally:
        await ctx.close()
    return snap


def diff_snapshots(
    before: Snapshot, after: Snapshot, fixed_rule: str | None = None, fixed_keys: list[str] | None = None
) -> RegressionReport:
    """`fixed_keys` are the weak keys of the findings this fix was supposed to remove. They are subtracted
    from `before` first - otherwise a fix that removes one issue and adds an identical-looking one nets to
    zero and the new problem is missed."""
    expected_after = before.weak - Counter(fixed_keys or [])
    new = after.weak - expected_after  # multiset difference: only genuine excess remains
    new_list = sorted(new.elements())
    lost = {r: before.passes[r] - after.passes.get(r, 0) for r in before.passes
            if after.passes.get(r, 0) < before.passes[r] and r != fixed_rule}
    return RegressionReport(
        new_findings=new_list, regressions_count=len(new_list), lost_passes=lost,
        verdict="pass" if not new_list else "fail",
        criteria_checked=sorted(set(before.rules_checked) | set(after.rules_checked)))

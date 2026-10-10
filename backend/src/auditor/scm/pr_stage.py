"""PR / issue text for each tier. All text passes the banned-claims filter before it can be published."""

from __future__ import annotations

from dataclasses import dataclass

from ..disclaimers import contains_banned_claim

STANDING_LINE = ("This change addresses specific automated findings. It does not establish that the page or "
                 "product meets any accessibility standard; a reviewer must verify each change.")

WORK_ORDER_LINE = ("This is an automated finding, not a determination about the page or product. "
                   "A person needs to assess it and decide what to do.")

TIER_TITLES = {
    "PR1": "a11y: mechanical fixes (no judgment required)",
    "PR2": "a11y: safe fixes - please review wording",
}


@dataclass
class FixSummary:
    finding_id: str
    rule_id: str
    sc_ids: list[str]
    severity: str
    page_url: str
    impact_text: str
    explanation: str
    diff: str
    proof: dict | None = None  # {verdict, relocation_score, before_ref, after_ref, runs_n}
    regression: dict | None = None  # {regressions_count, verdict}


def _proof_line(p: dict | None) -> str:
    if not p:
        return "Re-test: not yet run."
    v = {"pass": "now passes", "fail": "still fails", "inconclusive": "could not be re-tested"}.get(p["verdict"], p["verdict"])
    extra = f" (same element re-located, similarity {p['relocation_score']:.2f}; {p['runs_n']} runs)" if p.get("relocation_score") else ""
    return f"Re-test on the patched build: the originating check {v}{extra}."


def _reg_line(r: dict | None) -> str:
    if not r:
        return "Regression check: not yet run."
    return f"Regression check: {r['regressions_count']} new findings introduced on the pages touched."


def build_pr(tier: str, run_id: str, fixes: list[FixSummary]) -> tuple[str, str]:
    title = TIER_TITLES[tier]
    parts = [f"Automated accessibility fixes from audit run `{run_id}` ({len(fixes)} change(s)).", ""]
    for f in fixes:
        parts += [
            f"### {f.rule_id} - WCAG {', '.join(f.sc_ids) or 'n/a'} ({f.severity})",
            f"Page: {f.page_url}", "",
            f"**Impact:** {f.impact_text}", f"**Change:** {f.explanation}", "",
            _proof_line(f.proof), _reg_line(f.regression), "",
            "```diff", f.diff.strip(), "```", "",
        ]
    parts += ["---", STANDING_LINE]
    body = "\n".join(parts)
    for t in (title, body):
        if contains_banned_claim(t):
            raise ValueError("generated PR text contains a banned claim")
    return title, body


def build_work_order(f: FixSummary, reason: str) -> tuple[str, str]:
    title = f"a11y work order: {f.rule_id} on {f.page_url}"
    body = "\n".join([
        f"Automated finding that needs human judgment (reason: {reason}).", "",
        f"- Rule: `{f.rule_id}`  WCAG: {', '.join(f.sc_ids) or 'n/a'}  Severity: {f.severity}",
        f"- Page: {f.page_url}", "", f"**Impact:** {f.impact_text or 'See evidence.'}", "",
        "No code change was proposed for this item.", "", "---", WORK_ORDER_LINE,
    ])
    for t in (title, body):
        if contains_banned_claim(t):
            raise ValueError("generated issue text contains a banned claim")
    return title, body

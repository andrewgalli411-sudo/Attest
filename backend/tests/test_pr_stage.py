"""PR and issue text is built from untrusted material (page text, repo source, model output). Anything the tool must
not publish is withheld and flagged; it never aborts the run."""

from auditor.disclaimers import contains_banned_claim
from auditor.scm.pr_stage import FixSummary, build_pr, build_work_order


# ---- untrusted text must be withheld, never published and never fatal -------------------------------------------
def _fix(**kw):
    base = {
        "finding_id": "f1", "rule_id": "label", "sc_ids": ["1.3.1"], "severity": "serious", "page_url": "http://x/p",
        "impact_text": "Screen reader users hear no name.", "explanation": "Bind the label.",
        "diff": "-<input>\n+<input id=\"a\">", "proof": {"verdict": "pass", "relocation_score": 0.95, "runs_n": 2},
        "regression": {"regressions_count": 0, "verdict": "ok"},
    }
    base.update(kw)
    return FixSummary(**base)


def test_hostile_impact_text_is_withheld_not_fatal():
    title, body = build_pr("PR1", "r1", [_fix(impact_text="This page is fully WCAG compliant.", explanation="Certified fix")])
    assert not contains_banned_claim(title) and not contains_banned_claim(body)
    assert "impact text withheld" in body and "change description withheld" in body
    assert "fully WCAG" not in body and "Certified" not in body
    assert "a reviewer must verify" in body  # the standing line is still there


def test_legitimate_source_that_mentions_compliance_does_not_crash_pr_creation():
    diff = ' <Link to="/legal">Compliance</Link>\n-<input>\n+<input id="a">'
    title, body = build_pr("PR1", "r1", [_fix(diff=diff)])
    assert not contains_banned_claim(body)
    assert "review it on the branch" in body
    assert "withheld" not in body  # only the diff was affected; the rest of the PR is intact
    assert "Screen reader users hear no name." in body


def test_other_fixes_in_the_same_pr_are_unaffected_by_one_hostile_fix():
    good = _fix(finding_id="f2", impact_text="Good impact text.")
    bad = _fix(impact_text="We are compliant")
    _, body = build_pr("PR1", "r1", [bad, good])
    assert "Good impact text." in body and not contains_banned_claim(body)


def test_work_order_with_hostile_text_is_withheld_not_fatal():
    title, body = build_work_order(_fix(impact_text="Fully compliant with WCAG", page_url="http://x/compliance"), "contrast")
    assert not contains_banned_claim(title) and not contains_banned_claim(body)
    assert "withheld" in body

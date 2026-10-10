import pytest
from playwright.async_api import async_playwright

from auditor.demo import DEV_COMMAND, SEED_AUTH
from auditor.findings.detect import detect_state
from auditor.fix import verify
from auditor.fix.author import derive_dom_changes
from auditor.reprove.devserver import dev_server
from auditor.reprove.regress import Snapshot, diff_snapshots, snapshot_state, weak_key
from auditor.reprove.reprove import (
    BuildCheck,
    RunCheck,
    check_on_build,
    decide,
    rebase_steps,
    rebase_url,
)

ANCHOR = '<input name="email" type="email" autoComplete="email" />'
FIXED = '<input id="email" name="email" type="email" autoComplete="email" aria-label="Email address" />'


def edit(replacement):
    return {"file": "src/App.jsx", "anchor_text": ANCHOR, "replacement": replacement}


# ---------- decide(): every branch, no browser ----------
def run(**kw):
    base = {"relocated": True, "method": "stable_css", "score": 0.97, "reason": "ok", "applicable": True,
            "fails": False, "passes": True}
    return RunCheck(**{**base, **kw})


def build(*runs, clip=b"png"):
    return BuildCheck(list(runs), clip=clip)


FAILING = run(fails=True, passes=False)


@pytest.mark.parametrize("control,after,verdict,needle", [
    (build(FAILING, FAILING), build(run(), run()), "pass", "unpatched control still fails"),
    (build(run(relocated=False, applicable=False, fails=None, passes=None)), build(run(), run()),
     "inconclusive", "control: could not re-locate"),
    (build(FAILING, run()), build(run(), run()), "inconclusive", "flaky"),
    (build(run(), run()), build(run(), run()), "inconclusive", "did not reproduce the failure"),
    (build(FAILING, FAILING), build(run(relocated=False, applicable=False, fails=None, passes=None, reason="ambiguous"),
                                    run()), "inconclusive", "relocation failed"),
    (build(FAILING, FAILING), build(run(), FAILING), "inconclusive", "flaky"),
    (build(FAILING, FAILING), build(FAILING, FAILING), "fail", "still fails"),
    (build(FAILING, FAILING), build(run(applicable=False, passes=False, fails=False),
                                    run(applicable=False, passes=False, fails=False)),
     "inconclusive", "no longer applies"),
    (BuildCheck([]), build(run()), "inconclusive", "control"),
    (build(FAILING), BuildCheck([]), "inconclusive", "relocation failed"),
])
def test_decide_branches(control, after, verdict, needle):
    out = decide(control, after)
    assert out.verdict == verdict and needle in out.notes, out.notes
    assert out.rule_pass is (verdict == "pass")


def test_a_pass_requires_every_condition():
    ok = decide(build(FAILING, FAILING), build(run(), run()))
    assert ok.verdict == "pass"
    for broken in (build(run(applicable=False, passes=False), run(applicable=False, passes=False)),
                   build(run(relocated=False), run(relocated=False)), build(run(fails=True), run(fails=True))):
        assert decide(build(FAILING, FAILING), broken).verdict != "pass"


def test_rebase_url_and_steps():
    assert rebase_url("http://127.0.0.1:5000/a/b?x=1#h", "http://127.0.0.1:6000") == "http://127.0.0.1:6000/a/b?x=1#h"
    steps = [{"action": "goto", "url": "http://127.0.0.1:5000/x"}, {"action": "click", "selector": "#a"}]
    out = rebase_steps(steps, "http://127.0.0.1:7")
    assert out[0]["url"] == "http://127.0.0.1:7/x" and out[1] == steps[1] and steps[0]["url"].endswith(":5000/x")


# ---------- live: before build vs patched build ----------
async def prove(demo_repo, replacement, with_regression=False):
    repo, _ = demo_repo
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        try:
            with dev_server(repo, DEV_COMMAND) as before:
                found = await detect_state(br, before + "/", auth=SEED_AUTH, with_probes=False)
                lab = next(f for f in found if f.rule_id == "label")
                kw = {"page_url": before + "/", "steps": None, "auth": SEED_AUTH}
                ed = edit(replacement)
                ignore = derive_dom_changes([ed])
                control = await check_on_build(br, before, stored_fp=lab.fingerprint, rule_id="label",
                                               ignore_attrs=ignore, **kw)
                snap_before = await snapshot_state(br, before, with_probes=False, **kw) if with_regression else None
            verify.apply_edits(repo, [ed], {"src/App.jsx"})
            with dev_server(repo, DEV_COMMAND) as after_url:
                after = await check_on_build(br, after_url, stored_fp=lab.fingerprint, rule_id="label",
                                             ignore_attrs=ignore, **kw)
                snap_after = await snapshot_state(br, after_url, with_probes=False, **kw) if with_regression else None
        finally:
            await br.close()
    return decide(control, after), (snap_before, snap_after), lab


async def test_correct_fix_is_proved_on_the_same_element(demo_repo):
    out, _, _ = await prove(demo_repo, FIXED)
    assert out.verdict == "pass", out.notes
    assert out.baseline_failed and out.relocated and out.rule_pass and out.runs_n == 2
    assert out.relocation_score >= 0.85 and out.relocation_method == "stable_css"
    assert out.before_png[:8] == out.after_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.before_png != out.after_png and out.after_annotated


async def test_a_change_that_does_not_fix_the_rule_is_reported_as_fail(demo_repo):
    out, _, _ = await prove(demo_repo, ANCHOR.replace(" />", ' data-x="1" />'))
    assert out.verdict == "fail" and out.baseline_failed and not out.rule_pass


async def test_deleting_the_element_is_inconclusive_not_a_pass_or_a_lookalike(demo_repo):
    out, _, _ = await prove(demo_repo, "")
    assert out.verdict == "inconclusive" and not out.relocated and not out.rule_pass
    assert "relocation failed" in out.notes


async def test_fix_that_adds_a_new_problem_is_caught_by_the_regression_guard(demo_repo):
    out, (before, after), lab = await prove(
        demo_repo, FIXED + '\n          <input name="extra" type="text" />', with_regression=True)
    assert out.verdict == "pass"  # the targeted element really is fixed...
    rep = diff_snapshots(before, after, fixed_rule="label",
                         fixed_keys=[weak_key("label", lab.fingerprint)])
    assert rep.verdict == "fail" and rep.regressions_count >= 1  # ...but the patch introduced a new issue
    assert any('"label"' in k for k in rep.new_findings)


async def test_clean_fix_has_zero_regressions(demo_repo):
    out, (before, after), lab = await prove(demo_repo, FIXED, with_regression=True)
    rep = diff_snapshots(before, after, fixed_rule="label",
                         fixed_keys=[weak_key("label", lab.fingerprint)])
    assert out.verdict == "pass" and rep.verdict == "pass" and rep.regressions_count == 0
    assert "label" in rep.criteria_checked and not rep.lost_passes.get("color-contrast")


def test_diff_snapshots_unit():
    from collections import Counter
    a = Snapshot(Counter({"x": 1, "y": 2}), {"r1": 3, "r2": 1}, ["r1"])
    b = Snapshot(Counter({"x": 1, "y": 3, "z": 1}), {"r1": 3, "r2": 0}, ["r2"])
    rep = diff_snapshots(a, b)
    assert rep.new_findings == ["y", "z"] and rep.lost_passes == {"r2": 1} and rep.verdict == "fail"
    assert diff_snapshots(b, a).verdict == "pass"  # fewer findings is never a regression
    assert diff_snapshots(a, b, fixed_rule="r2").lost_passes == {}

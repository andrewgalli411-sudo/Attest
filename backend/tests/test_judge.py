import pytest
from fakes import FakeJudge

from auditor.pipeline.judge.judge import build_evidence_items, build_input, judge_bundle

SC_EV = {
    "axe": {"violations": [{"rule": "color-contrast", "impact": "serious", "sc": ["1.4.3"],
                            "nodes": [{"target": ["p"], "html": "<p>"}]}],
            "incomplete": [], "passes": [{"rule": "label", "impact": None, "sc": ["1.4.3"], "nodes": []}]},
    "probes": [{"probe": "keyboard_nav", "sc_ids": ["2.1.1"], "verdict": "pass", "observations": []}],
}
CRIT = {"sc_id": "1.4.3", "title": "Contrast (Minimum)", "level": "AA"}


def _run(mode, sc_ev=SC_EV):
    import asyncio
    items = build_evidence_items(sc_ev)
    payload = build_input(CRIT, "http://x/", None, items)
    return asyncio.run(judge_bundle(FakeJudge(mode), "claude-sonnet-5-5", payload, items)), items


def test_evidence_ids_are_stable_and_well_formed():
    ids = [i["id"] for i in build_evidence_items(SC_EV)]
    assert ids == ["axe:violation:color-contrast", "axe:pass:label", "probe:keyboard_nav"]


def test_good_judge_partial_support():
    p, _ = _run("good")
    assert p.valid and p.level == "partially_supports"


@pytest.mark.parametrize("mode,note", [
    ("unknown_ids", "unknown evidence ids"),
    ("banned", "banned"),
    ("garbage", "unparseable"),
    ("refusal", "refusal"),
])
def test_bad_judge_output_becomes_invalid_not_evaluated(mode, note):
    p, _ = _run(mode)
    assert not p.valid and p.level == "not_evaluated" and p.needs_human
    assert note in " ".join(p.guardrail_notes)


def test_retry_recovers_from_unknown_ids():
    p, _ = _run("unknown_then_fix")
    assert p.valid and p.level == "does_not_support"


def test_supports_with_failing_evidence_is_downgraded():
    p, _ = _run("overconfident_supports")
    assert p.level == "not_evaluated" and p.needs_human
    assert any("despite failing evidence" in n for n in p.guardrail_notes)


def test_failure_claim_without_failing_evidence_is_downgraded():
    only_pass = {"axe": {"violations": [], "incomplete": [], "passes": SC_EV["axe"]["passes"]}, "probes": []}
    p, _ = _run("failure_without_evidence", only_pass)
    assert p.level == "not_evaluated"


def test_supports_from_axe_only_has_confidence_capped():
    only_pass = {"axe": {"violations": [], "incomplete": [], "passes": SC_EV["axe"]["passes"]}, "probes": []}
    p, _ = _run("good", only_pass)
    assert p.level == "supports" and p.confidence <= 0.6


def test_request_shape_avoids_rejected_params():
    import asyncio
    items = build_evidence_items(SC_EV)
    client = FakeJudge("good")
    asyncio.run(judge_bundle(client, "claude-sonnet-5-5", build_input(CRIT, "u", None, items), items))
    kw = client.calls[0]
    assert "temperature" not in kw and "tool_choice" not in kw and "thinking" not in kw
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}

import pytest
from fakes import FakeTriage
from sqlalchemy import func, select

from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    FindingStatus,
    PageState,
    Severity,
    Tier,
    TriageVerdict,
)
from auditor.storage.evidence_store import EvidenceStore
from auditor.triage.gate import apply_gate
from auditor.triage.triage import run_triage, triage_one

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
HIT = {"unique": True, "reason": "react_fiber", "hit": {
    "file": "src/App.jsx", "line": 33, "col": 10, "method": "react_fiber",
    "anchor": '<input name="email" />', "excerpt": "33: <input name=\"email\" />"}}
NOHIT = {"unique": False, "reason": "ambiguous: 2 source locations match"}


def make_finding(sf, tmp_path, rule="label", stable=True, source=HIT, sev=Severity.critical, key="k"):
    store = EvidenceStore(tmp_path / "ev")
    with sf() as db:
        run = db.scalar(select(AuditRun)) or AuditRun(target_url="http://x")
        db.add(run)
        db.flush()
        ps = db.scalar(select(PageState)) or PageState(run_id=run.id, url="http://x/")
        db.add(ps)
        db.flush()
        f = Finding(run_id=run.id, page_state_id=ps.id, rule_id=rule, sc_ids=["3.3.2"], severity=sev,
                    source="axe", dedupe_key=key + rule, baseline_stable=stable)
        db.add(f)
        db.flush()
        db.add(FindingLocator(finding_id=f.id, fingerprint={"tag": "input", "stable_css": 'input[name="email"]',
                                                            "visible_text": "", "landmark_path": ["main"]}))
        ref, sha = store.put(run.id, ps.id, f"{f.id}-clip.png", PNG)
        db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="shot_clip", blob_ref=ref, sha256=sha))
        db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="dom_snippet", payload={"html": "<form><input></form>"}))
        db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="detector_detail", payload={"impact": "critical"}))
        if source is not None:
            db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="source_hit", payload=source))
        db.commit()
        return f.id, store


async def verdict(sf, tmp_path, client, **kw):
    fid, store = make_finding(sf, tmp_path, **{k: v for k, v in kw.items() if k != "model"})
    v = await triage_one(sf, store, client, "claude-opus-5-5", fid)
    with sf() as db:
        return v, db.get(Finding, fid)


async def test_good_path_auto_fix_pr1_and_records_everything(sf, tmp_path):
    client = FakeTriage("good")
    v, f = await verdict(sf, tmp_path, client)
    assert v.tier == Tier.auto_fix and v.pr_tier == "PR1" and v.valid and v.reason_code == "missing_label"
    assert f.status == FindingStatus.triaged and f.impact_text.startswith("A screen-reader user")
    with sf() as db:
        kinds = {e.kind for e in db.scalars(select(FindingEvidence))}
        assert {"impact", "triage"} <= kinds


async def test_request_shape_is_valid_for_current_models(sf, tmp_path):
    client = FakeTriage("good")
    fid, store = make_finding(sf, tmp_path)
    await triage_one(sf, store, client, "claude-opus-5-5", fid, send_image=True)  # screenshots are opt-in
    kw = client.calls[0]
    assert not ({"temperature", "top_p", "top_k", "tool_choice", "thinking"} & set(kw))
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    kinds = [b["type"] for b in kw["messages"][0]["content"]]
    assert kinds == ["text", "image"]  # element screenshot is sent as an image block


@pytest.mark.parametrize("rule", ["color-contrast", "image-alt", "probe:keyboard_trap:trapped_elements",
                                  "probe:modal_focus:escape_does_not_close"])
async def test_model_can_never_upgrade_human_only_rules(sf, tmp_path, rule):
    v, f = await verdict(sf, tmp_path, FakeTriage("always_auto", 0.99), rule=rule)
    assert v.tier == Tier.needs_human and v.reason_code == "not_automatable" and f.status == FindingStatus.needs_human


async def test_rule_not_on_allowlist_is_human(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto", 0.99), rule="aria-hidden-focus")
    assert v.tier == Tier.needs_human and "allowlist" in " ".join(v.gate_notes)


async def test_unstable_finding_is_never_auto_fixed(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto"), stable=False)
    assert v.tier == Tier.needs_human and v.reason_code == "unstable"


async def test_ambiguous_source_is_never_auto_fixed(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto"), source=NOHIT)
    assert v.tier == Tier.needs_human and v.reason_code == "ambiguous_source"


async def test_missing_source_evidence_is_never_auto_fixed(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto"), source=None)
    assert v.tier == Tier.needs_human and v.reason_code == "ambiguous_source"


async def test_low_confidence_downgrades(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto", 0.79))
    assert v.tier == Tier.needs_human and "below" in " ".join(v.gate_notes)


async def test_empty_fix_plan_downgrades(sf, tmp_path):
    v, _ = await verdict(sf, tmp_path, FakeTriage("empty_plan"))
    assert v.tier == Tier.needs_human


@pytest.mark.parametrize("rule,conf,pr", [("label", 0.95, "PR1"), ("label", 0.85, "PR2"),
                                          ("html-has-lang", 0.9, "PR1"), ("button-name", 0.99, "PR2"),
                                          ("link-name", 0.9, "PR2")])
async def test_pr_tier_assignment(sf, tmp_path, rule, conf, pr):
    v, _ = await verdict(sf, tmp_path, FakeTriage("always_auto", conf), rule=rule)
    assert v.tier == Tier.auto_fix and v.pr_tier == pr


@pytest.mark.parametrize("mode", ["banned_impact", "banned_plan", "unknown_ids", "bad_enum", "garbage", "refusal"])
async def test_bad_model_output_becomes_invalid_needs_human(sf, tmp_path, mode):
    client = FakeTriage(mode)
    v, f = await verdict(sf, tmp_path, client)
    assert not v.valid and v.tier == Tier.needs_human and f.status == FindingStatus.needs_human
    assert f.impact_text is None  # tainted text is never stored as the explanation


async def test_retry_recovers_from_unknown_citations(sf, tmp_path):
    client = FakeTriage("unknown_then_fix")
    v, _ = await verdict(sf, tmp_path, client)
    assert v.valid and v.tier == Tier.auto_fix and len(client.calls) == 2


async def test_identical_evidence_reuses_verdict_without_api_call(sf, tmp_path):
    client = FakeTriage("good")
    fid, store = make_finding(sf, tmp_path)
    await triage_one(sf, store, client, "claude-opus-5-5", fid)
    n = len(client.calls)
    again = await triage_one(sf, store, client, "claude-opus-5-5", fid)
    assert len(client.calls) == n and "reused cached verdict" in again.gate_notes and again.pr_tier == "PR1"
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(TriageVerdict)) == 2


async def test_run_triage_survives_api_errors(sf, tmp_path):
    class Boom:
        messages = None

        async def create(self, **kw):
            raise RuntimeError("api down")

    boom = Boom()
    boom.messages = boom
    fid, store = make_finding(sf, tmp_path)
    await run_triage(sf, store, boom, "m", AuditRun.__name__ and _run_id(sf))
    with sf() as db:
        f = db.get(Finding, fid)
        assert f.status == FindingStatus.needs_human, "a model outage must not leave findings stuck in 'caught'"
        v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == fid))
        assert not v.valid and "model error RuntimeError" in " ".join(v.gate_notes)
        assert f.impact_text is None


def _run_id(sf):
    with sf() as db:
        return db.scalar(select(AuditRun.id))


def test_gate_direct_never_trusts_unknown_enum():
    g = apply_gate(rule_id="label", model_out={"tier": "auto_fix", "reason_code": "weird"},
                   evidence_ids=set(), baseline_stable=True, source_unique=True)
    assert not g.valid and g.tier == "needs_human"


async def test_an_exhausted_model_budget_hands_remaining_findings_to_a_person(sf, tmp_path):
    from auditor.budget import BudgetedClient

    client = BudgetedClient(FakeTriage("good"), budget=0)  # nothing may be spent
    fid, store = make_finding(sf, tmp_path)
    v = await triage_one(sf, store, client, "claude-opus-5-5", fid)
    assert not v.valid and v.tier == Tier.needs_human and "budget" in " ".join(v.gate_notes)
    assert client.calls == 0 and client._inner.calls == []  # not a single call reached the model
    with sf() as db:
        assert db.get(Finding, fid).status == FindingStatus.needs_human

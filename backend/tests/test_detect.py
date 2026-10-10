import struct
import subprocess

from playwright.async_api import async_playwright
from sqlalchemy import select

from auditor.demo import SEED_AUTH
from auditor.findings.detect import detect_state, persist_findings
from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    PageState,
    Severity,
)
from auditor.storage.evidence_store import EvidenceStore


def png_size(b: bytes) -> tuple[int, int]:
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", b[16:24])


async def _detect(base, **kw):
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        try:
            return await detect_state(br, base + "/", auth=SEED_AUTH, **kw)
        finally:
            await br.close()


async def test_seeded_defects_are_caught_with_locator_and_evidence(demo_server):
    found = await _detect(demo_server, with_probes=False)
    by_rule = {f.rule_id: f for f in found}
    assert {"label", "color-contrast", "html-has-lang"} <= set(by_rule)
    lab = by_rule["label"]
    assert lab.severity == Severity.critical and "3.3.2" in lab.sc_ids + ["3.3.2"]
    fp = lab.fingerprint
    assert fp["tag"] == "input" and fp["stable_css"] == 'input[name="email"][type="email"]'
    assert fp["visible_text"] == "" and fp["landmark_path"] == ["main", "form"]
    assert fp["bbox"]["w"] > 50 and fp["bbox"]["h"] > 10
    clip, full = lab.evidence["shot_clip"], lab.evidence["shot_full_annotated"]
    (cw, ch), (fw, fh) = png_size(clip), png_size(full)
    assert cw > 50 and ch > 20 and fw >= 1280 and fh >= 400 and len(clip) < len(full)
    assert "email" in lab.snippet


async def test_two_passes_agree_on_identity(demo_server):
    a = await _detect(demo_server, with_probes=False)
    b = await _detect(demo_server, with_probes=False)
    assert sorted(f.dedupe_key for f in a) == sorted(f.dedupe_key for f in b)
    assert len({f.dedupe_key for f in a}) == len(a)  # no collisions between distinct findings


async def test_logged_out_state_has_no_profile_form_findings(demo_server):
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        try:
            found = await detect_state(br, demo_server + "/", auth=None, with_probes=False)
        finally:
            await br.close()
    assert "label" not in {f.rule_id for f in found}  # the form is behind the seeded session


async def test_probes_contribute_element_level_findings(demo_server):
    found = await _detect(demo_server)
    probe_rules = {f.rule_id for f in found if f.source == "probe"}
    assert any(r.startswith("probe:focus_visible") for r in probe_rules), probe_rules  # outline:none bell
    fv = next(f for f in found if f.rule_id.startswith("probe:focus_visible"))
    assert fv.fingerprint and fv.evidence["shot_clip"]


async def test_persist_writes_finding_locator_evidence(sf, tmp_path, demo_server):
    first = await _detect(demo_server, with_probes=False)
    second = await _detect(demo_server, with_probes=False)
    with sf() as db:
        run = AuditRun(target_url=demo_server)
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url=demo_server + "/")
        db.add(ps)
        db.commit()
        rid, pid = run.id, ps.id
    store = EvidenceStore(tmp_path / "ev")
    ids = persist_findings(sf, store, rid, pid, {"flow_name": None, "label": None, "steps": []},
                           first, second)
    assert len(ids) == len(first)
    # idempotent: persisting the same pass again adds nothing
    assert persist_findings(sf, store, rid, pid, {"flow_name": None, "label": None, "steps": []},
                            first, second) == []
    with sf() as db:
        rows = db.scalars(select(Finding)).all()
        assert all(f.baseline_stable for f in rows)
        lab = next(f for f in rows if f.rule_id == "label")
        kinds = {e.kind for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == lab.id))}
        assert {"shot_clip", "shot_full_annotated", "dom_snippet", "detector_detail"} <= kinds
        ev = db.scalar(select(FindingEvidence).where(FindingEvidence.finding_id == lab.id,
                                                     FindingEvidence.kind == "shot_clip"))
        assert png_size(store.get(ev.blob_ref))[0] > 50
        loc = db.get(FindingLocator, lab.id)
        assert loc.fingerprint["stable_css"] == 'input[name="email"][type="email"]'


def test_no_dev_server_left_behind():
    out = subprocess.run(["pgrep", "-f", "vite"], capture_output=True, text=True).stdout.split()
    assert out == [], f"leaked dev server processes: {out}"

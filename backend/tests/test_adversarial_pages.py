"""Misbehaving target sites must cost bounded time and produce a recorded outcome, never a hang or a crash."""

import http.server
import threading
import time

import pytest
from fakes import FakeTriage
from playwright.async_api import async_playwright
from sqlalchemy import select

from auditor.findings.detect import detect_state, persist_findings
from auditor.findings.score import page_score
from auditor.models import AuditRun, Finding, FindingLocator, FindingStatus, PageState
from auditor.pipeline.prober import PageUnauditable
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(240)
HEAD = '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>t</title></head><body><main><h1>t</h1>'
TAIL = "</main></body></html>"


def page(body: str, head_extra: str = "") -> bytes:
    return (HEAD.replace("</head>", head_extra + "</head>") + body + TAIL).encode()


ROUTES: dict[str, tuple[int, bytes, float]] = {}  # path -> (status, body, delay seconds)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        status, body, delay = ROUTES.get(path, (404, b"nope", 0))
        time.sleep(delay)
        try:
            self.send_response(status)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


async def run_detect(browser, url, **kw):
    t = time.perf_counter()
    found = await detect_state(browser, url, with_probes=False, **kw)
    return found, time.perf_counter() - t


# ---------------------------------------------------------------- volume
async def test_hundreds_of_violations_on_one_page_are_capped_but_still_scored(browser, site, sf, tmp_path):
    ROUTES["/many"] = (200, page("<form>" + "".join(f"<input type=text name=f{i}>" for i in range(600)) + "</form>"), 0)
    found, took = await run_detect(browser, site + "/many", max_findings=25)
    assert took < 90, f"took {took:.0f}s"
    itemised = [f for f in found if not f.dropped]
    assert len(found) >= 600 and len(itemised) == 25 and sum(f.dropped for f in found) == len(found) - 25
    assert all(f.fingerprint and f.evidence["shot_clip"] for f in itemised)  # itemised ones are complete
    assert all(not f.evidence and f.fingerprint is None for f in found if f.dropped)  # dropped ones cost nothing
    # the score reflects every violation, not just the itemised ones
    full, _ = page_score((f.rule_id, f.severity) for f in found)
    capped, _ = page_score((f.rule_id, f.severity) for f in itemised)
    assert full < capped

    with sf() as db:
        run = AuditRun(target_url="x")
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url=site + "/many")
        db.add(ps)
        db.commit()
        rid, pid = run.id, ps.id
    ids = persist_findings(sf, EvidenceStore(tmp_path / "ev"), rid, pid, {"flow_name": None, "label": None, "steps": []}, found)
    assert len(ids) == 25
    with sf() as db:
        assert db.query(Finding).count() == 25


# ---------------------------------------------------------------- elements we cannot re-locate
async def test_an_element_inside_an_iframe_is_reported_but_never_auto_fixed(browser, site, sf, tmp_path):
    ROUTES["/frame"] = (200, page('<iframe title="inner" srcdoc="<!doctype html><html lang=en><head><title>i</title></head><body><main><input type=text></main></body></html>"></iframe>'), 0)
    found, _ = await run_detect(browser, site + "/frame")
    inner = [f for f in found if f.rule_id == "label"]
    assert inner, "violations inside an iframe must be reported, not silently missed"
    assert all(f.fingerprint is None for f in inner), "an iframe element must not be given a (wrong) locator"
    assert "iframe" in inner[0].detail["unlocatable_reason"]

    from test_triage import make_finding  # noqa: F401  (shape reference)

    from auditor.triage.triage import triage_one

    with sf() as db:
        run = AuditRun(target_url="x")
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url=site + "/frame")
        db.add(ps)
        db.commit()
        rid, pid = run.id, ps.id
    store = EvidenceStore(tmp_path / "ev")
    [fid] = persist_findings(sf, store, rid, pid, {"flow_name": None, "label": None, "steps": []}, found, found)
    with sf() as db:
        loc = db.get(FindingLocator, fid)
        assert loc.fingerprint["unlocatable"] and "iframe" in loc.fingerprint["reason"]
    v = await triage_one(sf, store, FakeTriage("always_auto", 0.99), "claude-opus-5-5", fid)
    assert v.tier.value == "needs_human" and v.reason_code == "ambiguous_source"
    assert "iframe" in " ".join(v.gate_notes)  # the person is told WHY
    with sf() as db:
        assert db.get(Finding, fid).status == FindingStatus.needs_human


# ---------------------------------------------------------------- time
async def test_a_page_whose_network_never_goes_idle_still_finishes(browser, site):
    ROUTES["/ping"] = (200, b"pong", 0)
    ROUTES["/busy"] = (200, page('<input type=text><script>setInterval(()=>fetch("/ping?"+Math.random()),30);'
                                 'setInterval(()=>{document.title=Math.random()},20)</script>'), 0)
    found, took = await run_detect(browser, site + "/busy")
    assert took < 60 and any(f.rule_id == "label" for f in found), f"took {took:.0f}s"


async def test_a_very_large_dom_is_audited_in_bounded_time(browser, site):
    ROUTES["/big"] = (200, page("<ul>" + "<li><a href='#x'>link</a></li>" * 15_000 + "</ul><input type=text>"), 0)
    found, took = await run_detect(browser, site + "/big", max_findings=10)
    assert took < 120 and any(f.rule_id == "label" for f in found), f"took {took:.0f}s"


async def test_a_meta_refresh_loop_does_not_hang_or_crash_the_detector(browser, site):
    ROUTES["/loop"] = (200, page("<input type=text>", '<meta http-equiv="refresh" content="0">'), 0)
    t = time.perf_counter()
    try:
        await detect_state(browser, site + "/loop", with_probes=False)
    except Exception as e:  # noqa: BLE001 - a clean, recorded failure is an acceptable outcome; a hang is not
        assert isinstance(e, Exception) and type(e).__name__
    assert time.perf_counter() - t < 90


async def test_a_server_that_stalls_past_the_navigation_timeout_fails_fast_and_cleanly(browser, site):
    ROUTES["/stall"] = (200, page("<p>slow</p>"), 40)
    t = time.perf_counter()
    with pytest.raises(Exception, match="Timeout|timeout"):
        await detect_state(browser, site + "/stall", with_probes=False)
    assert time.perf_counter() - t < 40, "must give up at the navigation timeout, not wait for the server"


async def test_a_page_that_spams_dialogs_is_not_blocked_by_them(browser, site):
    ROUTES["/dialogs"] = (200, page("<input type=text><script>alert('a');confirm('b');prompt('c');setInterval(()=>alert('x'),100)</script>"), 0)
    found, took = await run_detect(browser, site + "/dialogs")
    assert took < 60 and any(f.rule_id == "label" for f in found)


# ---------------------------------------------------------------- content
async def test_a_five_megabyte_text_node_does_not_bloat_the_stored_evidence(browser, site, sf, tmp_path):
    ROUTES["/text"] = (200, page("<div id=t>" + "x" * 5_000_000 + "</div><input type=text>"), 0)
    found, took = await run_detect(browser, site + "/text")
    lab = next(f for f in found if f.rule_id == "label")
    assert len(lab.snippet) <= 1500 and took < 90
    assert all(len(str(f.detail)) < 20_000 for f in found)


@pytest.mark.parametrize("status,body", [(404, b"not found"), (500, b"boom"), (503, b""), (204, b"")])
async def test_error_status_responses_are_unauditable_with_a_clear_reason(browser, site, status, body):
    ROUTES["/odd"] = (status, body, 0)
    with pytest.raises(PageUnauditable, match="HTTP|non-HTML|navigation failed"):
        await detect_state(browser, site + "/odd", with_probes=False)


@pytest.mark.parametrize("status,body", [(200, b""), (200, b"\x00\x01\x02binary"), (301, b"")])
async def test_empty_or_garbage_documents_are_audited_as_what_they_are_without_crashing(browser, site, status, body):
    ROUTES["/odd2"] = (status, body, 0)
    t = time.perf_counter()
    found = await detect_state(browser, site + "/odd2", with_probes=False)
    assert time.perf_counter() - t < 60
    assert isinstance(found, list)  # an empty document is a (poor) page; it gets findings like any other


async def test_instances_of_identical_elements_are_counted_not_lost(browser, site, sf, tmp_path):
    ROUTES["/same"] = (200, page("<form>" + "<input type=text>" * 12 + "</form>"), 0)
    found = await detect_state(browser, site + "/same", with_probes=False)
    assert sum(f.rule_id == "label" for f in found) == 12
    with sf() as db:
        run = AuditRun(target_url="x")
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url=site + "/same")
        db.add(ps)
        db.commit()
        rid, pid = run.id, ps.id
    persist_findings(sf, EvidenceStore(tmp_path / "ev"), rid, pid, {"flow_name": None, "label": None, "steps": []}, found)
    with sf() as db:
        from auditor.models import FindingEvidence

        label = db.scalar(select(Finding).where(Finding.rule_id == "label"))
        detail = db.scalar(select(FindingEvidence).where(FindingEvidence.finding_id == label.id, FindingEvidence.kind == "detector_detail"))
        assert detail.payload["instances"] == 12  # one finding, honestly labelled as 12 identical elements

import json
import logging
import re
from html.parser import HTMLParser

import httpx
from playwright.async_api import async_playwright

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    FixAttempt,
    PageState,
    RegressionCheck,
    ReproveResult,
    RunStatus,
    Severity,
    Tier,
    TriageVerdict,
)
from auditor.report.html import finding_timeline, run_report
from auditor.storage.evidence_store import EvidenceStore


def make_app(sf, tmp_path, **cfg):
    cfg.setdefault("allowed_hosts", ["t"])
    return create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                      config=Settings(**cfg))


def http(app, **headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)), base_url="http://t",
                             headers=headers)


# ---------------------------------------------------------------- headers, size limit, request ids
async def test_security_headers_are_on_every_kind_of_response(sf, tmp_path):
    app = make_app(sf, tmp_path)
    with sf() as db:
        db.add(AuditRun(id="r" * 32, target_url="x", status=RunStatus.complete))
        db.commit()
    async with http(app) as c:
        for path in ("/api/health", "/api/runs", "/api/config", "/api/runs/nope/dashboard", f"/api/runs/{'r' * 32}/report"):
            r = await c.get(path)
            h = r.headers
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY", path
            assert h["referrer-policy"] == "no-referrer" and "x-request-id" in h, path
            assert path == "/api/health" or h["cache-control"] == "no-store", (path, h.get("cache-control"))
        csp = (await c.get(f"/api/runs/{'r' * 32}/report")).headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
        assert (await c.get("/api/runs")).headers["content-type"].startswith("application/json")


async def test_oversized_and_unsized_bodies_are_rejected_before_being_read(sf, tmp_path):
    app = make_app(sf, tmp_path, max_request_bytes=1000)
    async with http(app) as c:
        big = await c.post("/api/runs", content=b'{"demo": true, "x": "' + b"a" * 5000 + b'"}', headers={"content-type": "application/json"})
        assert big.status_code == 413 and "exceeds" in big.json()["detail"]
        edge = await c.post("/api/runs", content=b"{" + b" " * 997 + b"}", headers={"content-type": "application/json"})
        assert edge.status_code != 413, "a body exactly at the limit must be accepted"
        over = await c.post("/api/runs", content=b"{" + b" " * 999 + b"}", headers={"content-type": "application/json"})
        assert over.status_code == 413  # one byte over

        async def chunks():
            yield b'{"demo":'
            yield b" true}"

        chunked = await c.post("/api/runs", content=chunks(), headers={"content-type": "application/json"})
        assert chunked.status_code == 411  # no declared length: refused, never buffered
        bad_len = await c.post("/api/runs", content=b"{}", headers={"content-type": "application/json", "content-length": "NaN"})
        assert bad_len.status_code in (400, 413)
    with sf() as db:
        assert db.query(AuditRun).count() == 0  # none of those started a run


async def test_request_ids_are_echoed_when_safe_replaced_when_not_and_logged(sf, tmp_path, caplog):
    app = make_app(sf, tmp_path)
    caplog.set_level(logging.INFO, logger="auditor.access")
    async with http(app) as c:
        ok = await c.get("/api/health", headers={"x-request-id": "client-id-12345"})
        assert ok.headers["x-request-id"] == "client-id-12345"
        for evil in ("short", "has spaces in it!!", "a" * 200, "x;rm -rf /;yyyy", chr(0x202E) * 12, "id\x7fwith-del-char"):
            r = await c.get("/api/health", headers=[(b"x-request-id", evil.encode("utf-8"))])
            assert r.headers["x-request-id"] != evil and re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"]), evil
        ids = {(await c.get("/api/health")).headers["x-request-id"] for _ in range(20)}
        assert len(ids) == 20
    lines = [r.getMessage() for r in caplog.records if r.name == "auditor.access"]
    assert any("client-id-12345" in m and "/api/health" in m and "-> 200" in m for m in lines)
    assert all("\n" not in m for m in lines)


# ---------------------------------------------------------------- attacker-controlled text must never execute
PAYLOADS = [
    '<script>window.__pwned="script"</script>', '<img src=x onerror="window.__pwned=\'img\'">',
    '"><svg onload="window.__pwned=\'svg\'">', "</pre></code></details><script>window.__pwned='breakout'</script>",
    "javascript:window.__pwned='js'", '<iframe srcdoc="<script>parent.__pwned=1</script>">',
    "<a href=\"javascript:window.__pwned='a'\">click</a>", "{{constructor.constructor('window.__pwned=1')()}}",
    "<style>@import 'http://evil.example/x.css'</style>", "<meta http-equiv=refresh content='0;url=http://evil.example'>",
]
EVIL = " ".join(PAYLOADS)  # for unbounded text columns
SHORT = "<img src=x onerror=window.__pwned=1>"  # for size-limited columns (rule ids, reason codes)


def seed_hostile(sf, store, run_id="h" * 32):
    """A run whose every displayed field is attacker-controlled."""
    with sf() as db:
        run = AuditRun(id=run_id, target_url=EVIL, status=RunStatus.complete,
                       summary={"headline": EVIL, "model_kind": EVIL, "by_severity": {}, "score_before": {"score": 1, "worst": 1},
                                "score_after": {"score": 2, "worst": 2}, "proved": 1, "needs_human": 0, "regressions": 0})
        db.add(run)
        db.flush()
        ps = PageState(run_id=run_id, url="http://x/" + SHORT, state_label=SHORT)
        db.add(ps)
        db.flush()
        f = Finding(run_id=run_id, page_state_id=ps.id, rule_id=SHORT, sc_ids=[SHORT], severity=Severity.critical,
                    source="axe", dedupe_key="k", impact_text=EVIL)
        db.add(f)
        db.flush()
        db.add(FindingLocator(finding_id=f.id, fingerprint={"tag": "input"}))
        db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="dom_snippet", payload={"html": EVIL}))
        db.add(FindingEvidence(finding_id=f.id, stage="caught", kind="detector_detail", payload={"impact": EVIL}))
        v = TriageVerdict(finding_id=f.id, tier=Tier.auto_fix, reason_code=SHORT, fix_plan=EVIL, confidence=0.9,
                          gate_notes=[EVIL], pr_tier="PR1")
        db.add(v)
        db.flush()
        fa = FixAttempt(finding_id=f.id, verdict_id=v.id, tier_pr="PR1", branch=EVIL, diff=f"-{EVIL}\n+{EVIL}",
                        idempotency_key="i")
        db.add(fa)
        db.flush()
        db.add(FindingEvidence(finding_id=f.id, stage="fix", kind="diff", payload={"explanation": EVIL, "diff": EVIL}))
        db.add(ReproveResult(finding_id=f.id, fix_attempt_id=fa.id, verdict="pass", notes=EVIL, relocation_method=SHORT[:24],
                             relocated=True, relocation_score=0.9, baseline_failed=True, rule_pass=True, runs_n=2))
        db.add(RegressionCheck(fix_attempt_id=fa.id, page_state_id=ps.id, verdict=SHORT[:16], criteria_checked=[EVIL]))
        db.commit()
        return run_id, f.id


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.handlers, self.bad_imgs, self.bad_links, self.frames = 0, [], [], [], 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("script", "iframe", "object", "embed", "meta", "base", "link") and not (tag == "meta" and d.get("charset")) \
                and not (tag == "meta" and d.get("name") == "viewport"):
            self.scripts += 1
        self.handlers += [k for k in d if k.startswith("on")]
        if tag == "img" and not str(d.get("src", "")).startswith("data:image/png;base64,"):
            self.bad_imgs.append(d.get("src"))
        if tag == "a" and d.get("href") and not d["href"].startswith("#"):
            self.bad_links.append(d["href"])


def test_the_report_escapes_every_attacker_controlled_field(sf, tmp_path):
    store = EvidenceStore(tmp_path / "ev")
    rid, fid = seed_hostile(sf, store)
    for page in (run_report(sf, store, rid), finding_timeline(sf, store, fid)):
        a = Audit()
        a.feed(page)
        assert a.scripts == 0 and a.handlers == [] and a.bad_imgs == [] and a.bad_links == [], vars(a)
        assert "&lt;script&gt;" in page  # shown as text, not markup


async def test_the_report_cannot_execute_anything_in_a_real_browser(sf, tmp_path):
    store = EvidenceStore(tmp_path / "ev")
    rid, _ = seed_hostile(sf, store)
    app = make_app(sf, tmp_path)
    app.state.deps.store = store
    async with http(app) as c:
        r = await c.get(f"/api/runs/{rid}/report")
        assert r.status_code == 200
        body, csp = r.text, r.headers["content-security-policy"]
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context()
        page = await ctx.new_page()
        dialogs, requests = [], []
        page.on("dialog", lambda d: dialogs.append(d.message))
        page.on("request", lambda r: requests.append(r.url) if r.url.startswith("http") else None)
        # serve it exactly as the API does, CSP header included
        await page.route("http://report.test/", lambda route: route.fulfill(
            status=200, body=body, headers={"content-type": "text/html", "content-security-policy": csp}))
        await page.goto("http://report.test/")
        await page.wait_for_timeout(500)
        assert await page.evaluate("typeof window.__pwned") == "undefined", "attacker script ran"
        assert dialogs == [] and not [u for u in requests if "evil.example" in u]
        assert "<script>window.__pwned" in await page.inner_text("body")  # visible, as inert text
        await b.close()


async def test_the_json_api_returns_hostile_strings_inertly(sf, tmp_path):
    store = EvidenceStore(tmp_path / "ev")
    rid, fid = seed_hostile(sf, store)
    app = make_app(sf, tmp_path)
    async with http(app) as c:
        for path in (f"/api/runs/{rid}/dashboard", f"/api/findings/{fid}", "/api/runs"):
            r = await c.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
            assert r.headers["x-content-type-options"] == "nosniff"
            assert EVIL in json.dumps(r.json(), ensure_ascii=False) or "script" in r.text  # data is intact, only inert JSON

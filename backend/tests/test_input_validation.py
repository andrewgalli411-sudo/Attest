"""Everything a caller can put in a request body is hostile until validated."""

import httpx
import pytest
from sqlalchemy import select

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import AuditRun, Finding, FindingEvidence, FindingStatus, TriageVerdict
from auditor.storage.evidence_store import EvidenceStore
from auditor.textsafe import strip_nul


def app_for(sf, tmp_path):
    return create_app(sf, store=EvidenceStore(tmp_path / "ev"), client_factory=ScriptedClient, work_dir=str(tmp_path),
                      config=Settings(allowed_hosts=["t"]))


def http(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)), base_url="http://t")


BAD_PATHS = ["@evil.example/", "http://evil.example/", "https://evil.example", "//evil.example/x", "evil.example/x", "",
             "/..", "/a/../b", "/a/..", "\\evil", "/a\\b", "/has space", "/new\nline", "/tab\there", "/\x00nul",
             "/" + "a" * 600, "javascript:alert(1)", "/\u202eevil", " /x", "/x ", "file:///etc/passwd"]


@pytest.mark.parametrize("path", BAD_PATHS)
async def test_page_paths_that_could_leave_the_audited_app_are_rejected(sf, tmp_path, path):
    app = app_for(sf, tmp_path)
    async with http(app) as c:
        r = await c.post("/api/runs", json={"demo": True, "pages": [{"path": path}]})
        assert r.status_code == 422, (path, r.status_code)
    with sf() as db:
        assert db.query(AuditRun).count() == 0  # nothing was started


async def test_ordinary_page_paths_are_accepted(sf, tmp_path):
    app = app_for(sf, tmp_path)
    async with http(app) as c:
        for path in ("/", "/dashboard", "/settings/profile?tab=2", "/a/b-c_d.e", "/search?q=a%20b&x=1#top", "/@user/profile"):
            r = await c.post("/api/runs", json={"demo": True, "pages": [{"path": path, "label": "ok"}]})
            assert r.status_code == 202, (path, r.text)
            await c.post(f"/api/runs/{r.json()['id']}/cancel")


@pytest.mark.parametrize("body", [
    {"demo": True, "pages": [{"path": "/", "label": "x" * 101}]},
    {"demo": True, "pages": [{"path": "/", "label": "bad\x00label"}]},
    {"demo": True, "pages": [{"path": "/"}] * 26},
    {"demo": True, "pages": ["/not-an-object"]},
    {"demo": True, "pages": [{"path": 5}]},
    {"demo": True, "pages": [{"path": "/", "extra": 1, "label": ["x"]}]},
    {"demo": True, "only_rules": ["r"] * 51},
    {"demo": True, "max_fixes": 0}, {"demo": True, "max_fixes": 101}, {"demo": True, "max_fixes": "many"},
    {"demo": "yes please"}, {"demo": True, "with_probes": "sometimes"}, {"project": 5}, {"project": ["a"]},
    [], "text", 5, None,
])
async def test_malformed_request_bodies_get_a_422_never_a_500(sf, tmp_path, body):
    app = app_for(sf, tmp_path)
    async with http(app) as c:
        r = await c.post("/api/runs", json=body)
        assert r.status_code == 422, (body, r.status_code, r.text[:120])


async def test_garbage_that_is_not_json_is_a_422_and_does_not_crash(sf, tmp_path):
    app = app_for(sf, tmp_path)
    async with http(app) as c:
        for raw in (b"{not json", b"\xff\xfe\x00", b"", b"[" * 5000, b'{"demo": true' + b" " * 10):
            r = await c.post("/api/runs", content=raw, headers={"content-type": "application/json"})
            assert r.status_code in (400, 422), (raw[:20], r.status_code)
        assert (await c.get("/api/health")).status_code == 200  # still alive afterwards


async def test_if_a_bad_path_ever_reached_the_detector_it_would_not_navigate(sf, tmp_path, monkeypatch):
    """Second line of defence: even if validation were bypassed, the run refuses to leave the app's origin."""
    from auditor import remediate

    calls = []

    async def fake_detect(*a, **k):
        calls.append(a)
        return []

    monkeypatch.setattr(remediate, "detect_state", fake_detect)
    with pytest.raises(ValueError, match="origin"):
        await remediate._detect_page(sf, EvidenceStore(tmp_path), None, None, "run", "http://127.0.0.1:5000",
                                     {"path": "@evil.example/", "label": "x"}, remediate.RemediationConfig(repo_path=tmp_path, dev_command="x {port}"))
    assert calls == []


# ---------------------------------------------------------------- NUL bytes (Postgres rejects them)
def test_strip_nul_is_deep_and_touches_nothing_else():
    assert strip_nul({"a\x00": ["x\x00y", {"k": "\x00"}], "n": 5, "z": None}) == {"a": ["xy", {"k": ""}], "n": 5, "z": None}
    assert strip_nul("héllo \u202e") == "héllo \u202e"


async def test_a_model_reply_containing_nul_is_stored_cleanly_on_both_databases(sf, tmp_path):
    from fakes import FakeTriage
    from test_triage import make_finding

    from auditor.triage.triage import triage_one

    class Nul(FakeTriage):
        def _o(self, **kw):
            o = super()._o(**kw)
            o["impact_text"] = "A screen\x00reader user hears\x00 nothing."
            o["fix_plan"] = "Bind\x00 the label"
            return o

    fid, store = make_finding(sf, tmp_path)
    v = await triage_one(sf, store, Nul("good"), "claude-opus-5-5", fid)
    with sf() as db:
        f = db.get(Finding, fid)
        assert f.impact_text == "A screenreader user hears nothing." and "\x00" not in f.impact_text
        assert db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == fid)).fix_plan == "Bind the label"
        for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == fid)):
            assert "\x00" not in str(e.payload)
        assert f.status == FindingStatus.triaged and v.valid

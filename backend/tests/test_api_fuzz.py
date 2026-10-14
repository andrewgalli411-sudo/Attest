"""Property-based fuzzing of the whole API, generated from its own OpenAPI schema (schemathesis + hypothesis).

Invariants, for thousands of generated requests per endpoint, including malformed and boundary-value ones:
  * never a 5xx (a server error is a bug, whatever the input);
  * every response matches the schema the API publishes;
  * the server is still healthy afterwards.
The app is configured so nothing heavy can start (no demo, no custom runs); data endpoints see a seeded run so they
also exercise their success paths, not just 404s.
"""

import os

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance
from starlette.testclient import TestClient

from auditor.config import Settings
from auditor.devtools.scripted_client import ScriptedClient
from auditor.main import create_app
from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingLocator,
    PageState,
    RunStatus,
    Severity,
)
from auditor.storage.evidence_store import EvidenceStore

TOKEN = "fuzz-token-1234567890"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def fuzz_app(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from auditor.db import Base
    from auditor.seed import seed_criteria

    tmp = tmp_path_factory.mktemp("fuzz")
    engine = create_engine(f"sqlite:///{tmp}/f.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    with sf() as db:
        seed_criteria(db)
        run = AuditRun(id="a" * 32, target_url="demo", status=RunStatus.complete,
                       summary={"headline": "1 issues", "by_severity": {}, "score_before": {"score": 40, "worst": 40},
                                "score_after": {"score": 80, "worst": 80}})
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url="http://x/", state_label="home")
        db.add(ps)
        db.flush()
        f = Finding(id="b" * 32, run_id=run.id, page_state_id=ps.id, rule_id="label", sc_ids=["3.3.2"],
                    severity=Severity.critical, source="axe", dedupe_key="k", impact_text="x")
        db.add(f)
        db.flush()
        db.add(FindingLocator(finding_id=f.id, fingerprint={"tag": "input"}))
        db.add(FindingEvidence(id="c" * 32, finding_id=f.id, stage="caught", kind="dom_snippet", payload={"html": "<p>"}))
        db.commit()
    cfg = Settings(allowed_hosts=["testserver"], api_token=TOKEN, enable_docs=True, demo_enabled=False,
                   allow_custom_runs=False)
    return create_app(sf, store=EvidenceStore(tmp / "ev"), client_factory=ScriptedClient, work_dir=str(tmp), config=cfg)


schema = schemathesis.pytest.from_fixture("fuzz_schema")


@pytest.fixture(scope="module")
def fuzz_schema(fuzz_app):
    return schemathesis.openapi.from_asgi("/openapi.json", fuzz_app, headers=HEADERS)


@schema.parametrize()
@settings(max_examples=int(os.environ.get("A11Y_FUZZ_EXAMPLES", "60")), deadline=None, suppress_health_check=list(HealthCheck))
def test_no_endpoint_ever_returns_a_server_error_or_a_schema_violation(case):
    case.call_and_validate(headers=HEADERS, checks=[not_a_server_error, response_schema_conformance])


def test_the_server_is_still_healthy_after_the_fuzzing(fuzz_app):
    c = TestClient(fuzz_app)
    assert c.get("/api/health").json()["status"] == "ok"
    assert c.get("/api/runs", headers={**HEADERS, "host": "testserver"}).status_code == 200



# ---------------------------------------------------------------------------------------- accounts mode (multi-user)
ACCT_HEADERS = {"Cookie": "attest_session=fuzz-session-token", "X-Requested-With": "attest"}


@pytest.fixture(scope="module")
def fuzz_accounts_app(tmp_path_factory):
    """The same fuzzing, but signed in as a person on a multi-user server with a project, a fake GitHub and a fake network."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from cryptography.fernet import Fernet
    from fakegithub import FakeGitHub
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from auditor.db import Base
    from auditor.models import Account, ProjectRecord, UserSession
    from auditor.seed import seed_criteria

    tmp = tmp_path_factory.mktemp("fuzz-accounts")
    engine = create_engine(f"sqlite:///{tmp}/f.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    gh = FakeGitHub()
    gh.add_user("alice", 101)
    gh.add_installation(1, "alice", ["alice/web", "alice/api"])
    with sf() as db:
        seed_criteria(db)
        acct = Account(github_id=101, login="alice")
        db.add(acct)
        db.flush()
        db.add(UserSession(token_hash=hashlib.sha256(b"fuzz-session-token").hexdigest(), account_id=acct.id,
                           sealed_github_token=None, expires_at=datetime.now(UTC) + timedelta(days=1)))
        db.add(ProjectRecord(id="d" * 32, owner_id=acct.id, name="web", github_repo="alice/web", installation_id=1,
                             site_url="https://app.example.com", pages=[{"path": "/", "label": "home"}], verification_token="t" * 32))
        run = AuditRun(id="a" * 32, owner_id=acct.id, target_url="project:web", status=RunStatus.complete, config={"mode": "remote"},
                       summary={"headline": "1 issues", "score_before": {"score": 40, "worst": 40}})
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url="https://app.example.com/", state_label="home")
        db.add(ps)
        db.flush()
        db.add(Finding(id="b" * 32, run_id=run.id, page_state_id=ps.id, rule_id="label", sc_ids=["3.3.2"], severity=Severity.critical,
                       source="axe", dedupe_key="k", impact_text="x"))
        db.commit()
    key = Fernet.generate_key().decode()
    cfg = Settings(allowed_hosts=["testserver"], auth_mode="accounts", secrets_key=key, github_client_id="cid", github_client_secret="csec",
                   github_webhook_secret="fuzz-webhook-secret", public_url="http://testserver", enable_docs=True, demo_enabled=False)
    app = create_app(sf, store=EvidenceStore(tmp / "ev"), client_factory=ScriptedClient, work_dir=str(tmp), config=cfg)
    app.state.github_transport = gh.transport
    # sealed token for the seeded session, so the repo list talks to the fake GitHub
    with sf() as db:
        db.scalar(__import__("sqlalchemy").select(UserSession)).sealed_github_token = app.state.deps.vault.seal("gho_secret_alice")
        db.commit()

    class NoNetwork:  # verification must never touch the internet while fuzzing
        async def get_text(self, *a, **k):
            from auditor.safe_net import FetchError

            raise FetchError("no network in tests")

        async def txt_records(self, *a, **k):
            return []
    app.state.deps.net = NoNetwork()
    return app


@pytest.fixture(scope="module")
def fuzz_accounts_schema(fuzz_accounts_app):
    return schemathesis.openapi.from_asgi("/openapi.json", fuzz_accounts_app, headers=ACCT_HEADERS)


accounts_schema = schemathesis.pytest.from_fixture("fuzz_accounts_schema")


@accounts_schema.parametrize()
@settings(max_examples=int(os.environ.get("A11Y_FUZZ_EXAMPLES", "60")), deadline=None, suppress_health_check=list(HealthCheck))
def test_no_multi_user_endpoint_ever_returns_a_server_error_or_a_schema_violation(case):
    case.call_and_validate(headers=ACCT_HEADERS, checks=[not_a_server_error, response_schema_conformance])


def test_the_multi_user_server_is_still_healthy_and_the_seeded_data_is_intact(fuzz_accounts_app):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from auditor.models import Account, UserSession

    # The fuzzer may well have called /auth/logout (and logout works), so sign in again with a fresh session.
    d = fuzz_accounts_app.state.deps
    with d.sf() as db:
        db.add(UserSession(token_hash=hashlib.sha256(b"after-fuzz").hexdigest(), account_id=db.scalar(select(Account)).id,
                           expires_at=datetime.now(UTC) + timedelta(days=1)))
        db.commit()
    c = TestClient(fuzz_accounts_app)
    h = {"Cookie": "attest_session=after-fuzz", "X-Requested-With": "attest", "host": "testserver"}
    assert c.get("/api/health").json()["status"] == "ok"
    r = c.get("/api/runs", headers=h)
    assert r.status_code == 200 and "a" * 32 in [x["id"] for x in r.json()]
    assert "web" in [p["name"] for p in c.get("/api/projects", headers=h).json()]

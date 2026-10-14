"""Fair use on a shared server: one run at a time per person, daily and monthly caps, rate limits, and an off switch."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from starlette.requests import Request
from test_accounts import H, browser, sign_in

from auditor.admin import list_accounts, set_disabled
from auditor.api import routes
from auditor.models import Account, AuditRun, ProofJob, RunStatus, UserSession, WebhookDelivery
from auditor.purge import purge_housekeeping, purge_runs
from auditor.ratelimit import RateLimiter, client_ip
from auditor.remediate import record_tokens
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(300)


# ---- the limiter itself
def test_the_limiter_allows_a_burst_then_says_how_long_to_wait():
    now = [100.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert [rl.retry_after("x", "a", 3) for _ in range(3)] == [None, None, None]
    wait = rl.retry_after("x", "a", 3)
    assert wait is not None and 59 <= wait <= 60
    now[0] += 30
    assert 29 <= rl.retry_after("x", "a", 3) <= 30
    now[0] += 31
    assert rl.retry_after("x", "a", 3) is None  # the window slid past the first uses


def test_callers_and_limits_are_independent_and_zero_means_unlimited():
    rl = RateLimiter(clock=lambda: 0.0)
    for _ in range(5):
        rl.retry_after("runs", "alice", 5)
    assert rl.retry_after("runs", "alice", 5) is not None
    assert rl.retry_after("runs", "bob", 5) is None and rl.retry_after("repos", "alice", 5) is None
    assert all(rl.retry_after("free", "x", 0) is None for _ in range(1000))


def test_a_flood_of_made_up_callers_cannot_grow_the_table_without_bound():
    rl = RateLimiter(clock=lambda: 0.0, max_keys=100)
    for i in range(10_000):
        rl.retry_after("login", f"10.0.{i // 250}.{i % 250}", 5)
    assert len(rl._hits) <= 100


def request(peer="10.1.1.1", xff=None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 5000), "method": "GET", "path": "/"})


def test_forwarded_for_is_only_believed_when_the_operator_says_how_many_proxies_they_run():
    spoof = "6.6.6.6, 7.7.7.7"
    assert client_ip(request(xff=spoof), 0) == "10.1.1.1", "with no trusted proxy the header is just caller-supplied text"
    assert client_ip(request(xff="1.2.3.4"), 1) == "1.2.3.4"
    assert client_ip(request(xff="6.6.6.6, 1.2.3.4"), 1) == "1.2.3.4", "extra entries on the left were written by the caller"
    assert client_ip(request(xff="6.6.6.6, 5.5.5.5, 1.2.3.4"), 2) == "5.5.5.5"
    assert client_ip(request(xff="1.2.3.4"), 2) == "10.1.1.1", "fewer entries than trusted proxies: do not trust it"
    assert client_ip(request(xff="not-an-address"), 1) == "10.1.1.1"
    assert client_ip(request(), 1) == "10.1.1.1"


# ---- quotas, through the API
def seed(sf, owner_login, *, n=1, status=RunStatus.complete, age=timedelta(0), tokens=0, fresh=False):
    with sf() as db:
        owner = db.scalar(select(Account).where(Account.login == owner_login)).id
        for _ in range(n):
            db.add(AuditRun(owner_id=owner, target_url="project:web", status=status, created_at=datetime.now(UTC) - age,
                            model_tokens=tokens, heartbeat_at=datetime.now(UTC) if fresh else None))
        db.commit()


@pytest.fixture
def spy(monkeypatch):
    """Capture what a run would be started with, without starting it."""
    seen = []

    async def fake_execute(d, run_id, body, cfg):
        seen.append(cfg)
    monkeypatch.setattr(routes, "_execute", fake_execute)
    return seen


async def test_one_run_at_a_time_per_person_and_other_people_are_not_affected(world, sf, spy):
    async with browser(world.app) as alice, browser(world.app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        await world.attach(alice)
        await world.attach(bob, "bob")
        seed(sf, "alice", status=RunStatus.detecting, fresh=True)
        r = await alice.post("/api/runs", headers=H, json={"project": "web"})
        assert r.status_code == 429 and "already have 1 run" in r.json()["detail"] and r.headers["retry-after"]
        assert (await bob.post("/api/runs", headers=H, json={"project": "web"})).status_code == 202  # his own allowance
        await asyncio.sleep(0.2)  # the run itself starts as a background task
        assert len(spy) == 1


async def test_a_stale_unfinished_run_does_not_block_you_forever(world, sf, spy):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)
        seed(sf, "alice", status=RunStatus.detecting, age=timedelta(minutes=10), fresh=False)  # its worker died long ago
        assert (await c.post("/api/runs", headers=H, json={"project": "web"})).status_code == 202


async def test_the_daily_limit_counts_the_last_24_hours_only(world, sf, spy):
    world.app.state.deps.cfg.max_runs_per_day = 3
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)
        seed(sf, "alice", n=5, age=timedelta(hours=30))  # yesterday's runs do not count
        seed(sf, "alice", n=3, age=timedelta(hours=2))
        r = await c.post("/api/runs", headers=H, json={"project": "web"})
        assert r.status_code == 429 and "daily limit reached (3 runs in 24 hours)" in r.json()["detail"]
        assert 60 <= int(r.headers["retry-after"]) <= 22 * 3600
        assert spy == []
        usage = (await c.get("/api/usage")).json()
        assert usage["runs_today"] == 3 and usage["runs_per_day"] == 3


async def test_the_monthly_model_allowance_caps_spending_and_shrinks_each_runs_budget(world, sf, spy):
    d = world.app.state.deps
    d.cfg.monthly_model_token_cap, d.cfg.model_token_budget = 1000, 800
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)
        seed(sf, "alice", tokens=700)
        seed(sf, "alice", tokens=10_000_000, age=timedelta(days=45))  # last month's spending does not count
        assert (await c.post("/api/runs", headers=H, json={"project": "web"})).status_code == 202
        await asyncio.sleep(0.2)
        assert spy[0].model_token_budget == 300, "a run may spend only what the month has left (1000 - 700), not its usual 800"
        with sf() as db:  # the run just started has finished (the stubbed executor never ran it)
            for run in db.scalars(select(AuditRun).where(AuditRun.status == RunStatus.queued)):
                run.status = RunStatus.complete
            db.commit()
        seed(sf, "alice", tokens=300, status=RunStatus.complete)
        r = await c.post("/api/runs", headers=H, json={"project": "web"})
        assert r.status_code == 429 and "allowance is used up" in r.json()["detail"]
        usage = (await c.get("/api/usage")).json()
        assert usage["model_tokens_this_month"] == 1000 and usage["model_tokens_cap"] == 1000 and usage["projects"] == 1


async def test_limits_set_to_zero_are_off(world, sf, spy):
    d = world.app.state.deps
    d.cfg.max_runs_per_day = d.cfg.monthly_model_token_cap = d.cfg.max_runs_per_account = 0
    d.cfg.max_concurrent_runs = 1000  # the per-person limits are off; the server-wide cap is a separate setting
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        await world.attach(c)
        seed(sf, "alice", n=50, tokens=9_999_999, status=RunStatus.detecting, fresh=True)
        assert (await c.post("/api/runs", headers=H, json={"project": "web"})).status_code == 202


async def test_a_failed_run_still_counts_what_it_spent(world, sf):
    class Spent:
        used = 4321
    with sf() as db:
        acct = Account(github_id=1, login="x")
        db.add(acct)
        db.flush()
        run = AuditRun(owner_id=acct.id, target_url="project:web", status=RunStatus.failed)
        db.add(run)
        db.commit()
        rid = run.id
    record_tokens(sf, rid, Spent())
    with sf() as db:
        assert db.get(AuditRun, rid).model_tokens == 4321


# ---- rate limits, through the API
async def test_rate_limits_are_per_person_and_report_when_to_come_back(world):
    async with browser(world.app) as alice, browser(world.app) as bob:
        await sign_in(alice, "alice")
        await sign_in(bob, "bob")
        statuses = [(await alice.post("/api/projects", headers=H, json={})).status_code for _ in range(12)]
        assert statuses[:10] == [422] * 10 and statuses[10:] == [429, 429]
        r = await alice.post("/api/projects", headers=H, json={})
        assert "too many requests" in r.json()["detail"] and 1 <= int(r.headers["retry-after"]) <= 60
        assert (await bob.post("/api/projects", headers=H, json={})).status_code == 422  # bob has his own allowance


async def test_signing_in_is_limited_per_address_and_a_forged_forwarded_header_does_not_help(world):
    world.app.state.deps.cfg.rl_login_per_min = 3
    async with browser(world.app) as c:
        codes = [(await c.get("/api/auth/github/login", headers={"X-Forwarded-For": f"9.9.9.{i}"})).status_code for i in range(6)]
        assert codes == [302, 302, 302, 429, 429, 429], "inventing a new address in each request must not evade the limit"
    world.app.state.deps.cfg.trusted_proxy_hops = 1
    world.app.state.deps.limiter = RateLimiter()
    async with browser(world.app) as c:  # behind one trusted proxy, the address that proxy saw is the one counted
        mixed = [(await c.get("/api/auth/github/login", headers={"X-Forwarded-For": f"6.6.6.{i}, 1.2.3.{i % 2}"})).status_code for i in range(6)]
        assert mixed == [302] * 6  # two real addresses, each within its allowance of 3
        more = (await c.get("/api/auth/github/login", headers={"X-Forwarded-For": "5.5.5.5, 1.2.3.0"})).status_code
        assert more == 429  # the 4th from 1.2.3.0, whatever was written to its left


async def test_webhooks_are_limited_before_anything_else_is_looked_at(world):
    world.app.state.deps.cfg.rl_webhook_per_min = 2
    async with browser(world.app) as c:
        codes = [(await c.post("/api/github/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})).status_code for _ in range(4)]
        assert codes == [401, 401, 429, 429]


# ---- the off switch
async def test_disabling_an_account_ends_its_sessions_and_blocks_signing_in_again(world, sf):
    async with browser(world.app) as c:
        await sign_in(c, "alice")
        assert (await c.get("/api/runs")).status_code == 200
        assert [r["login"] for r in list_accounts(sf)] == ["alice"] and set_disabled(sf, "nobody", True) is False
        assert set_disabled(sf, "alice", True) is True
        assert (await c.get("/api/runs")).status_code == 401, "an existing session stops working at once"
        with sf() as db:
            assert db.scalars(select(UserSession)).all() == []
        r = await sign_in(c, "alice")
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
        assert list_accounts(sf)[0]["disabled"] is True
        assert set_disabled(sf, "alice", False) is True
        assert (await sign_in(c, "alice")).status_code == 302 and (await c.get("/api/runs")).status_code == 200


# ---- housekeeping
def test_purging_old_runs_also_removes_their_proof_jobs_and_old_bookkeeping(sf, tmp_path):
    now = datetime.now(UTC)
    with sf() as db:
        acct = Account(github_id=7, login="p")
        db.add(acct)
        db.flush()
        old = AuditRun(owner_id=acct.id, target_url="x", status=RunStatus.complete, created_at=now - timedelta(days=90))
        db.add(old)
        db.flush()
        db.add(ProofJob(run_id=old.id, branch="attest/x", deployment_sha="a" * 40, preview_url="https://x.vercel.app", status="done"))
        db.add(UserSession(token_hash="h1", account_id=acct.id, expires_at=now - timedelta(days=1)))
        db.add(UserSession(token_hash="h2", account_id=acct.id, expires_at=now + timedelta(days=1)))
        db.add(WebhookDelivery(id="old-delivery-1", event="ping", received_at=now - timedelta(days=60)))
        db.add(WebhookDelivery(id="new-delivery-1", event="ping", received_at=now))
        db.commit()
        old_id = old.id
    assert purge_runs(sf, EvidenceStore(tmp_path / "ev"), timedelta(days=30)) == [old_id]  # a foreign key would have refused this
    assert purge_housekeeping(sf) == {"expired_sessions": 1, "old_webhook_deliveries": 1}
    with sf() as db:
        assert db.scalars(select(ProofJob)).all() == []
        assert [s.token_hash for s in db.scalars(select(UserSession))] == ["h2"]
        assert [w.id for w in db.scalars(select(WebhookDelivery))] == ["new-delivery-1"]


async def test_a_session_that_slips_in_while_an_account_is_being_disabled_is_still_refused(world, sf):
    """Disabling deletes sessions and sign-in refuses the account, so this is the belt and braces for a race: if a session row
    exists anyway, the lookup itself must not honour it."""
    import hashlib

    async with browser(world.app) as c:
        await sign_in(c, "alice")
        with sf() as db:
            acct = db.scalar(select(Account))
            acct.disabled_at = datetime.now(UTC)  # switched off, but its session was not (yet) removed
            db.commit()
        assert hashlib.sha256(c.cookies["attest_session"].encode()).hexdigest() in [s.token_hash for s in sf().scalars(select(UserSession))]
        assert (await c.get("/api/runs")).status_code == 401

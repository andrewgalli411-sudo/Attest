from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from auditor import models
from auditor.demo import DEV_COMMAND, SEED_AUTH
from auditor.devtools.scripted_client import ScriptedClient
from auditor.models import AuditRun, Finding, RunStatus
from auditor.pipeline.events import EventBus
from auditor.purge import purge_runs
from auditor.remediate import RemediationConfig, run_remediation
from auditor.storage.evidence_store import EvidenceStore

ALL_TABLES = [t for t in models.Base.metadata.sorted_tables if t.name not in ("criterion",)]


def counts(sf):
    with sf() as db:
        return {t.name: db.scalar(select(func.count()).select_from(t)) for t in ALL_TABLES}


async def real_run(sf, store, repo):
    with sf() as db:
        run = AuditRun(target_url="demo")
        db.add(run)
        db.commit()
        rid = run.id
    cfg = RemediationConfig(repo_path=repo, dev_command=DEV_COMMAND, check_commands=["pnpm build"],
                            pages=[{"path": "/", "label": "profile"}], auth=SEED_AUTH)
    await run_remediation(rid, sf, store, EventBus(sf), ScriptedClient(), cfg)
    return rid


async def test_purging_a_real_run_removes_every_row_and_file_and_nothing_else(sf, tmp_path, demo_repo):
    repo, _ = demo_repo
    store = EvidenceStore(tmp_path / "ev")
    keep = await real_run(sf, store, repo)  # a recent run that must survive
    with sf() as db:  # a second, old, finished run - made by hand so the test does not pay for another full run
        old = AuditRun(target_url="old", status=RunStatus.complete, created_at=datetime.now(UTC) - timedelta(days=90))
        running = AuditRun(target_url="old-but-running", status=RunStatus.fixing, created_at=datetime.now(UTC) - timedelta(days=90))
        db.add_all([old, running])
        db.commit()
        old_id, running_id = old.id, running.id
    (store.root / old_id / "p").mkdir(parents=True)
    (store.root / old_id / "p" / "shot.png").write_bytes(b"x")

    before = counts(sf)
    assert before["finding"] > 0 and before["finding_evidence"] > 0

    assert purge_runs(sf, store, timedelta(days=30), dry_run=True) == [old_id]  # dry run changes nothing
    assert counts(sf) == before and (store.root / old_id).exists()

    assert purge_runs(sf, store, timedelta(days=30)) == [old_id]
    assert not (store.root / old_id).exists()
    with sf() as db:
        assert db.get(AuditRun, old_id) is None
        assert db.get(AuditRun, running_id) is not None  # old but still running: never deleted
        assert db.get(AuditRun, keep) is not None
    after = counts(sf)
    assert after["audit_run"] == before["audit_run"] - 1
    assert {k: v for k, v in after.items() if k != "audit_run"} == {k: v for k, v in before.items() if k != "audit_run"}, \
        "purging an unrelated old run must not touch the recent run's data"

    # now purge the recent run too (pretend it is old): every table must be empty of its data, files gone
    run_dir = store.root / keep
    assert run_dir.exists()
    assert purge_runs(sf, store, timedelta(days=30), now=datetime.now(UTC) + timedelta(days=60)) == [keep]
    assert not run_dir.exists()
    left = counts(sf)
    assert {k: v for k, v in left.items() if v} == {"audit_run": 1}, left  # only the still-running run remains
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(Finding)) == 0


def test_purge_never_deletes_outside_the_evidence_root(sf, tmp_path):
    store = EvidenceStore(tmp_path / "ev")
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    with sf() as db:
        r = AuditRun(target_url="x", status=RunStatus.failed, created_at=datetime.now(UTC) - timedelta(days=99))
        db.add(r)
        db.commit()
        rid = r.id
    assert purge_runs(sf, store, timedelta(days=1)) == [rid]
    assert (outside / "keep.txt").exists()

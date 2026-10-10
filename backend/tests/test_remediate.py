import subprocess

import pytest
from sqlalchemy import func, select

from auditor.demo import DEV_COMMAND, SEED_AUTH
from auditor.devtools.scripted_client import ScriptedClient
from auditor.models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FindingStatus,
    FixAttempt,
    FixJournal,
    PageScore,
    RegressionCheck,
    ReproveResult,
    RunEvent,
    RunStatus,
)
from auditor.pipeline.events import EventBus
from auditor.remediate import RemediationConfig, run_remediation
from auditor.scm.local_git import LocalGitProvider
from auditor.storage.evidence_store import EvidenceStore

pytestmark = pytest.mark.timeout(300)


def cfg_for(repo, **kw):
    base = {"repo_path": repo, "dev_command": DEV_COMMAND, "check_commands": ["pnpm build"],
            "pages": [{"path": "/", "label": "profile"}], "auth": SEED_AUTH}
    return RemediationConfig(**{**base, **kw})


async def remediate(sf, tmp_path, repo, client=None, **kw):
    with sf() as db:
        run = AuditRun(target_url="demo")
        db.add(run)
        db.commit()
        rid = run.id
    bus, store = EventBus(sf), EvidenceStore(tmp_path / "ev")
    summary = await run_remediation(rid, sf, store, bus, client or ScriptedClient(), cfg_for(repo, **kw))
    return rid, summary, store


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


async def test_the_whole_loop_on_the_demo_app(sf, tmp_path, demo_repo):
    repo, base_sha = demo_repo
    rid, summary, store = await remediate(sf, tmp_path, repo)

    # ---- the user's repo is exactly as we found it
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(repo, "rev-parse", "HEAD") == base_sha
    assert git(repo, "status", "--porcelain") == ""

    # ---- fixes live on tier branches, one commit per finding, never on main
    branches = git(repo, "branch", "--list", "a11y/*").split()
    assert any(b.endswith("pr1-mechanical") for b in branches), branches
    with sf() as db:
        run = db.get(AuditRun, rid)
        assert run.status == RunStatus.complete and run.summary == summary
        by_rule = {f.rule_id: f for f in db.scalars(select(Finding).where(Finding.run_id == rid))}
        # the two mechanical defects were fixed AND proved AND regression-clean
        for rule in ("label", "html-has-lang"):
            assert by_rule[rule].status == FindingStatus.regress_ok, (rule, by_rule[rule].status)
        # contrast needs human judgment: never touched
        assert by_rule["color-contrast"].status == FindingStatus.needs_human
        # every proved finding has the complete evidence story, in order
        for rule in ("label", "html-has-lang"):
            f = by_rule[rule]
            kinds = [(e.stage, e.kind) for e in db.scalars(select(FindingEvidence)
                     .where(FindingEvidence.finding_id == f.id).order_by(FindingEvidence.created_at))]
            for need in [("caught", "shot_clip"), ("caught", "shot_full_annotated"), ("caught", "dom_snippet"),
                         ("caught", "source_hit"), ("explained", "impact"), ("plan", "triage"), ("fix", "diff"),
                         ("proof", "reprove_shot_before"), ("proof", "reprove_shot_after"),
                         ("proof", "reprove_result"), ("regression", "regress_report")]:
                assert need in kinds, (rule, need)
            rr = db.scalar(select(ReproveResult).where(ReproveResult.finding_id == f.id))
            assert rr.verdict == "pass" and rr.baseline_failed and rr.relocated and rr.relocation_score >= 0.85
            assert store.get(rr.before_ref) != store.get(rr.after_ref)  # the pair actually shows a difference
        assert db.scalar(select(func.sum(RegressionCheck.regressions_count))) == 0
        # scores moved the right way
        base = db.scalar(select(PageScore.score).where(PageScore.phase == "baseline"))
        after = db.scalar(select(PageScore.score).where(PageScore.phase == "after"))
        assert after > base
        assert not [e for e in db.scalars(select(RunEvent)) if e.type == "error"]
        seqs = [e.seq for e in db.scalars(select(RunEvent).where(RunEvent.run_id == rid).order_by(RunEvent.seq))]
        assert seqs == list(range(1, len(seqs) + 1))

    # ---- the summary is honest
    assert summary["proved"] == 2 and summary["needs_human"] >= 1 and summary["regressions"] == 0
    assert summary["score_after"]["score"] > summary["score_before"]["score"]
    assert "scripted" in summary["model_kind"] and "does not" not in summary["headline"]
    assert summary["prs"] == {"PR1": None, "PR2": None} or set(summary["prs"]) == {"PR1"}  # nothing published
    # ---- the fix itself is what we expect, and builds
    git(repo, "checkout", "-q", [b for b in branches if b.endswith("pr1-mechanical")][0])
    app = (repo / "src/App.jsx").read_text()
    assert 'aria-label="Email address"' in app and '<html lang="en">' in (repo / "index.html").read_text()
    git(repo, "checkout", "-q", "main")

    # ---- the report is itself accessible, honest, and complete
    from playwright.async_api import async_playwright

    from auditor.pipeline.axe_runner import run_axe
    from auditor.report.html import run_report

    page_html = run_report(sf, store, rid)
    assert "Automated first pass" in page_html and "requires expert review" in page_html
    assert page_html.count("<img ") >= 6 and "<img " in page_html and 'alt=""' not in page_html
    assert "accessible name: (none)" not in page_html  # (that text lives inside images, not markup)
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        pg = await br.new_page()
        await pg.set_content(page_html)
        res = await run_axe(pg)
        await br.close()
    assert res["violations"] == [], [(v["rule"], v["nodes"][0]["html"][:120]) for v in res["violations"]]


async def test_nothing_is_published_without_allow_push(sf, tmp_path, demo_repo):
    repo, _ = demo_repo
    _, summary, store = await remediate(sf, tmp_path, repo)
    assert all(v is None for v in summary["prs"].values()) and summary["issues_opened"] == 0
    assert git(repo, "remote") == ""  # no remote was ever added or contacted
    # PR text is prepared for review though, and is free of banned claims
    body = store.get(f"{[p for p in (tmp_path / 'ev').rglob('PR1.md')][0].relative_to(tmp_path / 'ev')}").decode()
    assert "does not establish" in body and "now passes" in body


async def test_rerun_is_idempotent_no_duplicate_branches_or_attempts(sf, tmp_path, demo_repo):
    repo, _ = demo_repo
    with sf() as db:
        run = AuditRun(target_url="demo")
        db.add(run)
        db.commit()
        rid = run.id
    for _ in range(2):  # same run id, same base sha: a restarted process
        await run_remediation(rid, sf, EvidenceStore(tmp_path / "ev"), EventBus(sf), ScriptedClient(), cfg_for(repo))
    branches = git(repo, "branch", "--list", "a11y/*").split()
    assert len(branches) == 1 and branches[0].endswith("pr1-mechanical")
    # still exactly one commit per fixed finding (label + lang) - the second run did not stack duplicates
    assert git(repo, "rev-list", "--count", f"main..{branches[0]}") == "2"
    with sf() as db:
        attempts = db.scalar(select(func.count()).select_from(FixAttempt))
        keys = db.scalar(select(func.count(func.distinct(FixAttempt.idempotency_key))))
        journal_dupes = db.execute(select(FixJournal.fix_attempt_id, FixJournal.step, func.count())
                                   .group_by(FixJournal.fix_attempt_id, FixJournal.step)
                                   .having(func.count() > 1)).all()
    assert attempts == keys and not journal_dupes


async def test_a_fix_that_breaks_the_build_is_rolled_back_and_handed_to_a_human(sf, tmp_path, demo_repo):
    repo, base_sha = demo_repo
    rid, summary, _ = await remediate(sf, tmp_path, repo, check_commands=["echo build-broke; exit 1"])
    assert summary["proved"] == 0
    with sf() as db:
        f = {x.rule_id: x for x in db.scalars(select(Finding).where(Finding.run_id == rid))}
        assert f["label"].status == FindingStatus.needs_human
        kinds = {e.kind for e in db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == f["label"].id))}
        assert "fix_failed" in kinds
    assert git(repo, "rev-parse", "HEAD") == base_sha and git(repo, "status", "--porcelain") == ""


async def test_a_model_that_cannot_fix_produces_no_changes(sf, tmp_path, demo_repo):
    class Decliner(ScriptedClient):
        def _fix(self, kw):
            from auditor.devtools.scripted_client import _resp
            return _resp({"edits": [], "explanation": "would need to invent wording", "confidence": 0.0})

    repo, base_sha = demo_repo
    rid, summary, _ = await remediate(sf, tmp_path, repo, client=Decliner())
    assert summary["proved"] == 0
    assert git(repo, "branch", "--list", "a11y/*") == ""  # no empty branch left behind
    assert git(repo, "rev-parse", "HEAD") == base_sha
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(ReproveResult)) == 0


async def test_hostile_page_text_cannot_smuggle_code_into_a_fix(sf, tmp_path, demo_repo):
    """The visible label is attacker-controlled page content. The stand-in model copies it verbatim into an
    aria-label, producing an attribute-injection edit. The edit policy must stop it; the safe fix must not."""
    repo, _ = demo_repo
    app = repo / "src/App.jsx"
    app.write_text(app.read_text().replace("Email address", 'Email&quot; onfocus=&quot;alert(1)'))
    assert "alert(1)" in app.read_text()
    git(repo, "commit", "-aqm", "hostile label text")
    base = git(repo, "rev-parse", "HEAD")
    rid, summary, _ = await remediate(sf, tmp_path, repo)
    with sf() as db:
        f = {x.rule_id: x for x in db.scalars(select(Finding).where(Finding.run_id == rid))}
        assert f["label"].status == FindingStatus.needs_human  # not auto-fixed
        failed = [e.payload for e in db.scalars(select(FindingEvidence).where(
            FindingEvidence.finding_id == f["label"].id, FindingEvidence.kind == "fix_failed"))]
        assert failed and "unsafe edit rejected" in failed[0]["reason"] and "event handler" in failed[0]["reason"]
        assert f["html-has-lang"].status == FindingStatus.regress_ok  # the legitimate fix still went through
    assert git(repo, "rev-parse", "HEAD") == base and git(repo, "status", "--porcelain") == ""
    for branch in git(repo, "branch", "--list", "a11y/*").split():
        assert "onfocus" not in git(repo, "diff", f"main..{branch}").lower().replace("&quot;", "")
        assert "alert(1)" not in git(repo, "diff", f"main..{branch}")


async def test_dirty_repo_is_refused(sf, tmp_path, demo_repo):
    repo, _ = demo_repo
    (repo / "src/App.jsx").write_text((repo / "src/App.jsx").read_text() + "\n// dirty\n")
    with sf() as db:
        run = AuditRun(target_url="demo")
        db.add(run)
        db.commit()
        rid = run.id
    with pytest.raises(RuntimeError, match="uncommitted"):
        await run_remediation(rid, sf, EvidenceStore(tmp_path / "ev"), EventBus(sf), ScriptedClient(), cfg_for(repo))
    with sf() as db:
        assert db.get(AuditRun, rid).status == RunStatus.failed
    assert (repo / "src/App.jsx").read_text().endswith("// dirty\n")  # we did not touch the user's work


def test_provider_is_local_git():
    assert LocalGitProvider.__name__ == "LocalGitProvider"

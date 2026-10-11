import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from auditor.scm.base import PushNotAllowed, RepoError
from auditor.scm.local_git import BOT_NAME, LocalGitProvider
from auditor.scm.pr_stage import FixSummary, build_pr, build_work_order

FAKE_GH = """#!/bin/bash
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "pr list")   [ -f "$GH_STATE" ] && cat "$GH_STATE" || echo '[]' ;;
  "pr create") echo "https://example.invalid/pr/1"; echo '[{"url":"https://example.invalid/pr/1"}]' > "$GH_STATE" ;;
  "issue create") echo "https://example.invalid/issues/7" ;;
  *) echo "unexpected: $@" >&2; exit 9 ;;
esac
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True).stdout
    g("init", "-q", "-b", "main")
    g("config", "user.email", "dev@example.invalid")
    g("config", "user.name", "dev")
    (work / "a.txt").write_text("hello\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    g("remote", "add", "origin", str(bare))
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("GH_STATE", str(tmp_path / "gh.state"))
    return SimpleEnv(work, bare, gh, tmp_path / "gh.log", g)


class SimpleEnv:
    def __init__(self, work, bare, gh, log, g):
        self.work, self.bare, self.gh, self.log, self.g = work, bare, gh, log, g

    def provider(self, **kw):
        return LocalGitProvider(self.work, gh_bin=str(self.gh), **kw)

    def gh_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def test_branch_commit_authorship_and_isolation(env):
    p = env.provider()
    base = p.head_sha()
    p.create_branch("a11y/test", base)
    (env.work / "a.txt").write_text("changed\n")
    sha = p.commit("a11y: fix one thing", ["a.txt"])
    assert p.current_branch() == "a11y/test" and sha != base
    assert env.g("log", "-1", "--format=%an").strip() == BOT_NAME
    p.checkout("main")
    assert (env.work / "a.txt").read_text() == "hello\n"  # main untouched


def test_dirty_tree_blocks_branch_switch(env):
    p = env.provider()
    (env.work / "a.txt").write_text("dirty\n")
    with pytest.raises(RepoError, match="uncommitted"):
        p.create_branch("x", p.head_sha())
    with pytest.raises(RepoError, match="uncommitted"):
        p.checkout("main")


def test_empty_commit_and_banned_message_are_refused(env):
    p = env.provider()
    with pytest.raises(RepoError, match="nothing to commit"):
        p.commit("a11y: nothing", ["a.txt"])
    (env.work / "a.txt").write_text("x\n")
    with pytest.raises(RepoError, match="compliance"):
        p.commit("makes the page fully compliant", ["a.txt"])


def test_nothing_leaves_the_machine_without_allow_push(env):
    p = env.provider()  # allow_push defaults to False
    for call in (lambda: p.push("main"), lambda: p.open_pr("b", "main", "t", "b"),
                 lambda: p.open_issue("t", "b")):
        with pytest.raises(PushNotAllowed):
            call()
    assert env.gh_calls() == []
    refs = subprocess.run(["git", "for-each-ref"], cwd=env.bare, capture_output=True, text=True).stdout
    assert refs.strip() == ""  # the bare remote received nothing


def test_push_with_opt_in_reaches_the_remote(env):
    p = env.provider(allow_push=True)
    p.create_branch("a11y/p", p.head_sha())
    (env.work / "a.txt").write_text("c\n")
    p.commit("a11y: c", ["a.txt"])
    p.push("a11y/p")
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=env.bare, capture_output=True,
                          text=True).stdout
    assert "refs/heads/a11y/p" in refs


def test_open_pr_is_idempotent_per_branch(env):
    p = env.provider(allow_push=True)
    u1 = p.open_pr("a11y/p", "main", "title", "body")
    u2 = p.open_pr("a11y/p", "main", "title", "body")
    assert u1 == u2 == "https://example.invalid/pr/1"
    assert sum(1 for c in env.gh_calls() if c.startswith("pr create")) == 1


def test_banned_claims_blocked_even_with_opt_in(env):
    p = env.provider(allow_push=True)
    for t, b in (("fully compliant now", "ok"), ("ok", "This site is WCAG compliant"), ("ok", "certified accessible")):
        with pytest.raises(RepoError, match="compliance"):
            p.open_pr("b", "main", t, b)
        with pytest.raises(RepoError, match="compliance"):
            p.open_issue(t, b)
    assert not any(c.startswith(("pr create", "issue create")) for c in env.gh_calls())


def test_issue_creation_passes_labels(env):
    p = env.provider(allow_push=True)
    assert p.open_issue("work order", "body", ["a11y", "needs-human"]).endswith("/issues/7")
    call = next(c for c in env.gh_calls() if c.startswith("issue create"))
    assert "--label a11y" in call and "--label needs-human" in call


def test_failed_command_raises_with_context(env):
    with pytest.raises(RepoError, match="checkout"):
        env.provider().checkout("no-such-ref")


# ---------- PR text ----------
def fix(**kw):
    base = {"finding_id": "f1", "rule_id": "label", "sc_ids": ["3.3.2"], "severity": "critical",
            "page_url": "http://x/", "impact_text": "A screen-reader user hears an unlabeled field.",
            "explanation": "Adds an accessible name using the visible text.", "diff": "-a\n+b",
            "proof": {"verdict": "pass", "relocation_score": 0.97, "runs_n": 2},
            "regression": {"regressions_count": 0, "verdict": "pass"}}
    return FixSummary(**{**base, **kw})


def test_pr_body_contains_proof_regression_and_standing_line():
    title, body = build_pr("PR1", "run123", [fix()])
    assert "mechanical" in title and "now passes" in body and "similarity 0.97" in body
    assert "0 new findings" in body and "does not establish" in body and "```diff" in body


def test_pr_body_is_honest_when_proof_is_missing_or_negative():
    _, body = build_pr("PR2", "r", [fix(proof=None, regression=None)])
    assert "not yet run" in body
    _, body = build_pr("PR2", "r", [fix(proof={"verdict": "inconclusive", "relocation_score": 0, "runs_n": 2})])
    assert "could not be re-tested" in body and "now passes" not in body


def test_banned_text_in_any_field_is_withheld_from_pr_and_work_order():
    from auditor.disclaimers import contains_banned_claim

    _, body = build_pr("PR1", "r", [fix(explanation="Makes the form compliant")])
    assert "compliant" not in body and "change description withheld" in body
    t, body = build_work_order(fix(impact_text="This is certified accessible"), "contrast")
    assert "certified" not in body and not contains_banned_claim(t) and "impact text withheld" in body
    t, b = build_work_order(fix(), "contrast")
    assert "No code change was proposed" in b and "work order" in t
    assert json.dumps(b)  # serialisable


def test_gh_binary_missing_is_a_clear_error(tmp_path):
    p = LocalGitProvider(tmp_path, allow_push=True, gh_bin="/nonexistent/gh")
    with pytest.raises(FileNotFoundError):
        p.find_existing_pr("x")
    assert os.path.exists(tmp_path)
    assert Path(tmp_path).is_dir()

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor.fix import verify
from auditor.fix.author import author_fix, derive_dom_changes
from auditor.fix.journal import STEPS, Journal, get_or_create_attempt, idempotency_key
from auditor.fix.source_map import SourceHit
from auditor.models import AuditRun, Finding, FixAttempt, PageState, Severity, Tier, TriageVerdict

ANCHOR = '<input name="email" type="email" autoComplete="email" />'
SRC = f'<div className="field">\n  <span className="field-label">Email address</span>\n  {ANCHOR}\n</div>\n'
GOOD = {"file": "src/App.jsx", "anchor_text": ANCHOR,
        "replacement": '<input id="email" name="email" type="email" autoComplete="email" aria-label="Email address" />'}


def repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    return tmp_path


# ---------- applying edits ----------
def test_apply_and_verify_success_returns_diff(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC})
    r = verify.apply_and_verify(repo, [GOOD], {"src/App.jsx"}, ["true"])
    assert r.ok and "aria-label" in r.diff and all(c["ok"] for c in r.checks)
    assert 'aria-label="Email address"' in (repo / "src/App.jsx").read_text()


@pytest.mark.parametrize("mutate,msg", [
    (lambda e: {**e, "anchor_text": "<input nope />"}, "matches 0 times"),
    (lambda e: {**e, "file": "src/Other.jsx"}, "outside the mapped source files"),
    (lambda e: {**e, "file": "../escape.jsx"}, "outside the mapped source files"),
    (lambda e: {**e, "anchor_text": "  "}, "empty anchor"),
    (lambda e: {**e, "replacement": e["anchor_text"]}, "does not change anything"),
    (lambda e: {**e, "replacement": "x" * 3000}, "too large"),
])
def test_bad_edits_are_rejected_and_nothing_is_written(tmp_path, mutate, msg):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC})
    before = (repo / "src/App.jsx").read_text()
    r = verify.apply_and_verify(repo, [mutate(GOOD)], {"src/App.jsx"}, ["true"])
    assert not r.ok and msg in r.error
    assert (repo / "src/App.jsx").read_text() == before


def test_anchor_that_matches_twice_is_rejected(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC + SRC})
    r = verify.apply_and_verify(repo, [GOOD], {"src/App.jsx"}, ["true"])
    assert not r.ok and "matches 2 times" in r.error


def test_path_traversal_and_forbidden_dirs(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC, "node_modules/x.js": "a"})
    (tmp_path.parent / "outside.jsx").write_text("a")
    for f in ("../outside.jsx", "node_modules/x.js", ".git/config"):
        r = verify.apply_and_verify(repo, [{**GOOD, "file": f}], {f}, ["true"])
        assert not r.ok and r.error


def test_too_many_edits_and_huge_change_rejected(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC})
    r = verify.apply_and_verify(repo, [GOOD] * 6, {"src/App.jsx"}, [])
    assert not r.ok and "too many" in r.error
    big = {**GOOD, "replacement": "\n".join(["<br />"] * 60)}
    r = verify.apply_and_verify(repo, [big], {"src/App.jsx"}, [])
    assert not r.ok and "too large" in r.error


def test_failing_check_rolls_back_the_edit(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC})
    before = (repo / "src/App.jsx").read_text()
    r = verify.apply_and_verify(repo, [GOOD], {"src/App.jsx"}, ["echo building; exit 1", "echo never-runs"])
    assert not r.ok and "check failed" in r.error and len(r.checks) == 2  # git diff --check + failing cmd, stops
    assert (repo / "src/App.jsx").read_text() == before
    assert verify.git_diff(repo) == ""


def test_whitespace_errors_fail_git_diff_check(tmp_path):
    repo = repo_with(tmp_path, {"src/App.jsx": SRC})
    ws = {**GOOD, "replacement": GOOD["replacement"] + "   "}
    r = verify.apply_and_verify(repo, [ws], {"src/App.jsx"}, ["true"])
    assert not r.ok and r.checks[0]["cmd"] == "git diff --check"


def test_check_timeout_kills_process_group(tmp_path):
    marker = tmp_path / "alive"
    out = verify.run_checks(tmp_path, [f"sh -c 'sleep 30 & echo $! > {marker}; wait'"], timeout=1)
    assert not out[0]["ok"] and "timed out" in out[0]["tail"]
    import os
    import time
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


# ---------- DOM change derivation ----------
def test_derive_dom_changes_maps_jsx_names_and_values():
    edits = [{"anchor_text": '<input name="e" />',
              "replacement": '<label htmlFor="e">E</label><input id="e" name="e" aria-label="E" className="x" />'}]
    assert derive_dom_changes(edits) == ["aria-label", "class", "for", "id"]
    assert derive_dom_changes([{"anchor_text": "<html>", "replacement": '<html lang="en">'}]) == ["lang"]
    assert derive_dom_changes([{"anchor_text": '<a href="/x">', "replacement": '<a href="/y">'}]) == ["href"]


# ---------- authoring ----------
HIT = SourceHit("src/App.jsx", 3, 2, "react_fiber", ANCHOR, "")


class FakeAuthor:
    def __init__(self, payload=None, stop="end_turn", raw=None):
        self.payload, self.stop, self.raw, self.calls, self.messages = payload, stop, raw, [], self

    async def create(self, **kw):
        self.calls.append(kw)
        text = self.raw if self.raw is not None else json.dumps(self.payload)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=self.stop,
                               usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1}))


def payload(**o):
    base = {"edits": [GOOD], "explanation": "Associates the visible text with the field.", "confidence": 0.95}
    return {**base, **o}


async def author(client, **kw):
    return await author_fix(client, "claude-opus-5-5", {"rule": "label"}, HIT, "bind label", SRC, **kw)


async def test_author_accepts_a_good_edit_and_derives_changes():
    a = await author(FakeAuthor(payload()))
    assert a.valid and a.expected_dom_changes == ["aria-label", "id"]


@pytest.mark.parametrize("o,note", [
    ({"edits": []}, "declined"),
    ({"explanation": "This makes the form fully compliant."}, "banned"),
    ({"edits": [{**GOOD, "file": "src/Evil.jsx"}]}, "expected 'src/App.jsx'"),
    ({"edits": [{**GOOD, "anchor_text": "className"}]}, "occurs 2 times"),
    ({"confidence": 0.5}, "below 0.8"),
])
async def test_author_rejects_bad_output(o, note):
    a = await author(FakeAuthor(payload(**o)))
    assert not a.valid and note in " ".join(a.notes)


@pytest.mark.parametrize("client", [FakeAuthor(raw="not json"), FakeAuthor(payload(), stop="refusal"),
                                    FakeAuthor(payload(), stop="max_tokens")])
async def test_author_handles_garbage_refusal_truncation(client):
    a = await author(client)
    assert not a.valid and a.notes


async def test_author_request_shape_and_previous_error_feedback():
    c = FakeAuthor(payload())
    await author(c, previous_error="anchor matched 0 times")
    kw = c.calls[0]
    assert not ({"temperature", "tool_choice", "thinking"} & set(kw))
    assert "anchor matched 0 times" in kw["messages"][0]["content"]
    assert kw["output_config"]["format"]["type"] == "json_schema"


# ---------- journal / idempotency ----------
def seed_attempt(sf):
    with sf() as db:
        run = AuditRun(target_url="x")
        db.add(run)
        db.flush()
        ps = PageState(run_id=run.id, url="x")
        db.add(ps)
        db.flush()
        f = Finding(run_id=run.id, page_state_id=ps.id, rule_id="label", sc_ids=[], severity=Severity.critical,
                    source="axe", dedupe_key="k")
        db.add(f)
        db.flush()
        v = TriageVerdict(finding_id=f.id, tier=Tier.auto_fix, reason_code="missing_label")
        db.add(v)
        db.commit()
        return f.id, v.id


def test_same_key_returns_same_attempt_and_never_duplicates(sf):
    fid, vid = seed_attempt(sf)
    key = idempotency_key("k", "label", "abc123", "f1")
    a1, c1 = get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr="PR1", repo="r", base_sha="abc123", key=key)
    a2, c2 = get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr="PR1", repo="r", base_sha="abc123", key=key)
    assert a1 == a2 and c1 and not c2
    other = idempotency_key("k", "label", "different-sha", "f1")
    a3, c3 = get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr="PR1", repo="r", base_sha="d", key=other)
    assert c3 and a3 != a1
    with sf() as db:
        assert db.query(FixAttempt).count() == 2


def test_journal_resumes_at_first_missing_step(sf):
    fid, vid = seed_attempt(sf)
    aid, _ = get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr="PR1", repo="r", base_sha="s", key="k1")
    j = Journal(sf, aid)
    assert j.next_step() == "authored" and not j.done("authored")
    for s in ("authored", "applied", "verified"):
        j.record(s)
    assert Journal(sf, aid).next_step() == "committed"  # a fresh object (a "restarted process") resumes
    j.record("committed", "failed", {"why": "x"})        # a failed step is not "done"
    assert j.next_step() == "committed"
    j.record("committed")                                # retried and succeeded: still one row
    assert j.next_step() == "pushed" and j.completed() == STEPS[:4]


def test_concurrent_creators_converge_on_one_attempt(sf):
    import threading
    fid, vid = seed_attempt(sf)
    ids = []

    def go():
        ids.append(get_or_create_attempt(sf, finding_id=fid, verdict_id=vid, tier_pr="PR1", repo="r",
                                         base_sha="s", key="race")[0])

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(ids)) == 1

"""A repository we did not write can try to make us read outside it, write outside it, or run its code."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from auditor.fix import source_map as sm
from auditor.fix import verify
from auditor.fix.verify import EditError
from auditor.gitsafe import git_cmd, is_plain_path
from auditor.scm.local_git import LocalGitProvider

TAG = '<input name="email" type="email" />'


def git(repo, *a):
    return subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "App.jsx").write_text(f"<div>\n  {TAG}\n</div>\n")
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


FP = {"tag": "input", "stable_css": 'input[name="email"][type="email"]', "visible_text": ""}


# ---------------- symlinks ----------------
def test_a_symlink_to_a_file_outside_the_repo_is_never_read(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside-secret.jsx"
    outside.write_text(f"// SECRET-MARKER\n{TAG}\n")
    os.symlink(outside, repo / "src" / "evil.jsx")
    git(repo, "add", "-A")  # git tracks the link itself
    names = {p.name for p in sm.tracked_files(repo)}
    assert "evil.jsx" not in names and "App.jsx" in names
    hits, n = sm.from_grep(repo, FP)
    assert [h.file for h in hits] == ["src/App.jsx"] and n == 1  # the symlinked copy did not create ambiguity or a leak
    assert all("SECRET-MARKER" not in h.excerpt for h in hits)


def test_a_symlinked_directory_is_not_followed(tmp_path):
    repo = make_repo(tmp_path)
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    (outdir / "leak.jsx").write_text(f"// SECRET-MARKER\n{TAG}\n")
    os.symlink(outdir, repo / "src" / "linked")
    git(repo, "add", "-A")
    assert all("leak.jsx" not in str(p) for p in sm.tracked_files(repo))
    assert [h.file for h in sm.from_grep(repo, FP)[0]] == ["src/App.jsx"]


def test_a_symlink_loop_does_not_hang_or_crash(tmp_path):
    repo = make_repo(tmp_path)
    os.symlink(repo / "src", repo / "src" / "loop")
    sm.tracked_files(repo)  # must simply return
    os.symlink("a", repo / "src" / "a")  # self-referential link
    assert [p.name for p in sm.tracked_files(repo)] == ["App.jsx"]


def test_an_edit_cannot_be_written_through_a_symlink(tmp_path):
    repo = make_repo(tmp_path)
    target = tmp_path / "victim.jsx"
    target.write_text(f"<p>{TAG}</p>\n")
    os.symlink(target, repo / "src" / "link.jsx")
    edit = {"file": "src/link.jsx", "anchor_text": TAG, "replacement": TAG.replace("/>", 'aria-label="x" />')}
    r = verify.apply_and_verify(repo, [edit], {"src/link.jsx"}, [], rule_id="label")
    assert not r.ok and "symbolic link" in r.error
    assert target.read_text() == f"<p>{TAG}</p>\n"  # the file outside the repo is untouched


def test_an_edit_cannot_go_through_a_symlinked_directory_either(tmp_path):
    repo = make_repo(tmp_path)
    real = repo / "realdir"
    real.mkdir()
    (real / "x.jsx").write_text(f"<p>{TAG}</p>\n")
    os.symlink(real, repo / "alias")
    edit = {"file": "alias/x.jsx", "anchor_text": TAG, "replacement": TAG.replace("/>", 'aria-label="x" />')}
    r = verify.apply_and_verify(repo, [edit], {"alias/x.jsx"}, [], rule_id="label")
    assert not r.ok and "symbolic link" in r.error


def test_is_plain_path_distinguishes_real_paths_from_links(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    os.symlink(tmp_path / "d", tmp_path / "ld")
    os.symlink(tmp_path / "d" / "f", tmp_path / "lf")
    assert is_plain_path(tmp_path, tmp_path / "d" / "f")
    assert not is_plain_path(tmp_path, tmp_path / "ld" / "f") and not is_plain_path(tmp_path, tmp_path / "lf")
    assert not is_plain_path(tmp_path / "d", tmp_path / "lf")  # outside the root entirely


# ---------------- code that a repository asks git to run ----------------
def _script(path: Path, marker: Path) -> Path:
    path.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_hooks_fsmonitor_and_external_diff_do_not_run(tmp_path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "PWNED"
    for hook in ("pre-commit", "post-commit", "pre-push", "post-checkout", "commit-msg", "prepare-commit-msg"):
        _script(repo / ".git" / "hooks" / hook, marker)
    evil = _script(tmp_path / "evil.sh", marker)
    git(repo, "config", "core.fsmonitor", str(evil))
    git(repo, "config", "diff.external", str(evil))
    git(repo, "config", "core.pager", str(evil))

    p = LocalGitProvider(repo)
    base = p.head_sha()
    assert p.is_clean()
    p.create_branch("a11y/x", base)
    (repo / "src" / "App.jsx").write_text(f"<div>\n  {TAG.replace('/>', 'aria-label=\"x\" />')}\n</div>\n")
    assert "aria-label" in verify.git_diff(repo)
    sha = p.commit("a11y: change", ["src/App.jsx"])
    assert "aria-label" in p.show_diff(sha)
    p.checkout("main")
    sm.tracked_files(repo)
    assert not marker.exists(), "the repository's own hooks/config executed code"
    assert sha != base  # and the work itself still succeeded


def test_the_baseline_proves_the_attack_is_real_without_the_protections(tmp_path):
    """Control: with plain git the same repo DOES run the hook, so the test above is not vacuous."""
    repo = make_repo(tmp_path)
    marker = tmp_path / "PWNED-CONTROL"
    _script(repo / ".git" / "hooks" / "pre-commit", marker)
    (repo / "src" / "App.jsx").write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")  # plain git: no protections
    assert marker.exists()


def test_git_commands_we_build_carry_the_safety_flags():
    cmd = git_cmd("status")
    assert cmd[0] == "git" and "core.hooksPath=/dev/null" in cmd and "core.fsmonitor=false" in cmd and cmd[-1] == "status"


def test_a_binary_huge_or_endless_line_file_does_not_break_the_scanner(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "bin.jsx").write_bytes(os.urandom(50_000))
    (repo / "src" / "huge.jsx").write_text("x" * (sm.MAX_FILE_BYTES + 10))
    (repo / "src" / "oneline.jsx").write_text("<a " * 100_000)
    git(repo, "add", "-A")
    hits, n = sm.from_grep(repo, FP)
    assert [h.file for h in hits] == ["src/App.jsx"] and n == 1


def test_traversal_in_edit_paths_is_still_refused(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "x.jsx").write_text(TAG)
    for f in ("../x.jsx", "src/../../x.jsx", "/etc/hosts"):
        with pytest.raises(EditError):
            verify.apply_edits(repo, [{"file": f, "anchor_text": TAG, "replacement": TAG + " "}], {f})

"""Unpacking a stranger's repository snapshot: it must never write outside the workspace or keep anything dangerous."""

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from auditor.scm import workspace as ws
from auditor.scm.workspace import WorkspaceError, extract, init_scratch_repo


def tar_of(entries: list[tuple[str, bytes | None, dict]], *, top: str = "owner-repo-abc123") -> bytes:
    """entries: (name, content, options). content None means a directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, body, opt in entries:
            full = name if opt.get("raw") else f"{top}/{name}"
            info = tarfile.TarInfo(full)
            info.mode = opt.get("mode", 0o644)
            if "link" in opt:
                info.type, info.linkname = opt["type"], opt["link"]
                t.addfile(info)
            elif body is None:
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            else:
                info.size = len(body)
                t.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def test_ordinary_source_is_unpacked_with_plain_permissions(tmp_path):
    data = tar_of([("src/App.jsx", b"export default 1\n", {"mode": 0o755}), ("README.md", b"hi", {}), ("src", None, {})])
    r = extract(data, tmp_path / "w")
    assert r == {"files": 2, "bytes": 19, "skipped": 0}
    assert (tmp_path / "w/src/App.jsx").read_text() == "export default 1\n"
    assert (tmp_path / "w/src/App.jsx").stat().st_mode & 0o777 == 0o644  # the executable bit is not kept


@pytest.mark.parametrize("name", ["../../escape.txt", "/etc/cron.d/evil", "a/../../escape.txt", "top/../../escape.txt",
                                  "top/a/../../../../escape.txt", "top/..", "../top/x"])
def test_names_that_try_to_climb_out_write_nothing_outside_the_workspace(tmp_path, name):
    out = tmp_path / "sandbox" / "w"
    out.parent.mkdir()
    extract(tar_of([(name, b"pwned", {"raw": True}), ("keep.txt", b"fine", {})]), out)
    assert "escape.txt" not in [p.name for p in tmp_path.rglob("*")], "something was written outside the workspace"
    assert not Path("/etc/cron.d/evil").exists()
    assert [p.name for p in (tmp_path / "sandbox").iterdir()] == ["w"]  # the sandbox holds only the workspace
    assert all(out.resolve() in p.resolve().parents for p in out.rglob("*"))  # and everything written is inside it


@pytest.mark.parametrize("name", [".git/hooks/pre-commit", "sub/.git/config", "node_modules/pkg/index.js", "pkg/node_modules/x.js",
                                  "dist/app.js", "x/\ny", "x\\y", "x/\x01y"])
def test_dangerous_or_irrelevant_names_under_the_normal_top_folder_are_dropped(tmp_path, name):
    out = tmp_path / "w"
    r = extract(tar_of([(name, b"pwned", {}), ("keep.txt", b"fine", {})]), out)
    assert r["files"] == 1 and sorted(p.name for p in out.rglob("*") if p.is_file()) == ["keep.txt"]


def test_links_and_devices_are_never_written_or_followed(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    data = tar_of([("link-to-secret", None, {"type": tarfile.SYMTYPE, "link": str(secret)}),
                   ("hard", None, {"type": tarfile.LNKTYPE, "link": "../../etc/passwd"}),
                   ("dev", None, {"type": tarfile.CHRTYPE, "link": ""}),
                   ("fifo", None, {"type": tarfile.FIFOTYPE, "link": ""}),
                   ("real.txt", b"real", {})])
    out = tmp_path / "w"
    r = extract(data, out)
    assert r["files"] == 1 and r["skipped"] == 4
    assert [p.name for p in out.iterdir()] == ["real.txt"]
    assert not any(p.is_symlink() for p in out.rglob("*"))


def test_a_file_that_is_written_through_an_earlier_symlink_cannot_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    data = tar_of([("evil", None, {"type": tarfile.SYMTYPE, "link": str(outside)}), ("evil/owned.txt", b"x", {})])
    extract(data, tmp_path / "w")
    assert list(outside.iterdir()) == []  # the link was skipped, so the second entry is just a normal file inside


def test_size_limits_hold_even_if_the_header_lies(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "MAX_FILE_BYTES", 100)
    r = extract(tar_of([("big.bin", b"x" * 500, {}), ("small.txt", b"ok", {})]), tmp_path / "a")
    assert r["files"] == 1 and r["skipped"] == 1
    monkeypatch.setattr(ws, "MAX_FILES", 3)
    with pytest.raises(WorkspaceError, match="more than 3 files"):
        extract(tar_of([(f"f{i}.txt", b"x", {}) for i in range(10)]), tmp_path / "b")
    monkeypatch.setattr(ws, "MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(ws, "MAX_FILE_BYTES", 100)
    with pytest.raises(WorkspaceError, match="larger than Attest will read"):
        extract(tar_of([(f"f{i}.txt", b"x" * 8, {}) for i in range(3)]), tmp_path / "c")


@pytest.mark.parametrize("junk", [b"", b"not a tarball", b"\x1f\x8b\x08\x00garbage", os.urandom(4096)])
def test_garbage_is_a_clean_error_not_a_crash(tmp_path, junk):
    with pytest.raises(WorkspaceError):
        extract(junk, tmp_path / "w")


def test_it_will_not_unpack_into_a_folder_that_already_has_things(tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "w/mine.txt").write_text("keep")
    with pytest.raises(WorkspaceError, match="not empty"):
        extract(tar_of([("a.txt", b"x", {})]), tmp_path / "w")
    assert (tmp_path / "w/mine.txt").read_text() == "keep"


def test_the_scratch_repo_has_one_commit_and_a_base_nobody_else_shares(tmp_path):
    shas = []
    for n in ("a", "b"):
        out = tmp_path / n
        extract(tar_of([("src/App.jsx", b"same content", {})]), out)
        shas.append(init_scratch_repo(out, label="owner/repo@abc123"))
        log = subprocess.run(["git", "log", "--oneline"], cwd=out, capture_output=True, text=True, check=True).stdout.splitlines()
        assert len(log) == 1 and "snapshot of owner/repo@abc123" in log[0]
    assert shas[0] != shas[1], "two runs on identical source must not share a base commit (fix attempts are keyed on it)"


def test_hostile_git_config_in_the_archive_is_inert(tmp_path):
    """A repository can carry hooks and config. They are dropped (no .git), so git never runs anything of theirs."""
    marker = tmp_path / "ran"
    data = tar_of([(".git/hooks/pre-commit", f"#!/bin/sh\ntouch {marker}\n".encode(), {"mode": 0o755}),
                   (".git/config", b"[core]\n\tfsmonitor = touch " + str(marker).encode() + b"\n", {}),
                   (".gitattributes", b"* filter=evil\n", {}), ("a.txt", b"x", {})])
    out = tmp_path / "w"
    extract(data, out)
    init_scratch_repo(out, label="x")
    assert not marker.exists()

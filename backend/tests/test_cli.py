"""The m1_prove.py driver must fail with a usage error, never a traceback, on bad input."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "m1_prove.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=False)


def test_no_target_is_a_usage_error(tmp_path):
    r = run("--out", str(tmp_path / "o"))
    assert r.returncode == 2 and "give --demo or --repo" in r.stderr and "Traceback" not in r.stderr


def test_missing_or_non_git_repo_is_a_usage_error(tmp_path):
    for repo in (tmp_path / "does-not-exist", tmp_path):
        r = run("--repo", str(repo), "--out", str(tmp_path / "o"))
        assert r.returncode == 2 and "must be a git checkout" in r.stderr and "Traceback" not in r.stderr


@pytest.mark.parametrize("cookie", ["nonsense", "=novalue", ""])
def test_malformed_cookie_is_a_usage_error(cookie, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    r = run("--repo", str(repo), "--auth-cookie", cookie, "--out", str(tmp_path / "o"))
    assert r.returncode == 2 and "NAME=VALUE" in r.stderr and "Traceback" not in r.stderr

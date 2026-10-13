"""RepoProvider backed by a local git checkout plus the `git` and `gh` CLIs (v1 trust model)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..disclaimers import contains_banned_claim
from ..gitsafe import git_cmd, git_env
from .base import PushNotAllowed, RepoError

BOT_NAME, BOT_EMAIL = "a11y-bot", "a11y-bot@users.noreply.invalid"


class LocalGitProvider:
    def __init__(self, path: Path, *, remote: str = "origin", allow_push: bool = False,
                 gh_bin: str = "gh", git_bin: str = "git"):
        self.path, self.remote, self.allow_push = Path(path), remote, allow_push
        self.gh, self.git = gh_bin, git_bin

    # ---- plumbing
    def _run(self, *args: str, binary: str | None = None, check: bool = True) -> str:
        cmd = [binary, *args] if binary else git_cmd(*args, git=self.git)  # git: hooks/fsmonitor/ext-diff disabled
        r = subprocess.run(cmd, cwd=self.path, capture_output=True, text=True, check=False, env=git_env())
        if check and r.returncode != 0:
            raise RepoError(f"{' '.join([binary or self.git, *args])} failed ({r.returncode}): {r.stderr.strip()[:500]}")
        return r.stdout.strip()

    def _require_push(self, what: str) -> None:
        if not self.allow_push:
            raise PushNotAllowed(f"{what} refused: pushing is not enabled for this run (use --allow-push)")

    @staticmethod
    def _require_clean_text(*texts: str) -> None:
        for t in texts:
            if contains_banned_claim(t):
                raise RepoError("refusing to publish text containing a compliance/certification claim")

    # ---- reads
    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD")

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def is_clean(self) -> bool:
        return self._run("status", "--porcelain", "--untracked-files=no") == ""

    def branch_exists(self, name: str) -> bool:
        return self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False) != ""

    def show_diff(self, sha: str) -> str:
        return self._run("show", "--no-color", "--no-ext-diff", "--format=", sha)

    def delete_branch(self, name: str) -> None:
        """Only ever deletes branches this tool created."""
        if not name.startswith("a11y/"):
            raise RepoError(f"refusing to delete non-a11y branch {name!r}")
        self._run("branch", "-D", name)

    def restore(self, ref: str) -> None:
        """Force back to `ref`, discarding any uncommitted changes. Used for cleanup after a failed run."""
        self._run("checkout", "-q", "-f", ref)

    # ---- local writes
    def create_branch(self, name: str, base_sha: str) -> None:
        if not self.is_clean():
            raise RepoError("working tree has uncommitted changes; refusing to switch branches")
        self._run("checkout", "-q", "-B", name, base_sha)

    def checkout(self, ref: str) -> None:
        if not self.is_clean():
            raise RepoError("working tree has uncommitted changes; refusing to switch branches")
        self._run("checkout", "-q", ref)

    def commit(self, message: str, paths: list[str]) -> str:
        self._require_clean_text(message)
        self._run("add", "--", *paths)
        if self._run("diff", "--cached", "--name-only") == "":
            raise RepoError("nothing to commit")
        self._run("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m", message)
        return self.head_sha()

    def cherry_pick(self, sha: str) -> str:
        """Apply one commit on top of the current branch; on conflict, leave the tree exactly as it was."""
        r = subprocess.run(git_cmd("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "cherry-pick", sha,
                                   git=self.git), cwd=self.path, capture_output=True, text=True, check=False, env=git_env())
        if r.returncode != 0:
            self._run("cherry-pick", "--abort", check=False)
            raise RepoError(f"could not apply {sha[:10]} on its own: {(r.stderr or r.stdout).strip()[:300]}")
        return self.head_sha()

    # ---- outward-facing (explicit opt-in)
    def push(self, branch: str) -> None:
        self._require_push("push")
        self._run("push", "-u", self.remote, branch)

    def find_existing_pr(self, branch: str) -> str | None:
        out = self._run("pr", "list", "--head", branch, "--state", "all", "--json", "url", binary=self.gh)
        rows = json.loads(out or "[]")
        return rows[0]["url"] if rows else None

    def open_pr(self, branch: str, base: str, title: str, body: str) -> str:
        self._require_push("opening a pull request")
        self._require_clean_text(title, body)
        existing = self.find_existing_pr(branch)
        if existing:  # idempotent: never open a duplicate for the same branch
            return existing
        return self._run("pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body,
                         binary=self.gh)

    def open_issue(self, title: str, body: str, labels: list[str] | None = None) -> str:
        self._require_push("opening an issue")
        self._require_clean_text(title, body)
        args = ["issue", "create", "--title", title, "--body", body]
        for lb in labels or []:
            args += ["--label", lb]
        return self._run(*args, binary=self.gh)

"""Turning a downloaded repository snapshot into a scratch git repository we can read and edit. Never built, never run.

The archive comes from someone else's repository, so it is treated as hostile:
  * only ordinary files and directories are written - links, devices and anything else are skipped, never followed;
  * names are normalised and refused if they could land outside the workspace (absolute, `..`, control characters, a
    `.git` component that would plant hooks or config);
  * the number of files, the size of any one file, and the total size are capped, and compression bombs are caught by
    counting what is actually written, not what the archive claims;
  * written files get plain permissions (no executable bits, no setuid).
The scratch repository then exists only so the existing fix-authoring code (which works on git commits) can run; it is
deleted when the run ends, so customer source does not stay on our disk.
"""

from __future__ import annotations

import io
import os
import secrets
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from ..gitsafe import git_cmd, git_env

MAX_FILES = 40_000
MAX_FILE_BYTES = 2 * 1024 * 1024  # source files; bigger things are assets, not what we edit
MAX_TOTAL_BYTES = 250 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "vendor", "__pycache__", ".venv"}


class WorkspaceError(RuntimeError):
    pass


def _clean(name: str) -> PurePosixPath | None:
    """The member's path inside the workspace (first component, GitHub's `owner-repo-sha/`, stripped), or None to skip."""
    if "\x00" in name or any(ord(c) < 32 for c in name) or "\\" in name:
        return None
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        return None
    parts = p.parts[1:]  # drop the archive's top-level directory
    if not parts or any(part in SKIP_DIRS or part in (".", "") for part in parts):
        return None
    return PurePosixPath(*parts)


def extract(data: bytes, dest: Path) -> dict:
    """Unpack a gzip tarball into `dest` (which must be empty or absent). Returns {"files": n, "bytes": n, "skipped": n}."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise WorkspaceError("workspace is not empty")
    root = dest.resolve()
    files = total = skipped = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                rel = _clean(m.name)
                if not m.isfile() or rel is None:  # links, devices, fifos, directories (made as needed), bad names
                    skipped += 0 if m.isdir() else 1
                    continue
                if m.size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                files += 1
                if files > MAX_FILES:
                    raise WorkspaceError(f"the repository has more than {MAX_FILES} files")
                target = (root / rel).resolve()
                if root not in target.parents:  # belt and braces: whatever the name was, it must end up inside
                    skipped += 1
                    continue
                f = tar.extractfile(m)
                if f is None:
                    skipped += 1
                    continue
                body = f.read(MAX_FILE_BYTES + 1)  # count what is really there, not what the header says
                if len(body) > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                total += len(body)
                if total > MAX_TOTAL_BYTES:
                    raise WorkspaceError("the repository is larger than Attest will read")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                os.chmod(target, 0o644)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise WorkspaceError(f"the archive could not be read: {type(e).__name__}") from None
    return {"files": files, "bytes": total, "skipped": skipped}


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(git_cmd(*args), cwd=path, capture_output=True, text=True, check=False, env=git_env())
    if r.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def init_scratch_repo(path: Path, *, label: str) -> str:
    """Make `path` a git repository with one commit holding the snapshot. The commit message carries a fresh random
    nonce, so every run gets a base commit nobody else shares (fix attempts are keyed on it)."""
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "attest@users.noreply.invalid")
    _git(path, "config", "user.name", "attest")
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", f"snapshot of {label} [{secrets.token_hex(8)}]")
    return _git(path, "rev-parse", "HEAD")

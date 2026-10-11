"""Run git on repositories we did not write without letting them run code.

A repository can carry hooks (.git/hooks/*), an `fsmonitor` program, or external diff drivers in its own config,
and git will execute them during ordinary commands (commit, status, diff). We never need any of that.
Not covered: clean/smudge filters declared in .gitattributes + config. Only point this tool at repositories you
trust (it already runs their dev server and build, which is arbitrary code anyway); this removes the surprises.
"""

from __future__ import annotations

import os

GIT_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
             "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user"]


def git_cmd(*args: str, git: str = "git") -> list[str]:
    return [git, *GIT_FLAGS, *args]


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_ASKPASS")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def is_plain_path(root: str | os.PathLike, path: str | os.PathLike) -> bool:
    """True only if `path` (under `root`) reaches its file without passing through ANY symlink."""
    real_root = os.path.realpath(root)
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return not rel.startswith("..") and os.path.realpath(path) == os.path.join(real_root, rel)

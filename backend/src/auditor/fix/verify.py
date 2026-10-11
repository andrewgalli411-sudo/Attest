"""Apply LLM-authored edits deterministically and verify them before anything is committed.

An edit is {file, anchor_text, replacement}. The anchor must match EXACTLY ONCE in the file, the file
must be inside the repo and be the file (or a direct sibling of the file) the source mapper identified,
and the repo's own checks (build/lint/typecheck) must pass afterwards. Any failure rolls everything back.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..gitsafe import git_cmd, git_env, is_plain_path
from .safety import UnsafeEdit, check_edit_shape

MAX_EDITS = 5
MAX_REPLACEMENT_CHARS = 2000
MAX_CHANGED_LINES = 40


class EditError(ValueError):
    pass


@dataclass
class ApplyResult:
    ok: bool
    diff: str = ""
    error: str = ""
    checks: list[dict] = field(default_factory=list)


def _safe_path(repo: Path, rel: str) -> Path:
    p = (repo / rel).resolve()
    if not is_plain_path(repo, repo / rel) and p.exists():
        raise EditError(f"edit goes through a symbolic link: {rel!r}")
    if repo.resolve() not in p.parents:
        raise EditError(f"edit targets a path outside the repo: {rel!r}")
    if ".git" in p.relative_to(repo.resolve()).parts or "node_modules" in p.parts:
        raise EditError(f"edit targets a forbidden path: {rel!r}")
    if not p.is_file():
        raise EditError(f"edit targets a file that does not exist: {rel!r}")
    return p


def validate_edits(repo: Path, edits: list[dict], allowed_files: set[str], rule_id: str | None = None) -> None:
    if not edits:
        raise EditError("no edits")
    if len(edits) > MAX_EDITS:
        raise EditError(f"too many edits ({len(edits)} > {MAX_EDITS})")
    for e in edits:
        for k in ("file", "anchor_text", "replacement"):
            if not isinstance(e.get(k), str):
                raise EditError(f"edit missing string field {k!r}")
        if not e["anchor_text"].strip():
            raise EditError("empty anchor_text")
        if e["anchor_text"] == e["replacement"]:
            raise EditError("edit does not change anything")
        if len(e["replacement"]) > MAX_REPLACEMENT_CHARS:
            raise EditError("replacement too large")
        if e["file"] not in allowed_files:
            raise EditError(f"edit targets {e['file']!r}, outside the mapped source files {sorted(allowed_files)}")
        _safe_path(repo, e["file"])
        if rule_id is not None:
            try:
                check_edit_shape(rule_id, e["anchor_text"], e["replacement"])
            except UnsafeEdit as err:
                raise EditError(f"unsafe edit rejected: {err}") from err


def apply_edits(repo: Path, edits: list[dict], allowed_files: set[str], rule_id: str | None = None) -> dict[str, str]:
    """Returns {file: original_text} for rollback. Raises EditError (nothing written) on any problem."""
    validate_edits(repo, edits, allowed_files, rule_id)
    originals: dict[str, str] = {}
    new_texts: dict[str, str] = {}
    for e in edits:
        p = _safe_path(repo, e["file"])
        text = new_texts.get(e["file"]) or p.read_text()
        originals.setdefault(e["file"], p.read_text())
        n = text.count(e["anchor_text"])
        if n != 1:
            raise EditError(f"anchor_text matches {n} times in {e['file']} (must be exactly 1)")
        new_texts[e["file"]] = text.replace(e["anchor_text"], e["replacement"], 1)
    changed = sum(abs(new_texts[f].count("\n") - originals[f].count("\n")) + 1 for f in new_texts)
    if changed > MAX_CHANGED_LINES:
        raise EditError(f"change too large ({changed} lines)")
    for f, t in new_texts.items():  # all validated: now write
        (repo / f).write_text(t)
    return originals


def rollback(repo: Path, originals: dict[str, str]) -> None:
    for f, t in originals.items():
        (repo / f).write_text(t)


def git_diff(repo: Path) -> str:
    return subprocess.run(git_cmd("diff", "--no-color", "--no-ext-diff"), cwd=repo, check=True, capture_output=True,
                          text=True, env=git_env()).stdout


def run_checks(repo: Path, commands: list[str], timeout: int = 300) -> list[dict]:
    """Run each command in its own process group; on timeout kill the whole group (no orphans)."""
    out = []
    for cmd in commands:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        try:
            text, _ = proc.communicate(timeout=timeout)
            out.append({"cmd": cmd, "ok": proc.returncode == 0, "rc": proc.returncode, "tail": (text or "")[-1200:]})
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            out.append({"cmd": cmd, "ok": False, "rc": None, "tail": f"timed out after {timeout}s"})
        if not out[-1]["ok"]:
            break
    return out


def apply_and_verify(repo: Path, edits: list[dict], allowed_files: set[str], check_commands: list[str],
                     check_timeout: int = 300, rule_id: str | None = None) -> ApplyResult:
    try:
        originals = apply_edits(repo, edits, allowed_files, rule_id)
    except EditError as e:
        return ApplyResult(False, error=str(e))
    diff = git_diff(repo)
    ws = subprocess.run(git_cmd("diff", "--check", "--no-ext-diff"), cwd=repo, capture_output=True, text=True,
                        check=False, env=git_env())
    checks = [{"cmd": "git diff --check", "ok": ws.returncode == 0, "rc": ws.returncode, "tail": ws.stdout[-400:]}]
    if ws.returncode == 0:
        checks += run_checks(repo, check_commands, check_timeout)
    if not all(c["ok"] for c in checks):
        rollback(repo, originals)
        bad = next(c for c in checks if not c["ok"])
        return ApplyResult(False, diff=diff, error=f"check failed: {bad['cmd']}", checks=checks)
    return ApplyResult(True, diff=diff, checks=checks)

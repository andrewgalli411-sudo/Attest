"""Prepare the bundled demo app (seeded defects, login-gated page) as a throwaway git repo."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[3] / "demo-app-template"
DEV_COMMAND = "pnpm dev --port {port}"
SEED_AUTH = {"cookies": [{"name": "demo_session", "value": "1"}]}
INSTALL_TIMEOUT = 180


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _run_group(cmd: list[str], cwd: Path, timeout: int) -> None:
    """Run a command in its own process group; on timeout kill the whole group and raise."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=True, env={**os.environ, "CI": "1"})
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        raise TimeoutError(f"{' '.join(cmd)} timed out after {timeout}s") from None
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}): {out[-800:]}")


def _cache_dir() -> Path:
    key = hashlib.sha256((TEMPLATE / "package.json").read_bytes()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "a11y-demo-cache" / key


def _clone_tree(src: Path, dst: Path) -> None:
    """Copy-on-write clone where the filesystem supports it (APFS), else a symlink-preserving copy."""
    if shutil.which("cp") and subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True,
                                              check=False).returncode == 0:
        return
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def _install(dest: Path) -> None:
    """node_modules from a shared cache keyed by package.json; only the first call runs pnpm."""
    cache = _cache_dir()
    if not (cache / "node_modules").is_dir():
        _run_group(["pnpm", "install", "--prefer-offline"], dest, INSTALL_TIMEOUT)
        tmp = cache.with_name(cache.name + f".tmp{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        _clone_tree(dest / "node_modules", tmp / "node_modules")
        if (dest / "pnpm-lock.yaml").exists():
            shutil.copy2(dest / "pnpm-lock.yaml", tmp / "pnpm-lock.yaml")
        try:
            tmp.rename(cache)  # atomic publish; a concurrent winner is fine
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    _clone_tree(cache / "node_modules", dest / "node_modules")
    if (cache / "pnpm-lock.yaml").exists():
        shutil.copy2(cache / "pnpm-lock.yaml", dest / "pnpm-lock.yaml")


def prepare_demo_repo(dest: Path, install: bool = True) -> tuple[Path, str]:
    """Copy the template to `dest`, install deps, `git init` + commit. Returns (path, base_sha)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(TEMPLATE, dest, ignore=shutil.ignore_patterns("node_modules", "dist", ".git"))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "a11y-bot@example.invalid")
    _git(dest, "config", "user.name", "a11y-bot")
    if install:
        _install(dest)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "demo app baseline")
    return dest, _git(dest, "rev-parse", "HEAD")

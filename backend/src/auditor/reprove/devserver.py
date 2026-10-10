"""Run a target app's dev server for the duration of a block, and never leak the process tree."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import contextmanager
from pathlib import Path


class DevServerError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:  # urlopen raises for any non-2xx; 4xx still means "up and answering"
        return e.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """Terminate the whole process group (pnpm -> node -> vite -> esbuild) and wait until it is empty."""
    pgid = proc.pid  # start_new_session=True makes the child its own group leader
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.time() + wait
        while time.time() < deadline and _group_alive(pgid):
            if proc.poll() is None:
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.1)  # leader exited (and was reaped); wait for remaining members
        if not _group_alive(pgid):
            break
    proc.poll()


@contextmanager
def dev_server(cwd: str | Path, command: str, *, port: int | None = None, startup_timeout: float = 60.0,
               env: dict[str, str] | None = None):
    """`command` may contain {port}. Yields the base URL. Kills the whole process group on exit."""
    port = port or free_port()
    cmd = command.format(port=port)
    tail: deque[str] = deque(maxlen=40)
    log_path = Path(tempfile.mkdtemp(prefix="a11y-dev-")) / "devserver.log"  # never inside the user's repo
    logf = log_path.open("w")
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(cwd), stdout=logf, stderr=subprocess.STDOUT,
        env={**os.environ, "PORT": str(port), "BROWSER": "none", **(env or {})},
        start_new_session=True,  # own process group so we can kill pnpm -> node -> vite together
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _healthy(base + "/"):
                break
            time.sleep(0.25)
        else:
            pass
        if proc.poll() is not None or not _healthy(base + "/"):
            logf.flush()
            tail.extend(log_path.read_text().splitlines()[-40:])
            raise DevServerError(
                f"dev server did not become healthy at {base} (exit={proc.poll()}):\n" + "\n".join(tail))
        yield base
    finally:
        _kill_group(proc)
        logf.close()
        shutil.rmtree(log_path.parent, ignore_errors=True)

import functools
import http.server
import threading
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "fixtures" / "site"


@pytest.fixture(scope="session")
def site_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---------- database backends: sqlite always, real Postgres when binaries exist ----------
import glob
import shutil
import socket
import subprocess
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auditor import models  # noqa: F401
from auditor.db import Base
from auditor.seed import seed_criteria


def _pg_bin():
    hits = sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin/initdb")) or sorted(
        glob.glob("/usr/lib/postgresql/*/bin/initdb")) or (
        [shutil.which("initdb")] if shutil.which("initdb") else [])
    return str(__import__("pathlib").Path(hits[-1]).parent) if hits else None


@pytest.fixture(scope="session")
def pg_url():
    import os

    external = os.environ.get("A11Y_TEST_POSTGRES_URL")  # CI provides a service container; use it as-is
    if external:
        yield external
        return
    binp = _pg_bin()
    if not binp:
        if os.environ.get("A11Y_REQUIRE_POSTGRES"):  # CI must never silently skip half of the database coverage
            pytest.fail("Postgres tests are required (A11Y_REQUIRE_POSTGRES) but no Postgres is available")
        pytest.skip("no local postgres binaries")
    d = tempfile.mkdtemp(prefix="pgtest")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run([f"{binp}/initdb", "-D", f"{d}/data", "-U", "auditor", "--auth=trust", "-E", "UTF8"],
                   check=True, capture_output=True)
    subprocess.run([f"{binp}/pg_ctl", "-D", f"{d}/data", "-o", f"-p {port} -k {d} -c listen_addresses=127.0.0.1",
                    "-l", f"{d}/log", "-w", "start"], check=True, capture_output=True)
    subprocess.run([f"{binp}/createdb", "-h", "127.0.0.1", "-p", str(port), "-U", "auditor", "auditor"],
                   check=True, capture_output=True)
    yield f"postgresql+psycopg://auditor@127.0.0.1:{port}/auditor"
    subprocess.run([f"{binp}/pg_ctl", "-D", f"{d}/data", "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(params=["sqlite", "postgres"])
def sf(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path}/t.db", connect_args={"check_same_thread": False})
    else:
        engine = create_engine(request.getfixturevalue("pg_url"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        seed_criteria(db)
    yield factory
    engine.dispose()


# ---------- demo app (seeded defects) as a real running dev server ----------
@pytest.fixture
def demo_repo(tmp_path):
    from auditor.demo import prepare_demo_repo

    path, sha = prepare_demo_repo(tmp_path / "demo")
    return path, sha


@pytest.fixture
def demo_server(demo_repo):
    from auditor.demo import DEV_COMMAND
    from auditor.reprove.devserver import dev_server

    path, _ = demo_repo
    with dev_server(path, DEV_COMMAND) as base:
        yield base

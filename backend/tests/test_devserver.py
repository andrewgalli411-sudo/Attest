import os
import subprocess
import time
import urllib.request

import pytest

from auditor.reprove.devserver import DevServerError, dev_server


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_serves_then_stops_and_frees_the_port(tmp_path):
    with dev_server(tmp_path, "python3 -m http.server {port} --bind 127.0.0.1") as base:
        assert urllib.request.urlopen(base + "/", timeout=3).status == 200
        port = int(base.rsplit(":", 1)[1])
    time.sleep(0.2)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)


def test_grandchildren_are_killed_too(tmp_path):
    pidfile = tmp_path / "pids"
    # shell -> background sleeper -> the http server; killing only the shell would leak the rest
    cmd = f"sh -c 'sleep 300 & echo $! >> {pidfile}; exec python3 -m http.server {{port}} --bind 127.0.0.1'"
    with dev_server(tmp_path, cmd):
        pids = [int(x) for x in pidfile.read_text().split()]
        assert pids and all(_alive(p) for p in pids)
    time.sleep(0.3)
    assert not any(_alive(p) for p in pids), "grandchild process leaked"


def test_command_that_exits_immediately_reports_its_log(tmp_path):
    with pytest.raises(DevServerError, match="boom-marker"), dev_server(
        tmp_path, "echo boom-marker; exit 3", startup_timeout=10
    ):
        pass


def test_never_healthy_times_out_and_cleans_up(tmp_path):
    pidfile = tmp_path / "pid"
    cmd = f"sh -c 'echo $$ > {pidfile}; exec sleep 300'"
    t = time.time()
    with pytest.raises(DevServerError), dev_server(tmp_path, cmd, startup_timeout=2):
        pass
    assert time.time() - t < 15
    time.sleep(0.3)
    assert not _alive(int(pidfile.read_text()))


def test_sigterm_ignoring_process_is_force_killed(tmp_path):
    pidfile = tmp_path / "pid"
    cmd = (f"sh -c 'trap \"\" TERM; echo $$ > {pidfile}; "
           f"python3 -m http.server {{port}} --bind 127.0.0.1 & wait'")
    with dev_server(tmp_path, cmd):
        pass
    time.sleep(0.5)
    assert not _alive(int(pidfile.read_text()))
    assert subprocess.run(["pgrep", "-f", "http.server"], capture_output=True).stdout.split() == []


STATUS_SERVER = """
import http.server, sys
code = int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(code)
        self.end_headers()
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def test_a_404_at_root_still_counts_as_healthy_but_a_500_does_not(tmp_path):
    (tmp_path / "srv.py").write_text(STATUS_SERVER)
    with dev_server(tmp_path, "python3 srv.py {port} 404", startup_timeout=10) as base:
        assert base.startswith("http://127.0.0.1:")
    with pytest.raises(DevServerError), dev_server(tmp_path, "python3 srv.py {port} 500", startup_timeout=3):
        pass

import functools
import http.server
import threading
from pathlib import Path

import pytest

from auditor.pipeline.prober import probe_url

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "probes"


@pytest.fixture(scope="module")
def purl():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    h.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _p(res, name):
    return next(p for p in res["probes"] if p["probe"] == name)


CASES = [
    ("focus_visible", "focus_good.html", "pass"), ("focus_visible", "focus_bad.html", "fail"),
    ("form_labels", "form_good.html", "pass"), ("form_labels", "form_bad.html", "fail"),
    ("name_role_value", "nrv_good.html", "pass"), ("name_role_value", "nrv_bad.html", "fail"),
    ("modal_focus", "modal_good.html", "pass"), ("modal_focus", "modal_bad.html", "fail"),
]


@pytest.mark.parametrize("probe,page,verdict", CASES)
async def test_probe_verdict(purl, probe, page, verdict):
    pr = _p(await probe_url(f"{purl}/{page}"), probe)
    assert pr["error"] is None, pr
    assert pr["verdict"] == verdict, pr["observations"]

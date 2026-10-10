import json

from auditor.pipeline.prober import probe_url


def _kb(res):
    return next(p for p in res["probes"] if p["probe"] == "keyboard_nav")


async def test_bad_page_findings(site_url):
    res = await probe_url(site_url + "/index.html")
    assert res["error"] is None
    rules = {v["rule"] for v in res["axe"]["violations"]}
    assert {"color-contrast", "image-alt"} <= rules
    kb = _kb(res)
    assert kb["verdict"] == "fail"
    assert any(o["kind"] == "unreachable" and "div" in o["selector"] for o in kb["observations"])


async def test_good_page_clean(site_url):
    res = await probe_url(site_url + "/good.html")
    assert res["error"] is None
    assert res["axe"]["violations"] == []
    assert _kb(res)["verdict"] == "pass"


async def test_deterministic(site_url):
    runs = [json.dumps(await probe_url(site_url + "/index.html"), sort_keys=True) for _ in range(5)]
    assert len(set(runs)) == 1


import pytest


@pytest.fixture(scope="module")
def hostile_url():
    import functools
    import http.server
    import shutil
    import threading

    root = SITE_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures"
    shutil.copy(SITE_ROOT / "site" / "good.html", root / "hostile" / "good.html")
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root / "hostile"))
    h.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_csp_does_not_block_axe(hostile_url):
    res = await probe_url(hostile_url + "/csp.html")
    assert res["axe"] is not None and res["error"] is None


async def test_shadow_dom_pierced(hostile_url):
    kb = _kb(await probe_url(hostile_url + "/shadow.html"))
    assert kb["verdict"] == "fail"
    unreachable = [o["selector"] for o in kb["observations"] if o["kind"] == "unreachable"]
    assert len(unreachable) == 1 and ">>>" in unreachable[0]


async def test_iframe_does_not_stop_traversal(hostile_url):
    assert _kb(await probe_url(hostile_url + "/iframe.html"))["verdict"] == "pass"


async def test_large_page_not_falsely_failed(hostile_url):
    assert _kb(await probe_url(hostile_url + "/big.html"))["verdict"] == "pass"


async def test_client_redirect_audits_final_page(hostile_url):
    res = await probe_url(hostile_url + "/nav.html")
    assert res["final_url"].endswith("good.html")


@pytest.mark.parametrize("path,msg", [("/nope.html", "HTTP 404"), ("/data.json", "non-HTML")])
async def test_unauditable_pages_recorded_not_audited(hostile_url, path, msg):
    res = await probe_url(hostile_url + path)
    assert msg in res["error"] and res["axe"] is None
    assert res["probes"][0]["verdict"] == "inconclusive"


@pytest.mark.parametrize("url", ["file:///etc/hosts", "not a url", "javascript:alert(1)"])
async def test_bad_schemes_rejected(url):
    res = await probe_url(url)
    assert "unsupported scheme" in res["error"] and res["axe"] is None


def _probe(res, name):
    return next(p for p in res["probes"] if p["probe"] == name)


async def test_trap_detected(hostile_url):
    assert _probe(await probe_url(hostile_url + "/trap.html"), "keyboard_trap")["verdict"] == "fail"


async def test_trap_with_escape_key_passes(hostile_url):
    pr = _probe(await probe_url(hostile_url + "/trap_escape.html"), "keyboard_trap")
    assert pr["verdict"] == "pass"


async def test_no_trap_on_normal_pages(site_url, hostile_url):
    for u in (site_url + "/good.html", site_url + "/index.html", hostile_url + "/iframe.html",
              hostile_url + "/shadow.html"):
        assert _probe(await probe_url(u), "keyboard_trap")["verdict"] == "pass", u

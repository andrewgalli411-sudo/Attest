import asyncio
import functools
import http.server
import threading
import time
from pathlib import Path

from auditor.pipeline.prober import probe_url

root = Path(__file__).resolve().parents[1] / "fixtures"
def serve(d):
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d)); h.log_message = lambda *a, **k: None
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h); threading.Thread(target=s.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{s.server_address[1]}"
base = serve(root / "hostile")
# serve good.html alongside
import shutil; shutil.copy(root / "site" / "good.html", root / "hostile" / "good.html")
cases = [f"{base}/{n}" for n in ["csp.html","alert.html","shadow.html","iframe.html","trap.html","nav.html","loop.html","axeclobber.html","big.html","data.json","nope.html"]] + ["http://127.0.0.1:1/", "not a url", "file:///etc/hosts"]
async def main():
    for u in cases:
        t = time.time()
        try:
            r = await asyncio.wait_for(probe_url(u), 120)
            kb = [p for p in r["probes"] if p["probe"]=="keyboard_nav"][0]
            print(f"{u.split('/')[-1]:16} {time.time()-t:5.1f}s axe_err={r['error']!s:.70} axe={'ok' if r['axe'] else None} kb={kb['verdict']} kb_err={kb['error']!s:.60}")
        except BaseException as e:
            print(f"{u.split('/')[-1]:16} CRASH {type(e).__name__}: {str(e)[:100]}")
asyncio.run(main())

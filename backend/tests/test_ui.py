"""The real UI in a real browser against the real backend (SQLite file, scripted model, demo app)."""

import asyncio
import re
import secrets
import sys
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from auditor.pipeline.axe_runner import run_axe
from auditor.reprove.devserver import dev_server, free_port

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
# A build folder of their own: Next allows one dev server per folder, and a developer may already have one running.
TEST_DIST = ".next-test"
pytestmark = [pytest.mark.timeout(420),
              pytest.mark.skipif(not (FRONTEND / "node_modules").is_dir(), reason="frontend deps not installed")]


def _stack(tmp_path, token: str, ui_auth: str = ""):
    api_port = free_port()
    # ANTHROPIC_API_KEY="" keeps these tests on the scripted model even when a developer has a real key in backend/.env
    env = {"DATABASE_URL": f"sqlite:///{tmp_path}/ui.db", "EVIDENCE_DIR": str(tmp_path / "ev"), "ANTHROPIC_API_KEY": ""}
    if token:
        env["API_TOKEN"] = token
    api_cmd = f"{sys.executable} -m uvicorn auditor.main:create_app --factory --host 127.0.0.1 --port {api_port}"
    web_env = {"NEXT_DIST_DIR": TEST_DIST, "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}", "AUDITOR_API_TOKEN": token}
    if ui_auth:
        web_env["UI_BASIC_AUTH"] = ui_auth
    return (dev_server(tmp_path, api_cmd, port=api_port, env=env),
            dev_server(FRONTEND, "pnpm exec next dev --port {port} --hostname 127.0.0.1", startup_timeout=120, env=web_env))


@pytest.fixture(params=["open", "token"])
def stack(request, tmp_path):
    token = "tok-" + secrets.token_hex(12) if request.param == "token" else ""
    api_cm, web_cm = _stack(tmp_path, token)
    with api_cm as api, web_cm as web:
        yield api, web, token


async def axe_violations(page):
    return [(v["rule"], v["nodes"][0]["html"][:100]) for v in (await run_axe(page))["violations"]]


async def start_demo_run(page, web, *, probes=False):
    """Open the New run drawer, start the bundled demo, and land on the run's dashboard."""
    await page.goto(web + "/", wait_until="networkidle")
    await page.get_by_role("button", name="New run").click()
    dialog = page.get_by_role("dialog", name="New run")
    await expect(dialog).to_be_visible()
    if not probes:  # probes add findings and time; the flow tests want the three deterministic demo findings
        await dialog.get_by_label("Run behavioural probes").uncheck()
    await dialog.get_by_role("button", name="Start run").click()
    await page.wait_for_url("**/runs/*")


async def test_start_watch_drill_down_and_every_page_is_accessible(stack):
    api, web, token = stack
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        bodies: list[str] = []

        async def keep(resp):
            try:
                if resp.request.resource_type in ("document", "script", "xhr", "fetch"):
                    bodies.append(await resp.text())
            except Exception:  # noqa: BLE001 - streams and redirects have no body to keep
                pass

        page.on("response", lambda r: asyncio.ensure_future(keep(r)))
        requested: list[str] = []
        page.on("request", lambda r: requested.append(r.url))

        # ---- runs page: accessible, disclaimer present, first Tab stop is the skip link
        await page.goto(web + "/", wait_until="networkidle")
        loaded = await page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').length")
        assert loaded >= 2, "the bundled IBM Plex fonts must load"
        await expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
        await expect(page.get_by_text("Custom repository")).to_have_count(0)  # arbitrary commands are off by default
        assert await axe_violations(page) == []
        await page.keyboard.press("Tab")
        assert await page.evaluate("document.activeElement.className") == "skip-link"
        assert "does not certify compliance" in await page.inner_text("body")
        await expect(page.get_by_role("navigation", name="Primary").get_by_role("link", name="Runs")).to_have_attribute("aria-current", "page")

        # ---- the New run drawer is a real dialog: focus moves in, is trapped, Esc closes it and focus returns
        new_run = page.get_by_role("button", name="New run")
        await new_run.click()
        dialog = page.get_by_role("dialog", name="New run")
        await expect(dialog).to_be_visible()
        assert await page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
        assert await axe_violations(page) == [], "the open drawer must pass axe"
        for _ in range(12):  # tab around: focus never leaves the dialog
            await page.keyboard.press("Tab")
            assert await page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
        await page.keyboard.press("Escape")
        await expect(dialog).to_have_count(0)
        assert await page.evaluate("document.activeElement.textContent.trim()") == "New run"

        # ---- start the demo run and land on the dashboard
        await start_demo_run(page, web)
        await expect(page.get_by_text("Run complete.")).to_be_visible(timeout=240_000)
        body = await page.inner_text("body")
        assert "scripted" in body, "a run written by the stand-in must say so"
        assert "Draft · needs review" in body and "Issues found" in body
        stages = page.get_by_role("region", name="Pipeline").get_by_role("listitem")
        await expect(stages).to_have_count(5)
        await expect(stages.nth(0)).to_contain_text("3 findings")
        await expect(stages.nth(2)).to_contain_text("2 edits passed safety + build")
        await expect(page.get_by_role("region", name="Summary")).to_contain_text("41")
        assert await axe_violations(page) == [], "dashboard must pass axe"

        # ---- grouped by WCAG criterion; filters work and say which one is pressed
        await expect(page.get_by_text("Contrast (Minimum)")).to_be_visible()
        needs = page.get_by_role("button", name="Needs a person")
        await needs.click()
        await expect(needs).to_have_attribute("aria-pressed", "true")
        await expect(page.get_by_role("link", name="color-contrast")).to_be_visible()
        await expect(page.get_by_role("link", name="html-has-lang")).to_have_count(0)
        await page.get_by_role("button", name="Fixed and proved").click()
        await expect(page.get_by_role("link", name="label", exact=True)).to_be_visible()

        # ---- review the fixes: the full evidence story, images actually loaded
        await page.get_by_role("link", name=re.compile(r"Review 2 changes")).click()
        await page.wait_for_url("**/findings/*?filter=fixed")
        for heading in ("Caught", "What a user experiences", "Plan", "The change", "Proof on the patched build", "Regression check"):
            await expect(page.get_by_role("heading", name=re.compile(heading))).to_be_visible(timeout=20_000)
        diff_block = page.get_by_role("region", name="Code change")
        await expect(diff_block).to_contain_text("aria-label")
        await page.wait_for_function("[...document.images].filter(i => i.alt).length >= 4 && [...document.images].every(i => i.complete)")
        sizes = await page.evaluate("[...document.images].filter(i => i.alt && i.alt !== 'attest').map(i => [i.naturalWidth, i.naturalHeight, i.alt])")
        assert len(sizes) >= 4 and all(w > 0 and h > 0 and alt for w, h, alt in sizes), sizes
        assert "Passed." in await page.inner_text("body")
        await expect(page.get_by_text("baseline_failed: yes")).to_be_visible()  # the unpatched control still fails
        assert await axe_violations(page) == [], "finding timeline must pass axe"

        # ---- keyboard: J/K move through the list, A records a decision, and it survives a reload
        first = page.url
        h1 = page.get_by_role("heading", level=1)
        await expect(h1).to_contain_text("label")
        await page.keyboard.press("j")
        await expect(h1).to_contain_text("html-has-lang")  # wait for the page to show it before the next key, like a person would
        assert page.url != first
        await page.keyboard.press("k")
        await expect(h1).to_contain_text("label")
        assert page.url == first
        await page.keyboard.press("j")
        await expect(h1).to_contain_text("html-has-lang")
        await page.keyboard.press("a")
        await expect(page.get_by_text("Recorded: you accepted this change.")).to_be_visible()
        await page.reload(wait_until="networkidle")
        await expect(page.get_by_text("Recorded: you accepted this change.")).to_be_visible()
        await page.keyboard.press("r")
        await expect(page.get_by_text("Recorded: you rejected this change.")).to_be_visible()

        # ---- pull requests: the decision shows up; a project that cannot publish says so and cannot publish
        await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Pull requests").click()
        await expect(page.get_by_role("table", name="Pull requests")).to_be_visible()
        await expect(page.get_by_role("cell", name="Rejected")).to_have_count(1)
        await expect(page.get_by_text("not set up to publish")).to_be_visible()
        await expect(page.get_by_role("button", name=re.compile(r"^Publish"))).to_be_disabled()
        assert await axe_violations(page) == [], "pull requests must pass axe"

        # ---- reports and settings
        await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Reports").click()
        await expect(page.get_by_role("article", name="Report preview")).to_contain_text("Accessibility evidence report")
        await expect(page.get_by_role("article", name="Report preview")).to_contain_text("not a certificate of WCAG conformance")
        await expect(page.get_by_role("button", name="Save as PDF")).to_be_enabled()
        assert await axe_violations(page) == [], "reports must pass axe"
        await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Settings").click()
        await expect(page.get_by_role("heading", name="Model and budget")).to_be_visible()
        assert await axe_violations(page) == [], "settings must pass axe"

        # ---- the run shows up in the list
        await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Runs").click()
        table = page.get_by_role("table", name=re.compile("Runs"))
        await expect(table).to_be_visible()
        await expect(table.get_by_role("row")).to_have_count(2)  # header + the run
        await expect(table).to_contain_text("2 fixed in PRs (proved)")

        # ---- a phone: the sidebar is out of the tab order until the menu opens
        await page.set_viewport_size({"width": 390, "height": 800})
        await page.reload(wait_until="networkidle")
        assert await page.evaluate("document.querySelector('aside').inert") is True
        assert await axe_violations(page) == [], "the phone layout must pass axe"
        await page.get_by_role("button", name="Open navigation").click()
        assert await page.evaluate("document.querySelector('aside').inert") is False
        await expect(page.get_by_role("navigation", name="Primary")).to_be_visible()
        await page.keyboard.press("Escape")
        assert await page.evaluate("document.querySelector('aside').inert") is True
        assert not [e for e in errors if "favicon" not in e], errors
        external = [u for u in requested if not u.startswith(web) and not u.startswith(("data:", "blob:"))]
        assert external == [], f"the UI must not call any other origin (fonts, analytics, CDNs): {external}"
        if token:
            await asyncio.sleep(0.5)
            assert bodies and not any(token in b for b in bodies), "API token reached the browser"
            assert token not in await page.evaluate("JSON.stringify([localStorage, sessionStorage, document.cookie])")
            # the backend itself really does enforce it: no token, no service
            assert httpx.get(api + "/api/runs").status_code == 401
            assert httpx.get(api + "/api/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        await browser.close()


async def test_a_running_run_can_be_cancelled_from_the_ui(stack):
    api, web, token = stack
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await start_demo_run(page, web)
        await page.get_by_role("button", name="Cancel run").click(timeout=60_000)
        await expect(page.get_by_text("Run cancelled").first).to_be_visible(timeout=60_000)
        await expect(page.get_by_role("button", name="Cancel run")).to_have_count(0)
        # a second run is allowed again once the first is over
        await page.goto(web + "/", wait_until="networkidle")
        await expect(page.get_by_role("table").get_by_text("Cancelled", exact=True).first).to_be_visible()
        await browser.close()


def test_the_ui_password_gate_protects_pages_and_the_api_proxy(tmp_path):
    api_cm, web_cm = _stack(tmp_path, "", ui_auth="auditor:correct-horse")
    with api_cm, web_cm as web:
        assert httpx.get(web + "/", timeout=60).status_code == 401
        assert httpx.get(web + "/", timeout=60).headers["www-authenticate"].startswith("Basic")
        assert httpx.get(web + "/api/runs", timeout=60).status_code == 401  # the proxy is behind the gate too
        assert httpx.get(web + "/", auth=("auditor", "wrong"), timeout=60).status_code == 401
        assert httpx.get(web + "/", auth=("auditor", "correct-hors"), timeout=60).status_code == 401
        assert httpx.get(web + "/", auth=("auditor", "correct-horse"), timeout=60).status_code == 200
        assert httpx.get(web + "/api/runs", auth=("auditor", "correct-horse"), timeout=60).status_code == 200


def test_the_proxy_refuses_path_tricks_and_reports_a_dead_backend(tmp_path):
    api_cm, web_cm = _stack(tmp_path, "")
    with api_cm, web_cm as web:
        # Dot-segments get normalised by the HTTP stack; the point is that nothing outside /api is reachable
        # through the proxy, and that the backend's own non-API routes (docs, schema) stay unreachable.
        for bad in ("/api/..%2f..%2fetc%2fpasswd", "/api/%5c..%5cx", "/api/runs/%2e%2e/%2e%2e/openapi.json",
                    "/api/../docs", "/api/runs/..%2f..%2fdocs", "/api/%2e%2e%2fopenapi.json"):
            r = httpx.get(web + bad, timeout=60)
            assert r.status_code in (400, 404), (bad, r.status_code)
            # (a 404 page may echo the requested segment; what must never appear is the backend's schema/docs/file content)
            assert '"paths"' not in r.text and not re.search(r'"openapi"\s*:\s*"3', r.text) and "Swagger UI" not in r.text, bad
            assert "root:" not in r.text, bad
        inside = httpx.get(web + "/api/runs/%2e%2e/config", timeout=60)  # normalises to /api/config: legitimate
        assert inside.status_code == 200 and "demo_enabled" in inside.json()
        assert httpx.get(web + "/api/health", timeout=60).json()["status"] == "ok"
    with web_cm2(tmp_path) as web2:  # backend deliberately not started
        r = httpx.get(web2 + "/api/runs", timeout=60)
        assert r.status_code == 502 and "not reachable" in r.json()["detail"]


def web_cm2(tmp_path):
    return dev_server(FRONTEND, "pnpm exec next dev --port {port} --hostname 127.0.0.1", startup_timeout=120,
                      env={"NEXT_DIST_DIR": TEST_DIST, "API_INTERNAL_URL": f"http://127.0.0.1:{free_port()}", "AUDITOR_API_TOKEN": ""})


# ---------------------------------------------------------------- the production build, with its CSP, in a real browser
@pytest.fixture(scope="module")
def prod_build():
    """`next build` once per module into its own directory (so it cannot disturb a dev server)."""
    import os
    import subprocess

    env = {**os.environ, "NEXT_DIST_DIR": ".next-prod", "NODE_ENV": "production"}
    r = subprocess.run(["pnpm", "exec", "next", "build"], cwd=FRONTEND, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    return env


async def test_the_production_build_works_end_to_end_with_its_csp_and_a_token(tmp_path, prod_build):
    token = "tok-" + secrets.token_hex(12)
    api_port = free_port()
    api_cmd = f"{sys.executable} -m uvicorn auditor.main:create_app --factory --host 127.0.0.1 --port {api_port}"
    api_env = {"DATABASE_URL": f"sqlite:///{tmp_path}/p.db", "EVIDENCE_DIR": str(tmp_path / "ev"), "API_TOKEN": token}
    web_env = {"NEXT_DIST_DIR": ".next-prod", "NODE_ENV": "production", "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}",
               "AUDITOR_API_TOKEN": token}
    with dev_server(tmp_path, api_cmd, port=api_port, env=api_env), dev_server(
            FRONTEND, "pnpm exec next start --port {port} --hostname 127.0.0.1", startup_timeout=120, env=web_env) as web:
        head = httpx.get(web + "/", timeout=60)
        h = head.headers
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert "x-powered-by" not in h and h["referrer-policy"] == "no-referrer"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            violations, errors = [], []
            await page.add_init_script("document.addEventListener('securitypolicyviolation', e => "
                                       "(window.__csp = window.__csp || []).push(e.violatedDirective + ' ' + e.blockedURI));")
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await start_demo_run(page, web)
            await expect(page.get_by_text("Run complete.")).to_be_visible(timeout=240_000)  # live SSE through the proxy, in prod
            assert await axe_violations(page) == []
            await page.get_by_role("link", name=re.compile(r"Review 2 changes")).click()
            await expect(page.get_by_role("heading", name=re.compile("Proof on the patched build"))).to_be_visible(timeout=30_000)
            await page.wait_for_function("[...document.images].filter(i => i.alt).length >= 4 && [...document.images].every(i => i.complete)")
            sizes = await page.evaluate("[...document.images].map(i => [i.naturalWidth, i.naturalHeight])")
            assert all(w > 0 and h > 0 for w, h in sizes), "CSP must not block the evidence images or the logo"
            assert await axe_violations(page) == []
            for path in ("/pull-requests", "/reports", "/settings"):
                await page.goto(web + path, wait_until="networkidle")
                assert await axe_violations(page) == [], path
            violations = await page.evaluate("window.__csp || []")
            assert violations == [], f"CSP violations in production: {violations}"
            assert not [e for e in errors if "favicon" not in e], errors
            assert token not in await page.content()
            await browser.close()


async def test_accept_then_publish_through_the_ui_pushes_only_what_was_accepted(tmp_path):
    """The whole outward-facing path in a real browser: accept one fix, reject the other, confirm, and see the pull request.
    Uses a real git repo and bare remote and a fake `gh`; nothing touches GitHub."""
    import json
    import os
    import stat
    import subprocess

    from test_publish import FAKE_GH, remote_branches

    from auditor.demo import SEED_AUTH, prepare_demo_repo

    repo, base = prepare_demo_repo(tmp_path / "web")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {"web": {
        "repo_path": str(repo), "dev_command": "pnpm dev --port {port}", "check_commands": ["pnpm build"],
        "pages": [{"path": "/", "label": "profile"}], "auth_cookies": SEED_AUTH["cookies"], "allow_push": True}}}))

    api_port = free_port()
    api_cmd = f"{sys.executable} -m uvicorn auditor.main:create_app --factory --host 127.0.0.1 --port {api_port}"
    api_env = {"DATABASE_URL": f"sqlite:///{tmp_path}/p.db", "EVIDENCE_DIR": str(tmp_path / "ev"), "ANTHROPIC_API_KEY": "",
               "PROJECTS_FILE": str(projects), "PATH": f"{bindir}:{os.environ['PATH']}",
               "GH_LOG": str(tmp_path / "gh_log"), "GH_STATE": str(tmp_path / "gh_state"), "GH_COUNT": str(tmp_path / "gh_count")}
    web_env = {"NEXT_DIST_DIR": TEST_DIST, "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}", "AUDITOR_API_TOKEN": ""}
    with dev_server(tmp_path, api_cmd, port=api_port, env=api_env) as api, dev_server(
            FRONTEND, "pnpm exec next dev --port {port} --hostname 127.0.0.1", startup_timeout=120, env=web_env) as web:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(web + "/", wait_until="networkidle")
            await page.get_by_role("button", name="New run").click()
            dialog = page.get_by_role("dialog", name="New run")
            await dialog.get_by_label("Run behavioural probes").uncheck()
            await expect(dialog.get_by_label("Publish results")).to_be_enabled()  # this project may publish; the box stays off
            await dialog.get_by_role("button", name="Start run").click()
            await page.wait_for_url("**/runs/*")
            await expect(page.get_by_text("Run complete.")).to_be_visible(timeout=240_000)

            # nothing accepted yet: publishing is not possible
            await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Pull requests").click()
            await expect(page.get_by_role("button", name=re.compile(r"^Publish"))).to_be_disabled()

            # accept the label fix and reject the language fix (decisions through the API; the UI path is covered above)
            run_id = httpx.get(f"{api}/api/runs", timeout=30).json()[0]["id"]
            rows = httpx.get(f"{api}/api/runs/{run_id}/pull-requests", timeout=30).json()["rows"]
            for r in rows:
                action = "accept_pr" if r["rule_id"] == "label" else "reject_pr"
                assert httpx.post(f"{api}/api/findings/{r['finding_id']}/decision", json={"action": action}, timeout=30).status_code == 201

            await page.get_by_role("navigation", name="Primary").get_by_role("link", name="Pull requests").click()
            await page.reload(wait_until="networkidle")  # decisions were recorded behind the page's back
            publish = page.get_by_role("button", name="Publish 1 accepted")
            await expect(publish).to_be_enabled()
            assert remote_branches(bare) == []  # nothing has left the machine yet

            # publishing asks first, and the confirmation passes axe
            await publish.click()
            confirm = page.get_by_role("alertdialog")
            await expect(confirm).to_contain_text("Only the fixes you accepted are included")
            assert await axe_violations(page) == []
            await confirm.get_by_role("button", name="Cancel").click()
            assert remote_branches(bare) == []  # cancelling pushed nothing
            await publish.click()
            await page.get_by_role("button", name="Push and open pull requests").click()
            await expect(page.get_by_text(re.compile(r"1 pull request opened"))).to_be_visible(timeout=60_000)
            await expect(page.get_by_role("button", name="Published")).to_be_disabled()
            await expect(page.get_by_text("PR #1 · open")).to_be_visible()

            branches = remote_branches(bare)
            assert len(branches) == 1
            assert subprocess.run(["git", "rev-list", "--count", f"{base}..{branches[0]}"], cwd=bare, capture_output=True,
                                  text=True, check=True).stdout.strip() == "1"  # one fix, not both
            assert (tmp_path / "gh_log").read_text().count("pr create") == 1
            assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""
            assert await axe_violations(page) == []
            await browser.close()


class FakeGitHub:
    """A real HTTP server that plays GitHub's sign-in: authorize (redirects back with a code), token exchange, /user."""

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        outer = self
        self.current = "alice"
        self.users = {"alice": {"id": 101, "login": "alice", "name": "Alice A"}, "bob": {"id": 202, "login": "bob", "name": "Bob B"}}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output quiet
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                if u.path == "/login/oauth/authorize":
                    self.send_response(302)
                    self.send_header("Location", f"{q['redirect_uri'][0]}?code=code-{outer.current}&state={q['state'][0]}")
                    self.end_headers()
                elif u.path == "/user":
                    who = self.headers.get("Authorization", "").removeprefix("Bearer gho_secret_")
                    self._json(outer.users[who]) if who in outer.users else self._json({}, 401)
                else:
                    self._json({}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                who = parse_qs(body)["code"][0].removeprefix("code-")
                self._json({"access_token": f"gho_secret_{who}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def test_sign_in_with_github_end_to_end_and_people_cannot_see_each_others_runs(tmp_path):
    from cryptography.fernet import Fernet

    gh = FakeGitHub()
    web_port, api_port = free_port(), free_port()
    web_url = f"http://127.0.0.1:{web_port}"
    api_cmd = f"{sys.executable} -m uvicorn auditor.main:create_app --factory --host 127.0.0.1 --port {api_port}"
    api_env = {"DATABASE_URL": f"sqlite:///{tmp_path}/a.db", "EVIDENCE_DIR": str(tmp_path / "ev"), "ANTHROPIC_API_KEY": "",
               "AUTH_MODE": "accounts", "PUBLIC_URL": web_url, "SECRETS_KEY": Fernet.generate_key().decode(),
               "GITHUB_CLIENT_ID": "cid", "GITHUB_CLIENT_SECRET": "csec", "GITHUB_WEB_BASE": gh.url, "GITHUB_API_BASE": gh.url}
    web_env = {"NEXT_DIST_DIR": TEST_DIST, "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}", "AUDITOR_API_TOKEN": ""}
    try:
        with dev_server(tmp_path, api_cmd, port=api_port, env=api_env), dev_server(
                FRONTEND, "pnpm exec next dev --port {port} --hostname 127.0.0.1", port=web_port, startup_timeout=120, env=web_env):
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                alice = await browser.new_context(viewport={"width": 1280, "height": 900})
                page = await alice.new_page()
                requested: list[str] = []
                page.on("request", lambda r: requested.append(r.url))

                # signed out: every page leads to the sign-in page, which is accessible and says what it will do
                await page.goto(web_url + "/runs/anything", wait_until="networkidle")
                await page.wait_for_url("**/login?next=*")
                await expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
                assert await axe_violations(page) == []
                assert await page.locator("aside").count() == 0  # no app chrome before signing in

                # the whole round trip through GitHub, and back to where alice was going
                gh.current = "alice"
                await page.get_by_role("link", name="Continue with GitHub").click()
                await page.wait_for_url(web_url + "/runs/anything")
                await expect(page.get_by_text("alice", exact=True)).to_be_visible()
                cookies = {c["name"]: c for c in await alice.cookies()}
                assert cookies["attest_session"]["httpOnly"] is True and cookies["attest_session"]["sameSite"] == "Lax"
                assert "attest_session" not in await page.evaluate("document.cookie")  # page scripts cannot read it
                await page.goto(web_url + "/", wait_until="networkidle")
                await expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
                assert await axe_violations(page) == []

                # alice starts a run
                await start_demo_run(page, web_url)
                alice_run = page.url
                await expect(page.get_by_text("Run complete.")).to_be_visible(timeout=240_000)

                # bob, in his own browser, sees none of it - not in the list, and not by typing the address
                bob = await browser.new_context(viewport={"width": 1280, "height": 900})
                bpage = await bob.new_page()
                gh.current = "bob"
                await bpage.goto(web_url + "/", wait_until="networkidle")
                await bpage.get_by_role("link", name="Continue with GitHub").click()
                await bpage.wait_for_url(web_url + "/")
                await expect(bpage.get_by_text("bob", exact=True)).to_be_visible()
                await expect(bpage.get_by_text("No runs yet")).to_be_visible()
                await bpage.goto(alice_run, wait_until="networkidle")
                await expect(bpage.get_by_text("run not found")).to_be_visible()
                assert "alice" not in await bpage.inner_text("body")
                assert await bpage.locator("[aria-label='Summary']").count() == 0

                # signing out ends the session, and the next page load is sent to sign in again
                await page.goto(web_url + "/", wait_until="networkidle")
                await page.get_by_role("button", name="Sign out").click()
                await page.wait_for_url("**/login*")
                await expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
                assert await page.locator("aside").count() == 0  # the previous person's sidebar and runs are gone
                await page.goto(web_url + "/", wait_until="networkidle")
                await page.wait_for_url("**/login?next=*")
                external = [u for u in requested if not u.startswith(web_url) and not u.startswith(gh.url) and not u.startswith("data:")]
                assert external == [], external
                await browser.close()
    finally:
        gh.close()

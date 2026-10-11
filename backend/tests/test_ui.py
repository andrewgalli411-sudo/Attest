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
pytestmark = [pytest.mark.timeout(420),
              pytest.mark.skipif(not (FRONTEND / "node_modules").is_dir(), reason="frontend deps not installed")]


def _stack(tmp_path, token: str, ui_auth: str = ""):
    api_port = free_port()
    env = {"DATABASE_URL": f"sqlite:///{tmp_path}/ui.db", "EVIDENCE_DIR": str(tmp_path / "ev")}
    if token:
        env["API_TOKEN"] = token
    api_cmd = f"{sys.executable} -m uvicorn auditor.main:create_app --factory --host 127.0.0.1 --port {api_port}"
    web_env = {"API_INTERNAL_URL": f"http://127.0.0.1:{api_port}", "AUDITOR_API_TOKEN": token}
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

        # ---- runs page: accessible, banner present, first Tab stop is the skip link
        await page.goto(web + "/", wait_until="networkidle")
        await expect(page.get_by_role("heading", name="Accessibility runs")).to_be_visible()
        await expect(page.get_by_text("Custom repository")).to_have_count(0)  # arbitrary commands are off by default
        assert await axe_violations(page) == []
        await page.keyboard.press("Tab")
        assert await page.evaluate("document.activeElement.className") == "skip-link"
        assert "requires expert review" in await page.inner_text("body")

        # ---- start the demo run and land on the dashboard
        await page.get_by_role("button", name="Run on the bundled demo app").click()
        await page.wait_for_url("**/runs/*")
        status = page.get_by_role("status").first
        await expect(status).not_to_be_empty(timeout=30_000)
        await expect(page.get_by_text("fixed in PRs (proved)")).to_be_visible(timeout=240_000)
        await expect(page.get_by_text("Run complete").or_(page.get_by_text("fixed in PRs (proved)"))).to_be_visible()

        body = await page.inner_text("body")
        assert "3 issues" in body and "need you" in body and "scripted" in body
        await expect(page.get_by_text("issues found")).to_be_visible()
        assert await axe_violations(page) == [], "dashboard must pass axe"

        # ---- grouped by WCAG criterion; filters work
        await expect(page.get_by_text("WCAG 1.4.3 Contrast (Minimum)")).to_be_visible()
        await page.get_by_label("Status").select_option("needs_human")
        await expect(page.get_by_text("color-contrast")).to_be_visible()
        await expect(page.get_by_text("html-has-lang")).to_have_count(0)
        await page.get_by_label("Status").select_option("fixed")
        await expect(page.get_by_role("link", name="label", exact=True)).to_be_visible()

        # ---- drill into the fixed finding: the full evidence story, images actually loaded
        await page.get_by_role("link", name="label", exact=True).click()
        await page.wait_for_url("**/findings/*")
        for heading in ("1. Caught", "2. What a user experiences", "3. Plan", "4. The change",
                        "5. Proof on the patched build", "6. Regression check"):
            await expect(page.get_by_role("heading", name=heading)).to_be_visible(timeout=20_000)
        diff_block = page.get_by_label("Code change")
        await expect(diff_block).to_contain_text("aria-label")
        await expect(diff_block).to_contain_text("+")
        await page.wait_for_function("[...document.images].length >= 4 && [...document.images].every(i => i.complete)")
        sizes = await page.evaluate("[...document.images].map(i => [i.naturalWidth, i.naturalHeight, i.alt])")
        assert len(sizes) >= 4 and all(w > 0 and h > 0 and alt for w, h, alt in sizes), sizes
        assert "Passed" in await page.inner_text("body")
        assert await axe_violations(page) == [], "finding timeline must pass axe"

        # ---- recording a human decision
        await page.get_by_role("button", name="Accept this change").click()
        await expect(page.get_by_text("Recorded: you accepted this change.")).to_be_visible()

        # ---- the run shows up in the list; dark mode also passes
        await page.goto(web + "/", wait_until="networkidle")
        await expect(page.get_by_role("table")).to_be_visible()
        await page.emulate_media(color_scheme="dark")
        assert await axe_violations(page) == []
        assert not [e for e in errors if "favicon" not in e], errors
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
        await page.goto(web + "/", wait_until="networkidle")
        await page.get_by_role("button", name="Run on the bundled demo app").click()
        await page.wait_for_url("**/runs/*")
        await page.get_by_role("button", name="Cancel run").click(timeout=60_000)
        await expect(page.get_by_text("Run cancelled")).to_be_visible(timeout=60_000)
        await expect(page.get_by_role("button", name="Cancel run")).to_have_count(0)
        # a second run is allowed again once the first is over
        await page.goto(web + "/", wait_until="networkidle")
        await expect(page.get_by_text("cancelled").first).to_be_visible()
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
                      env={"API_INTERNAL_URL": f"http://127.0.0.1:{free_port()}", "AUDITOR_API_TOKEN": ""})


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
            await page.goto(web + "/", wait_until="networkidle")
            assert await axe_violations(page) == []
            await page.get_by_role("button", name="Run on the bundled demo app").click()
            await page.wait_for_url("**/runs/*")
            await expect(page.get_by_text("fixed in PRs (proved)")).to_be_visible(timeout=240_000)  # live SSE through the proxy, in prod
            assert await axe_violations(page) == []
            await page.get_by_role("link", name="label", exact=True).first.click()
            await expect(page.get_by_role("heading", name="5. Proof on the patched build")).to_be_visible(timeout=30_000)
            await page.wait_for_function("[...document.images].length >= 4 && [...document.images].every(i => i.complete)")
            sizes = await page.evaluate("[...document.images].map(i => [i.naturalWidth, i.naturalHeight])")
            assert all(w > 0 and h > 0 for w, h in sizes), "CSP must not block the evidence images"
            assert await axe_violations(page) == []
            violations = await page.evaluate("window.__csp || []")
            assert violations == [], f"CSP violations in production: {violations}"
            assert not [e for e in errors if "favicon" not in e], errors
            assert token not in await page.content()
            await browser.close()

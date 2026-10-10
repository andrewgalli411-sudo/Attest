import functools
import http.server
import threading
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from auditor.findings.locator import build, install, relocate, similarity

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "locator"
FIX_ATTRS = ["id", "for", "aria-label"]  # what a label fix is expected to add


@pytest.fixture(scope="module")
def lurl():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    h.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


async def _page(browser, url):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    await page.goto(url)
    await install(page)
    return page


async def _capture(browser, lurl, name, selector, ignore=None):
    page = await _page(browser, f"{lurl}/{name}")
    loc = await build(await page.query_selector(selector), ignore)
    await page.context.close()
    return loc


async def _relocate(browser, lurl, name, stored, ignore=FIX_ATTRS):
    page = await _page(browser, f"{lurl}/{name}")
    rel = await relocate(page, stored, ignore)
    attr = await rel.handle.evaluate("e => e.getAttribute('name') || e.tagName") if rel.found else None
    await page.context.close()
    return rel, attr


@pytest.fixture
async def email(browser, lurl):
    return await _capture(browser, lurl, "before.html", 'input[name="email"]')


async def test_relocates_after_label_fix(browser, lurl, email):
    rel, attr = await _relocate(browser, lurl, "fixed.html", email)
    assert rel.found and attr == "email" and rel.score >= 0.85, rel


async def test_relocates_through_wrapper_element(browser, lurl, email):
    rel, attr = await _relocate(browser, lurl, "wrapped.html", email)
    assert rel.found and attr == "email", rel


async def test_reordered_dom_finds_email_not_a_positional_neighbour(browser, lurl, email):
    rel, attr = await _relocate(browser, lurl, "reordered.html", email)
    assert rel.found and attr == "email", rel
    assert rel.method == "stable_css"


async def test_removed_element_is_not_matched_to_a_similar_one(browser, lurl, email):
    rel, _ = await _relocate(browser, lurl, "removed.html", email)
    assert not rel.found, f"false relocation onto another element: {rel}"


async def test_renamed_identity_attribute_is_not_assumed_to_be_the_same_element(browser, lurl, email):
    """name= identifies the field. A silent rename is indistinguishable from a different element."""
    rel, _ = await _relocate(browser, lurl, "renamed.html", email)
    assert not rel.found, f"relocated across an undeclared identity change: {rel}"


async def test_declared_identity_change_does_relocate_via_text_and_landmark(browser, lurl, email):
    """If the fix itself is what renames `name`, it declares that and the element is found."""
    rel, attr = await _relocate(browser, lurl, "renamed.html", email, ignore=[*FIX_ATTRS, "name"])
    assert rel.found and attr == "email_v2" and rel.method == "text_landmark", rel


async def test_identical_twins_are_ambiguous_not_guessed(browser, lurl):
    first = await _capture(browser, lurl, "twins_before.html", "input:nth-of-type(1)")
    rel, _ = await _relocate(browser, lurl, "twins_after.html", first)
    assert not rel.found and "ambiguous" in rel.reason, rel


async def test_decoys_with_same_text_in_other_landmarks_are_ignored(browser, lurl):
    save = await _capture(browser, lurl, "before.html", 'form button[type="submit"]')
    page = await _page(browser, f"{lurl}/decoy.html")
    rel = await relocate(page, save, [])
    in_form = await rel.handle.evaluate("e => !!e.closest('form')") if rel.found else None
    await page.context.close()
    assert rel.found and in_form is True, rel


async def test_hidden_ignore_list_actually_matters(browser, lurl, email):
    """Without declaring the expected edits, a heavily edited element scores lower (sanity check)."""
    page = await _page(browser, f"{lurl}/fixed.html")
    cand = await build(await page.query_selector('input[name="email"]'), [])
    await page.context.close()
    assert similarity(email, cand) < 1.0


async def test_different_tag_never_matches(email):
    assert similarity(email, {**email, "tag": "select"}) == 0.0


async def test_textless_element_removed_never_relocates_onto_a_lookalike_sibling(browser, lurl):
    """Inputs with no placeholder have empty visible text; the sibling input shares landmark+ancestry."""
    email = await _capture(browser, lurl, "notext_before.html", 'input[name="email"]')
    rel, attr = await _relocate(browser, lurl, "notext_removed.html", email, ignore=[])
    assert not rel.found, f"FALSE RELOCATION onto {attr}: {rel}"

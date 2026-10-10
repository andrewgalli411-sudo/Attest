import subprocess
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from auditor.demo import SEED_AUTH
from auditor.findings import locator as loc
from auditor.fix import source_map as sm
from auditor.pipeline.prober import _open


# ---------- scanner ----------
@pytest.mark.parametrize("src,expected", [
    ('<input name="a" />rest', '<input name="a" />'),
    ('<a href="x>y">t</a>', '<a href="x>y">'),
    ("<button onClick={() => go(1)} disabled>x</button>", "<button onClick={() => go(1)} disabled>"),
    ("<div style={{a: '}'}} className='b>c'>", "<div style={{a: '}'}} className='b>c'>"),
    ("<input\n  name='e'\n  type=\"email\"\n/>", "<input\n  name='e'\n  type=\"email\"\n/>"),
    ("<p data-x={`a>b${1}`}>", "<p data-x={`a>b${1}`}>"),
])
def test_opening_tag_end_handles_jsx_edge_cases(src, expected):
    end = sm.opening_tag_end(src, 0)
    assert src[:end] == expected


def test_unterminated_tag_returns_none():
    assert sm.opening_tag_end('<input name="a"', 0) is None


# ---------- helpers ----------
def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def fp(tag="input", css=None, text="", **kw):
    return {"tag": tag, "stable_css": css, "visible_text": text, "landmark_path": [], "ancestry": [], **kw}


# ---------- grep ----------
def test_grep_finds_the_demo_email_input_uniquely(demo_repo):
    path, _ = demo_repo
    res, n = sm.from_grep(path, fp(css='input[name="email"][type="email"]'))
    assert n == 1
    assert len(res) == 1 and res[0].file == "src/App.jsx" and res[0].method == "grep"
    assert res[0].anchor == '<input name="email" type="email" autoComplete="email" />'


def test_grep_finds_html_lang_target_in_index_html(demo_repo):
    path, _ = demo_repo
    res, _ = sm.from_grep(path, fp(tag="html"))
    assert [h.file for h in res] == ["index.html"] and res[0].anchor == "<html>"


def test_two_identical_inputs_are_ambiguous_not_guessed(tmp_path):
    repo = make_repo(tmp_path, {"a.jsx": '<input name="q" />\n<input name="q" />\n'})
    import asyncio
    r = asyncio.run(sm.resolve(repo, fp(css='input[name="q"]')))
    assert not r.unique and r.hit is None and "ambiguous: 2" in r.reason


def test_same_tag_in_two_files_is_ambiguous(tmp_path):
    repo = make_repo(tmp_path, {"a.jsx": '<input name="q" />', "b.jsx": '<input name="q" />'})
    import asyncio
    r = asyncio.run(sm.resolve(repo, fp(css='input[name="q"]')))
    assert not r.unique and r.hit is None and "ambiguous: 2" in r.reason


def test_no_match_is_reported_not_invented(tmp_path):
    repo = make_repo(tmp_path, {"a.jsx": '<input name="other" />'})
    import asyncio
    r = asyncio.run(sm.resolve(repo, fp(css='input[name="email"]')))
    assert not r.unique and r.hit is None


def test_node_modules_and_dist_are_never_candidates(tmp_path):
    repo = make_repo(tmp_path, {"node_modules/lib/x.jsx": '<input name="q" />',
                                "dist/y.html": '<input name="q" />', "src/z.jsx": '<input name="q" />'})
    hits, _ = sm.from_grep(repo, fp(css='input[name="q"]'))
    assert [h.file for h in hits] == ["src/z.jsx"]


def test_anchor_extends_over_previous_lines_until_unique(tmp_path):
    body = '<div id="a">\n  <input name="q" />\n</div>\n<div id="b">\n  <input name="q" />\n</div>\n'
    repo = make_repo(tmp_path, {"a.jsx": body})
    text = (repo / "a.jsx").read_text()
    start = text.rindex("<input")
    end = sm.opening_tag_end(text, start)
    anchor = sm._unique_anchor(text, start, end)
    assert anchor is not None and text.count(anchor) == 1 and anchor.startswith("  <input") is False


def test_multiline_tag_anchor_is_exact(tmp_path):
    src = '<input\n  name="email"\n  type="email"\n/>\n'
    repo = make_repo(tmp_path, {"a.jsx": src})
    hits, _ = sm.from_grep(repo, fp(css='input[name="email"]'))
    assert len(hits) == 1 and hits[0].anchor == src.strip() and hits[0].line == 1


def test_to_repo_rel_rejects_paths_outside_and_accepts_foreign_prefix(tmp_path):
    repo = make_repo(tmp_path, {"src/App.jsx": "x"})
    assert sm._to_repo_rel(repo, "/etc/passwd") is None
    assert sm._to_repo_rel(repo, "/some/other/machine/src/App.jsx") == "src/App.jsx"
    assert sm._to_repo_rel(repo, "src/App.jsx") == "src/App.jsx"
    assert sm._to_repo_rel(repo, "../../escape.jsx") is None


# ---------- react fiber (live) ----------
async def test_fiber_source_matches_grep_on_the_live_demo(demo_repo, demo_server):
    path, _ = demo_repo
    async with async_playwright() as pw:
        br = await pw.chromium.launch()
        ctx, page, _ = await _open(br, demo_server + "/", None, SEED_AUTH)
        try:
            await loc.install(page)
            el = await page.query_selector('input[name="email"]')
            fpr = await loc.build(el)
            res = await sm.resolve(path, fpr, el)
        finally:
            await ctx.close()
            await br.close()
    assert res.unique and res.hit.method == "react_fiber" and "corroborated" in res.reason
    assert res.hit.file == "src/App.jsx"
    assert res.hit.anchor == '<input name="email" type="email" autoComplete="email" />'
    assert "name=\"email\"" in res.hit.excerpt


async def test_unlocatable_element_never_resolves(tmp_path):
    r = await sm.resolve(tmp_path, {"unlocatable": True})
    assert not r.unique and r.hit is None

"""DOM element -> source file + exact opening-tag text (the edit anchor).

Two methods, strongest first:
  1. react_fiber : React 18 dev builds expose `_debugSource` {fileName, lineNumber, columnNumber}.
  2. grep        : literal tokens from the element's fingerprint (name="x", id, visible text, tag) searched
                   in tracked source files.
A result is `unique` only if exactly one source location matches. Anything else -> not unique, and the
caller must treat the finding as needs-human. We never pick a "best guess" file.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import ElementHandle

SOURCE_EXT = {".jsx", ".tsx", ".js", ".ts", ".vue", ".svelte", ".html", ".astro", ".mdx"}
SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".git", ".nuxt", "coverage"}
MAX_FILE_BYTES = 400_000
EXCERPT = 15

_FIBER_JS = """
el => {
  const k = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
  if (!k) return null;
  let f = el[k];
  while (f && !f._debugSource) f = f.return;
  return f ? {file: f._debugSource.fileName, line: f._debugSource.lineNumber,
              col: f._debugSource.columnNumber ?? null} : null;
}
"""


@dataclass
class SourceHit:
    file: str  # repo-relative, posix
    line: int  # 1-based line of the element's opening '<'
    col: int | None
    method: str  # react_fiber | grep
    anchor: str  # exact opening-tag text, unique in the file
    excerpt: str = ""


@dataclass
class Resolution:
    hit: SourceHit | None
    unique: bool
    reason: str
    candidates: list[SourceHit] = field(default_factory=list)


def tracked_files(repo: Path) -> list[Path]:
    """Source files in the repo (git-tracked when possible; otherwise a filtered walk)."""
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=repo, check=True,
                             capture_output=True, text=True).stdout.splitlines()
        paths = [repo / p for p in out]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [p for p in repo.rglob("*") if p.is_file()]
    return [p for p in paths
            if p.suffix in SOURCE_EXT and not (set(p.relative_to(repo).parts) & SKIP_DIRS)
            and p.is_file() and p.stat().st_size <= MAX_FILE_BYTES]


def opening_tag_end(text: str, start: int) -> int | None:
    """Index just past the '>' that closes the opening tag beginning at text[start] == '<'.
    Understands quoted strings and {...} JSX expressions (so '=>' or '>' inside braces don't end it)."""
    i, n, depth, quote = start + 1, len(text), 0, None
    while i < n:
        c = text[i]
        if quote:
            if c == "\\" and quote != "`":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == ">" and depth == 0:
            return i + 1
        i += 1
    return None


def _line_start_offsets(text: str) -> list[int]:
    offs, o = [0], 0
    for ln in text.split("\n"):
        o += len(ln) + 1
        offs.append(o)
    return offs


def _unique_anchor(text: str, start: int, end: int) -> str | None:
    """Opening-tag text; if it occurs more than once, extend backwards by whole preceding lines until it
    is unique. None if no extension makes it unique."""
    tag = text[start:end]
    if text.count(tag) == 1:
        return tag
    offs = _line_start_offsets(text)
    line = text.count("\n", 0, start)
    for back in range(1, 6):
        if line - back < 0:
            break
        ctx = text[offs[line - back]:end]
        if text.count(ctx) == 1:
            return ctx
    return None


def _hit_at(repo: Path, rel: str, line: int, col: int | None, method: str, tag: str) -> SourceHit | None:
    text = (repo / rel).read_text(errors="replace")
    offs = _line_start_offsets(text)
    if not (1 <= line <= len(offs) - 1):
        return None
    lo = offs[line - 1]
    if col is not None and text[lo + col:lo + col + 1] == "<":
        start = lo + col
    else:  # find '<tag' on that line, else walk up a few lines (multi-line props)
        start = -1
        for ln in range(line - 1, max(0, line - 4) - 1, -1):
            m = re.search(rf"<{re.escape(tag)}(?=[\s/>])", text[offs[ln]:offs[ln + 1]])
            if m:
                start = offs[ln] + m.start()
                break
        if start < 0:
            return None
    end = opening_tag_end(text, start)
    if end is None:
        return None
    anchor = _unique_anchor(text, start, end)
    if anchor is None:
        return None
    ln0 = text.count("\n", 0, start)
    lines = text.split("\n")
    excerpt = "\n".join(f"{i + 1}: {lines[i]}" for i in range(max(0, ln0 - EXCERPT), min(len(lines), ln0 + EXCERPT)))
    return SourceHit(rel, ln0 + 1, start - offs[ln0], method, anchor, excerpt)


def _to_repo_rel(repo: Path, file_name: str) -> str | None:
    p = Path(file_name)
    repo = repo.resolve()
    if p.is_absolute():
        try:
            return p.resolve().relative_to(repo).as_posix()
        except ValueError:
            pass
        # path from another machine/container: match by trailing components
        for k in range(len(p.parts) - 1, 0, -1):
            cand = repo.joinpath(*p.parts[k:])
            if cand.is_file():
                return cand.relative_to(repo).as_posix()
        return None
    return p.as_posix() if (repo / p).is_file() else None


async def from_fiber(repo: Path, el: ElementHandle, tag: str) -> SourceHit | None:
    info = await el.evaluate(_FIBER_JS)
    if not info or not info.get("file"):
        return None
    rel = _to_repo_rel(repo, info["file"])
    if rel is None or set(Path(rel).parts) & SKIP_DIRS:
        return None
    return _hit_at(repo, rel, info["line"], info.get("col"), "react_fiber", tag)


# For these, "visible text" is the text of everything inside them, not a literal in their own source.
CONTAINER_TAGS = {"html", "body", "main", "header", "footer", "nav", "section", "article", "aside", "div",
                  "form", "ul", "ol", "table", "tbody", "thead", "tr", "fieldset", "dialog"}
MAX_TEXT_TOKEN = 80


def _text_token(fp: dict) -> str:
    t = fp.get("visible_text") or ""
    if fp.get("tag") in CONTAINER_TAGS or len(t) > MAX_TEXT_TOKEN:
        return ""
    return t


def _tokens(fp: dict) -> list[str]:
    """Literal strings likely to appear in source for this element."""
    toks: list[str] = []
    css = fp.get("stable_css") or ""
    toks += re.findall(r'="([^"]+)"', css)  # [name="email"] -> email
    if css.startswith("#"):
        toks.append(css[1:])
    if _text_token(fp):
        toks.append(_text_token(fp))
    return [t for t in dict.fromkeys(toks) if len(t) >= 2]


def from_grep(repo: Path, fp: dict) -> tuple[list[SourceHit], int]:
    """Returns (hits, n_matching_tags). n_matching_tags counts EVERY tag occurrence that fits the element,
    including ones whose edit anchor could not be made unique: element identity != text uniqueness."""
    tag = fp["tag"]
    toks = _tokens(fp)
    tag_re = re.compile(rf"<{re.escape(tag)}(?=[\s/>])")
    hits: list[SourceHit] = []
    n_matches = 0
    for path in tracked_files(repo):
        text = path.read_text(errors="replace")
        rel = path.relative_to(repo).as_posix()
        for m in tag_re.finditer(text):
            end = opening_tag_end(text, m.start())
            if end is None:
                continue
            region = text[m.start():end]
            # with tokens: every attribute-ish token must literally appear in this tag's opening text
            attr_toks = [t for t in toks if t != _text_token(fp)]
            if attr_toks and not all(t in region for t in attr_toks):
                continue
            if not attr_toks and _text_token(fp) and _text_token(fp) not in text[m.start():m.start() + 400]:
                continue
            n_matches += 1
            line = text.count("\n", 0, m.start()) + 1
            h = _hit_at(repo, rel, line, m.start() - text.rfind("\n", 0, m.start()) - 1, "grep", tag)
            if h:
                hits.append(h)
    return hits, n_matches


async def resolve(repo: Path, fp: dict, el: ElementHandle | None = None) -> Resolution:
    """Fiber first (if we have the live element), then grep. unique=False unless exactly one location."""
    if fp.get("unlocatable"):
        return Resolution(None, False, "element could not be located in the page")
    fiber = None
    if el is not None:
        fiber = await from_fiber(repo, el, fp["tag"])
    grep, n_matches = from_grep(repo, fp)
    if fiber is not None:
        agree = [g for g in grep if (g.file, g.line) == (fiber.file, fiber.line)]
        # grep corroborates when it also finds it; disagreement is tolerated only if grep found nothing
        if grep and not agree and len(grep) == 1:
            return Resolution(None, False, "fiber and grep disagree on location", [fiber, *grep])
        return Resolution(fiber, True, "react_fiber" + (" + grep corroborated" if agree else ""), [fiber])
    if n_matches == 1 and len(grep) == 1:
        return Resolution(grep[0], True, "grep: single matching location", grep)
    if n_matches == 0:
        return Resolution(None, False, "no source location matches the element", [])
    return Resolution(None, False, f"ambiguous: {n_matches} source locations match", grep)

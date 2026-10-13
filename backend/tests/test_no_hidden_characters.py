"""Trojan Source guard: invisible control and bidirectional-override characters in source code can make code
look different from what it does. Auto-formatters have inserted them here more than once, so this is enforced."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP = {"node_modules", ".venv", ".next", ".next-prod", ".next-test", ".git", "evidence", "__pycache__", ".pytest_cache", ".ruff_cache",
        "dist"}  # generated output and dependencies are not source we write or review
EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".md", ".json", ".yml", ".yaml", ".toml", ".html", ".sh"}
FORBIDDEN = set(range(0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20)) | {0x7F} | set(range(0x202A, 0x202F)) \
    | set(range(0x2066, 0x206A)) | {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}


def test_no_source_file_contains_invisible_or_bidi_override_characters():
    offenders = []
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in EXT and not (set(p.relative_to(ROOT).parts) & SKIP) and p.stat().st_size < 3_000_000:
            text = p.read_text(errors="replace")
            bad = sorted({hex(ord(c)) for c in text if ord(c) in FORBIDDEN})
            # the vendored, minified axe-core is third-party code that legitimately contains zero-width characters
            if bad and "vendor" not in p.parts and p.name != "pnpm-lock.yaml":
                offenders.append((p.relative_to(ROOT).as_posix(), bad))
    assert offenders == [], offenders

"""Scrub text before it is sent to the model. Page content and source code can contain personal data and
secrets; the model only needs structure and the wording the fix reuses (labels, attribute names).

This is a safety net, not a guarantee: it removes common high-risk patterns. Deployments that cannot send
page or source content to a third party at all should not use the language-model path.

Performance is part of the contract: this runs synchronously on untrusted text (page content, source files), so
every rule must be linear. Quantifiers are bounded, and PEM blocks are found with str.find, not backtracking
regexes. tests/test_redos.py feeds each rule adversarial input and enforces a time limit.
"""

from __future__ import annotations

import math
import re
from itertools import pairwise

MAX_CHARS = 200_000  # callers send far less; a backstop so no single call can be made to run long
KEY_BLOCK_MAX = 10_000

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sk|pk|rk)[-_](live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{16,}\b"), "[api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[github-token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[jwt]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\"?\s*[:=]\s*)(\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s\"',;<>]{6,})"),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)\bvalue\s*=\s*(\"[^\"]*\"|'[^']*')"), 'value="[redacted]"'),
    # RFC limits (64-char local part, 255-char domain): bounded, so a long run of letters is not rescanned from
    # every start position (the unbounded form was quadratic: 200 KB of "a" took ~15 s).
    (re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}"), "[email]"),
    (re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,40}\d(?![\w.])"), "[number]"),
]

PLACEHOLDER = re.compile(r"\[(?:email|number|token|jwt|api-key|aws-key|github-token|private-key|redacted)\]")


def _strip_private_keys(text: str) -> str:
    """Replace PEM private-key blocks. Linear: uses str.find (C speed), consumes at least one marker per loop,
    and treats an unterminated BEGIN as key material up to KEY_BLOCK_MAX characters."""
    out: list[str] = []
    pos = 0
    while True:
        i = text.find("-----BEGIN ", pos)
        if i < 0:
            break
        head_end = text.find("-----", i + 11, i + 80)
        if head_end < 0 or "PRIVATE KEY" not in text[i:head_end]:
            out.append(text[pos:i + 11])
            pos = i + 11
            continue
        j = text.find("-----END ", head_end, i + KEY_BLOCK_MAX)
        close = text.find("-----", j + 9, j + 90) if j >= 0 else -1
        stop = close + 5 if close >= 0 else min(len(text), i + KEY_BLOCK_MAX)
        out.append(text[pos:i])
        out.append("[private-key]")
        pos = stop
    out.append(text[pos:])
    return "".join(out)


# `=` is only allowed as base64 padding at the end, so `name=value` fragments are not swallowed as one token
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/-]{20,31}={0,2}(?![A-Za-z0-9_+/=-])")
_KEBAB = re.compile(r"[a-z]+(?:-[a-z]+)+(?:-\d+)?")


_WORDISH = re.compile(r"[A-Z]?[a-z]{3,}|[A-Z]{2,}(?![a-z])")


def _word_fraction(s: str) -> float:
    """Share of the letters that sit in word-shaped pieces (`Profile`, `Panel`, `API`). Names are built from
    words; random keys almost never contain even three lowercase letters in a row."""
    letters = "".join(c for c in s if c.isalpha())
    return sum(len(m) for m in _WORDISH.findall(letters)) / len(letters) if letters else 0.0


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _digit_letter_transitions(s: str) -> float:
    """Letter<->digit boundaries per character. Random tokens interleave them (~0.25+); identifiers clump a few
    digits together (`A11Y_TEST_URL`, `field-2`, `sha256`), which gives a low ratio."""
    return sum(1 for a, b in pairwise(s) if (a.isdigit() and b.isalpha()) or (a.isalpha() and b.isdigit())) / len(s)


def _looks_random(tok: str) -> bool:
    """An unlabeled secret: letters AND digits, statistically random, and interleaved the way random text is.
    Words and identifiers fail at least one of those tests. (A human-chosen password like `hunter2-super-secret`
    looks like words and is NOT caught: that is a stated limit, not an oversight. Measured on random base62 tokens
    of 20-31 characters: about 94% are caught, with no false positives on identifiers or this project's source.
    Loosening it to ~98% starts mangling long camelCase names, which can break the text a fix is anchored to.)"""
    t = tok.rstrip("=")
    if not (any(c.isdigit() for c in t) and any(c.isalpha() for c in t)) or _KEBAB.fullmatch(t):
        return False
    h = _entropy(t)
    interleaved = _digit_letter_transitions(t) >= 0.15 and h >= 3.4
    mixed_case_random = (any(c.isupper() for c in t) and any(c.islower() for c in t) and h >= 4.2
                         and _word_fraction(t) < 0.6)
    return interleaved or mixed_case_random


_LONG = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


def _long_token_is_secret(tok: str) -> bool:
    """32+ characters with no spaces. Hashes, keys and ids have digits or random mixed case; long identifiers
    (`evidence_blob_api_evidence__evidence_id__get`, `accessibility-conformance-report`) are plain words."""
    if any(c.isdigit() for c in tok):
        return True
    letters = tok.replace("-", "").replace("_", "")
    mixed = any(c.isupper() for c in letters) and any(c.islower() for c in letters)
    return (mixed and _entropy(letters) >= 4.0 and _word_fraction(letters) < 0.6) or (
        not mixed and tok == letters and _entropy(letters) >= 4.3 and _word_fraction(letters) < 0.6)


def _redact_random(text: str) -> str:
    text = _LONG.sub(lambda m: "[token]" if _long_token_is_secret(m.group(0)) else m.group(0), text)
    return _CANDIDATE.sub(lambda m: "[token]" if _looks_random(m.group(0)) else m.group(0), text)


def redact(text: str) -> str:
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[truncated]"
    text = _strip_private_keys(text)
    for pat, repl in _RULES:
        text = pat.sub(repl, text)
    return _redact_random(text)


def redact_json(obj):
    """Redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    return obj

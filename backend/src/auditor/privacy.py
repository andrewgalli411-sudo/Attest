"""Scrub text before it is sent to the model. Page content and source code can contain personal data and
secrets; the model only needs structure and the wording the fix reuses (labels, attribute names).

This is a safety net, not a guarantee: it removes common high-risk patterns. Deployments that cannot send
page or source content to a third party at all should not use the language-model path.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[private-key]"),
    (re.compile(r"\b(sk|pk|rk)[-_](live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{16,}\b"), "[api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[github-token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[jwt]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\"?\s*[:=]\s*)(\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s\"',;<>]{6,})"),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)\bvalue\s*=\s*(\"[^\"]*\"|'[^']*')"), 'value="[redacted]"'),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])"), "[number]"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "[token]"),
]


PLACEHOLDER = re.compile(r"\[(?:email|number|token|jwt|api-key|aws-key|github-token|private-key|redacted)\]")


def redact(text: str) -> str:
    for pat, repl in _RULES:
        text = pat.sub(repl, text)
    return text


def redact_json(obj):
    """Redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    return obj

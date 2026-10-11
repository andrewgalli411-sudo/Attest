"""Text that came from a web page or a model must be safe to store.

PostgreSQL rejects NUL (U+0000) in text and in JSON strings; one stray NUL would fail the whole run. Strip them
(and nothing else) at the boundary where untrusted text is persisted.
"""

from __future__ import annotations


def strip_nul(obj):
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, list):
        return [strip_nul(x) for x in obj]
    if isinstance(obj, dict):
        return {strip_nul(k): strip_nul(v) for k, v in obj.items()}
    return obj

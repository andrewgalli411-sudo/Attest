"""A deterministic stand-in for AsyncAnthropic, for running the whole remediation loop without an API key.

NOT a language model. It applies fixed rules for a few mechanical findings and declines everything else,
using the same request/response shapes as the real client so the pipeline code paths are identical.
Every run that uses it says so in its summary. Real quality evaluation needs the real client.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

SAFE = {"label", "html-has-lang", "select-name"}


def _resp(obj: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(obj))], stop_reason="end_turn",
        usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 0, "output_tokens": 0, "scripted": True}))


def _visible_label(snippet: str, tag_open: str) -> str | None:
    """Text of the element immediately before the field's tag in the DOM snippet."""
    m = re.search(r">\s*([^<>]{2,60}?)\s*</(?:span|div|p|strong|b|label|small)>\s*<" + re.escape(tag_open[1:6]), snippet)
    return m.group(1).strip() if m else None


class ScriptedClient:
    scripted = True

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        system = kw["system"][0]["text"]
        if "triage step" in system:
            return self._triage(kw)
        if "smallest possible source edit" in system:
            return self._fix(kw)
        raise RuntimeError("ScriptedClient: unrecognised prompt")

    # ---- triage
    def _triage(self, kw):
        content = kw["messages"][0]["content"]
        text = next(b["text"] for b in content if b["type"] == "text")
        items = {i["id"]: i for i in json.loads(text)["evidence"]}
        rule = items["finding"]["rule"]
        found = items["source"].get("found")
        ok = rule in SAFE and found
        reason = {"label": "missing_label", "html-has-lang": "missing_lang"}.get(rule, "not_automatable")
        return _resp({
            "tier": "auto_fix" if ok else "needs_human",
            "reason_code": reason if ok else ("ambiguous_source" if rule in SAFE else "not_automatable"),
            "impact_text": {
                "label": "A screen-reader user hears this field announced without saying what it is for.",
                "html-has-lang": "A screen reader may read the whole page in the wrong language.",
            }.get(rule, "A user of assistive technology may be affected; a person needs to assess how."),
            "fix_plan": {"label": "Give the field an accessible name using the existing visible text.",
                         "html-has-lang": 'Declare the page language on <html>.'}.get(rule, "") if ok else "",
            "confidence": 0.95 if ok else 0.6,
            "evidence_cited": ["finding", "dom", "source"],
            "preconditions": ["visible label text exists next to the field"] if rule == "label" else [],
            "coverage_gaps": [],
        })

    # ---- fix authoring
    def _fix(self, kw):
        p = json.loads(kw["messages"][0]["content"])
        anchor, rule = p["anchor"], p["finding"]["rule"]
        decline = lambda why: _resp({"edits": [], "explanation": why, "confidence": 0.0})
        if rule == "html-has-lang":
            if "lang=" in anchor:
                return decline("lang already present")
            new = anchor.replace("<html", '<html lang="en"', 1)
            return _resp({"edits": [{"file": p["file"], "anchor_text": anchor, "replacement": new}],
                          "explanation": "Declares the page language so assistive technology can pick the right voice.",
                          "confidence": 0.9})
        if rule in ("label", "select-name"):
            label = _visible_label(p["finding"].get("dom", ""), anchor)
            if not label:
                return decline("no visible label text found to reuse; wording would have to be invented")
            if "aria-label" in anchor:
                return decline("aria-label already present")
            new = re.sub(r"(\s*/?>)$", f' aria-label="{label}"' + r"\1", anchor, count=1)
            return _resp({"edits": [{"file": p["file"], "anchor_text": anchor, "replacement": new}],
                          "explanation": f'Gives the field the accessible name "{label}", reusing the visible text next to it.',
                          "confidence": 0.95})
        return decline(f"no scripted fix for {rule}")

import json
from types import SimpleNamespace


def _resp(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
        usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1, "output_tokens": 1}),
    )


class FakeJudge:
    """Stand-in for AsyncAnthropic. `mode` selects well-behaved or adversarial behaviour."""

    def __init__(self, mode="good"):
        self.mode, self.calls, self.messages = mode, [], self
        self.requests = []

    async def create(self, **kw):
        self.calls.append(kw)
        payload = json.loads(kw["messages"][0]["content"])
        ev = payload["evidence"]
        fails = [e["id"] for e in ev if e["outcome"] == "fail"]
        passes = [e["id"] for e in ev if e["outcome"] == "pass"]
        m = self.mode
        if m == "refusal":
            return _resp("", "refusal")
        if m == "garbage":
            return _resp("not json at all")
        if m == "unknown_ids":
            return _resp(json.dumps(self._p("does_not_support", ["axe:violation:made-up"])))
        if m == "unknown_then_fix":
            if len(kw["messages"]) == 1:
                return _resp(json.dumps(self._p("does_not_support", ["nope"])))
            return _resp(json.dumps(self._p("does_not_support", fails or ["x"])))
        if m == "banned":
            return _resp(json.dumps(self._p("supports", passes[:1], remark="This page is fully compliant.")))
        if m == "overconfident_supports":  # says Supports despite failures
            return _resp(json.dumps(self._p("supports", fails + passes, conf=0.99)))
        if m == "failure_without_evidence":
            return _resp(json.dumps(self._p("does_not_support", [e["id"] for e in ev][:1])))
        # good: derive honestly from evidence
        if fails and passes:
            return _resp(json.dumps(self._p("partially_supports", fails + passes[:1])))
        if fails:
            return _resp(json.dumps(self._p("does_not_support", fails)))
        if passes:
            return _resp(json.dumps(self._p("supports", passes, conf=0.7)))
        return _resp(json.dumps(self._p("not_evaluated", [])))

    @staticmethod
    def _p(level, cited, remark="Observed from automated evidence.", conf=0.8):
        return {"level": level, "remark": remark, "confidence": conf, "evidence_cited": cited,
                "reasoning": "r", "coverage_gaps": [], "needs_human": False}


class FakeTriage:
    """Stand-in for AsyncAnthropic used by triage. `mode` picks well-behaved or adversarial output."""

    def __init__(self, mode="good", confidence=0.95):
        self.mode, self.confidence = mode, confidence
        self.calls, self.messages = [], self

    async def create(self, **kw):
        self.calls.append(kw)
        text = next(b["text"] for b in kw["messages"][0]["content"] if b["type"] == "text")
        items = {i["id"]: i for i in json.loads(text)["evidence"]}
        m = self.mode
        if m == "refusal":
            return _resp("", "refusal")
        if m == "garbage":
            return _resp("{{ nope")
        if m == "bad_enum":
            return _resp(json.dumps(self._o(tier="maybe")))
        if m == "unknown_ids":
            return _resp(json.dumps(self._o(cited=["made-up"])))
        if m == "unknown_then_fix":
            return _resp(json.dumps(self._o(cited=["nope"] if len(self.calls) == 1 else ["finding", "dom"])))
        if m == "banned_impact":
            return _resp(json.dumps(self._o(impact="After this change the page is fully compliant.")))
        if m == "banned_plan":
            return _resp(json.dumps(self._o(plan="Ensures WCAG conformance and is certified.")))
        if m == "always_auto":  # tries to auto-fix everything
            return _resp(json.dumps(self._o()))
        if m == "empty_plan":
            return _resp(json.dumps(self._o(plan="")))
        # good: auto_fix only when the source was found
        if items["source"].get("found"):
            return _resp(json.dumps(self._o()))
        return _resp(json.dumps(self._o(tier="needs_human", reason="ambiguous_source", plan="")))

    def _o(self, tier="auto_fix", reason="missing_label", cited=None, plan="Bind the visible text as the label",
           impact="A screen-reader user hears an unlabeled edit field."):
        return {"tier": tier, "reason_code": reason, "impact_text": impact, "fix_plan": plan,
                "confidence": self.confidence, "evidence_cited": cited if cited is not None else ["finding", "dom", "source"],
                "preconditions": ["visible label text is adjacent"], "coverage_gaps": []}

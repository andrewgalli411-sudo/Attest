"""LLM judge: maps one evidence bundle to a proposed VPAT level, with a hard evidence contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ...disclaimers import contains_banned_claim

PROMPT_VERSION = "v1"
LEVELS = ["supports", "partially_supports", "does_not_support", "not_applicable", "not_evaluated"]

SYSTEM_PROMPT = """You are an accessibility conformance analyst drafting one row of a VPAT 2.5 \
(WCAG 2.1) report for a human auditor to review. You receive ONE success criterion and the \
automated evidence collected for ONE page state. Your output is a draft proposal, never a verdict.

Rules:
1. Judge ONLY from the supplied evidence items. Do not use outside knowledge of the site.
2. Every claim must cite evidence item ids in `evidence_cited`. Cite only ids you were given.
3. Automated tests cannot establish conformance for most criteria. If the evidence does not cover \
the criterion's requirements, answer `not_evaluated` and set `needs_human` true. Absence of \
failures is not evidence of conformance.
4. `supports` requires explicit passing evidence that covers the relevant elements and NO failing \
evidence. Any failure alongside passing instances is `partially_supports`. Failures with no \
passing coverage are `does_not_support`. Use `not_applicable` only if evidence shows the feature \
the criterion governs is absent.
5. Use VPAT terms only (Supports, Partially Supports, Does Not Support, Not Applicable). Never \
write the words compliant, compliance, certified, or "conforms to".
6. `remark` states observable facts (element selectors, rule names, counts) in 1-3 sentences. No \
remediation advice.
7. `confidence` in [0,1]: 0.8+ only when deterministic evidence covers both pass and fail cases; \
<=0.5 when relying on inference; <=0.6 when only automated `passes` exist for a criterion \
automation cannot fully test.
8. List what you could not check in `coverage_gaps`."""

SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": LEVELS},
        "remark": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_cited": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        "needs_human": {"type": "boolean"},
    },
    "required": ["level", "remark", "confidence", "evidence_cited", "reasoning",
                 "coverage_gaps", "needs_human"],
    "additionalProperties": False,
}


@dataclass
class Proposal:
    level: str
    remark: str
    confidence: float
    evidence_cited: list[str]
    reasoning: str = ""
    coverage_gaps: list[str] = field(default_factory=list)
    needs_human: bool = True
    valid: bool = True
    guardrail_notes: list[str] = field(default_factory=list)
    model_id: str = ""
    prompt_version: str = PROMPT_VERSION
    input_hash: str = ""
    usage: dict | None = None
    raw: str = ""


def build_evidence_items(sc_ev: dict) -> list[dict]:
    """Stable, id-addressed evidence list. Ids are the citation contract."""
    items: list[dict] = []
    names = {"violations": ("violation", "fail"), "incomplete": ("incomplete", "needs_review"),
             "passes": ("pass", "pass")}
    for kind, (label, outcome) in names.items():
        for rule in sc_ev["axe"][kind]:
            items.append({
                "id": f"axe:{label}:{rule['rule']}", "kind": "axe", "outcome": outcome,
                "rule": rule["rule"], "impact": rule["impact"], "node_count": len(rule["nodes"]),
                "sample_nodes": rule["nodes"][:5],
            })
    for p in sc_ev["probes"]:
        items.append({
            "id": f"probe:{p['probe']}", "kind": "probe",
            "outcome": {"pass": "pass", "fail": "fail", "inconclusive": "inconclusive"}[p["verdict"]],
            "error": p.get("error"), "observations": p["observations"][:25],
        })
    return items


def build_input(criterion: dict, page_url: str, state: str | None, items: list[dict]) -> dict:
    return {
        "criterion": {k: criterion[k] for k in ("sc_id", "title", "level")},
        "page": {"url": page_url, "state": state},
        "evidence": items,
    }


def input_hash(payload: dict, model: str) -> str:
    blob = json.dumps([payload, model, PROMPT_VERSION], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def apply_guardrails(p: Proposal, items: list[dict]) -> Proposal:
    """Deterministic checks the LLM cannot talk its way past."""
    ids = {i["id"] for i in items}
    outcomes = {i["outcome"] for i in items}

    unknown = [c for c in p.evidence_cited if c not in ids]
    if unknown:
        p.valid = False
        p.guardrail_notes.append(f"cited unknown evidence ids: {unknown}")
    if p.level not in LEVELS:
        p.valid = False
        p.guardrail_notes.append(f"unknown level {p.level!r}")
    if contains_banned_claim(p.remark) or contains_banned_claim(p.reasoning):
        p.valid = False
        p.guardrail_notes.append("banned compliance/certification wording")

    if not p.valid:
        p.level, p.needs_human, p.confidence = "not_evaluated", True, 0.0
        return p

    if p.level == "supports":
        if "fail" in outcomes:
            p.level, p.needs_human = "not_evaluated", True
            p.guardrail_notes.append("Supports proposed despite failing evidence")
        elif "pass" not in outcomes:
            p.level, p.needs_human = "not_evaluated", True
            p.guardrail_notes.append("Supports proposed without any passing evidence")
        elif not any(i["kind"] == "probe" and i["outcome"] == "pass" for i in items):
            p.confidence = min(p.confidence, 0.6)
    if p.level in ("partially_supports", "does_not_support") and "fail" not in outcomes:
        p.level, p.needs_human = "not_evaluated", True
        p.guardrail_notes.append("failure level proposed without failing evidence")
    if p.level == "not_evaluated" or outcomes & {"needs_review", "inconclusive"}:
        p.needs_human = True
    p.confidence = max(0.0, min(1.0, float(p.confidence)))
    if p.confidence < 0.5:
        p.needs_human = True
    return p


def _invalid(model: str, h: str, note: str, raw: str = "", usage: dict | None = None) -> Proposal:
    return Proposal("not_evaluated", f"Judge produced no usable proposal ({note}).", 0.0, [],
                    valid=False, guardrail_notes=[note], model_id=model, input_hash=h,
                    usage=usage, raw=raw)


async def judge_bundle(client, model: str, payload: dict, items: list[dict]) -> Proposal:
    """One structured-output call (no forced tool_choice / sampling params: rejected on 5.5)."""
    h = input_hash(payload, model)
    messages = [{"role": "user", "content": json.dumps(payload, sort_keys=True)}]
    last_note = ""
    for attempt in range(2):
        resp = await client.messages.create(
            model=model,
            max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else None
        if resp.stop_reason in ("refusal", "max_tokens"):
            return _invalid(model, h, f"stop_reason={resp.stop_reason}", usage=usage)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            p = Proposal(
                level=data["level"], remark=data["remark"], confidence=float(data["confidence"]),
                evidence_cited=list(data["evidence_cited"]), reasoning=data.get("reasoning", ""),
                coverage_gaps=list(data.get("coverage_gaps", [])),
                needs_human=bool(data.get("needs_human", True)),
                model_id=model, input_hash=h, usage=usage, raw=text,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            last_note = f"unparseable response: {type(e).__name__}"
            continue
        p = apply_guardrails(p, items)
        if p.valid:
            return p
        last_note = "; ".join(p.guardrail_notes)
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"Invalid: {last_note}. Re-answer citing only these ids: "
                                        f"{[i['id'] for i in items]}. Avoid prohibited wording."},
        ]
    return _invalid(model, h, last_note or "no response")



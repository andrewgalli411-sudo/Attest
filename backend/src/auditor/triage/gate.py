"""Deterministic gate. The model may downgrade a finding to needs_human; it can NEVER upgrade one."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..disclaimers import contains_banned_claim
from .schema import (
    ALWAYS_HUMAN_PREFIXES,
    CONFIDENCE_FLOOR,
    PR1_CONFIDENCE,
    PR1_RULES,
    PR2_RULES,
    REASONS,
    SAFE_RULES,
    TIERS,
)


@dataclass
class GateResult:
    tier: str
    reason_code: str
    pr_tier: str | None  # PR1 | PR2 | None (None => work-order issue)
    valid: bool
    notes: list[str] = field(default_factory=list)


def apply_gate(
    *, rule_id: str, model_out: dict, evidence_ids: set[str], baseline_stable: bool,
    source_unique: bool, source_reason: str = "",
) -> GateResult:
    notes: list[str] = []
    valid = True

    tier, reason = model_out.get("tier"), model_out.get("reason_code")
    if tier not in TIERS or reason not in REASONS:
        return GateResult("needs_human", "other", None, False, [f"invalid enum values tier={tier!r} reason={reason!r}"])

    unknown = [c for c in model_out.get("evidence_cited", []) if c not in evidence_ids]
    if unknown:
        valid = False
        notes.append(f"cited unknown evidence ids: {unknown}")
    for key in ("impact_text", "fix_plan"):
        if contains_banned_claim(model_out.get(key, "")):
            valid = False
            notes.append(f"banned compliance/certification wording in {key}")
    if not valid:
        return GateResult("needs_human", "other", None, False, notes)

    if tier == "needs_human":
        return GateResult("needs_human", reason, None, True, notes)

    # ---- the model asked for auto_fix: every condition below must hold or we downgrade ----
    def human(code: str, why: str) -> GateResult:
        notes.append(why)
        return GateResult("needs_human", code, None, True, notes)

    if any(rule_id.startswith(p) for p in ALWAYS_HUMAN_PREFIXES):
        return human("not_automatable", f"rule {rule_id} always requires human judgment")
    if rule_id not in SAFE_RULES:
        return human("not_automatable", f"rule {rule_id} is not on the safe-fix allowlist")
    if not source_unique:  # checked first so the person is told the real reason (e.g. "inside an iframe")
        return human("ambiguous_source", f"no unique source location: {source_reason}")
    if not baseline_stable:
        return human("unstable", "finding was not present in both baseline passes")
    conf = float(model_out.get("confidence", 0))
    if conf < CONFIDENCE_FLOOR:
        return human(reason, f"confidence {conf:.2f} below {CONFIDENCE_FLOOR}")
    if not model_out.get("fix_plan", "").strip():
        return human(reason, "auto_fix proposed without a fix plan")

    pr = "PR1" if (rule_id in PR1_RULES and conf >= PR1_CONFIDENCE) else "PR2"
    if rule_id in PR2_RULES:
        pr = "PR2"
    return GateResult("auto_fix", reason, pr, True, notes)

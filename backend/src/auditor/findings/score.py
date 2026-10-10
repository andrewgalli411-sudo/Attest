"""Page score 0-100, severity-weighted.

contribution(rule) = weight(severity) * (1 + 0.5 * log2(instances))     # repeats count, diminishing
score = round(100 * exp(-sum(contributions) / K))

One Critical (25) must hurt more than ten Minors (10 distinct rules x 1); fixing anything never lowers it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from ..models import Severity

WEIGHTS = {Severity.critical: 25.0, Severity.serious: 10.0, Severity.moderate: 3.0, Severity.minor: 1.0}
K = 50.0


def page_score(findings: Iterable[tuple[str, Severity]]) -> tuple[int, dict]:
    """findings: (rule_id, severity) per instance on ONE page/state. Returns (score, breakdown)."""
    by_rule: dict[str, list[Severity]] = defaultdict(list)
    for rule, sev in findings:
        by_rule[rule].append(sev)
    total, rows = 0.0, []
    for rule, sevs in sorted(by_rule.items()):
        worst = min(sevs, key=list(Severity).index)  # enum order is critical..minor
        c = WEIGHTS[worst] * (1 + 0.5 * math.log2(len(sevs)))
        total += c
        rows.append({"rule": rule, "severity": worst.value, "instances": len(sevs), "penalty": round(c, 2)})
    return max(0, min(100, round(100 * math.exp(-total / K)))), {"penalty": round(total, 2), "rules": rows}


def run_score(page_scores: list[int], weights: list[int] | None = None) -> dict:
    """Mean (optionally weighted) plus the worst page, so one terrible page isn't averaged away."""
    if not page_scores:
        return {"score": 100, "worst": 100}
    w = weights or [1] * len(page_scores)
    mean = sum(s * x for s, x in zip(page_scores, w, strict=True)) / sum(w)
    return {"score": round(mean), "worst": min(page_scores)}

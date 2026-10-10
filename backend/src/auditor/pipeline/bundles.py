"""Turn one probed page into per-success-criterion evidence bundles."""

from __future__ import annotations

from .probes.base import PROBE_SCS


def sc_evidence(page_result: dict) -> dict[str, dict]:
    """sc_id -> {"axe": {violations, incomplete, passes}, "probes": [...]}. Only SCs with evidence."""
    out: dict[str, dict] = {}

    def slot(sc: str) -> dict:
        return out.setdefault(
            sc, {"axe": {"violations": [], "incomplete": [], "passes": []}, "probes": []}
        )

    axe = page_result.get("axe")
    if axe:
        for kind in ("violations", "incomplete", "passes"):
            for rule in axe[kind]:
                for sc in rule["sc"]:
                    slot(sc)["axe"][kind].append(rule)
    for probe in page_result.get("probes", []):
        for sc in probe["sc_ids"] or PROBE_SCS.get(probe["probe"], []):
            slot(sc)["probes"].append(probe)
    return out

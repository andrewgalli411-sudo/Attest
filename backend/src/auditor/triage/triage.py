"""Triage: one structured-output call per Finding, then the deterministic gate, then persistence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..models import Finding, FindingEvidence, FindingLocator, FindingStatus, Tier, TriageVerdict
from ..privacy import redact, redact_json
from ..storage.evidence_store import EvidenceStore
from .gate import apply_gate
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .schema import OUTPUT_SCHEMA, RULE_HELP

log = logging.getLogger(__name__)
CONCURRENCY = 4


def build_items(f: Finding, fp: dict, snippet: str, detail: dict, source: dict) -> list[dict]:
    snippet, detail = redact(snippet), redact_json(detail)  # nothing personal or secret leaves the machine
    items = [
        {"id": "finding", "rule": f.rule_id, "wcag": f.sc_ids, "severity": f.severity.value,
         "help": RULE_HELP.get(f.rule_id, ""), "detector": detail},
        {"id": "dom", "html": snippet},
        {"id": "locator", "tag": fp.get("tag"), "visible_text": fp.get("visible_text"),
         "landmark_path": fp.get("landmark_path"), "stable_css": fp.get("stable_css")},
    ]
    if source.get("unique") and source.get("hit"):
        h = source["hit"]
        items.append({"id": "source", "found": True, "file": h["file"], "line": h["line"],
                      "method": h["method"], "anchor": h["anchor"], "excerpt": redact(h["excerpt"])})
    else:
        items.append({"id": "source", "found": False, "reason": source.get("reason", "not resolved")})
    return items


def input_hash(items: list[dict], model: str, baseline_stable: bool, source_unique: bool, with_image: bool = False) -> str:
    blob = json.dumps([items, model, PROMPT_VERSION, baseline_stable, source_unique, with_image], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


async def call_model(client, model: str, items: list[dict], image_png: bytes | None) -> tuple[dict | None, dict | None, str]:
    """Returns (parsed_output | None, usage, note). One retry on unparseable output is left to caller."""
    content: list[dict] = [{"type": "text", "text": json.dumps({"evidence": items}, sort_keys=True)}]
    if image_png:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": base64.b64encode(image_png).decode()}})
    resp = await client.messages.create(
        model=model, max_tokens=3000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else None
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None, usage, f"stop_reason={resp.stop_reason}"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
        data["confidence"] = float(data["confidence"])
        return data, usage, ""
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        return None, usage, f"unparseable response: {type(e).__name__}"


async def triage_one(
    sf: sessionmaker, store: EvidenceStore, client, model: str, finding_id: str, send_image: bool = False
) -> TriageVerdict:
    with sf() as db:
        f = db.get(Finding, finding_id)
        loc = db.get(FindingLocator, finding_id)
        evs = db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == finding_id)).all()
        by_kind = {e.kind: e for e in evs}
        snippet = (by_kind["dom_snippet"].payload or {}).get("html", "") if "dom_snippet" in by_kind else ""
        detail = (by_kind["detector_detail"].payload or {}) if "detector_detail" in by_kind else {}
        source = (by_kind["source_hit"].payload or {}) if "source_hit" in by_kind else {
            "unique": False, "reason": "source mapping was not run"}
        clip_ref = by_kind["shot_clip"].blob_ref if "shot_clip" in by_kind else None
        rule_id, stable = f.rule_id, f.baseline_stable
        fp = loc.fingerprint
    image = store.get(clip_ref) if (clip_ref and send_image) else None  # screenshots are opt-in
    items = build_items(f, fp, snippet, detail, source)
    ids = {i["id"] for i in items}
    h = input_hash(items, model, stable, bool(source.get("unique")), image is not None)

    with sf() as db:  # reuse an identical, valid prior verdict: no new API call
        prior = db.scalar(select(TriageVerdict).where(TriageVerdict.input_hash == h, TriageVerdict.valid.is_(True))
                          .order_by(TriageVerdict.created_at.desc()))
        if prior is not None:
            return _record(sf, finding_id, prior.tier.value, prior.reason_code, prior.fix_plan, prior.confidence,
                           prior.evidence_cited, prior.gate_notes + ["reused cached verdict"], True,
                           prior.model_id, h, {"cached_from": prior.id}, None, prior.pr_tier)

    out, usage, note, gate = None, None, "", None
    for attempt in range(2):
        out, usage, note = await call_model(client, model, items, image)
        if out is None:
            continue
        gate = apply_gate(rule_id=rule_id, model_out=out, evidence_ids=ids, baseline_stable=stable,
                          source_unique=bool(source.get("unique")), source_reason=source.get("reason", ""))
        if gate.valid:
            break
        note = "; ".join(gate.notes)
    if out is None or gate is None or not gate.valid:
        return _record(sf, finding_id, "needs_human", "other", "", 0.0, [], [note or "no usable triage output"],
                       False, model, h, usage, None, None)
    return _record(sf, finding_id, gate.tier, gate.reason_code, out.get("fix_plan", ""), out["confidence"],
                   out.get("evidence_cited", []), gate.notes, True, model, h, usage, out.get("impact_text", ""),
                   gate.pr_tier)


def _record(sf, finding_id, tier, reason, plan, conf, cited, notes, valid, model, h, usage, impact, pr_tier):
    with sf() as db:
        v = TriageVerdict(finding_id=finding_id, tier=Tier(tier), reason_code=reason, pr_tier=pr_tier,
                          fix_plan=plan or "",
                          confidence=conf, evidence_cited=cited, gate_notes=notes, valid=valid, model_id=model,
                          prompt_version=PROMPT_VERSION, input_hash=h, usage=usage)
        db.add(v)
        f = db.get(Finding, finding_id)
        f.status = FindingStatus.triaged if tier == "auto_fix" else FindingStatus.needs_human
        if impact:
            f.impact_text = impact
            db.add(FindingEvidence(finding_id=finding_id, stage="explained", kind="impact",
                                   payload={"text": impact}))
        db.add(FindingEvidence(finding_id=finding_id, stage="plan", kind="triage",
                               payload={"tier": tier, "reason": reason, "fix_plan": plan, "pr_tier": pr_tier,
                                        "confidence": conf, "gate_notes": notes}))
        db.commit()
        db.refresh(v)
        return v


async def run_triage(sf: sessionmaker, store: EvidenceStore, client, model: str, run_id: str,
                     send_image: bool = False) -> list[str]:
    with sf() as db:
        ids = list(db.scalars(select(Finding.id).where(Finding.run_id == run_id,
                                                       Finding.status == FindingStatus.caught)))
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(fid: str) -> None:
        async with sem:
            try:
                await triage_one(sf, store, client, model, fid, send_image)
            except Exception as e:  # noqa: BLE001 - one finding must not stop the run
                log.warning("triage failed for %s: %s", fid, e)

    await asyncio.gather(*[one(i) for i in ids])
    return ids

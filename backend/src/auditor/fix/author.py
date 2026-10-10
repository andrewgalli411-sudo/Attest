"""Author source edits for one auto_fix finding (structured output; anchor-exact edits, never free diffs)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..disclaimers import contains_banned_claim
from ..privacy import PLACEHOLDER, redact, redact_json
from .safety import UnsafeEdit, check_edit_shape
from .source_map import SourceHit

PROMPT_VERSION = "f1"
MAX_FILE_CHARS = 24_000

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {"type": "array", "items": {
            "type": "object",
            "properties": {"file": {"type": "string"}, "anchor_text": {"type": "string"},
                           "replacement": {"type": "string"}},
            "required": ["file", "anchor_text", "replacement"], "additionalProperties": False}},
        "explanation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["edits", "explanation", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write the smallest possible source edit that fixes ONE accessibility finding, \
for a human to review in a pull request.

Rules:
1. Return edits as {file, anchor_text, replacement}. `file` must be exactly the file given. \
`anchor_text` must be copied VERBATIM from the source (including whitespace) and must occur exactly \
once in that file; `replacement` is what replaces it. Start from the provided `anchor` and keep your \
change confined to it plus, if the fix needs a new element next to it, the immediately adjacent markup.
2. Make the minimal change. Do not reformat, rename, reorder or refactor anything else.
3. Do not invent user-facing wording. Reuse text that already exists in the source (for example an \
existing visible label). If the fix would require wording that is not present, return no edits and \
explain why in `explanation`.
4. Preserve the file's conventions (quote style, indentation, JSX vs HTML attribute names such as \
htmlFor/className in JSX).
5. For form fields prefer a real <label> association (htmlFor + id, or wrapping) over aria-label when \
a visible label text exists. Ensure any new id is unique and stable (derive it from the field name).
6. `explanation`: 1-3 plain sentences for the PR description: what changed and why a user benefits. \
Never say the page or change is compliant, conforming, accessible-by-default, or certified; describe \
only the edit.
7. `confidence` in [0,1]: your confidence the edit is correct and complete. Use <0.8 if unsure."""

_ATTR = re.compile(r"""([A-Za-z_:][\w:.-]*)\s*=""")
_JSX_TO_DOM = {"htmlFor": "for", "className": "class", "tabIndex": "tabindex", "readOnly": "readonly",
               "autoComplete": "autocomplete", "maxLength": "maxlength", "ariaLabel": "aria-label"}


@dataclass
class AuthoredFix:
    edits: list[dict] = field(default_factory=list)
    expected_dom_changes: list[str] = field(default_factory=list)
    explanation: str = ""
    confidence: float = 0.0
    valid: bool = False
    notes: list[str] = field(default_factory=list)
    usage: dict | None = None


def derive_dom_changes(edits: list[dict]) -> list[str]:
    """DOM attributes whose presence/value the edit changes (so relocation can ignore them).
    Computed from the edit text itself - not trusted from the model."""
    changed: set[str] = set()
    for e in edits:
        before = {_JSX_TO_DOM.get(a, a): 1 for a in _ATTR.findall(e["anchor_text"])}
        after = {_JSX_TO_DOM.get(a, a): 1 for a in _ATTR.findall(e["replacement"])}
        changed |= set(after) - set(before)
        # attributes present on both sides but with a different value token
        for a in set(before) & set(after):
            pat = re.compile(rf"""{re.escape(a)}\s*=\s*("[^"]*"|'[^']*'|\{{[^}}]*\}})""")
            if pat.findall(e["anchor_text"]) != pat.findall(e["replacement"]):
                changed.add(a)
    return sorted(changed - {"key", "ref"})


def build_user_content(finding: dict, hit: SourceHit, plan: str, file_text: str,
                       previous_error: str | None) -> str:
    body = redact(file_text if len(file_text) <= MAX_FILE_CHARS else hit.excerpt)
    payload = {
        "finding": redact_json(finding),
        "intended_fix": plan,
        "file": hit.file,
        "anchor": hit.anchor,
        "source": body,
    }
    if previous_error:
        payload["previous_attempt_failed_because"] = previous_error
    return json.dumps(payload, sort_keys=True)


async def author_fix(client, model: str, finding: dict, hit: SourceHit, plan: str, file_text: str,
                     previous_error: str | None = None) -> AuthoredFix:
    resp = await client.messages.create(
        model=model, max_tokens=4000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": build_user_content(finding, hit, plan, file_text, previous_error)}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": FIX_SCHEMA}},
    )
    out = AuthoredFix(usage=resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else None)
    if resp.stop_reason in ("refusal", "max_tokens"):
        out.notes.append(f"stop_reason={resp.stop_reason}")
        return out
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
        out.edits = [dict(e) for e in data["edits"]]
        out.explanation = str(data["explanation"])
        out.confidence = float(data["confidence"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        out.notes.append(f"unparseable response: {type(e).__name__}")
        return out

    if not out.edits:
        out.notes.append("model declined to edit: " + out.explanation[:200])
        return out
    if contains_banned_claim(out.explanation) or any(contains_banned_claim(e["replacement"]) for e in out.edits):
        out.notes.append("banned compliance/certification wording")
        return out
    for e in out.edits:
        if e["file"] != hit.file:
            out.notes.append(f"edit targets {e['file']!r}, expected {hit.file!r}")
            return out
        if file_text.count(e["anchor_text"]) != 1:
            out.notes.append(f"anchor_text occurs {file_text.count(e['anchor_text'])} times (need exactly 1)")
            return out
    for e in out.edits:  # the model saw scrubbed text; a placeholder in its output means it would write '[email]' into the page
        if len(PLACEHOLDER.findall(e["replacement"])) > len(PLACEHOLDER.findall(e["anchor_text"])):
            out.notes.append("edit would insert redacted placeholder text; wording must come from a human")
            return out
    rule = finding.get("rule")
    if rule is not None:  # reject unsafe shapes now so the model gets the reason and can retry
        for e in out.edits:
            try:
                check_edit_shape(rule, e["anchor_text"], e["replacement"])
            except UnsafeEdit as err:
                out.notes.append(f"unsafe edit rejected: {err}")
                return out
    if out.confidence < 0.8:
        out.notes.append(f"authoring confidence {out.confidence:.2f} below 0.8")
        return out
    out.expected_dom_changes = derive_dom_changes(out.edits)
    out.valid = True
    return out

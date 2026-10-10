from __future__ import annotations

TIERS = ["auto_fix", "needs_human"]
REASONS = [
    "missing_label", "missing_lang", "missing_aria_name", "heading_order", "name_role_value",
    "contrast", "alt_meaningfulness", "custom_widget_focus", "ambiguous_source", "unstable",
    "not_automatable", "other",
]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": TIERS},
        "reason_code": {"type": "string", "enum": REASONS},
        "impact_text": {"type": "string"},
        "fix_plan": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_cited": {"type": "array", "items": {"type": "string"}},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tier", "reason_code", "impact_text", "fix_plan", "confidence", "evidence_cited",
                 "preconditions", "coverage_gaps"],
    "additionalProperties": False,
}

# Rules whose fix is a small, local, well-understood source edit. Everything else is needs_human.
# PR1 = mechanical / zero judgment; PR2 = safe but a human should glance at the wording.
PR1_RULES = {"html-has-lang", "html-lang-valid", "label", "select-name"}
PR2_RULES = {"button-name", "link-name", "aria-input-field-name", "aria-toggle-field-name",
             "aria-command-name"}  # heading-order is not here: it has no edit policy, so it is never auto-fixed
SAFE_RULES = PR1_RULES | PR2_RULES

# Never auto-fixed no matter what the model says: they require human judgment.
ALWAYS_HUMAN_PREFIXES = ("color-contrast", "image-alt", "input-image-alt", "role-img-alt", "svg-img-alt",
                         "probe:", "scrollable-region", "frame-title", "video-caption", "audio-caption")

CONFIDENCE_FLOOR = 0.8
PR1_CONFIDENCE = 0.9

RULE_HELP = {
    "label": "Form element has no programmatic label (WCAG 1.3.1 / 3.3.2 / 4.1.2).",
    "html-has-lang": "<html> has no lang attribute, so assistive tech cannot choose the right voice (WCAG 3.1.1).",
    "html-lang-valid": "<html> lang value is not a valid language code (WCAG 3.1.1).",
    "select-name": "<select> has no accessible name (WCAG 4.1.2).",
    "button-name": "Button has no discernible text (WCAG 4.1.2).",
    "link-name": "Link has no discernible text (WCAG 2.4.4 / 4.1.2).",
    "heading-order": "Heading levels skip (WCAG 1.3.1).",
    "color-contrast": "Text/background contrast is below the WCAG minimum (WCAG 1.4.3).",
}

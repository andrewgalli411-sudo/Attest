"""Severity assignment. axe impact is inherited verbatim; probes use a fixed table."""

from __future__ import annotations

from ..models import Severity

_AXE = {"critical": Severity.critical, "serious": Severity.serious,
        "moderate": Severity.moderate, "minor": Severity.minor}

# (probe, observation kind) -> severity
PROBE_SEVERITY: dict[tuple[str, str], Severity] = {
    ("keyboard_trap", "trapped_elements"): Severity.critical,
    ("keyboard_nav", "unreachable"): Severity.serious,
    ("form_labels", "no_accessible_name"): Severity.serious,
    ("form_labels", "name_from_weak_source"): Severity.moderate,
    ("form_labels", "error_not_associated"): Severity.serious,
    ("name_role_value", "missing_name"): Severity.serious,
    ("name_role_value", "missing_role"): Severity.moderate,
    ("name_role_value", "label_not_in_name"): Severity.moderate,
    ("focus_visible", "no_visible_focus"): Severity.serious,
    ("modal_focus", "focus_not_moved_into_dialog"): Severity.serious,
    ("modal_focus", "focus_escaped_modal"): Severity.serious,
    ("modal_focus", "escape_does_not_close"): Severity.serious,
    ("modal_focus", "focus_not_returned_to_trigger"): Severity.moderate,
    ("modal_focus", "trigger_not_keyboard_operable"): Severity.serious,
}


def axe_severity(impact: str | None) -> Severity:
    return _AXE.get(impact or "", Severity.moderate)  # axe omits impact on some "incomplete" nodes


def probe_severity(probe: str, kind: str) -> Severity:
    return PROBE_SEVERITY.get((probe, kind), Severity.moderate)

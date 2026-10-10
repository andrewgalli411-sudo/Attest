from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass
class ProbeResult:
    probe: str
    sc_ids: list[str]
    verdict: Literal["pass", "fail", "inconclusive"]
    observations: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# Default SC coverage per probe; used so a crashed probe still lands as "inconclusive" evidence.
PROBE_SCS = {
    "keyboard_nav": ["2.1.1"],
    "keyboard_trap": ["2.1.2"],
    "focus_visible": ["2.4.7"],
    "modal_focus": ["2.4.3", "2.1.2", "4.1.2"],
    "form_labels": ["1.3.1", "3.3.2", "4.1.2", "3.3.1"],
    "name_role_value": ["4.1.2", "2.5.3"],
}

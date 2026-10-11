from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page

AXE_PATH = Path(__file__).resolve().parents[3] / "vendor" / "axe.min.js"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
_SC_TAG = re.compile(r"^wcag(\d)(\d)(\d+)$")


def tags_to_sc(tags: list[str]) -> list[str]:
    """axe tag 'wcag143' -> '1.4.3'; 'wcag1410' -> '1.4.10'."""
    out = []
    for t in tags:
        m = _SC_TAG.match(t)
        if m:
            out.append(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
    return sorted(set(out))


async def run_axe(page: Page) -> dict:
    # axe aggregates results across frames only if it is loaded in each of them. Without this, everything inside an
    # iframe is silently missing from the audit.
    source = AXE_PATH.read_text()
    await page.add_script_tag(content=source)
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            await frame.add_script_tag(content=source)
        except Exception:
            continue
    raw = await page.evaluate(
        "tags => axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: "
        "['violations','incomplete','passes']})",
        AXE_TAGS,
    )

    def norm(items: list[dict]) -> list[dict]:
        rows = [
            {
                "rule": i["id"],
                "impact": i.get("impact"),
                "sc": tags_to_sc(i.get("tags", [])),
                "nodes": sorted(
                    ({"target": n["target"], "html": n["html"][:300]} for n in i["nodes"]),
                    key=lambda n: str(n["target"]),
                ),
            }
            for i in items
        ]
        return sorted(rows, key=lambda r: r["rule"])

    return {
        "axe_version": raw["testEngine"]["version"],
        "violations": norm(raw["violations"]),
        "incomplete": norm(raw["incomplete"]),
        "passes": norm(raw["passes"]),
    }

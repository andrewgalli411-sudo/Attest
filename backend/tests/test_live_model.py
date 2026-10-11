"""Runs the whole loop with the REAL model. Skipped unless A11Y_LIVE=1 (it spends a few cents per run).

    A11Y_LIVE=1 uv run pytest tests/test_live_model.py

It checks outcomes a human reviewer would care about, not exact wording (model text varies run to run).
"""

import os

import pytest
from sqlalchemy import select
from test_remediate import remediate

from auditor.config import settings
from auditor.disclaimers import contains_banned_claim
from auditor.models import Finding, FindingEvidence, FindingStatus, TriageVerdict

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("A11Y_LIVE"), reason="set A11Y_LIVE=1 to run against the real model"),
    pytest.mark.timeout(600),
]


async def test_real_model_triages_and_fixes_the_demo_correctly(sf, tmp_path, demo_repo):
    if not (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.fail("A11Y_LIVE=1 but no ANTHROPIC_API_KEY is configured")
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    rid, summary, _ = await remediate(sf, tmp_path, demo_repo[0], client=client)

    assert summary["model_kind"] == "language model"
    assert summary["proved"] == 2 and summary["regressions"] == 0, summary["headline"]
    with sf() as db:
        f = {x.rule_id: x for x in db.scalars(select(Finding).where(Finding.run_id == rid))}
        # judgment calls must stay with a person, whatever the model says
        assert f["color-contrast"].status == FindingStatus.needs_human
        # the mechanical ones are fixed, proved, and regression-clean
        assert f["label"].status == FindingStatus.regress_ok and f["html-has-lang"].status == FindingStatus.regress_ok
        for x in f.values():
            v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == x.id))
            assert v.valid and v.model_id.startswith("claude-")
            assert x.impact_text and not contains_banned_claim(x.impact_text)  # plain-English impact, no claims
        diff = db.scalar(select(FindingEvidence).where(FindingEvidence.finding_id == f["label"].id, FindingEvidence.kind == "diff"))
        edits = diff.payload["edits"]
        assert len(edits) == 1 and edits[0]["file"] == "src/App.jsx"
        # whichever valid fix it chose, the field must end up with a programmatic name from the visible text
        assert 'Email address' in edits[0]["replacement"]
        assert ("htmlFor" in edits[0]["replacement"] and 'id="email"' in edits[0]["replacement"]) or "aria-label" in edits[0]["replacement"]

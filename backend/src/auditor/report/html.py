"""Static, self-contained HTML for a run: triage dashboard + one evidence timeline per finding.

Images are embedded (data URIs) so the file can be reviewed offline. The page itself is built to be
accessible (landmarks, headings, alt text, contrast) - an accessibility tool's own output has to pass
its own checks, and the test suite runs axe against it.
"""

from __future__ import annotations

import base64
import html
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..disclaimers import DRAFT_BANNER, contains_banned_claim
from ..models import (
    AuditRun,
    Finding,
    FindingEvidence,
    FixAttempt,
    PageState,
    RegressionCheck,
    ReproveResult,
    TriageVerdict,
)
from ..storage.evidence_store import EvidenceStore

e = html.escape

STATUS_LABEL = {
    "caught": "Caught", "triaged": "Planned for auto-fix", "needs_human": "Needs a person",
    "fix_authored": "Fix written", "pr_open": "Pull request open", "proved": "Fix proved",
    "prove_failed": "Fix did not work", "inconclusive": "Could not be re-tested",
    "regress_ok": "Fixed and proved, no regressions", "regress_fail": "Fixed but caused a regression",
}
SEV_ORDER = ["critical", "serious", "moderate", "minor"]

CSS = """
:root{--bg:#fff;--fg:#14171a;--muted:#4a5560;--line:#d5dbe1;--card:#f6f8fa;--ok:#0b6b3a;--bad:#a4161a;--warn:#7a4a00;--link:#0b4fae}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--fg:#e8edf1;--muted:#a9b4be;--line:#2c353d;--card:#171d23;--ok:#5fd18d;--bad:#ff8d8f;--warn:#f1be64;--link:#8ab8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
a{color:var(--link)}header,main,footer{max-width:60rem;margin:0 auto;padding:1rem}
.banner{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--warn);padding:.75rem 1rem;border-radius:6px}
h1{margin:.5rem 0}h2{margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.25rem}
.headline{font-size:1.4rem;font-weight:700;margin:.5rem 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.75rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.75rem}
.card b{display:block;font-size:1.6rem}
table{border-collapse:collapse;width:100%}th,td{border:1px solid var(--line);padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:var(--card)}.sev{font-weight:700}.sev-critical,.sev-serious{color:var(--bad)}.sev-moderate{color:var(--warn)}
.st-regress_ok,.st-proved{color:var(--ok);font-weight:600}.st-needs_human,.st-regress_fail,.st-prove_failed{color:var(--bad);font-weight:600}
.timeline{list-style:none;padding:0;margin:0;border-left:3px solid var(--line)}
.timeline>li{margin:0 0 1.25rem 1rem;padding:.75rem 1rem;background:var(--card);border:1px solid var(--line);border-radius:8px}
.timeline h4{margin:.1rem 0 .5rem}figure{margin:0}img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:4px;background:#fff}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:1rem}
pre{overflow:auto;background:var(--bg);border:1px solid var(--line);padding:.6rem;border-radius:6px;font-size:.85rem}
.sr{position:absolute;left:-9999px}.add{color:var(--ok)}.del{color:var(--bad)}code{font-family:ui-monospace,monospace}
"""


def _img(store: EvidenceStore, ref: str | None, alt: str) -> str:
    if not ref:
        return ""
    b64 = base64.b64encode(store.get(ref)).decode()
    return f'<img src="data:image/png;base64,{b64}" alt="{e(alt)}">'


def _diff_html(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        cls = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else ""
        out.append(f'<span class="{cls}">{e(line)}</span>' if cls else e(line))
    return "<pre><code>" + "\n".join(out) + "</code></pre>"


def _safe(text: str) -> str:
    """Generated prose goes through the same banned-claims filter as PR text before it is shown."""
    return e("[withheld: contained a compliance claim]" if contains_banned_claim(text) else text)


def finding_timeline(sf: sessionmaker, store: EvidenceStore, finding_id: str) -> str:
    with sf() as db:
        f = db.get(Finding, finding_id)
        ps = db.get(PageState, f.page_state_id)
        evs = db.scalars(select(FindingEvidence).where(FindingEvidence.finding_id == finding_id)
                         .order_by(FindingEvidence.created_at)).all()
        v = db.scalar(select(TriageVerdict).where(TriageVerdict.finding_id == finding_id)
                      .order_by(TriageVerdict.created_at.desc()))
        rr = db.scalar(select(ReproveResult).where(ReproveResult.finding_id == finding_id))
        fa = db.scalar(select(FixAttempt).where(FixAttempt.finding_id == finding_id))
        rg = db.scalar(select(RegressionCheck).join(FixAttempt, FixAttempt.id == RegressionCheck.fix_attempt_id)
                       .where(FixAttempt.finding_id == finding_id))
    by_kind = {ev.kind: ev for ev in evs}
    el = (by_kind["detector_detail"].payload or {}) if "detector_detail" in by_kind else {}
    parts = [f'<section id="finding-{e(finding_id)}" aria-labelledby="h-{e(finding_id)}">',
             f'<h3 id="h-{e(finding_id)}"><code>{e(f.rule_id)}</code> on {e(ps.state_label or ps.url)}</h3>',
             f'<p><span class="sev sev-{f.severity.value}">{f.severity.value.title()}</span> · WCAG {e(", ".join(f.sc_ids) or "n/a")}'
             f' · <span class="st-{f.status.value}">{e(STATUS_LABEL.get(f.status.value, f.status.value))}</span></p>',
             '<ol class="timeline">']

    # 1. caught
    snippet = (by_kind["dom_snippet"].payload or {}).get("html", "") if "dom_snippet" in by_kind else ""
    parts.append('<li><h4>1. Caught</h4>'
                 + '<div class="pair">'
                 + (f'<figure>{_img(store, by_kind["shot_clip"].blob_ref, "The affected element on the real page, boxed, with the role and accessible name assistive technology receives")}'
                    '<figcaption>The element, as caught</figcaption></figure>' if "shot_clip" in by_kind else "")
                 + (f'<figure>{_img(store, by_kind["shot_full_annotated"].blob_ref, "Full page with the affected element outlined")}'
                    '<figcaption>In context on the page</figcaption></figure>' if "shot_full_annotated" in by_kind else "")
                 + '</div>'
                 + f'<details><summary>HTML around the element</summary><pre><code>{e(snippet)}</code></pre></details>'
                 + (f'<p>Detector impact: {e(str(el.get("impact") or "n/a"))}</p>' if el else "") + '</li>')

    # 2. explained
    if f.impact_text:
        parts.append(f'<li><h4>2. What a user experiences</h4><p>{_safe(f.impact_text)}</p></li>')
    # 3. plan
    if v:
        tier = "Can be fixed automatically (as a pull request a person reviews)" if v.tier.value == "auto_fix" \
            else "Needs a person"
        notes = f' <small>({e("; ".join(v.gate_notes))})</small>' if v.gate_notes else ""
        parts.append(f'<li><h4>3. Plan</h4><p><b>{e(tier)}</b> - reason: {e(v.reason_code)}{notes}</p>'
                     + (f'<p>Intended fix: {_safe(v.fix_plan)}</p>' if v.fix_plan else "")
                     + (f'<p>Confidence {v.confidence:.2f}</p>' if v.valid else "<p>No usable automated triage.</p>")
                     + '</li>')
    # 4. fix
    if fa and fa.diff:
        expl = (by_kind["diff"].payload or {}).get("explanation", "") if "diff" in by_kind else ""
        parts.append(f'<li><h4>4. The change</h4><p>{_safe(expl)}</p>{_diff_html(fa.diff)}'
                     + (f'<p>Branch: <code>{e(fa.branch)}</code> (tier {e(fa.tier_pr)})</p>' if fa.branch else "") + '</li>')
    elif "fix_failed" in by_kind:
        parts.append(f'<li><h4>4. No safe change could be made</h4><p>{_safe(str((by_kind["fix_failed"].payload or {}).get("reason", "")))}</p></li>')
    # 5. proof
    if rr:
        verdict = {"pass": "Passed", "fail": "Still fails", "inconclusive": "Inconclusive"}[rr.verdict]
        parts.append('<li><h4>5. Proof on the patched build</h4>'
                     f'<p><b>{verdict}.</b> {_safe(rr.notes or "")}</p>'
                     f'<p>Unpatched control still failed: {"yes" if rr.baseline_failed else "no"} · '
                     f'same element re-located: {"yes" if rr.relocated else "no"} (similarity {rr.relocation_score:.2f}, {e(rr.relocation_method or "-")}) · '
                     f'runs: {rr.runs_n}</p>'
                     '<div class="pair">'
                     f'<figure>{_img(store, rr.before_ref, "Before the fix: the same element on the unpatched build")}<figcaption>Before</figcaption></figure>'
                     f'<figure>{_img(store, rr.after_ref, "After the fix: the same element on the patched build")}<figcaption>After</figcaption></figure>'
                     '</div></li>')
    # 6. regression
    if rg:
        parts.append(f'<li><h4>6. Regression check</h4><p>{rg.regressions_count} new findings introduced on the page '
                     f'({len(rg.criteria_checked)} rules re-checked). Result: <b>{e(rg.verdict)}</b>.</p></li>')
    parts.append("</ol></section>")
    return "\n".join(parts)


def run_report(sf: sessionmaker, store: EvidenceStore, run_id: str) -> str:
    with sf() as db:
        run = db.get(AuditRun, run_id)
        findings = db.scalars(select(Finding).where(Finding.run_id == run_id)).all()
        summary = run.summary or {}
    by_sc: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        for sc in (f.sc_ids or ["other"]):
            by_sc[sc].append(f)
    rows = []
    for sc in sorted(by_sc):
        fs = by_sc[sc]
        fixed = sum(1 for f in fs if f.status.value in ("proved", "regress_ok", "regress_fail"))
        human = sum(1 for f in fs if f.status.value == "needs_human")
        rows.append(f"<tr><th scope='row'>WCAG {e(sc)}</th><td>{len(fs)}</td><td>{fixed}</td><td>{human}</td></tr>")
    order = sorted(findings, key=lambda f: (SEV_ORDER.index(f.severity.value), f.rule_id))
    sev_counts = summary.get("by_severity", {})
    sb, sa = summary.get("score_before", {}), summary.get("score_after", {})
    headline = e(summary.get("headline", f"{len(findings)} issues"))
    body = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Accessibility audit - run {e(run_id[:8])}</title><style>{CSS}</style></head><body>",
        f"<header><p class='banner' role='note'><b>Draft.</b> {e(DRAFT_BANNER)}</p>",
        f"<h1>Accessibility audit</h1><p class='headline'>{headline}</p></header><main id='main'>",
        "<section aria-labelledby='sum'><h2 id='sum'>Summary</h2><div class='cards'>",
        f"<div class='card'><b>{len(findings)}</b>issues found</div>",
        f"<div class='card'><b>{summary.get('proved', 0)}</b>fixed in pull requests and proved</div>",
        f"<div class='card'><b>{summary.get('needs_human', 0)}</b>need a person</div>",
        f"<div class='card'><b>{summary.get('regressions', 0)}</b>regressions introduced</div>",
        f"<div class='card'><b>{sb.get('score', '-')} to {sa.get('score', '-')}</b>page score (0-100)</div>",
        "</div>",
        "<p>Severity: " + ", ".join(f"{e(k)} {sev_counts.get(k, 0)}" for k in SEV_ORDER) + ".</p>",
        f"<p>Changes were produced by: {e(summary.get('model_kind', 'unknown'))}. Every change is a pull request for a person to review.</p></section>",
        "<section aria-labelledby='crit'><h2 id='crit'>By success criterion</h2><table><caption>Issues grouped by WCAG criterion</caption>"
        "<thead><tr><th scope='col'>Criterion</th><th scope='col'>Issues</th><th scope='col'>Fixed &amp; proved</th><th scope='col'>Need a person</th></tr></thead><tbody>",
        *rows, "</tbody></table></section>",
        "<section aria-labelledby='all'><h2 id='all'>All findings</h2><table><caption>Every finding, most severe first</caption><thead><tr>"
        "<th scope='col'>Severity</th><th scope='col'>Rule</th><th scope='col'>Status</th><th scope='col'>Evidence</th></tr></thead><tbody>",
    ]
    for f in order:
        body.append(f"<tr><td class='sev sev-{f.severity.value}'>{f.severity.value.title()}</td><td><code>{e(f.rule_id)}</code></td>"
                    f"<td class='st-{f.status.value}'>{e(STATUS_LABEL.get(f.status.value, f.status.value))}</td>"
                    f"<td><a href='#finding-{e(f.id)}'>View evidence<span class='sr'> for {e(f.rule_id)}</span></a></td></tr>")
    body.append("</tbody></table></section><section aria-labelledby='ev'><h2 id='ev'>Evidence</h2>")
    for f in order:
        body.append(finding_timeline(sf, store, f.id))
    body.append("</section></main><footer><p>" + e(DRAFT_BANNER) + "</p></footer>"
                "</body></html>")
    page = "\n".join(body)
    if contains_banned_claim(page.replace(e(DRAFT_BANNER), "")):
        raise ValueError("report contains a banned compliance claim")
    return page

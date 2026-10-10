"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type EvidenceOut, type FindingDetail } from "@/lib/api";
import { ErrorNote, SeverityBadge, StatusBadge } from "@/components/ui";

function Img({ e, alt }: { e?: EvidenceOut; alt: string }) {
  if (!e?.url) return null;
  // Evidence images are served by the API at runtime, so next/image optimisation does not apply.
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={api.url(e.url)} alt={alt} className="h-auto max-w-full rounded border border-line bg-white" />;
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="overflow-auto rounded border border-line bg-bg p-3 text-sm" tabIndex={0} aria-label="Code change">
      <code>
        {text.split("\n").map((l, i) => {
          const add = l.startsWith("+") && !l.startsWith("+++");
          const del = l.startsWith("-") && !l.startsWith("---");
          return <span key={i} className={add ? "text-ok" : del ? "text-bad" : ""}>{l}{"\n"}</span>;
        })}
      </code>
    </pre>
  );
}

export default function FindingView({ id }: { id: string }) {
  const [f, setF] = useState<FindingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    api.finding(id).then(setF).catch((e: Error) => setError(e.message));
  }, [id]);

  if (error) return <ErrorNote message={error} />;
  if (!f) return <p role="status">Loading...</p>;

  const ev = (kind: string) => f.evidence.find((e) => e.kind === kind);
  const snippet = (ev("dom_snippet")?.payload as { html?: string } | undefined)?.html;
  const step = "rounded-lg border border-line bg-card p-4";

  async function decide(action: string) {
    await api.decide(id, action);
    setSaved(action === "accept_pr" ? "Recorded: you accepted this change." : "Recorded: you rejected this change.");
  }

  return (
    <>
      <p><Link href={`/runs/${f.run_id}`}>&larr; Back to the dashboard</Link></p>
      <h1 className="mt-2 text-2xl font-bold"><code>{f.rule_id}</code> on {f.page}</h1>
      <p className="mt-1">
        <SeverityBadge severity={f.severity} /> - WCAG {f.sc_ids.join(", ") || "n/a"} - <StatusBadge status={f.status} />
      </p>

      <ol className="mt-6 grid gap-4" aria-label="Evidence timeline">
        <li className={step}>
          <h2 className="text-lg font-semibold">1. Caught</h2>
          <div className="mt-2 grid gap-4 sm:grid-cols-2">
            <figure><Img e={ev("shot_clip")} alt="The affected element on the real page, boxed, labelled with the role and accessible name assistive technology receives" /><figcaption className="text-sm text-muted">The element, as caught</figcaption></figure>
            <figure><Img e={ev("shot_full_annotated")} alt="The full page with the affected element outlined" /><figcaption className="text-sm text-muted">In context on the page</figcaption></figure>
          </div>
          {snippet && <details className="mt-2"><summary className="cursor-pointer">HTML around the element</summary><pre className="overflow-auto p-2 text-sm" tabIndex={0}>{snippet}</pre></details>}
        </li>

        {f.impact_text && (
          <li className={step}><h2 className="text-lg font-semibold">2. What a user experiences</h2><p className="mt-1">{f.impact_text}</p></li>
        )}

        {f.triage && (
          <li className={step}>
            <h2 className="text-lg font-semibold">3. Plan</h2>
            <p className="mt-1"><strong>{f.triage.tier === "auto_fix" ? "Can be fixed automatically, as a pull request a person reviews" : "Needs a person"}</strong> - reason: {f.triage.reason}</p>
            {f.triage.fix_plan && <p>Intended fix: {f.triage.fix_plan}</p>}
            {f.triage.gate_notes.length > 0 && <p className="text-sm text-muted">{f.triage.gate_notes.join("; ")}</p>}
          </li>
        )}

        {f.fix && (
          <li className={step}>
            <h2 className="text-lg font-semibold">4. The change</h2>
            <p className="mt-1">{f.fix.explanation}</p>
            <p className="text-sm text-muted">Branch <code>{f.fix.branch}</code> (tier {f.fix.tier}){f.fix.pr_url ? <> - <a href={f.fix.pr_url}>pull request</a></> : null}</p>
            <div className="mt-2"><Diff text={f.fix.diff} /></div>
          </li>
        )}

        {f.proof && (
          <li className={step}>
            <h2 className="text-lg font-semibold">5. Proof on the patched build</h2>
            <p className="mt-1"><strong>{f.proof.verdict === "pass" ? "Passed" : f.proof.verdict === "fail" ? "Still fails" : "Inconclusive"}.</strong> {f.proof.notes}</p>
            <p className="text-sm text-muted">
              Unpatched control still failed: {f.proof.baseline_failed ? "yes" : "no"} - same element re-located: {f.proof.relocated ? "yes" : "no"}
              {" "}(similarity {f.proof.relocation_score.toFixed(2)}) - runs: {f.proof.runs}
            </p>
            <div className="mt-2 grid gap-4 sm:grid-cols-2">
              <figure><Img e={ev("reprove_shot_before")} alt="Before the fix: the same element on the unpatched build, with its accessible name" /><figcaption className="text-sm text-muted">Before</figcaption></figure>
              <figure><Img e={ev("reprove_shot_after")} alt="After the fix: the same element on the patched build, with its accessible name" /><figcaption className="text-sm text-muted">After</figcaption></figure>
            </div>
          </li>
        )}

        {f.regression && (
          <li className={step}>
            <h2 className="text-lg font-semibold">6. Regression check</h2>
            <p className="mt-1">{f.regression.regressions} new findings introduced on the page ({f.regression.rules_checked} rules re-checked). Result: <strong>{f.regression.verdict}</strong>.</p>
          </li>
        )}
      </ol>

      {f.fix && (
        <section aria-labelledby="dec-h" className="mt-6">
          <h2 id="dec-h" className="text-lg font-semibold">Your decision</h2>
          <div className="mt-2 flex gap-3">
            <button type="button" onClick={() => decide("accept_pr")} className="rounded bg-fg px-4 py-2 font-semibold text-bg">Accept this change</button>
            <button type="button" onClick={() => decide("reject_pr")} className="rounded border border-line px-4 py-2 font-semibold">Reject this change</button>
          </div>
          <p role="status" className="mt-2">{saved}</p>
        </section>
      )}
    </>
  );
}

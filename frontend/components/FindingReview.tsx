"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, type Dashboard, type EvidenceOut, type FindingDetail } from "@/lib/api";
import { useCrumbs } from "@/components/shell/context";
import { btn, ErrorNote, Loading, SeverityMark, StatusPill } from "@/components/ui";
import { countFor, FILTERS, flatten, kindOf, matches, parseFilter } from "@/lib/findings";

function Img({ e, alt }: { e?: EvidenceOut; alt: string }) {
  if (!e?.url) {
    return <div className="flex aspect-[16/10] items-center justify-center rounded-md border border-line bg-subtle text-xs text-muted">No screenshot captured</div>;
  }
  // Evidence images are served by the API at runtime, so next/image optimisation does not apply.
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={api.url(e.url)} alt={alt} className="aspect-[16/10] w-full rounded-md border border-line bg-white object-contain" />;
}

type DiffLine = { n: number | null; mark: "+" | "-" | " "; text: string };
function parseDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldN = 0, newN = 0, inHunk = false;
  for (const raw of diff.split("\n")) {
    const h = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (h) { oldN = Number(h[1]); newN = Number(h[2]); inHunk = true; continue; }
    if (!inHunk || raw.startsWith("\\")) continue;
    if (raw.startsWith("+")) out.push({ n: newN++, mark: "+", text: raw.slice(1) });
    else if (raw.startsWith("-")) out.push({ n: oldN++, mark: "-", text: raw.slice(1) });
    else if (raw.length > 0 || out.length) { out.push({ n: newN, mark: " ", text: raw.slice(1) }); oldN++; newN++; }
  }
  while (out.length && out[out.length - 1].mark === " " && out[out.length - 1].text === "") out.pop();
  return out;
}
function diffFile(diff: string): string {
  const m = diff.match(/^\+\+\+ b\/(.+)$/m);
  return m ? m[1] : "";
}

function Step({ n, title, last, tone = "ink", children }: { n: number; title: string; last?: boolean; tone?: "ink" | "ok" | "bad" | "warn"; children: React.ReactNode }) {
  const bg = { ink: "bg-ink", ok: "bg-ok", bad: "bg-bad", warn: "bg-warn" }[tone];
  return (
    <li className="grid grid-cols-[28px_1fr] gap-4">
      <div className="flex flex-col items-center">
        <span aria-hidden="true" className={`flex size-6 flex-none items-center justify-center rounded-full font-mono text-[11px] text-white ${bg}`}>{n}</span>
        {!last && <span aria-hidden="true" className="w-px flex-1 bg-field" />}
      </div>
      <div className="min-w-0 pb-[26px]">
        <h2 className="m-0 mb-2.5 text-[15px] font-semibold"><span className="sr-only">Step {n}: </span>{title}</h2>
        {children}
      </div>
    </li>
  );
}

function Tile({ ok, title, detail }: { ok: boolean; title: string; detail: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-md border border-line bg-surface p-3">
      <span aria-hidden="true" className={`font-semibold ${ok ? "text-ok" : "text-bad"}`}>{ok ? "✓" : "✕"}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-medium"><span className="sr-only">{ok ? "Passed: " : "Failed: "}</span>{title}</div>
        <div className="font-mono text-xs text-muted">{detail}</div>
      </div>
    </div>
  );
}

export function FindingReview({ runId, fid }: { runId: string; fid: string }) {
  const router = useRouter();
  const search = useSearchParams();
  const filter = parseFilter(search.get("filter"));
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [f, setF] = useState<FindingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    api.dashboard(runId).then((d) => live && setDash(d)).catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [runId]);
  useEffect(() => {
    let live = true;
    api.finding(fid).then((d) => { if (live) { setF(d); setError(null); } }).catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [fid]);

  const all = useMemo(() => (dash ? flatten(dash) : []), [dash]);
  const list = useMemo(() => all.filter((x) => matches(x, filter)), [all, filter]);
  const idx = list.findIndex((x) => x.id === fid);
  const filterLabel = FILTERS.find((x) => x.key === filter)?.label ?? "All";

  useCrumbs([{ label: "Runs", href: "/" }, { label: runId.slice(0, 8), href: `/runs/${runId}`, mono: true },
    ...(f ? [{ label: f.rule_id, mono: true }] : [])]);

  const decision = f ? (decisions[f.id] ?? f.decision) : null;
  const href = (id: string) => `/runs/${runId}/findings/${id}${filter === "all" ? "" : `?filter=${filter}`}`;

  async function decide(action: "accept_pr" | "reject_pr") {
    if (!f?.fix || saving) return;
    const id = f.id;
    const before = decisions[id];
    setDecisions((d) => ({ ...d, [id]: action })); // optimistic
    setSaving(true);
    try {
      await api.decide(id, action);
    } catch (e) {
      setDecisions((d) => { const n = { ...d }; if (before) n[id] = before; else delete n[id]; return n; });
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey || (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) || t?.isContentEditable) return;
      const k = e.key.toLowerCase();
      if ((k === "j" || k === "k") && list.length) {
        const next = list[Math.min(list.length - 1, Math.max(0, idx + (k === "j" ? 1 : -1)))];
        if (next && next.id !== fid) router.push(href(next.id));
      } else if (k === "a" || k === "r") {
        void decide(k === "a" ? "accept_pr" : "reject_pr");
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const ev = (kind: string) => f?.evidence.find((e) => e.kind === kind);
  const snippet = (ev("dom_snippet")?.payload as { html?: string } | undefined)?.html;
  const lines = f?.fix ? parseDiff(f.fix.diff) : [];

  return (
    <div className="grid min-h-[calc(100vh-52px)] grid-cols-1 md:grid-cols-[minmax(200px,260px)_minmax(0,1fr)]">
      <title>{f ? `${f.rule_id} · Attest` : "Finding · Attest"}</title>
      <nav aria-label="Findings" className="border-b border-line bg-surface md:sticky md:top-[52px] md:h-[calc(100vh-52px)] md:overflow-y-auto md:border-b-0 md:border-r">
        <div className="flex items-baseline justify-between gap-2 border-b border-divider px-4 py-3">
          <h2 className="m-0 text-[12.5px] font-semibold">{filter === "all" ? "All findings" : filterLabel} · {list.length}</h2>
          <span className="hidden font-mono text-[11px] text-muted md:inline">J / K to move</span>
        </div>
        <ul className="m-0 max-h-60 list-none overflow-y-auto p-0 md:max-h-none md:overflow-visible">
          {list.map((x) => {
            const on = x.id === fid;
            return (
              <li key={x.id}>
                <Link href={href(x.id)} aria-current={on ? "true" : undefined}
                  className={`flex flex-col gap-1 border-l-[3px] py-[11px] pl-[13px] pr-4 no-underline hover:bg-page ${on ? "border-accent bg-selected" : "border-transparent"}`}>
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="font-mono text-[12.5px] font-medium text-ink">{x.rule_id}</span>
                    <span className="truncate font-mono text-[11.5px] text-muted">{x.page}</span>
                  </span>
                  <span className="flex items-center gap-2 text-[11.5px]">
                    <SeverityMark severity={x.severity} />
                    <span className="text-muted">{["proved", "regress_ok"].includes(x.status) ? "Proved" : kindOf(x.status) === "needs_human" ? "Needs a person" : ""}</span>
                  </span>
                </Link>
              </li>
            );
          })}
          {dash && list.length === 0 && <li className="px-4 py-6 text-muted">No findings match this filter.</li>}
        </ul>
        <p className="m-0 border-t border-divider px-4 py-2 text-xs text-muted">
          {FILTERS.map((x) => `${x.label} ${countFor(all, x.key)}`).join(" · ")}
        </p>
      </nav>

      <div className="min-w-0">
        <div className="max-w-[920px] px-4 pb-[120px] pt-6 sm:px-[22px]">
          {error && <ErrorNote message={error} />}
          {!f && !error && <Loading what="finding" />}
          {f && (
            <>
              <div className="mb-6 flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px]">
                  <SeverityMark severity={f.severity} />
                  <span aria-hidden="true" className="text-faint-3">·</span>
                  <span className="font-mono">WCAG {f.sc_ids.map((s) => (f.sc_titles?.[s] ? `${s} ${f.sc_titles[s]}` : s)).join(", ") || "n/a"}</span>
                  <StatusPill status={f.status} long />
                </div>
                <h1 className="m-0 font-mono text-[23px] font-medium">
                  {f.rule_id} <span className="whitespace-nowrap font-sans text-lg font-normal text-muted">on {f.page}</span>
                </h1>
                {(f.selector || f.source) && (
                  <p className="m-0 font-mono text-[12.5px] text-muted">{[f.selector, f.source].filter(Boolean).join(" · ")}</p>
                )}
                {f.instances > 1 && <p className="m-0 text-xs text-muted">{f.instances} identical elements on this page share this finding.</p>}
              </div>

              <ol aria-label="Evidence timeline" className="m-0 list-none p-0">
                <Step n={1} title="Caught">
                  <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3.5">
                    <figure className="m-0"><Img e={ev("shot_clip")} alt="The affected element on the real page, boxed" /><figcaption className="mt-1 text-xs text-muted">The element, as caught</figcaption></figure>
                    <figure className="m-0"><Img e={ev("shot_full_annotated")} alt="The full page with the affected element outlined" /><figcaption className="mt-1 text-xs text-muted">In context on the page</figcaption></figure>
                  </div>
                  {snippet && (
                    <pre tabIndex={0} aria-label="HTML around the element" className="mb-0 mt-3 overflow-auto rounded-md border border-line bg-surface p-3 font-mono text-[12.5px] leading-[1.6]">{snippet}</pre>
                  )}
                </Step>

                <Step n={2} title="What a user experiences">
                  <p className="m-0 max-w-[640px] text-[15px] leading-[1.55]">{f.impact_text ?? "A person needs to assess how this affects users."}</p>
                </Step>

                {f.triage && (
                  <Step n={3} title="Plan">
                    <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[13px]">
                      <dt className="text-muted">Route</dt>
                      <dd className="m-0">{f.triage.tier === "auto_fix" ? "Auto-fix, as a pull request a person reviews" : "Needs a person: work order"}</dd>
                      <dt className="text-muted">Reason</dt>
                      <dd className="m-0 font-mono text-xs">{f.triage.reason}</dd>
                      <dt className="text-muted">{f.triage.tier === "auto_fix" ? "Intended fix" : "Suggested next step"}</dt>
                      <dd className="m-0">{f.triage.fix_plan || "Assess the element and decide what the user should hear."}</dd>
                      {f.triage.gate_notes.length > 0 && (<><dt className="text-muted">Checks</dt><dd className="m-0 text-muted">{f.triage.gate_notes.join("; ")}</dd></>)}
                    </dl>
                  </Step>
                )}

                {f.fix && (
                  <Step n={4} title="The change">
                    <p className="mb-2 mt-0 max-w-[640px]">{f.fix.explanation}</p>
                    <div className="overflow-hidden rounded-md border border-line bg-surface">
                      <div className="flex flex-wrap justify-between gap-2 border-b border-line bg-subtle px-3 py-2 font-mono text-xs">
                        <span>{diffFile(f.fix.diff)}</span>
                        <span className="text-muted">{f.fix.branch} · build passed{f.fix.pr_url ? <> · <a href={f.fix.pr_url}>pull request</a></> : null}</span>
                      </div>
                      <div tabIndex={0} role="region" aria-label="Code change" className="overflow-x-auto py-1.5">
                        {lines.map((l, i) => (
                          <div key={i} className={`grid min-w-max grid-cols-[40px_18px_1fr] font-mono text-[12.5px] leading-[1.7] whitespace-pre ${l.mark === "+" ? "bg-add-bg text-ok" : l.mark === "-" ? "bg-del-bg text-bad" : ""}`}>
                            <span aria-hidden="true" className="select-none pr-2 text-right text-muted">{l.n}</span>
                            <span aria-hidden="true">{l.mark === " " ? "" : l.mark}</span>
                            <span><span className="sr-only">{l.mark === "+" ? "Added: " : l.mark === "-" ? "Removed: " : ""}</span>{l.text}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </Step>
                )}

                {f.proof && (
                  <Step n={5} title="Proof on the patched build" tone={f.proof.verdict === "pass" ? "ok" : f.proof.verdict === "fail" ? "bad" : "warn"}>
                    <p className="mb-3 mt-0">
                      <strong className={f.proof.verdict === "pass" ? "text-ok" : f.proof.verdict === "fail" ? "text-bad" : "text-warn"}>
                        {f.proof.verdict === "pass" ? "Passed." : f.proof.verdict === "fail" ? "Still fails." : "Inconclusive."}
                      </strong> {f.proof.notes}
                    </p>
                    <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-2.5">
                      <Tile ok={f.proof.baseline_failed} title="Unpatched control still fails" detail={`baseline_failed: ${f.proof.baseline_failed ? "yes" : "no"}`} />
                      <Tile ok={f.proof.relocated && f.proof.relocation_score >= 0.8} title="Same element re-located" detail={`similarity ${f.proof.relocation_score.toFixed(2)}`} />
                      <Tile ok={f.proof.verdict === "pass"} title="Rule passes every run" detail={f.proof.verdict === "pass" ? `${f.proof.runs} of ${f.proof.runs} runs` : `not in all ${f.proof.runs} runs`} />
                    </div>
                    <div className="mt-3.5 grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3.5">
                      <figure className="m-0"><Img e={ev("reprove_shot_before")} alt="Before the fix: the same element on the unpatched build" /><figcaption className="mt-1 text-xs text-muted">Before (unpatched)</figcaption></figure>
                      <figure className="m-0"><Img e={ev("reprove_shot_after")} alt="After the fix: the same element on the patched build" /><figcaption className="mt-1 text-xs text-muted">After (patched)</figcaption></figure>
                    </div>
                  </Step>
                )}

                {f.regression && f.regression.regressions === 0 && (
                  <Step n={6} title="Regression check" last>
                    <p className="m-0">No new findings on the page. {f.regression.rules_checked} rules were re-checked on the patched build.</p>
                  </Step>
                )}
                {f.regression && f.regression.regressions > 0 && (
                  <Step n={6} title="Regression check" last tone="bad">
                    <p className="m-0">{f.regression.regressions} new finding{f.regression.regressions === 1 ? "" : "s"} appeared on the page after this change. Do not merge without reviewing them.</p>
                  </Step>
                )}
              </ol>
            </>
          )}
        </div>

        {f?.fix && (
          <div data-no-print className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-line bg-surface px-4 py-3 sm:px-7">
            <p role="status" className={`m-0 text-[13px] ${decision === "accept_pr" ? "text-ok" : decision === "reject_pr" ? "text-bad" : "text-muted"}`}>
              {decision === "accept_pr" ? "Recorded: you accepted this change."
                : decision === "reject_pr" ? "Recorded: you rejected this change."
                : <>Branch <span className="font-mono">{f.fix.branch}</span> · awaiting your review</>}
            </p>
            <div className="flex gap-2.5">
              <button type="button" onClick={() => decide("reject_pr")} disabled={saving} className={btn.secondary}>
                Reject <kbd className="rounded border border-field px-1 font-mono text-[11px] text-muted">R</kbd>
              </button>
              <button type="button" onClick={() => decide("accept_pr")} disabled={saving} className={btn.accent}>
                Accept change <kbd className="rounded border border-[#5c8d90] px-1 font-mono text-[11px] text-[#d7e6e6]">A</kbd>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

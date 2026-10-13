"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Dashboard } from "@/lib/api";
import { useApp, useCrumbs } from "@/components/shell/context";
import { btn, Card, Empty, ErrorNote, Loading, Page, PageHeader } from "@/components/ui";
import { shortId, when } from "@/lib/format";

function ReportDocument({ d }: { d: Dashboard }) {
  const name = d.meta.project ?? d.run.target;
  const groups = [...d.groups].sort((a, b) => a.sc.localeCompare(b.sc, undefined, { numeric: true }));
  const stats = [
    { label: "Score", value: `${d.score_before?.score ?? "—"} → ${d.score_after?.score ?? "—"}`, cls: "" },
    { label: "Fixed and proved", value: d.counts.fixed, cls: "text-ok" },
    { label: "To a person", value: d.counts.needs_human, cls: "" },
    { label: "Regressions", value: d.counts.regressions, cls: "" },
  ];
  return (
    <div className="flex flex-col gap-6 px-5 pb-9 pt-8 sm:px-9">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.06em] text-accent">Accessibility evidence report</span>
          <h2 className="m-0 text-[26px] font-semibold">{name}</h2>
          <span className="font-mono text-[12.5px] text-muted">
            {shortId(d.run.id)} · {when(d.meta.started_at)}{d.meta.base_branch ? ` · ${d.meta.base_branch}` : ""} · {d.meta.page_count} {d.meta.page_count === 1 ? "page" : "pages"}
          </span>
        </div>
        <Image src="/attest-mark.png" alt="" width={29} height={34} unoptimized className="h-[34px] w-auto flex-none" />
      </div>

      <dl className="m-0 grid grid-cols-2 border-y border-line sm:grid-cols-4" style={{ borderTopColor: "var(--color-ink)" }}>
        {stats.map((s) => (
          <div key={s.label} className="py-3.5 pr-3">
            <dt className="text-xs text-muted">{s.label}</dt>
            <dd className={`tabular m-0 text-2xl font-medium ${s.cls}`}>{s.value}</dd>
          </div>
        ))}
      </dl>

      <section aria-labelledby="by-sc-h">
        <h3 id="by-sc-h" className="m-0 mb-2 text-sm font-semibold">By WCAG success criterion</h3>
        <div role="table" aria-label="Findings by success criterion">
          <div role="row" className="grid grid-cols-[70px_1fr_60px_60px_80px] border-b border-ink pb-1.5 text-[11.5px] text-muted">
            {["SC", "Criterion", "Issues", "Fixed", "To a person"].map((h) => <div key={h} role="columnheader">{h}</div>)}
          </div>
          {groups.map((g) => (
            <div key={g.sc} role="row" className="grid grid-cols-[70px_1fr_60px_60px_80px] border-b border-divider py-2 text-[13px]">
              <div role="cell" className="font-mono">{g.sc}</div>
              <div role="cell">{g.title}</div>
              <div role="cell" className="tabular font-mono">{g.total}</div>
              <div role="cell" className="tabular font-mono text-ok">{g.fixed}</div>
              <div role="cell" className="tabular font-mono">{g.needs_human}</div>
            </div>
          ))}
          {groups.length === 0 && <p className="m-0 py-3 text-muted">No issues were found.</p>}
        </div>
      </section>

      <section aria-labelledby="contents-h">
        <h3 id="contents-h" className="m-0 mb-2 text-sm font-semibold">Contents</h3>
        <ol className="m-0 flex list-decimal flex-col gap-1 pl-5 text-[13px] text-body">
          <li>Summary and page scores</li>
          <li>Fixes, each with its diff and before/after proof</li>
          <li>Work orders for issues that need a person</li>
          <li>Method: tools, rules, probes and run settings</li>
        </ol>
        <p className="mb-0 mt-3 text-[13px]">
          <a href={api.url(`/api/runs/${d.run.id}/report`)} target="_blank" rel="noreferrer">Open the full printable report with every diff and screenshot</a>
        </p>
      </section>

      <p className="m-0 border-t border-line pt-3 text-xs text-muted">
        Automated first pass produced by Attest. Every change is a pull request for a person to review before merging. This report is not a certificate of WCAG conformance.
      </p>
    </div>
  );
}

export function ReportsView({ runId }: { runId?: string }) {
  const { runs, loadError } = useApp();
  const done = (runs ?? []).filter((r) => r.status === "complete");
  const selected = done.find((r) => r.id === runId) ?? (runId ? undefined : done[0]);
  const id = selected?.id ?? runId;
  const [d, setD] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useCrumbs(id ? [{ label: "Reports", href: "/reports" }, { label: shortId(id), mono: true }] : [{ label: "Reports" }]);

  useEffect(() => {
    if (!id) return;
    let live = true;
    api.dashboard(id).then((x) => { if (live) { setD(x); setError(null); } }).catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [id]);

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/reports/${id}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy the link. Copy it from the address bar instead.");
    }
  }

  const shown = d && d.run.id === id ? d : null;
  return (
    <Page>
      <title>Reports · Attest</title>
      <div data-no-print>
        <PageHeader title="Reports">
          Every completed run produces an evidence report you can print or save as PDF and share with your team, auditors or customers.
        </PageHeader>
      </div>
      {(error || loadError) && <ErrorNote message={error ?? loadError ?? ""} />}
      {runs === null && !loadError && <Loading what="reports" />}
      {runs && done.length === 0 && <Empty>No completed runs yet. A report appears here when a run finishes.</Empty>}

      {done.length > 0 && (
        <div className="flex flex-wrap gap-5">
          <Card label="Completed runs" className="max-w-full flex-[1_1_300px] self-start sm:max-w-[380px]" >
            <div data-no-print>
              <h2 className="m-0 border-b border-divider px-4 py-3 text-[12.5px] font-semibold">Completed runs · {done.length}</h2>
              <ul className="m-0 list-none p-0">
                {done.map((r) => {
                  const on = r.id === id;
                  return (
                    <li key={r.id}>
                      <Link href={`/reports/${r.id}`} aria-current={on ? "true" : undefined}
                        className={`flex flex-col gap-0.5 border-l-[3px] py-[11px] pl-[13px] pr-4 no-underline hover:bg-page ${on ? "border-accent bg-selected" : "border-transparent"}`}>
                        <span className="flex items-baseline justify-between gap-2 text-ink">
                          <span className="font-medium">{r.project ?? r.target}</span>
                          <span className="tabular font-mono text-xs">{r.score_before ?? "—"} → {r.score_after ?? "—"}</span>
                        </span>
                        <span className="truncate font-mono text-[11.5px] text-muted">{shortId(r.id)} · {when(r.created_at)}{r.base_branch ? ` · ${r.base_branch}` : ""}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          </Card>

          <article aria-label="Report preview" className="min-w-0 flex-[1_1_520px] rounded-lg border border-line bg-surface">
            <div data-no-print className="flex items-center justify-between gap-3 border-b border-line px-[18px] py-2.5">
              <span className="text-[13px] font-semibold">Preview</span>
              <div className="flex gap-2">
                <button type="button" className={btn.secondary} onClick={copyLink} disabled={!id}>{copied ? "Link copied" : "Copy link"}</button>
                <button type="button" className={btn.primary} onClick={() => window.print()} disabled={!shown}>Save as PDF</button>
              </div>
            </div>
            {shown ? <ReportDocument d={shown} /> : !error && id ? <p role="status" className="m-0 px-6 py-8 text-muted">Loading report…</p> : null}
            {id && !selected && runs && <p className="m-0 px-6 py-8 text-muted">This run has no finished report.</p>}
          </article>
        </div>
      )}
    </Page>
  );
}

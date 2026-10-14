"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, type PullRequestRow, type PullRequestsOut } from "@/lib/api";
import { useApp, useCrumbs } from "@/components/shell/context";
import { btn, Card, Empty, ErrorNote, Loading, Page, PageHeader, Segmented, StatusPill } from "@/components/ui";
import { shortId } from "@/lib/format";

const COLS = "grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_130px_150px_150px_12px]";
type Filter = "awaiting" | "accepted" | "rejected" | "held_back" | "all";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "awaiting", label: "Awaiting review" }, { key: "accepted", label: "Accepted" }, { key: "rejected", label: "Rejected" },
  { key: "held_back", label: "Held back" }, { key: "all", label: "All" },
];
const DECISION: Record<string, { label: string; cls: string }> = {
  awaiting: { label: "Awaiting review", cls: "text-body" }, accepted: { label: "Accepted", cls: "text-ok" },
  rejected: { label: "Rejected", cls: "text-bad" }, held_back: { label: "Held back", cls: "text-warn" },
};
const PROOF_STATUS: Record<string, string> = { proved: "regress_ok", not_retested: "inconclusive", fix_failed: "prove_failed", regression: "regress_fail" };

function repoOf(rows: PullRequestRow[]): string | null {
  const m = rows.find((r) => r.pr_url)?.pr_url?.match(/^https?:\/\/[^/]+\/([^/]+\/[^/]+)\/pull\//);
  return m ? m[1] : null;
}

export default function PullRequestsView() {
  const { runs, loadError, reload } = useApp();
  const search = useSearchParams();
  const wanted = search.get("run");
  const run = runs?.find((r) => r.id === wanted) ?? runs?.find((r) => (r.fixed ?? 0) > 0) ?? runs?.[0];
  const [data, setData] = useState<PullRequestsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [confirming, setConfirming] = useState(false);
  const [publishing, setPublishing] = useState(false);
  useCrumbs([{ label: "Pull requests" }]);

  const runId = run?.id;
  const load = useCallback(() => {
    if (!runId) return;
    api.pullRequests(runId).then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message));
  }, [runId]);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);

  async function publish() {
    if (!runId) return;
    setPublishing(true);
    setError(null);
    try {
      await api.publish(runId);
      setConfirming(false);
      load();
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPublishing(false);
    }
  }

  const rows = (data?.rows ?? []).filter((r) => filter === "all" || r.decision === filter);
  const n = data?.publishable ?? 0;
  const done = (data?.published ?? 0) > 0 && n === 0;
  const canPublish = !!data?.can_publish && n > 0;
  const prCount = new Set((data?.rows ?? []).map((r) => r.pr_url).filter(Boolean)).size;
  const repo = repoOf(data?.rows ?? []);

  let notice = "Publishing is off for this run. Accept proved fixes in Findings, then publish them here.";
  if (data && !data.can_publish && n === 0 && !done) notice = "This project is not set up to publish. Fixes stay on the server for you to review.";
  if (data && !data.can_publish && n > 0) notice = `${n} accepted. This project is not set up to publish, so these stay on the server.`;
  if (data?.can_publish && n > 0) notice = `${n} accepted. Publishing pushes those branches and opens pull requests. Nothing has left the server yet.`;
  if (done) notice = `${prCount} pull request${prCount === 1 ? "" : "s"} opened${repo ? ` on ${repo}` : ""}.`;

  return (
    <Page>
      <title>Pull requests · Attest</title>
      <PageHeader title="Pull requests"
        actions={
          <button type="button" className={btn.primary} disabled={!canPublish || publishing} onClick={() => setConfirming(true)}>
            {done ? "Published" : `Publish ${n} accepted`}
          </button>
        }>
        Each automatic fix lives on its own branch. Only fixes that were proved and that you accepted can be published to the remote.
      </PageHeader>

      {(error || loadError) && <ErrorNote message={error ?? loadError ?? ""} />}
      {runs === null && !loadError && <Loading what="runs" />}
      {runs && !run && <Empty>No runs yet.</Empty>}

      {confirming && (
        <div role="alertdialog" aria-labelledby="confirm-h" aria-describedby="confirm-d" className="rounded-md border border-warn bg-warn-bg p-4">
          <h2 id="confirm-h" className="m-0 text-sm font-semibold">Publish {n} accepted {n === 1 ? "fix" : "fixes"}?</h2>
          <p id="confirm-d" className="mb-3 mt-1 text-[13px]">
            This pushes new branches to the project&apos;s remote and opens pull requests. Only the fixes you accepted are included. A person still has to review and merge them.
          </p>
          <div className="flex gap-2.5">
            <button type="button" className={btn.primary} onClick={publish} disabled={publishing}>{publishing ? "Publishing…" : "Push and open pull requests"}</button>
            <button type="button" className={btn.secondary} onClick={() => setConfirming(false)} disabled={publishing}>Cancel</button>
          </div>
        </div>
      )}

      {run && (
        <>
          <div role="status" className="flex items-center gap-2.5 rounded-md border border-line bg-surface px-3.5 py-2.5">
            <span aria-hidden="true" className={`size-[7px] flex-none rounded-full ${done ? "bg-accent" : "bg-faint-3"}`} />
            <span className="text-[13px]">{notice}</span>
          </div>

          <Card label="Pull requests" className="overflow-x-auto">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-[18px] py-3">
              <h2 className="m-0 text-[13px] font-semibold">
                <span className="font-mono">{shortId(run.id)}</span> · {run.project ?? run.target}
              </h2>
              <Segmented label="Filter pull requests" value={filter} onChange={setFilter}
                options={FILTERS.map((f) => ({ ...f, count: (data?.rows ?? []).filter((r) => f.key === "all" || r.decision === f.key).length }))} />
            </div>
            {!data && !error && <p role="status" className="m-0 px-5 py-6 text-muted">Loading…</p>}
            {data && (
              <div role="table" aria-label="Pull requests" className="min-w-[860px]">
                <div role="row" className={`grid ${COLS} gap-4 border-b border-line bg-subtle px-[18px] py-2.5 text-[11.5px] font-medium uppercase tracking-[0.04em] text-muted`}>
                  {["Branch", "Change", "Proof", "Your decision", "Remote", ""].map((h, i) => (
                    <div key={i} role="columnheader">{h || <span className="sr-only">Open</span>}</div>
                  ))}
                </div>
                {rows.length === 0 && <Empty>Nothing here yet.</Empty>}
                {rows.map((r) => {
                  const d = DECISION[r.decision];
                  return (
                    <div key={r.finding_id} role="row" className={`relative grid ${COLS} items-center gap-4 border-b border-divider px-[18px] py-3 last:border-b-0 hover:bg-subtle`}>
                      <div role="cell" className="min-w-0">
                        <Link href={`/runs/${run.id}/findings/${r.finding_id}`} className="block truncate font-mono text-[12.5px] font-medium text-ink no-underline after:absolute after:inset-0 after:content-['']">{r.branch}</Link>
                        <span className="font-mono text-[11.5px] text-muted">{r.file ?? "—"} · +{r.added} −{r.removed}</span>
                      </div>
                      <div role="cell" className="min-w-0">
                        <span className="font-mono text-[13px] font-medium">{r.rule_id}</span>
                        <div className="truncate text-xs text-muted">WCAG {r.sc_ids.join(", ") || "n/a"} · <span className="font-mono">{r.page}</span></div>
                      </div>
                      <div role="cell"><StatusPill status={PROOF_STATUS[r.proof]} /></div>
                      <div role="cell" className={`text-[13px] font-medium ${d.cls}`}>{d.label}</div>
                      <div role="cell" className={`text-[13px] ${["open", "merged"].includes(r.remote) ? "font-medium text-accent" : "text-muted"}`}>
                        {["open", "merged", "closed"].includes(r.remote) ? `PR${r.pr_number ? ` #${r.pr_number}` : ""} · ${r.remote}` : r.remote === "server_only" ? "Server only" : "Not publishable"}
                      </div>
                      <div role="cell" aria-hidden="true" className="text-faint-2">›</div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </>
      )}
    </Page>
  );
}

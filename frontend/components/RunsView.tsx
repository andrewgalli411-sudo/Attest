"use client";

import Link from "next/link";
import { useApp, useCrumbs } from "@/components/shell/context";
import { btn, Card, Empty, ErrorNote, Loading, Page, PageHeader, RunStatusDot } from "@/components/ui";
import { shortId, when } from "@/lib/format";

const COLS = "grid-cols-[110px_minmax(0,1.4fr)_140px_minmax(0,1fr)_120px_90px_110px]";

export function RunsView() {
  const { runs, loadError, openNewRun, project } = useApp();
  useCrumbs([{ label: "Runs" }]);

  return (
    <Page>
      <title>Runs · Attest</title>
      <PageHeader
        title="Runs"
        actions={<button type="button" onClick={openNewRun} className={btn.primary}>New run</button>}
      >
        A run drives the app like a real user, finds issues, writes safe fixes on a branch, then re-tests the patched build to prove each one.
      </PageHeader>

      {loadError && <ErrorNote message={loadError} />}
      {runs === null && !loadError && <Loading what="runs" />}

      {runs && (
        <Card label="Runs" className="overflow-x-auto">
          <div role="table" aria-label={project ? `Runs for ${project}` : "Runs"} className="min-w-[880px]">
            <div role="row" className={`grid ${COLS} gap-4 border-b border-line bg-subtle px-[18px] py-2.5 text-[11.5px] font-medium uppercase tracking-[0.04em] text-muted`}>
              {["Run", "Project", "Status", "Result", "Score", "Proved", "To a person"].map((h) => (
                <div key={h} role="columnheader">{h}</div>
              ))}
            </div>
            {runs.length === 0 && <Empty>No runs yet. Start one with “New run”.</Empty>}
            {runs.map((r) => (
              <div key={r.id} role="row" className={`relative grid ${COLS} items-center gap-4 border-b border-divider px-[18px] py-[13px] last:border-b-0 hover:bg-subtle`}>
                <div role="cell" className="flex flex-col">
                  <Link href={`/runs/${r.id}`} className="font-mono text-[12.5px] font-medium text-ink no-underline after:absolute after:inset-0 after:content-['']">
                    {shortId(r.id)}
                  </Link>
                  <span className="text-xs text-muted">{when(r.created_at)}</span>
                </div>
                <div role="cell" className="flex min-w-0 flex-col">
                  <span className="truncate font-medium">{r.project ?? r.target}</span>
                  {r.base_branch && (
                    <span className="truncate font-mono text-[11.5px] text-muted">
                      {r.base_branch}{r.page_count ? ` · ${r.page_count} ${r.page_count === 1 ? "page" : "pages"}` : ""}
                    </span>
                  )}
                </div>
                <div role="cell"><RunStatusDot status={r.status} /></div>
                <div role="cell" className="truncate text-[13px] text-body">{r.result ?? "In progress…"}</div>
                <div role="cell" className="tabular font-mono text-[13px] font-medium">
                  {r.score_before != null && r.score_after != null ? (
                    <><span className="sr-only">Score </span>{r.score_before}<span aria-hidden="true"> → </span><span className="sr-only"> to </span>{r.score_after}</>
                  ) : "—"}
                </div>
                <div role="cell" className="tabular font-mono text-ok">{r.fixed ?? "—"}</div>
                <div role="cell" className="tabular font-mono">{r.needs_human ?? "—"}</div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </Page>
  );
}

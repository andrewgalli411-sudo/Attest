"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Dashboard } from "@/lib/api";
import { useRunEvents, type RunEvent } from "@/lib/useRunEvents";
import { ErrorNote, SeverityBadge, Stat, StatusBadge } from "@/components/ui";

const STAGE_TEXT: Record<string, string> = {
  detect: "Driving the app and finding issues",
  triage: "Deciding what can be fixed automatically",
  fix: "Writing and checking fixes",
  reprove: "Re-testing the patched build to prove each fix",
};

function progressText(events: RunEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "stage.started") return STAGE_TEXT[String(e.payload.stage)] ?? "Working";
    if (e.type === "run.summary") return "Run complete";
    if (e.type === "error") return "The run failed";
  }
  return "Starting";
}

export default function RunView({ id }: { id: string }) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [severity, setSeverity] = useState("all");
  const [filter, setFilter] = useState("all");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    api.dashboard(id).then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message));
  }, [id]);
  useEffect(() => { load(); }, [load]);

  // Refresh the dashboard when something changes, but not on every single event.
  const { events, state } = useRunEvents(id, () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(load, 400);
  });
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const running = data ? !["complete", "failed", "cancelled", "reviewed", "exported"].includes(data.run.status) : true;
  const failed = data?.run.status === "failed";
  const cancelled = data?.run.status === "cancelled";
  const [cancelling, setCancelling] = useState(false);
  async function cancel() {
    setCancelling(true);
    try { await api.cancel(id); load(); } catch (e) { setError((e as Error).message); } finally { setCancelling(false); }
  }
  const errEvent = events.find((e) => e.type === "error");
  const match = (f: { severity: string; status: string }) =>
    (severity === "all" || f.severity === severity) &&
    (filter === "all" || (filter === "fixed" ? ["proved", "regress_ok", "regress_fail"].includes(f.status) : f.status === filter));

  return (
    <>
      <p><Link href="/">&larr; All runs</Link></p>
      <h1 className="mt-2 text-2xl font-bold">Triage dashboard</h1>

      <p role="status" className="mt-2 text-muted">
        {running ? `${progressText(events)}... ` : ""}
        {state === "error" && running ? "(live updates reconnecting)" : ""}
      </p>

      {running && data && (
        <p className="mt-2"><button type="button" onClick={cancel} disabled={cancelling} className="rounded border border-line px-3 py-1.5 font-semibold">Cancel run</button></p>
      )}
      {cancelled && <p role="status" className="mt-2 font-semibold text-warn">Run cancelled. Nothing was pushed, and the repository was restored.</p>}
      {error && <ErrorNote message={error} />}
      {failed && <ErrorNote message={String(errEvent?.payload.error ?? "The run failed. See the event log for details.")} />}

      {data && (
        <>
          <p className="mt-2 text-xl font-bold">{data.headline}</p>
          <p className="text-sm text-muted">
            Changes were produced by: {data.model_kind ?? "unknown"}. {data.notice}
          </p>

          <section aria-labelledby="sum-h" className="mt-4">
            <h2 id="sum-h" className="sr-only">Summary</h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <Stat value={data.counts.total} label="issues found" />
              <Stat value={data.counts.fixed} label="fixed in pull requests and proved" />
              <Stat value={data.counts.needs_human} label="need a person" />
              <Stat value={data.counts.regressions} label="regressions introduced" />
              <Stat
                value={<span>{data.score_before?.score ?? "-"}<span aria-hidden="true"> → </span><span className="sr-only"> to </span>{data.score_after?.score ?? "-"}</span>}
                label="page score (0 to 100)" />
            </div>
          </section>

          <section aria-labelledby="pages-h" className="mt-6">
            <h2 id="pages-h" className="text-xl font-semibold">Pages</h2>
            <ul className="mt-2 grid gap-1">
              {data.pages.map((p) => (
                <li key={p.id}><span className="font-mono">{p.label}</span>: score {p.score_before ?? "-"} to {p.score_after ?? "-"}</li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="groups-h" className="mt-6">
            <h2 id="groups-h" className="text-xl font-semibold">By WCAG success criterion</h2>
            <div className="mt-2 flex flex-wrap gap-4">
              <label>Severity{" "}
                <select className="rounded border border-line bg-bg px-2 py-1" value={severity} onChange={(e) => setSeverity(e.target.value)}>
                  <option value="all">All</option>
                  {["critical", "serious", "moderate", "minor"].map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <label>Status{" "}
                <select className="rounded border border-line bg-bg px-2 py-1" value={filter} onChange={(e) => setFilter(e.target.value)}>
                  <option value="all">All</option>
                  <option value="fixed">Fixed and proved</option>
                  <option value="needs_human">Needs a person</option>
                  <option value="inconclusive">Could not be re-tested</option>
                </select>
              </label>
            </div>

            {data.groups.length === 0 && !running && <p className="mt-3">No issues were found.</p>}
            {data.groups.map((g) => {
              const rows = g.findings.filter(match);
              if (rows.length === 0) return null;
              return (
                <details key={g.sc} open className="mt-3 rounded-lg border border-line bg-card">
                  <summary className="cursor-pointer p-3 font-semibold">
                    WCAG {g.sc} {g.title} - {g.total} issue{g.total === 1 ? "" : "s"}, {g.fixed} fixed, {g.needs_human} need a person
                  </summary>
                  <ul className="grid gap-2 p-3 pt-0">
                    {rows.map((f) => (
                      <li key={f.id} className="rounded border border-line bg-bg p-2">
                        <Link href={`/findings/${f.id}`} className="font-mono font-semibold">{f.rule_id}</Link>
                        <span> on {f.page} - </span><SeverityBadge severity={f.severity} /><span> - </span><StatusBadge status={f.status} />
                        {f.impact_text && <p className="text-sm text-muted">{f.impact_text}</p>}
                      </li>
                    ))}
                  </ul>
                </details>
              );
            })}
          </section>

          <p className="mt-6"><a href={api.url(`/api/runs/${id}/report`)}>Open the printable evidence report</a></p>
        </>
      )}
      {!data && !error && <p role="status">Loading...</p>}
    </>
  );
}

"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Dashboard } from "@/lib/api";
import { useCrumbs } from "@/components/shell/context";
import { btn, Card, DraftChip, ErrorNote, Loading, Page, RunStatusChip, Segmented, SeverityMark, StatusPill } from "@/components/ui";
import { duration, plural, shortId, when } from "@/lib/format";
import { countFor, FILTERS, flatten, isFinished, kindOf, matches, SEVERITIES, type FilterKey } from "@/lib/findings";
import { useRunEvents } from "@/lib/useRunEvents";

const STAGE_TEXT: Record<string, string> = {
  detect: "Driving the app and finding issues…",
  triage: "Deciding what can be fixed automatically…",
  fix: "Writing and checking fixes…",
  prove: "Re-testing the patched build to prove each fix…",
  regress: "Checking for regressions…",
};

const scNum = (sc: string) => sc.split(".").map((n) => Number(n) || 0);
function scCompare(a: string, b: string) {
  if (a === "other") return 1;
  if (b === "other") return -1;
  const x = scNum(a), y = scNum(b);
  for (let i = 0; i < 3; i++) if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) - (y[i] ?? 0);
  return 0;
}

export default function RunView({ id }: { id: string }) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [cancelling, setCancelling] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    api.dashboard(id).then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message));
  }, [id]);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);

  // Refresh when something changes, but not on every single event.
  const { events, state } = useRunEvents(id, () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(load, 400);
  });
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const projectName = data?.meta.project ?? data?.run.target ?? "Run";
  useCrumbs([{ label: "Runs", href: "/" }, { label: shortId(id), mono: true }]);

  const flat = useMemo(() => (data ? flatten(data) : []), [data]);
  const running = data ? !isFinished(data.run.status) : true;
  const failed = data?.run.status === "failed";
  const cancelled = data?.run.status === "cancelled";
  const errEvent = events.find((e) => e.type === "error");
  const firstFixed = flat.find((f) => kindOf(f.status) === "proved");
  const provedCount = countFor(flat, "fixed");

  async function cancel() {
    setCancelling(true);
    try { await api.cancel(id); load(); } catch (e) { setError((e as Error).message); } finally { setCancelling(false); }
  }

  const current = data?.stages.find((s) => s.state === "current");
  const statusLine = !data ? "" : running ? (current ? STAGE_TEXT[current.key] : "Starting…")
    : failed ? "The run failed." : cancelled ? "Run cancelled." : `Run complete. Repository restored to ${data.meta.base_branch ?? "main"}.`;

  const unknown = (n: number) => (running && n === 0 ? "—" : n);

  return (
    <Page>
      <title>{`${projectName} · Attest`}</title>
      {error && <ErrorNote message={error} />}
      {!data && !error && <Loading what="run" />}

      {data && (
        <>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 flex-[1_1_420px] flex-col gap-1.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">{projectName}</h1>
                <span className="font-mono text-xs font-medium text-muted">{shortId(id)}</span>
                <RunStatusChip status={data.run.status} />
                <DraftChip />
              </div>
              <p className="m-0 text-muted">
                Started {when(data.meta.started_at).toLowerCase()}
                {data.meta.base_branch && ` · ${data.meta.base_branch}${data.meta.base_sha ? ` @ ${data.meta.base_sha}` : ""}`}
                {` · ${plural(data.meta.page_count, "page")}`}
                {data.meta.duration_s != null && ` · ${duration(data.meta.duration_s)}`}
                {` · publish ${data.meta.publish ? "on" : "off"}`}
                {data.model_kind && ` · changes written by: ${data.model_kind}`}
              </p>
            </div>
            <div className="flex flex-none items-center gap-2.5">
              {running ? (
                <button type="button" onClick={cancel} disabled={cancelling} className={btn.secondary}>Cancel run</button>
              ) : (
                <>
                  <Link href={`/reports/${id}`} className={`${btn.secondary} no-underline`}>Evidence report</Link>
                  {firstFixed && (
                    <Link href={`/runs/${id}/findings/${firstFixed.id}?filter=fixed`} className={`${btn.primary} text-white no-underline hover:text-white`}>
                      Review {plural(provedCount, "change")}
                    </Link>
                  )}
                </>
              )}
            </div>
          </div>

          {failed && <ErrorNote message={String(errEvent?.payload.error ?? data.run.result ?? "The run failed. See the event log for details.")} />}
          {cancelled && <p role="status" className="m-0 font-medium text-warn">Run cancelled. Nothing was pushed, and the repository was restored.</p>}
          {state === "error" && running && <p role="status" className="m-0 text-muted">Live updates are reconnecting…</p>}

          <Card label="Summary" className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
            <div className="col-span-2 border-r border-divider px-5 py-[18px]">
              <div className="text-xs text-muted">Page score, before → after</div>
              <div className="tabular mt-1 flex items-baseline gap-2.5 tracking-[-0.03em]">
                <span className="text-[40px] font-medium leading-none text-faint">{data.score_before?.score ?? "—"}</span>
                <span aria-hidden="true" className="text-xl text-faint-3">→</span>
                <span className="sr-only"> to </span>
                <span className="text-[40px] font-semibold leading-none">{data.score_after?.score ?? "—"}</span>
                {data.score_before && data.score_after && (
                  <span className="font-mono text-[13px] font-medium tracking-normal text-ok">
                    {data.score_after.score - data.score_before.score >= 0 ? "+" : ""}{data.score_after.score - data.score_before.score}
                  </span>
                )}
              </div>
              <div className="mt-1 text-xs text-muted">
                {data.score_before ? `Worst page ${data.score_before.worst} · ` : ""}severity-weighted, 0–100
              </div>
            </div>
            {[
              { label: "Issues found", value: data.counts.total, note: `${data.severity.critical ?? 0} critical · ${data.severity.serious ?? 0} serious`, cls: "" },
              { label: "Fixed and proved", value: unknown(data.counts.fixed), note: "as pull requests", cls: "text-ok" },
              { label: "Need a person", value: unknown(data.counts.needs_human), note: "work orders", cls: "" },
              { label: "Regressions", value: running && data.counts.fixed === 0 ? "—" : data.counts.regressions, note: `${data.stages.find((s) => s.key === "regress")?.detail.match(/(\d+) rules/)?.[1] ?? 0} rules re-checked`, cls: "" },
            ].map((c) => (
              <div key={c.label} className="border-r border-divider px-5 py-[18px] last:border-r-0">
                <div className="text-xs text-muted">{c.label}</div>
                <div className={`tabular mt-1 text-[30px] font-medium leading-tight ${c.cls}`}>{c.value}</div>
                <div className="text-xs text-muted">{c.note}</div>
              </div>
            ))}
          </Card>

          <Card label="Pipeline" className="px-5 py-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="m-0 text-[13px] font-semibold">Pipeline</h2>
              <span role="status" className="text-[12.5px] text-muted">{statusLine}</span>
            </div>
            <ol className="m-0 grid list-none grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3.5 p-0">
              {data.stages.map((s, i) => {
                const reached = s.state === "done" || s.state === "current";
                return (
                  <li key={s.key} aria-current={s.state === "current" ? "step" : undefined} className="flex flex-col gap-1">
                    <span aria-hidden="true" className={`h-1 rounded-sm ${s.state === "done" ? "bg-teal" : s.state === "current" ? "at-pulse bg-accent/55" : "bg-line"}`} />
                    <span className={`font-mono text-[11px] font-medium ${reached ? "text-accent" : "text-faint-2"}`}>{String(i + 1).padStart(2, "0")}</span>
                    <span className="text-[13px] font-medium">{s.title}<span className="sr-only">, {s.state === "done" ? "done" : s.state === "current" ? "in progress" : s.state === "stopped" ? "stopped" : "waiting"}</span></span>
                    <span className="text-xs text-muted">{s.detail || (s.state === "current" ? "Working…" : "")}</span>
                  </li>
                );
              })}
            </ol>
          </Card>

          <div className="flex flex-wrap gap-5">
            <Card label="Findings by success criterion" className="min-w-0 flex-[1_1_560px]">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-[18px] py-3">
                <h2 className="m-0 text-[13px] font-semibold">Findings by WCAG success criterion</h2>
                <Segmented label="Filter findings" value={filter} onChange={setFilter}
                  options={FILTERS.map((f) => ({ ...f, count: countFor(flat, f.key) }))} />
              </div>
              {data.groups.length === 0 && <p className="m-0 px-[18px] py-6 text-muted">{running ? "No findings yet." : "No issues were found."}</p>}
              {[...data.groups].sort((a, b) => scCompare(a.sc, b.sc)).map((g) => {
                const rows = g.findings.filter((f) => matches(f, filter));
                if (rows.length === 0) return null;
                return (
                  <div key={g.sc} role="group" aria-label={`WCAG ${g.sc} ${g.title}`} className="border-b border-divider last:border-b-0">
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 bg-subtle px-[18px] py-2">
                      <span className="font-mono text-xs font-medium">{g.sc}</span>
                      <span className="text-[13px] font-medium">{g.title}</span>
                      <span className="text-xs text-muted">{plural(g.total, "issue")} · {g.fixed} fixed · {g.needs_human} to a person</span>
                    </div>
                    <ul className="m-0 list-none p-0">
                      {rows.map((f) => (
                        <li key={`${g.sc}-${f.id}`} className="relative grid grid-cols-[84px_minmax(0,1fr)_auto_12px] items-center gap-3.5 px-[18px] py-[9px] hover:bg-subtle">
                          <SeverityMark severity={f.severity} />
                          <div className="min-w-0">
                            <Link href={`/runs/${id}/findings/${f.id}?filter=${filter}`} className="font-mono text-[13px] font-medium text-ink no-underline after:absolute after:inset-0 after:content-['']">
                              {f.rule_id}
                            </Link>
                            <div className="truncate text-xs text-muted"><span className="font-mono">{f.page}</span></div>
                          </div>
                          <StatusPill status={f.status} />
                          <span aria-hidden="true" className="text-faint-2">›</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
              {data.groups.length > 0 && flat.every((f) => !matches(f, filter)) && <p className="m-0 px-[18px] py-6 text-muted">Nothing here yet.</p>}
            </Card>

            <div className="flex min-w-0 flex-[1_1_280px] flex-col gap-5">
              <Card label="Pages" className="px-[18px] py-4">
                <h2 className="m-0 mb-3 text-[13px] font-semibold">Pages</h2>
                <ul className="m-0 flex list-none flex-col gap-3.5 p-0">
                  {data.pages.map((p) => (
                    <li key={p.id}>
                      <div className="mb-1 flex items-baseline justify-between gap-2">
                        <span className="truncate font-mono text-xs">{p.path}</span>
                        <span className="tabular font-mono text-xs text-muted">
                          {p.score_before ?? "—"} → <span className="font-medium text-ink">{p.score_after ?? "—"}</span>
                        </span>
                      </div>
                      <div aria-hidden="true" className="relative h-1.5 rounded-full bg-divider">
                        <div className="absolute inset-y-0 left-0 rounded-full bg-teal" style={{ width: `${p.score_after ?? 0}%` }} />
                        <div className="absolute inset-y-0 left-0 rounded-full bg-accent" style={{ width: `${p.score_before ?? 0}%` }} />
                      </div>
                    </li>
                  ))}
                </ul>
              </Card>
              <Card label="Severity" className="px-[18px] py-4">
                <h2 className="m-0 mb-3 text-[13px] font-semibold">Severity</h2>
                <ul className="m-0 flex list-none flex-col gap-2 p-0">
                  {SEVERITIES.map((s) => (
                    <li key={s} className="flex items-center justify-between">
                      <SeverityMark severity={s} />
                      <span className="tabular font-mono text-[13px]">{data.severity[s] ?? 0}</span>
                    </li>
                  ))}
                </ul>
              </Card>
            </div>
          </div>
        </>
      )}
    </Page>
  );
}

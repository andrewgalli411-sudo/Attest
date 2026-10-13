"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useApp, useCrumbs } from "@/components/shell/context";
import { Empty, ErrorNote, Page, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { flatten } from "@/lib/findings";

/** "Findings" in the sidebar: review the newest run's findings. */
export function FindingsIndexView() {
  const { runs, loadError } = useApp();
  const router = useRouter();
  const [empty, setEmpty] = useState(false);
  useCrumbs([{ label: "Findings" }]);

  useEffect(() => {
    if (!runs) return;
    const latest = runs.find((r) => (r.findings ?? 0) > 0) ?? runs[0];
    if (!latest) { const t = setTimeout(() => setEmpty(true), 0); return () => clearTimeout(t); }
    let live = true;
    api.dashboard(latest.id).then((d) => {
      if (!live) return;
      const first = flatten(d)[0];
      if (first) router.replace(`/runs/${latest.id}/findings/${first.id}`);
      else router.replace(`/runs/${latest.id}`);
    }).catch(() => live && setEmpty(true));
    return () => { live = false; };
  }, [runs, router]);

  return (
    <Page>
      <title>Findings · Attest</title>
      <PageHeader title="Findings">Every issue a run found, with the evidence for each.</PageHeader>
      {loadError && <ErrorNote message={loadError} />}
      {empty ? <Empty>No findings yet. Start a run from the Runs page.</Empty> : <p role="status" className="m-0 text-muted">Opening the latest run…</p>}
    </Page>
  );
}

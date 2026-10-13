import { Suspense } from "react";
import { ReportsView } from "@/components/ReportsView";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page(props: PageProps<"/reports/[runId]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading report…</p>}>
      {props.params.then(({ runId }) => (
        <ReportsView runId={runId} />
      ))}
    </Suspense>
  );
}

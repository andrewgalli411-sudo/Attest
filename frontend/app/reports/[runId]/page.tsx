import { Suspense } from "react";
import { ReportsView } from "@/components/ReportsView";

export default function Page(props: PageProps<"/reports/[runId]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading report…</p>}>
      {props.params.then(({ runId }) => (
        <ReportsView runId={runId} />
      ))}
    </Suspense>
  );
}

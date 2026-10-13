import { Suspense } from "react";
import PullRequestsView from "./view";

export default function Page() {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading pull requests…</p>}>
      <PullRequestsView />
    </Suspense>
  );
}

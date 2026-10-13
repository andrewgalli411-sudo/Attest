import { Suspense } from "react";
import PullRequestsView from "./view";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page() {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading pull requests…</p>}>
      <PullRequestsView />
    </Suspense>
  );
}

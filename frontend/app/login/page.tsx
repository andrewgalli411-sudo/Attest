import { Suspense } from "react";
import { LoginView } from "./view";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page() {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading…</p>}>
      <LoginView />
    </Suspense>
  );
}

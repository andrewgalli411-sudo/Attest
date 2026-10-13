import { Suspense } from "react";
import { RedirectToFinding } from "@/components/RedirectToFinding";

/** Old address of a finding. It now lives under its run, next to the list of that run's findings. */
// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page(props: PageProps<"/findings/[id]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading finding…</p>}>
      {props.params.then(({ id }) => (
        <RedirectToFinding fid={id} />
      ))}
    </Suspense>
  );
}

import { Suspense } from "react";
import { FindingReview } from "@/components/FindingReview";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page(props: PageProps<"/runs/[id]/findings/[fid]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading finding…</p>}>
      {props.params.then(({ id, fid }) => (
        <FindingReview runId={id} fid={fid} />
      ))}
    </Suspense>
  );
}

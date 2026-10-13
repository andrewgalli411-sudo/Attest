import { Suspense } from "react";
import { FindingReview } from "@/components/FindingReview";

export default function Page(props: PageProps<"/runs/[id]/findings/[fid]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading finding…</p>}>
      {props.params.then(({ id, fid }) => (
        <FindingReview runId={id} fid={fid} />
      ))}
    </Suspense>
  );
}

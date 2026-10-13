import { Suspense } from "react";
import RunView from "./view";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page(props: PageProps<"/runs/[id]">) {
  return (
    <Suspense fallback={<p role="status" className="p-7 text-muted">Loading run…</p>}>
      {props.params.then(({ id }) => (
        <RunView id={id} />
      ))}
    </Suspense>
  );
}

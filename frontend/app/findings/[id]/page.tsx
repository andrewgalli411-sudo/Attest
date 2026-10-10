import { Suspense } from "react";
import FindingView from "./view";

export default function Page(props: PageProps<"/findings/[id]">) {
  return (
    <Suspense fallback={<p role="status">Loading finding...</p>}>
      {props.params.then(({ id }) => (
        <FindingView id={id} />
      ))}
    </Suspense>
  );
}

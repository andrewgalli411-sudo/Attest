import { Suspense } from "react";
import RunView from "./view";

export default function Page(props: PageProps<"/runs/[id]">) {
  return (
    <Suspense fallback={<p role="status">Loading run...</p>}>
      {props.params.then(({ id }) => (
        <RunView id={id} />
      ))}
    </Suspense>
  );
}

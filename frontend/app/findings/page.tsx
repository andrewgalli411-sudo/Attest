import { FindingsIndexView } from "@/components/FindingsIndex";

// Every screen depends on who is signed in, so none can be a static shell (see the Next.js guide for authenticated apps).
export const instant = false;

export default function Page() {
  return <FindingsIndexView />;
}

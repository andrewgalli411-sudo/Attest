"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

export function RedirectToFinding({ fid }: { fid: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.finding(fid).then((f) => router.replace(`/runs/${f.run_id}/findings/${fid}`)).catch((e: Error) => setError(e.message));
  }, [fid, router]);
  return <div className="p-7">{error ? <ErrorNote message={error} /> : <p role="status" className="m-0 text-muted">Loading finding…</p>}</div>;
}

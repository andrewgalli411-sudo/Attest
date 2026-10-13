"use client";

import { createContext, useContext, useEffect, useMemo } from "react";
import type { ConfigOut, RunOut } from "@/lib/api";

export type Crumb = { label: string; href?: string; mono?: boolean };

export type Entry = { name: string; branch: string | null; pages: number | null };

export type AppState = {
  config: ConfigOut | null;
  /** Runs for the selected project, newest first. null while loading. */
  runs: RunOut[] | null;
  loadError: string | null;
  reload: () => void;
  entries: Entry[];
  project: string | null;
  setProject: (name: string) => void;
  openNewRun: () => void;
  setCrumbs: (c: Crumb[]) => void;
};

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const v = useContext(AppContext);
  if (!v) throw new Error("useApp must be used inside <AppShell>");
  return v;
}

/** Pages declare their breadcrumb; the shell renders it. */
export function useCrumbs(crumbs: Crumb[]): void {
  const { setCrumbs } = useApp();
  const key = JSON.stringify(crumbs);
  const stable = useMemo(() => JSON.parse(key) as Crumb[], [key]);
  useEffect(() => {
    setCrumbs(stable);
  }, [setCrumbs, stable]);
}

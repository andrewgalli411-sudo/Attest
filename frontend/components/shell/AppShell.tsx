"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { api, type AuthConfig, type ConfigOut, type RunOut } from "@/lib/api";
import { initials, tokens } from "@/lib/format";
import { AppContext, type Crumb, type Entry } from "./context";
import { NewRunDrawer } from "./NewRunDrawer";

const DESKTOP = "(min-width: 768px)";
function useIsDesktop(): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(DESKTOP);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => window.matchMedia(DESKTOP).matches,
    () => true,
  );
}

const STORE_KEY = "attest.project";
function remembered(): string | null {
  try { return window.localStorage.getItem(STORE_KEY); } catch { return null; }
}
function remember(name: string) {
  try { window.localStorage.setItem(STORE_KEY, name); } catch { /* private mode: the choice just isn't remembered */ }
}

type NavKey = "runs" | "findings" | "prs" | "reports" | "settings";
function activeNav(path: string): NavKey {
  if (path.startsWith("/pull-requests")) return "prs";
  if (path.startsWith("/reports")) return "reports";
  if (path.startsWith("/settings")) return "settings";
  if (path.startsWith("/findings") || /^\/runs\/[^/]+\/findings/.test(path)) return "findings";
  return "runs";
}

function defaultCrumbs(path: string): Crumb[] {
  const label = { runs: "Runs", findings: "Findings", prs: "Pull requests", reports: "Reports", settings: "Settings" }[activeNav(path)];
  return [{ label }];
}

export function AppShell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const desktop = useIsDesktop();
  const [auth, setAuth] = useState<AuthConfig | null>(null);
  const [config, setConfig] = useState<ConfigOut | null>(null);
  const [allRuns, setAllRuns] = useState<RunOut[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [crumbState, setCrumbState] = useState<{ path: string; crumbs: Crumb[] } | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(() => {
    api.runs().then((r) => { setAllRuns(r); setLoadError(null); }).catch((e: Error) => setLoadError(e.message));
  }, []);
  // Who is this? On a server with accounts, nothing else is fetched until someone is signed in.
  useEffect(() => {
    api.authConfig().then(setAuth).catch((e: Error) => setLoadError(e.message));
  }, []);
  const bare = path === "/login";
  const ready = !!auth && (auth.mode !== "accounts" || auth.signed_in);
  useEffect(() => {
    if (auth && !ready && !bare) router.replace(`/login?next=${encodeURIComponent(path)}`);
  }, [auth, ready, bare, path, router]);
  useEffect(() => {
    if (!ready) return;
    api.config().then(setConfig).catch((e: Error) => setLoadError(e.message));
    const t = setTimeout(() => setChosen(remembered()), 0);
    return () => clearTimeout(t);
  }, [ready]);
  useEffect(() => {
    if (!ready) return;
    const t = setTimeout(reload, 0); // also refreshes the sidebar counts whenever you move around
    return () => clearTimeout(t);
  }, [path, reload, ready]);
  useEffect(() => {
    const t = setTimeout(() => setNavOpen(false), 0);
    return () => clearTimeout(t);
  }, [path]);

  const entries: Entry[] = useMemo(() => {
    const out: Entry[] = (config?.projects ?? []).map((p) => ({ name: p.name, branch: p.base_branch, pages: p.pages.length }));
    if (config?.demo_enabled) out.push({ name: "demo", branch: "main", pages: 1 });
    return out;
  }, [config]);
  const project = entries.find((e) => e.name === chosen)?.name ?? entries[0]?.name ?? null;
  const current = entries.find((e) => e.name === project) ?? null;
  const runs = useMemo(() => (allRuns && project ? allRuns.filter((r) => r.project === project) : allRuns), [allRuns, project]);
  const setProject = useCallback((n: string) => { setChosen(n); remember(n); setMenuOpen(false); }, []);

  const setCrumbs = useCallback((c: Crumb[]) => setCrumbState({ path, crumbs: c }), [path]);
  const crumbs = crumbState && crumbState.path === path ? crumbState.crumbs : defaultCrumbs(path);

  const openNewRun = useCallback(() => {
    opener.current = document.activeElement as HTMLElement | null;
    setDrawer(true);
  }, []);
  const closeDrawer = useCallback(() => {
    setDrawer(false);
    opener.current?.focus();
  }, []);

  useEffect(() => {
    if (!menuOpen && !navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setMenuOpen(false); setNavOpen(false); } };
    const onDown = (e: MouseEvent) => { if (menuOpen && menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false); };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => { document.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onDown); };
  }, [menuOpen, navOpen]);

  const latest = runs?.[0];
  const completed = (runs ?? []).filter((r) => r.status === "complete");
  const counts: Record<NavKey, number | null> = {
    runs: runs?.length ?? null,
    findings: latest?.findings ?? null,
    prs: latest?.fixed ?? null,
    reports: completed.length,
    settings: null,
  };
  const nav: { key: NavKey; label: string; href: string }[] = [
    { key: "runs", label: "Runs", href: "/" },
    { key: "findings", label: "Findings", href: "/findings" },
    { key: "prs", label: "Pull requests", href: "/pull-requests" },
    { key: "reports", label: "Reports", href: "/reports" },
    { key: "settings", label: "Settings", href: "/settings" },
  ];
  const here = activeNav(path);
  const hidden = !desktop && !navOpen;
  const s = config?.settings;
  if (bare) return <main id="main" tabIndex={-1} className="outline-none">{children}</main>; // sign-in: no app chrome
  if (!ready) return <p role="status" className="p-7 text-muted">Loading…</p>;

  return (
    <AppContext.Provider value={{ config, runs, loadError, reload, entries, project, setProject, openNewRun, setCrumbs }}>
      <a className="skip-link" href="#main">Skip to main content</a>
      <div className="flex min-h-screen">
        {!desktop && navOpen && (
          <button type="button" aria-label="Close navigation" className="fixed inset-0 z-30 bg-ink/30" onClick={() => setNavOpen(false)} />
        )}
        <aside
          id="sidebar"
          aria-label="Sidebar"
          // `inert` takes the off-screen drawer out of the tab order and the accessibility tree on small screens
          {...(hidden ? { inert: true } : {})}
          className={`z-40 flex h-screen w-[232px] flex-none flex-col border-r border-line bg-surface transition-transform md:sticky md:top-0 md:translate-x-0 ${desktop ? "sticky top-0" : `fixed inset-y-0 left-0 ${navOpen ? "translate-x-0" : "-translate-x-full"}`}`}
        >
          <div className="flex items-center px-[18px] pb-4 pt-[18px]">
            <Image src="/attest-logo.png" alt="attest" width={88} height={30} unoptimized priority className="block h-[30px] w-auto" />
          </div>

          <div className="relative px-3 pb-3" ref={menuRef}>
            <button
              type="button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => entries.length > 1 && setMenuOpen((o) => !o)}
              className="flex w-full items-center gap-2.5 rounded-md border border-line bg-subtle px-2.5 py-2 text-left hover:bg-hover"
            >
              <span aria-hidden="true" className="flex size-[22px] items-center justify-center rounded bg-ink font-mono text-[11px] font-medium text-white">
                {current ? initials(current.name) : "–"}
              </span>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-[13px] font-medium">{current?.name ?? "No project"}</span>
                {current && (
                  <span className="truncate font-mono text-[11px] text-muted">{current.branch ?? "main"} · {current.pages ?? 0} {current.pages === 1 ? "page" : "pages"}</span>
                )}
              </span>
              {entries.length > 1 && <span aria-hidden="true" className="text-[11px] text-muted">▾</span>}
              <span className="sr-only">{entries.length > 1 ? "Switch project" : "Current project"}</span>
            </button>
            {menuOpen && (
              <div role="menu" aria-label="Projects" className="absolute inset-x-3 top-full z-10 mt-1 rounded-md border border-line bg-surface p-1 shadow-[0_1px_2px_rgba(0,0,0,0.08)]">
                {entries.map((e) => (
                  <button key={e.name} type="button" role="menuitemradio" aria-checked={e.name === project} onClick={() => setProject(e.name)}
                    className={`block w-full rounded px-2.5 py-1.5 text-left text-[13px] hover:bg-hover ${e.name === project ? "font-semibold" : ""}`}>
                    {e.name}
                  </button>
                ))}
              </div>
            )}
          </div>

          <nav aria-label="Primary" className="flex flex-col gap-0.5 px-3 py-1">
            {nav.map((n) => {
              const on = here === n.key;
              const c = counts[n.key];
              return (
                <Link key={n.key} href={n.href} aria-current={on ? "page" : undefined}
                  className={`flex items-center justify-between gap-2 rounded-[5px] px-2.5 py-[7px] text-[13.5px] no-underline hover:bg-hover hover:text-ink ${on ? "bg-hover font-semibold text-ink" : "text-body"}`}>
                  <span>{n.label}</span>
                  {c !== null && <span className="font-mono text-[11px] font-normal text-muted">{c}</span>}
                </Link>
              );
            })}
          </nav>

          <div className="mt-auto flex flex-col gap-2.5 border-t border-line px-[18px] py-3.5">
            <p className="m-0 text-[11.5px] leading-[1.45] text-muted">
              Automated first pass. Every change is a pull request a person reviews. Attest does not certify compliance.
            </p>
            {auth?.mode === "accounts" && auth.login && (
              <div className="flex items-center gap-2">
                <span aria-hidden="true" className="flex size-6 flex-none items-center justify-center rounded-full bg-tint text-[11px] font-semibold text-accent">
                  {auth.login.slice(0, 2).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12.5px]">{auth.login}</span>
                <button type="button" className="rounded px-1.5 py-0.5 text-xs text-muted hover:bg-hover hover:text-ink"
                  onClick={async () => {
                    await api.logout();
                    setAuth({ mode: "accounts", signed_in: false, login: null }); // drop everything the last person could see
                    setConfig(null);
                    setAllRuns(null);
                    router.replace("/login");
                  }}>
                  Sign out
                </button>
              </div>
            )}
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header data-app-bar className="sticky top-0 z-[5] flex h-[52px] flex-none items-center justify-between gap-4 border-b border-line bg-page px-4 sm:px-7">
            <div className="flex min-w-0 items-center gap-3">
              {!desktop && (
                <button type="button" aria-label="Open navigation" aria-expanded={navOpen} aria-controls="sidebar" onClick={() => setNavOpen(true)}
                  className="rounded border border-field bg-surface px-2 py-1 text-[13px]">
                  <span aria-hidden="true">☰</span>
                </button>
              )}
              <nav aria-label="Breadcrumb" className="min-w-0 text-[13px] text-muted">
                <ol className="m-0 flex list-none items-center gap-2 p-0">
                  {crumbs.map((c, i) => {
                    const last = i === crumbs.length - 1;
                    return (
                      <li key={`${i}-${c.label}`} className="flex min-w-0 items-center gap-2">
                        {i > 0 && <span aria-hidden="true" className="text-faint-3">/</span>}
                        {c.href && !last ? (
                          <Link href={c.href} className={`truncate text-muted no-underline hover:text-ink ${c.mono ? "font-mono" : ""}`}>{c.label}</Link>
                        ) : (
                          <span aria-current={last ? "page" : undefined} className={`truncate ${last ? "text-ink" : ""} ${c.mono ? "font-mono" : ""}`}>{c.label}</span>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </nav>
            </div>
            {s && (
              <span className="hidden whitespace-nowrap rounded border border-line px-[7px] py-[3px] font-mono text-[11px] text-muted sm:inline">
                {s.model_configured ? `Model: ${s.triage_model}` : "Model: not configured"} · {tokens(s.model_token_budget)} tokens per run
              </span>
            )}
          </header>
          <main id="main" tabIndex={-1} className="flex min-w-0 flex-1 flex-col outline-none">{children}</main>
        </div>
      </div>
      <NewRunDrawer open={drawer} onClose={closeDrawer} />
    </AppContext.Provider>
  );
}

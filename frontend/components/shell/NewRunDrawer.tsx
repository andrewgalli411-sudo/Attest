"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, type StartRun } from "@/lib/api";
import { btn, ErrorNote } from "@/components/ui";
import { useApp } from "./context";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const field = "w-full rounded-md border border-field bg-surface px-2.5 py-2 text-[13.5px]";

export function NewRunDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { config, entries, project } = useApp();
  const router = useRouter();
  const panel = useRef<HTMLDivElement>(null);
  const [pick, setPick] = useState<string | null>(null);
  const [probes, setProbes] = useState(true);
  const [publish, setPublish] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Development-only custom repository form (the server refuses it unless ALLOW_CUSTOM_RUNS is set).
  const [repo, setRepo] = useState("");
  const [dev, setDev] = useState("pnpm dev --port {port}");
  const [pages, setPages] = useState("/");
  const [checks, setChecks] = useState("pnpm build");
  const [cookie, setCookie] = useState("");

  const chosen = pick ?? project ?? "";
  const proj = config?.projects.find((p) => p.name === chosen);
  const isDemo = chosen === "demo" && !proj;
  const chips = proj ? proj.pages.map((p) => String(p.path)) : isDemo ? ["/"] : [];

  useEffect(() => {
    if (!open) return;
    // Move focus into the dialog as soon as it exists: the first form field, else the first control.
    (panel.current?.querySelector<HTMLElement>("form select, form input, form button") ?? panel.current?.querySelector<HTMLElement>("button"))?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      else if (!panel.current.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, onClose]);

  if (!open) return null;

  async function start(body: Partial<StartRun>) {
    setBusy(true);
    setError(null);
    try {
      const run = await api.startRun(body);
      onClose();
      router.push(`/runs/${run.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (isDemo) void start({ demo: true, pages: [{ path: "/", label: "profile" }], with_probes: probes });
    else void start({ project: chosen, publish: publish && !!proj?.can_publish, with_probes: probes });
  }

  function submitCustom(e: React.FormEvent) {
    e.preventDefault();
    const [name, ...rest] = cookie.split("=");
    void start({
      repo_path: repo, dev_command: dev, with_probes: probes,
      pages: pages.split(",").map((p) => p.trim()).filter(Boolean).map((p) => ({ path: p, label: p })),
      check_commands: checks.split("\n").map((c) => c.trim()).filter(Boolean),
      auth_cookies: cookie.includes("=") ? [{ name, value: rest.join("=") }] : [],
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div aria-hidden="true" className="absolute inset-0 bg-ink/[0.28]" onClick={onClose} />
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby="new-run-h"
        className="relative flex h-full w-full max-w-[440px] flex-col border-l border-line bg-surface">
        <div className="flex items-center justify-between border-b border-line px-6 py-5">
          <h2 id="new-run-h" className="m-0 text-base font-semibold">New run</h2>
          <button type="button" aria-label="Close" onClick={onClose} className="rounded px-2 py-1 text-lg leading-none text-muted hover:bg-hover">
            <span aria-hidden="true">×</span>
          </button>
        </div>

        <form id="new-run-form" onSubmit={submit} className="flex flex-1 flex-col gap-5 overflow-y-auto px-6 py-5">
          {error && <ErrorNote message={error} />}
          {entries.length === 0 ? (
            <p role="status" className="m-0 text-muted">No projects are configured on this server. Ask an operator to define one in projects.json.</p>
          ) : (
            <>
              <label className="flex flex-col gap-1.5 text-[13px] font-medium">
                Project
                <select className={`${field} font-normal`} value={chosen} onChange={(e) => { setPick(e.target.value); setPublish(false); }}>
                  {entries.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
                </select>
              </label>

              <div className="flex flex-col gap-1.5">
                <span className="text-[13px] font-medium" id="pages-label">Pages</span>
                <ul aria-labelledby="pages-label" className="m-0 flex list-none flex-wrap gap-1.5 p-0">
                  {chips.map((c) => (
                    <li key={c} className="rounded border border-field bg-subtle px-[9px] py-1 font-mono text-[12.5px]">{c}</li>
                  ))}
                </ul>
                <span className="text-xs text-muted">Defined by the operator in projects.json. Logged-in pages use the seeded dev session.</span>
              </div>

              <label className="flex items-start gap-2.5">
                <input type="checkbox" className="mt-0.5 size-4 accent-accent" checked={probes} onChange={(e) => setProbes(e.target.checked)} />
                <span className="flex flex-col"><span className="text-[13px] font-medium">Run behavioural probes</span>
                  <span className="text-xs text-muted">Keyboard navigation, traps, focus visibility, modal focus, form labels, name/role/value.</span></span>
              </label>

              <label className="flex items-start gap-2.5">
                <input type="checkbox" className="mt-0.5 size-4 accent-accent" checked={publish && !!proj?.can_publish} disabled={!proj?.can_publish}
                  onChange={(e) => setPublish(e.target.checked)} />
                <span className="flex flex-col"><span className="text-[13px] font-medium">Publish results</span>
                  <span className="text-xs text-muted">
                    Push branches and open pull requests and issues on the remote. Leave off to keep everything on the server.
                    {!proj?.can_publish && " This project is not allowed to publish."}
                  </span></span>
              </label>
            </>
          )}

          {config?.allow_custom_runs && (
            <details className="rounded-md border border-line p-3">
              <summary className="cursor-pointer text-[13px] font-medium">Custom repository (development only)</summary>
              <p className="text-xs text-bad">This server allows arbitrary commands. Never enable it on a shared or public server.</p>
              <div className="mt-2 grid gap-3">
                <label className="text-[13px]">Repository path<input className={`${field} mt-1`} value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="/Users/you/code/app" /></label>
                <label className="text-[13px]">Dev server command<input className={`${field} mt-1`} value={dev} onChange={(e) => setDev(e.target.value)} /></label>
                <label className="text-[13px]">Pages to audit (comma separated paths)<input className={`${field} mt-1`} value={pages} onChange={(e) => setPages(e.target.value)} /></label>
                <label className="text-[13px]">Checks that must pass after each fix (one per line)<textarea className={`${field} mt-1`} rows={2} value={checks} onChange={(e) => setChecks(e.target.value)} /></label>
                <label className="text-[13px]">Seeded session cookie (name=value, optional)<input className={`${field} mt-1`} value={cookie} onChange={(e) => setCookie(e.target.value)} autoComplete="off" /></label>
                <button type="button" disabled={busy || !repo} className={btn.secondary} onClick={submitCustom}>Start custom run</button>
              </div>
            </details>
          )}
        </form>

        <div className="flex justify-end gap-2.5 border-t border-line px-6 py-4">
          <button type="button" className={btn.secondary} onClick={onClose}>Cancel</button>
          <button type="submit" form="new-run-form" className={btn.primary} disabled={busy || entries.length === 0}>Start run</button>
        </div>
      </div>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, api, type ConfigOut, type RunOut } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

export default function RunsPage() {
  const router = useRouter();
  const [runs, setRuns] = useState<RunOut[] | null>(null);
  const [config, setConfig] = useState<ConfigOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [project, setProject] = useState("");
  const [publish, setPublish] = useState(false);
  // Only used when the server has custom runs enabled (development).
  const [repo, setRepo] = useState("");
  const [dev, setDev] = useState("pnpm dev --port {port}");
  const [pages, setPages] = useState("/");
  const [checks, setChecks] = useState("pnpm build");
  const [cookie, setCookie] = useState("");

  useEffect(() => {
    api.runs().then(setRuns).catch((e: ApiError) => setError(e.message));
    api.config().then((c) => { setConfig(c); setProject(c.projects[0]?.name ?? ""); }).catch((e: ApiError) => setError(e.message));
  }, []);

  async function start(body: Parameters<typeof api.startRun>[0]) {
    setBusy(true);
    setError(null);
    try {
      const run = await api.startRun(body);
      router.push(`/runs/${run.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const selected = config?.projects.find((p) => p.name === project);
  const field = "mt-1 w-full rounded border border-line bg-bg px-2 py-1.5";
  const primary = "rounded bg-fg px-4 py-2 font-semibold text-bg disabled:opacity-60";

  function onCustom(e: React.FormEvent) {
    e.preventDefault();
    const [name, ...rest] = cookie.split("=");
    void start({
      repo_path: repo, dev_command: dev,
      pages: pages.split(",").map((p) => p.trim()).filter(Boolean).map((p) => ({ path: p, label: p })),
      check_commands: checks.split("\n").map((c) => c.trim()).filter(Boolean),
      auth_cookies: cookie.includes("=") ? [{ name, value: rest.join("=") }] : [],
    });
  }

  return (
    <>
      <h1 className="text-2xl font-bold">Accessibility runs</h1>
      {error && <div className="mt-4"><ErrorNote message={error} /></div>}

      <section aria-labelledby="start-h" className="mt-6">
        <h2 id="start-h" className="text-xl font-semibold">Start a run</h2>
        <p className="mt-1 text-muted">
          A run drives the app, finds issues, proposes fixes on a branch, then re-tests the patched build to prove each fix.
          Only one run can be active at a time.
        </p>

        {config && config.projects.length > 0 && (
          <form className="mt-4 grid gap-3 rounded-lg border border-line bg-card p-4" onSubmit={(e) => { e.preventDefault(); void start({ project, publish }); }}>
            <h3 className="text-lg font-semibold">Audit a project</h3>
            <label>Project
              <select className={field} value={project} onChange={(e) => { setProject(e.target.value); setPublish(false); }}>
                {config.projects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
              </select>
            </label>
            {selected?.can_publish && (
              <label className="flex items-start gap-2">
                <input type="checkbox" className="mt-1.5 h-4 w-4" checked={publish} onChange={(e) => setPublish(e.target.checked)} />
                <span>Publish results: push branches and open pull requests and issues on this project&apos;s remote
                  <span className="block text-sm text-bad">Leave unchecked to keep everything on the server.</span></span>
              </label>
            )}
            <div><button type="submit" disabled={busy || !project} className={primary}>Start run</button></div>
          </form>
        )}

        {config?.demo_enabled && (
          <p className="mt-4">
            <button type="button" disabled={busy} onClick={() => start({ demo: true, pages: [{ path: "/", label: "profile" }] })} className={primary}>
              Run on the bundled demo app
            </button>
          </p>
        )}

        {config && config.projects.length === 0 && !config.demo_enabled && !config.allow_custom_runs && (
          <p className="mt-3" role="status">No projects are configured on this server. Ask an operator to define one.</p>
        )}

        {config?.allow_custom_runs && (
          <form onSubmit={onCustom} className="mt-6 grid gap-4 rounded-lg border border-line bg-card p-4" aria-describedby="repo-help">
            <h3 className="text-lg font-semibold">Custom repository (development only)</h3>
            <p id="repo-help" className="text-sm text-bad">This server allows arbitrary commands. Never enable it on a shared or public server.</p>
            <label>Repository path<input className={field} value={repo} onChange={(e) => setRepo(e.target.value)} required placeholder="/Users/you/code/app" /></label>
            <label>Dev server command<input className={field} value={dev} onChange={(e) => setDev(e.target.value)} required /></label>
            <label>Pages to audit (comma separated paths)<input className={field} value={pages} onChange={(e) => setPages(e.target.value)} required /></label>
            <label>Checks that must pass after each fix (one per line)<textarea className={field} rows={2} value={checks} onChange={(e) => setChecks(e.target.value)} /></label>
            <label>Seeded session cookie (name=value, optional)<input className={field} value={cookie} onChange={(e) => setCookie(e.target.value)} autoComplete="off" /></label>
            <div><button type="submit" disabled={busy || !repo} className={primary}>Start run</button></div>
          </form>
        )}
      </section>

      <section aria-labelledby="past-h" className="mt-10">
        <h2 id="past-h" className="text-xl font-semibold">Previous runs</h2>
        {runs === null && !error && <p role="status">Loading runs...</p>}
        {runs?.length === 0 && <p className="text-muted">No runs yet.</p>}
        {runs && runs.length > 0 && (
          <table className="mt-2 w-full border-collapse text-left">
            <caption className="sr-only">Previous runs, newest first</caption>
            <thead><tr>
              <th scope="col" className="border border-line bg-card p-2">Started</th>
              <th scope="col" className="border border-line bg-card p-2">Target</th>
              <th scope="col" className="border border-line bg-card p-2">Status</th>
              <th scope="col" className="border border-line bg-card p-2">Result</th>
            </tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td className="border border-line p-2"><Link href={`/runs/${r.id}`}>{new Date(r.created_at).toLocaleString()}</Link></td>
                  <td className="border border-line p-2 font-mono text-sm">{r.target}</td>
                  <td className="border border-line p-2">{r.status}</td>
                  <td className="border border-line p-2">{r.headline ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

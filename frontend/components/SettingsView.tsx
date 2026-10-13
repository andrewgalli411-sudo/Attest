"use client";

import type { ReactNode } from "react";
import { useApp, useCrumbs } from "@/components/shell/context";
import { Card, ErrorNote, Loading, Page, PageHeader } from "@/components/ui";
import { plural, tokens } from "@/lib/format";

function Section({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children: ReactNode }) {
  return (
    <Card label={title}>
      <div className="border-b border-line px-5 py-3.5">
        <h2 className="m-0 text-sm font-semibold">{title}</h2>
        {subtitle && <p className="m-0 mt-0.5 text-xs text-muted">{subtitle}</p>}
      </div>
      <dl className="m-0">{children}</dl>
    </Card>
  );
}

function Row({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  // A flat dt + dd group (the only structure a definition list allows), laid out with grid.
  return (
    <div className="grid grid-cols-[minmax(0,560px)_1fr] items-center gap-x-6 gap-y-0.5 border-b border-divider px-5 py-3.5 last:border-b-0 max-sm:grid-cols-1">
      <dt className="col-start-1 row-start-1 text-[13px] font-medium">{label}</dt>
      {hint && <dd className="col-start-1 row-start-2 m-0 text-xs text-muted">{hint}</dd>}
      <dd className="m-0 font-mono text-[12.5px] sm:col-start-2 sm:row-span-2 sm:row-start-1 sm:justify-self-end">{children}</dd>
    </div>
  );
}

const onOff = (b: boolean) => (b ? "On" : "Off");

export function SettingsView() {
  const { config, loadError } = useApp();
  useCrumbs([{ label: "Settings" }]);
  const s = config?.settings;

  return (
    <Page narrow>
      <title>Settings · Attest</title>
      <PageHeader title="Settings">
        How this server is configured. These are set by whoever operates it, in the server&apos;s environment and in <span className="font-mono text-[12.5px]">projects.json</span>, so they are read-only here.
      </PageHeader>
      {loadError && <ErrorNote message={loadError} />}
      {!config && !loadError && <Loading what="settings" />}

      {config && s && (
        <>
          <Section title="Projects" subtitle="Defined by the operator in projects.json. Callers can never supply repositories or commands.">
            {config.projects.length === 0 && !config.demo_enabled && (
              <div className="px-5 py-4 text-muted">No projects are configured.</div>
            )}
            {config.projects.map((p) => (
              <Row key={p.name} label={p.name} hint={<>Base branch <span className="font-mono">{p.base_branch}</span> · {plural(p.pages.length, "page")}</>}>
                {p.can_publish ? "May publish" : "Server only"}
              </Row>
            ))}
            {config.demo_enabled && <Row label="demo" hint="The bundled demo app. It can never publish.">Server only</Row>}
          </Section>

          <Section title="Publishing" subtitle="Nothing leaves the server unless a project allows it and a person accepts the fix.">
            <Row label="Pull requests on the remote" hint="A run publishes only when its project sets allow_push, and only the fixes you accept on the Pull requests page.">
              {config.projects.some((p) => p.can_publish) ? "Enabled for some projects" : "Off for every project"}
            </Row>
            <Row label="Custom repositories" hint="Running arbitrary commands is for local development only.">{onOff(config.allow_custom_runs)}</Row>
          </Section>

          <Section title="Model and budget">
            <Row label="Triage model" hint="Sorts findings and plans fixes. Safety checks are not model-based.">{s.model_configured ? s.triage_model : "Not configured"}</Row>
            <Row label="Fix model" hint="Drafts the code change. Edits are still checked by code before they are applied.">{s.model_configured ? s.fix_model : "Not configured"}</Row>
            <Row label="Token budget per run" hint="A run stops using the model when it reaches this, hands the rest to a person, and still completes.">{tokens(s.model_token_budget)} tokens</Row>
            <Row label="Fallback model on refusal" hint="If the primary model declines a request, retry it once on a fallback model.">{onOff(s.refusal_fallback)}</Row>
          </Section>

          <Section title="Privacy and data">
            <Row label="Remove personal data before model calls" hint="Emails, phone numbers, tokens, keys and form values are redacted from page and source text.">Always on</Row>
            <Row label="Send screenshots to the model" hint="Off by default: only text is sent.">{onOff(s.send_screenshots_to_model)}</Row>
            <Row label="Sign-in required" hint="Whether the API needs a token.">{onOff(config.auth_required)}</Row>
            <Row label="Deleting old run data" hint={<>Run evidence includes screenshots of audited pages. The operator removes it on a schedule with <span className="font-mono">python -m auditor.purge --days 30</span>.</>}>Operator</Row>
          </Section>

          <Section title="Limits">
            <Row label="Runs at once">{s.max_concurrent_runs}</Row>
            <Row label="Time limit per run">{Math.round(s.run_timeout_s / 60)} min</Row>
            <Row label="Findings itemised per page" hint="The rest are counted and lower the score, but are not itemised.">{s.max_findings_per_page}</Row>
          </Section>
        </>
      )}
    </Page>
  );
}

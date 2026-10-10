import type { ReactNode } from "react";

export function DraftBanner() {
  return (
    <p role="note" className="rounded-md border border-line border-l-8 border-l-warn bg-card px-3 py-2 text-sm">
      <strong>Draft.</strong> Automated first pass - requires expert review. This tool does not certify that any page
      or product meets an accessibility standard.
    </p>
  );
}

const SEV: Record<string, { label: string; cls: string; mark: string }> = {
  critical: { label: "Critical", cls: "text-bad", mark: "!!" },
  serious: { label: "Serious", cls: "text-bad", mark: "!" },
  moderate: { label: "Moderate", cls: "text-warn", mark: "~" },
  minor: { label: "Minor", cls: "text-muted", mark: "-" },
};

export function SeverityBadge({ severity }: { severity: string }) {
  const s = SEV[severity] ?? { label: severity, cls: "", mark: "" };
  return (
    <span className={`font-semibold ${s.cls}`}>
      <span aria-hidden="true">{s.mark} </span>{s.label}
    </span>
  );
}

const STATUS: Record<string, { label: string; cls: string }> = {
  caught: { label: "Caught", cls: "text-muted" },
  triaged: { label: "Planned for auto-fix", cls: "text-muted" },
  needs_human: { label: "Needs a person", cls: "text-bad" },
  fix_authored: { label: "Fix written", cls: "text-muted" },
  pr_open: { label: "Pull request open", cls: "text-muted" },
  proved: { label: "Fix proved", cls: "text-ok" },
  prove_failed: { label: "Fix did not work", cls: "text-bad" },
  inconclusive: { label: "Could not be re-tested", cls: "text-warn" },
  regress_ok: { label: "Fixed and proved, no regressions", cls: "text-ok" },
  regress_fail: { label: "Fixed but caused a regression", cls: "text-bad" },
};

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status, cls: "" };
  return <span className={`font-semibold ${s.cls}`}>{s.label}</span>;
}

export function Stat({ value, label }: { value: ReactNode; label: string }) {
  return (
    <div className="rounded-lg border border-line bg-card p-3">
      <div className="text-2xl font-bold">{value}</div>
      <div className="text-sm text-muted">{label}</div>
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-md border border-bad bg-card p-3 text-bad">
      <strong>Something went wrong.</strong> {message}
    </div>
  );
}

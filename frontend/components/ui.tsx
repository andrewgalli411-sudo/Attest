import type { ReactNode } from "react";
import { KIND, SEVERITY, kindOf, type Severity } from "@/lib/findings";

/** Class strings for the three button styles in the design. */
export const btn = {
  primary: "inline-flex items-center justify-center gap-2 rounded-md bg-ink px-4 py-[9px] text-[13.5px] font-medium text-white hover:bg-ink-hover disabled:bg-faint-3 disabled:hover:bg-faint-3",
  secondary: "inline-flex items-center justify-center gap-2 rounded-md border border-field bg-surface px-3.5 py-2 text-[13px] font-medium text-ink hover:bg-hover disabled:opacity-60",
  accent: "inline-flex items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-[13px] font-medium text-white hover:bg-accent-hover disabled:opacity-60",
};

export function PageHeader({ title, children, actions }: { title: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">{title}</h1>
        {children && <p className="m-0 max-w-[640px] text-muted [text-wrap:pretty]">{children}</p>}
      </div>
      {actions}
    </div>
  );
}

export function Page({ children, narrow }: { children: ReactNode; narrow?: boolean }) {
  return (
    <div className={`flex w-full flex-col gap-[22px] px-4 pb-12 pt-7 sm:px-7 sm:pb-14 ${narrow ? "max-w-[880px]" : "max-w-[1240px]"}`}>
      {children}
    </div>
  );
}

export function Card({ children, className = "", label }: { children: ReactNode; className?: string; label?: string }) {
  return (
    <section aria-label={label} className={`rounded-lg border border-line bg-surface ${className}`}>
      {children}
    </section>
  );
}

/** Severity is always a diamond plus the word: never colour alone. */
export function SeverityMark({ severity, withLabel = true }: { severity: string; withLabel?: boolean }) {
  const s = SEVERITY[severity as Severity] ?? { label: severity, fg: "text-muted", bg: "bg-muted" };
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${s.fg}`}>
      <span aria-hidden="true" className={`inline-block size-[7px] rotate-45 ${s.bg}`} />
      {withLabel ? s.label : <span className="sr-only">{s.label}</span>}
    </span>
  );
}

export function StatusPill({ status, long = false }: { status: string; long?: boolean }) {
  const k = KIND[kindOf(status)];
  return (
    <span className={`inline-block whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ${k.fg} ${k.bg}`}>
      {long ? k.long : k.short}
    </span>
  );
}

const RUN_STATUS: Record<string, { label: string; fg: string }> = {
  complete: { label: "Complete", fg: "text-ok" },
  failed: { label: "Failed", fg: "text-bad" },
  cancelled: { label: "Cancelled", fg: "text-warn" },
};

export function RunStatusDot({ status }: { status: string }) {
  const s = RUN_STATUS[status] ?? { label: "Running", fg: "text-accent" };
  const running = !RUN_STATUS[status];
  return (
    <span className={`inline-flex items-center gap-1.5 text-[13px] font-medium ${s.fg}`}>
      <span aria-hidden="true" className={`size-[7px] rounded-full bg-current ${running ? "at-pulse" : ""}`} />
      {s.label}
    </span>
  );
}

export function RunStatusChip({ status }: { status: string }) {
  const done = status === "complete";
  const bad = status === "failed";
  const cancelled = status === "cancelled";
  const cls = done ? "bg-ok-bg text-ok" : bad ? "bg-bad-bg text-bad" : cancelled ? "bg-warn-bg text-warn" : "bg-tint text-accent";
  const label = done ? "Complete" : bad ? "Failed" : cancelled ? "Cancelled" : "Running";
  return <span className={`inline-block rounded-full px-[9px] py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}

export function DraftChip() {
  return <span className="inline-block rounded-full border border-field px-[9px] py-0.5 text-xs font-medium text-muted">Draft · needs review</span>;
}

/** Segmented filter: real buttons with aria-pressed, counts in mono. */
export function Segmented<T extends string>({
  label, value, onChange, options,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { key: T; label: string; count?: number }[];
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-0.5 rounded-md bg-hover p-0.5">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          aria-pressed={value === o.key}
          onClick={() => onChange(o.key)}
          className={`rounded px-2.5 py-[5px] text-[12.5px] ${value === o.key ? "bg-surface font-medium shadow-[0_1px_2px_rgba(0,0,0,0.08)]" : "text-body"}`}
        >
          {o.label}
          {o.count !== undefined && <span className="ml-1.5 font-mono text-[11px] text-muted">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-md border border-bad bg-bad-bg p-3 text-bad">
      <strong>Something went wrong.</strong> {message}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p role="status" className="text-muted">Loading {what}…</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-5 py-8 text-center text-muted">{children}</p>;
}

/** A label/value pair used in the plan table and settings rows. */
export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono ${className}`}>{children}</span>;
}

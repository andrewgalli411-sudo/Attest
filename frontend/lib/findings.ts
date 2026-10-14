import type { Dashboard, FindingRow } from "./api";

export type Kind = "proved" | "ready" | "pr_open" | "needs_human" | "not_retested" | "fix_failed" | "regression" | "in_progress";

/** Backend status -> what the reviewer sees. */
export function kindOf(status: string): Kind {
  switch (status) {
    case "regress_ok":
    case "proved":
      return "proved";
    case "fix_authored":
      return "ready";
    case "pr_open":
      return "pr_open";
    case "needs_human":
      return "needs_human";
    case "inconclusive":
      return "not_retested";
    case "prove_failed":
      return "fix_failed";
    case "regress_fail":
      return "regression";
    default:
      return "in_progress";
  }
}

export const KIND: Record<Kind, { long: string; short: string; fg: string; bg: string }> = {
  proved: { long: "Fixed and proved, no regressions", short: "Proved", fg: "text-ok", bg: "bg-ok-bg" },
  ready: { long: "Fix ready for review, not yet proved", short: "Ready for review", fg: "text-accent", bg: "bg-tint" },
  pr_open: { long: "Pull request open, waiting for proof", short: "PR open", fg: "text-accent", bg: "bg-tint" },
  needs_human: { long: "Needs a person", short: "Needs a person", fg: "text-body", bg: "bg-neutral-bg" },
  not_retested: { long: "Could not be re-tested", short: "Not re-tested", fg: "text-warn", bg: "bg-warn-bg" },
  fix_failed: { long: "Fix did not work", short: "Fix failed", fg: "text-bad", bg: "bg-bad-bg" },
  regression: { long: "Fixed but caused a regression", short: "Regression", fg: "text-bad", bg: "bg-bad-bg" },
  in_progress: { long: "In progress", short: "In progress", fg: "text-accent", bg: "bg-tint" },
};

export const SEVERITIES = ["critical", "serious", "moderate", "minor"] as const;
export type Severity = (typeof SEVERITIES)[number];

export const SEVERITY: Record<Severity, { label: string; fg: string; bg: string }> = {
  critical: { label: "Critical", fg: "text-sev-critical", bg: "bg-sev-critical" },
  serious: { label: "Serious", fg: "text-sev-serious", bg: "bg-sev-serious" },
  moderate: { label: "Moderate", fg: "text-sev-moderate", bg: "bg-sev-moderate" },
  minor: { label: "Minor", fg: "text-sev-minor", bg: "bg-sev-minor" },
};

export type FilterKey = "all" | "fixed" | "ready" | "human" | "unproven";
export const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "fixed", label: "Fixed and proved" },
  { key: "ready", label: "Ready for review" },
  { key: "human", label: "Needs a person" },
  { key: "unproven", label: "Not proved" },
];

export function parseFilter(v: string | null | undefined): FilterKey {
  return FILTERS.some((f) => f.key === v) ? (v as FilterKey) : "all";
}

export function matches(f: { status: string }, key: FilterKey): boolean {
  const k = kindOf(f.status);
  switch (key) {
    case "all": return true;
    case "fixed": return k === "proved";
    case "ready": return k === "ready" || k === "pr_open";
    case "human": return k === "needs_human";
    case "unproven": return k === "not_retested" || k === "fix_failed" || k === "regression";
  }
}

/** One row per finding. A finding that maps to several success criteria appears in several groups; the flat list has it once. */
export function flatten(d: Dashboard): FindingRow[] {
  const seen = new Set<string>();
  const out: FindingRow[] = [];
  for (const g of d.groups) {
    for (const f of g.findings) {
      if (!seen.has(f.id)) {
        seen.add(f.id);
        out.push(f);
      }
    }
  }
  const rank = (s: string) => SEVERITIES.indexOf(s as Severity);
  return out.sort((a, b) => rank(a.severity) - rank(b.severity) || a.rule_id.localeCompare(b.rule_id));
}

export function countFor(rows: FindingRow[], key: FilterKey): number {
  return rows.filter((r) => matches(r, key)).length;
}

export const RUN_DONE = new Set(["complete", "failed", "cancelled", "reviewed", "exported"]);
export const isFinished = (status: string) => RUN_DONE.has(status);

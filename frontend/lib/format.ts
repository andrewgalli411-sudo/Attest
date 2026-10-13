/** Small display helpers. Everything here runs in the browser after data loads, so locale and time zone are the viewer's. */

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function initials(name: string): string {
  const parts = name.split(/[\s_-]+/).filter(Boolean);
  const two = parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2);
  return two.toLowerCase();
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "Today 13:02", "Yesterday 09:10", or "12 Oct 14:20". */
export function when(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86_400_000);
  if (diff === 0) return `Today ${hm}`;
  if (diff === 1) return `Yesterday ${hm}`;
  return `${d.getDate()} ${d.toLocaleString("en-GB", { month: "short" })} ${hm}`;
}

/** "47s", "5m 47s". */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  const s = Math.round(seconds);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
}

/** 412_000 -> "412k", 1_500_000 -> "1.5M". */
export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

import type { components } from "./api-types";

export type Dashboard = components["schemas"]["Dashboard"];
export type FindingDetail = components["schemas"]["FindingDetail"];
export type FindingRow = components["schemas"]["FindingRow"];
export type RunOut = components["schemas"]["RunOut"];
export type StartRun = components["schemas"]["StartRun"];
export type EvidenceOut = components["schemas"]["EvidenceOut"];
export type StageOut = components["schemas"]["StageOut"];
export type PullRequestRow = components["schemas"]["PullRequestRow"];
export type PullRequestsOut = components["schemas"]["PullRequestsOut"];
export type PublishOut = components["schemas"]["PublishOut"];
export type ProjectInfo = components["schemas"]["ProjectInfo"];
export type AuthConfig = components["schemas"]["AuthConfig"];
export type Me = components["schemas"]["Me"];

// Same-origin: requests go to this app's /api proxy, which attaches the server-side API token.
export const API_BASE = "";
export type ConfigOut = components["schemas"]["ConfigOut"];

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    // X-Requested-With is what the API requires for any change: a cross-site form cannot send it.
    res = await fetch(`${API_BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", "X-Requested-With": "attest", ...init?.headers } });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check that the web app and the backend are running.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      if (res.status === 401 && detail === "sign in required" && typeof window !== "undefined") {
        // The session ended: go and sign in again, then come back here.
        // A hard navigation on purpose: the session is gone, so nothing in memory should survive.
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination
        window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
        detail = "Your session ended. Signing you in again…";
      } else if (res.status === 401) {
        detail = "Not authorised. The web app is missing or has the wrong API token.";
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  config: () => request<ConfigOut>("/api/config"),
  authConfig: () => request<AuthConfig>("/api/auth/config"),
  me: () => request<Me>("/api/me"),
  logout: async () => { await fetch("/api/auth/logout", { method: "POST", headers: { "X-Requested-With": "attest" } }); },
  cancel: (id: string) => request<RunOut>(`/api/runs/${id}/cancel`, { method: "POST" }),
  runs: () => request<RunOut[]>("/api/runs"),
  startRun: (body: Partial<StartRun>) => request<RunOut>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  dashboard: (id: string) => request<Dashboard>(`/api/runs/${id}/dashboard`),
  finding: (id: string) => request<FindingDetail>(`/api/findings/${id}`),
  decide: (id: string, action: string, note?: string) =>
    request(`/api/findings/${id}/decision`, { method: "POST", body: JSON.stringify({ action, note }) }),
  pullRequests: (id: string) => request<PullRequestsOut>(`/api/runs/${id}/pull-requests`),
  publish: (id: string) => request<PublishOut>(`/api/runs/${id}/publish`, { method: "POST" }),
  url: (path: string) => `${API_BASE}${path}`,
  eventsUrl: (id: string) => `${API_BASE}/api/runs/${id}/events`,
};

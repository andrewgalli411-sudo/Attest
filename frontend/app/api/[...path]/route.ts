/**
 * Same-origin proxy to the backend API.
 *
 * The browser only ever talks to this app. The backend's bearer token (AUDITOR_API_TOKEN) is attached here,
 * on the server, so it is never shipped to the browser, never appears in page source, and the backend does
 * not need to enable CORS. Server-sent events are streamed through unchanged.
 */
const BASE = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
const TOKEN = process.env.AUDITOR_API_TOKEN ?? "";

const FORWARD_REQUEST = ["content-type", "accept", "last-event-id"];
const FORWARD_RESPONSE = ["content-type", "cache-control", "retry-after", "www-authenticate", "x-accel-buffering"];

async function forward(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  // Refuse anything that could escape /api on the backend.
  if (path.some((s) => s === "" || s === "." || s === ".." || s.includes("/") || s.includes("\\"))) {
    return Response.json({ detail: "bad path" }, { status: 400 });
  }
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, BASE);
  target.search = new URL(req.url).search;

  const headers = new Headers();
  for (const h of FORWARD_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "POST" ? await req.text() : undefined,
      signal: req.signal, // client disconnects (e.g. a closed SSE stream) cancel the upstream request
      cache: "no-store",
    });
  } catch {
    return Response.json({ detail: "The backend is not reachable from the web server." }, { status: 502 });
  }

  const out = new Headers();
  for (const h of FORWARD_RESPONSE) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = forward;
export const POST = forward;

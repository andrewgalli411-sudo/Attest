import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Optional password gate for the whole UI (HTTP Basic). Set UI_BASIC_AUTH="user:password" to enable.
 * It is a baseline for small deployments; put the app behind your SSO / reverse proxy for anything larger.
 */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function proxy(request: NextRequest) {
  const expected = process.env.UI_BASIC_AUTH;
  if (!expected) return NextResponse.next();
  const header = request.headers.get("authorization") ?? "";
  const [scheme, encoded] = header.split(" ");
  let given = "";
  try {
    given = scheme?.toLowerCase() === "basic" && encoded ? atob(encoded) : "";
  } catch {
    given = "";
  }
  if (given && safeEqual(given, expected)) return NextResponse.next();
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Accessibility Auditor", charset="UTF-8"' },
  });
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };

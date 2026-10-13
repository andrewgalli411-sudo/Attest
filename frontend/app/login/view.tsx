"use client";

import Image from "next/image";
import { useSearchParams } from "next/navigation";
import { btn } from "@/components/ui";

/** Only a path on this site is accepted as the place to return to; the server checks it again. */
function safeNext(v: string | null): string {
  return v && v.startsWith("/") && !v.startsWith("//") && !v.includes("\\") ? v : "/";
}

export function LoginView() {
  const next = safeNext(useSearchParams().get("next"));
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <title>Sign in · Attest</title>
      <div className="flex w-full max-w-[400px] flex-col gap-6 rounded-lg border border-line bg-surface p-8">
        <Image src="/attest-logo.png" alt="attest" width={88} height={30} unoptimized priority className="block h-[30px] w-auto" />
        <div className="flex flex-col gap-1.5">
          <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">Sign in</h1>
          <p className="m-0 text-muted">
            Attest finds accessibility issues in your site, proposes fixes as pull requests, and proves each one on your preview deployment.
          </p>
        </div>
        {/* A plain link: the browser leaves for GitHub, then comes back to the app signed in. */}
        <a href={`/api/auth/github/login?next=${encodeURIComponent(next)}`} className={`${btn.primary} text-white no-underline hover:text-white`}>
          Continue with GitHub
        </a>
        <p className="m-0 text-xs text-muted">
          Attest asks GitHub who you are. It can only read or change a repository after you install the Attest GitHub App on it.
          Automated first pass. Attest does not certify compliance.
        </p>
      </div>
    </div>
  );
}

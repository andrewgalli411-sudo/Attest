import type { Metadata } from "next";
import localFont from "next/font/local";
import { Suspense } from "react";
import "./globals.css";
import { AppShell } from "@/components/shell/AppShell";

// Self-hosted (SIL OFL, see app/fonts/LICENSE.txt): builds and tests never depend on reaching Google Fonts.
const plexSans = localFont({ src: "./fonts/ibm-plex-sans.woff2", variable: "--font-plex-sans", weight: "100 700", display: "swap" });
const plexMono = localFont({
  src: [
    { path: "./fonts/ibm-plex-mono-400.woff2", weight: "400", style: "normal" },
    { path: "./fonts/ibm-plex-mono-500.woff2", weight: "500", style: "normal" },
  ],
  variable: "--font-plex-mono",
  display: "swap",
});

// Every screen depends on who is signed in, so none of them can be a static shell. Opting out of instant-navigation
// validation is what the Next.js guide for authenticated apps prescribes.
export const instant = false;

export const metadata: Metadata = {
  title: { default: "Attest", template: "%s · Attest" },
  description: "Finds accessibility issues, proposes fixes as pull requests, and proves each fix with a re-test.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable} h-full`}>
      <body className="min-h-full">
        {/* The shell reads the URL, which can only happen at request time; the fallback keeps the page shell static. */}
        <Suspense fallback={<p role="status" className="p-7 text-muted">Loading…</p>}>
          <AppShell>{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}

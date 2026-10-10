import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { DraftBanner } from "@/components/ui";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Accessibility Auditor",
  description: "Finds accessibility issues, proposes fixes as pull requests, and proves each fix with a re-test.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col">
        <a className="skip-link" href="#main">Skip to main content</a>
        <header className="border-b border-line">
          <div className="mx-auto max-w-5xl px-4 py-3 flex flex-wrap items-center gap-4">
            <Link href="/" className="font-bold text-lg no-underline text-fg">Accessibility Auditor</Link>
            <nav aria-label="Primary">
              <Link href="/">Runs</Link>
            </nav>
          </div>
          <div className="mx-auto max-w-5xl px-4 pb-3"><DraftBanner /></div>
        </header>
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-5xl px-4 py-6 flex-1">{children}</main>
        <footer className="border-t border-line">
          <p className="mx-auto max-w-5xl px-4 py-4 text-sm text-muted">
            Automated first pass - requires expert review. Every change is a pull request for a person to review.
          </p>
        </footer>
      </body>
    </html>
  );
}

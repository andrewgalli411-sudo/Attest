# Phase 1: attach any project through GitHub and a URL

Attest never runs your code on its own servers. A project is a GitHub repository (read through the GitHub App, never
cloned and executed) plus a site URL Attest can audit in a browser. Fixes become pull requests opened by the App; the
proof comes from the pull request's preview deployment, which your own infrastructure builds.

## Decisions
- **Two modes, chosen by the operator** (`AUTH_MODE`): `local` (today: one person, API token or loopback, projects in
  `projects.json`) and `accounts` (sign in with GitHub, every record owned by an account, projects stored in the database).
- **In `accounts` mode** operator projects and custom repo/command runs are off, always. A shared server must never run
  one tenant's commands.
- **Ownership is enforced in the API**, not the UI: a run, finding, evidence file or event stream that belongs to someone
  else answers 404 (never 403, so existence is not revealed).
- **Sessions** are server-side (random token, only its hash is stored), HttpOnly, SameSite=Lax, and unsafe requests
  must carry `X-Requested-With: attest` (cross-site forms cannot set it).
- **Secrets** (test-login cookies, the user's GitHub token) are encrypted at rest with `SECRETS_KEY` and never returned.
- **No build check before the PR**: Attest cannot run your build. Your CI runs on the PR, and the proof waits for the preview.
- **Source mapping on a deployed site has no React dev info**, so it is by unique match in the source only: fewer
  automatic fixes, never a guess.

## Milestones (each tested and committed on its own)
1. Accounts and ownership (GitHub sign-in, sessions, per-account scoping of every endpoint).
2. Projects in the database (repo, site URL, pages, encrypted test login), listing repos through the installation.
3. Domain ownership checks and network-safe auditing (no private addresses, redirects and rebinding covered).
4. Audit a live URL with source read through GitHub (tarball into a temp folder, read-only, never executed).
5. Pull requests opened by the App (Git Data API commits, only accepted + safe edits).
6. Webhooks and preview re-proof (signed events, same-element proof against the PR's preview URL).
7. Quotas, rate limits and per-account spend caps.
8. Onboarding UI (connect, pick repo, verify domain, pages and login, first audit) and the GitHub App setup guide.

## GitHub App settings (needed from milestone 2; see docs/GITHUB_APP.md when written)
Permissions: Metadata (read), Contents (read & write), Pull requests (read & write), Issues (read & write, optional),
Deployments (read). Events: `deployment_status`, `pull_request`, `installation`, `installation_repositories`.
User authorisation callback: `<PUBLIC_URL>/api/auth/github/callback`. Webhook URL: `<PUBLIC_URL>/api/github/webhook`.

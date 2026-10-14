# Running Attest for other people

`AUTH_MODE=accounts` turns Attest into a shared service: people sign in with GitHub, attach their own projects (a repository
plus its live site), and see only their own data. **Attest never builds or runs a customer's code on your server.** It audits
the deployed site in a browser, reads the source through the GitHub App, and proves each fix on the pull request's own
preview deployment, which the customer's pipeline builds.

For a single person on one machine, leave `AUTH_MODE=local` (the default) and use `docs/RUNBOOK.md`.

## What to set
| Setting | Meaning |
|---|---|
| `AUTH_MODE=accounts` | switches everything below on. Operator projects and custom commands are **off** in this mode, always |
| `PUBLIC_URL` | the address people use. `https://` makes session cookies `Secure` |
| `SECRETS_KEY` | encrypts test-login cookies and GitHub tokens at rest. Losing it makes those unreadable; back it up with the database |
| `GITHUB_*` | see `docs/GITHUB_APP.md` |
| `ALLOWED_HOSTS` | JSON list of the host names you serve, for example `["attest.example.com"]` |
| `ALLOWED_SITE_PORTS` | ports a customer's site may use (default `[443]`) |
| `PREVIEW_HOST_SUFFIXES` | hosting platforms where previews may live (default vercel.app, netlify.app, pages.dev, onrender.com, fly.dev) |
| `TRUSTED_PROXY_HOPS` | how many reverse proxies you run in front (see below). Default `0` |
| `MAX_RUNS_PER_ACCOUNT`, `MAX_RUNS_PER_DAY`, `MONTHLY_MODEL_TOKEN_CAP`, `MAX_PROJECTS_PER_ACCOUNT` | fair use per person (0 = off) |
| `RL_*_PER_MIN` | rate limits on sign-in, project changes, runs, repository lists, publishing and webhooks |
| `ANTHROPIC_API_KEY` | the model key. **Your** spend, which is why the monthly cap exists |

Use Postgres (`DATABASE_URL`) and run `alembic upgrade head` before starting.

## The things that keep strangers' sites from being a way into your network
Attest drives a browser to addresses that customers type. Three layers stand between that browser and your network:

1. **An address check** when a project is created: https only, a real public domain name, no credentials, no odd ports, no
   private names, no IP addresses. The customer must also **prove they own the site** (a file or a DNS record) before any audit.
2. **A proxy that every browser connection goes through.** It looks the name up itself, refuses anything that is not a public
   address at the moment of connecting (so a redirect to `localhost`, a name that resolves privately, or a DNS trick all fail),
   allows only web ports, and has its own limits per run.
3. **An egress firewall you provide.** A check inside the application cannot undo a connection already made, so also deny the
   audit workers any route to private networks. For example, on the host or in the container network, block outbound traffic to
   `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (including the cloud metadata address `169.254.169.254`),
   `127.0.0.0/8` other than the worker's own proxy, `100.64.0.0/10`, `fc00::/7` and `fe80::/10`. Allow DNS, the database, and the
   public internet.

Also: run the API as an unprivileged user in a container with a read-only filesystem except its scratch and evidence folders;
give it no cloud credentials; and keep `SECRETS_KEY` and the GitHub private key in a secret manager.

## Reverse proxy and the client address
Rate limits that are per address need the real client address. Leave `TRUSTED_PROXY_HOPS=0` unless your proxy writes
`X-Forwarded-For`; then set it to the number of proxies you run (usually `1`). Attest counts that many entries from the right,
and ignores anything further left because the caller wrote it.

## Operating it
```
python -m auditor.admin list                 who has signed in, with their runs and model use
python -m auditor.admin disable LOGIN        switch an account off at once (ends its sessions, refuses sign-in)
python -m auditor.admin enable LOGIN
python -m auditor.purge --days 30            delete finished runs and their evidence; also clears expired sessions and old webhook records
```
Schedule `purge` (daily is fine). Evidence holds screenshots of customers' pages, so keep it short-lived and treat the volume as sensitive.
Back up the database and the evidence volume together.

## What a customer sees and what it means
- A fix is a **proposal** until its pull request's preview deployment is re-audited; the dashboard says so everywhere.
- Nothing is ever merged by Attest, and no output claims compliance or certification.
- Their login cookie for the site is stored encrypted and is never shown again, only its name.

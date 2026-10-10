# Operations runbook

This tool finds accessibility problems, proposes fixes as pull requests, and re-tests the patched build to
prove each fix. **Everything it produces is an automated first pass for a person to review. It never
states that a product is compliant or certified; tests enforce that on model output, pull-request and issue
text, and the report. Fixed UI wording is reviewed by hand.**

## What it can and cannot do
- Finds issues with axe-core plus six behavioural probes, on pages you list, including logged-in pages via a
  seeded dev session.
- Auto-fixes only a short list of mechanical rules (`label`, `select-name`, `html-has-lang`,
  `html-lang-valid`, `button-name`, `link-name`, three ARIA name rules) and only when the element maps to exactly
  one place in your source. Contrast, alt-text quality, focus management, and every behavioural-probe finding
  are always handed to a person as a work order.
- Proves a fix only if: an unpatched control still fails, the *same element* is re-located on the patched
  build, and the rule passes there in every run. Otherwise it reports "fix did not work" or "could not be
  re-tested", never a pass.
- **Not covered:** native apps, PDFs, screen-reader automation, elements inside iframes, production
  builds (proofs run against your dev server), and most WCAG criteria that need human judgment.
- **Not yet validated:** quality of the model's triage and fixes on real codebases. The test suite uses a
  deterministic stand-in because no API key was available during development. Run it on a non-critical repo
  first and review every pull request.

## Deploy
```
cp .env.example .env            # fill in POSTGRES_PASSWORD, API_TOKEN, REPOS_DIR, ANTHROPIC_API_KEY
cp deploy/projects.example.json deploy/projects.json   # define your projects
docker compose up -d --build
```
The Docker files were written but **not built or run** during development (no Docker available); expect
to fix small things on first build. Without Docker: `uv run alembic upgrade head`, `uv run python -m auditor.bootstrap`,
`uv run uvicorn auditor.main:create_app --factory`, then `pnpm build && pnpm start` in `frontend/`.

## Security model
- **Callers cannot make the server run commands.** Repos, dev commands, check commands and session secrets
  live in `projects.json`, written by an operator. The API takes a project *name*. `ALLOW_CUSTOM_RUNS` exists for
  local development only.
- **Authentication:** with `API_TOKEN` set every `/api` call needs `Authorization: Bearer <token>`. Without it
  only loopback clients are served. The `Host` header is allow-listed to block DNS-rebinding. The web app's
  server attaches the token, so browsers never see it. Put the web app behind your SSO or set `UI_BASIC_AUTH`.
- **Model-written edits are constrained by code**, per rule (`fix/safety.py`): only the attributes that fix that
  rule may be added, nothing may be removed or changed, and scripts, event handlers (including Vue/Alpine/
  Svelte/Angular bindings), URLs, code-execution constructs, and invisible/bidi characters are rejected. A rule
  with no policy cannot be auto-fixed. Edits must also stay in one file, pass your build, and are merged by a person.
- **Nothing is published by default.** Pushing branches and opening PRs/issues requires the project to set
  `allow_push` *and* the run to request `publish`. Published text is filtered for compliance claims.
- Your working tree must be clean to start; fixes are committed on `a11y/...` branches only; the repo is
  restored to its branch afterwards, including after a cancel, crash, or timeout.

## Privacy: what is sent to the model
Page HTML excerpts and source-code excerpts are sent to the Anthropic API for triage and fix writing, after
redaction of emails, phone/number runs, tokens, API keys, private keys, `value=` attributes and `password:`-style
assignments. **A bare secret with no recognisable shape and no key name (for example the word `hunter2` alone) is
not detected.** Screenshots are never sent unless `SEND_SCREENSHOTS_TO_MODEL=true`. If your data policy
forbids sending page or source content to a third party, do not configure an API key.

## Operations
- **Limits:** one run at a time (`MAX_CONCURRENT_RUNS`), each at most `RUN_TIMEOUT_S`; `POST /api/runs/{id}/cancel`
  stops a run and cleans up its dev server and branch state.
- **Restarts:** runs live in the API process. After a restart, runs that were in progress are marked failed with a
  clear reason; start them again (fixes already committed are reused, not duplicated).
- **Retention:** `python -m auditor.purge --days 30 [--dry-run]` deletes finished runs and every file they own.
  Evidence contains screenshots/HTML of audited pages: schedule this, and treat the evidence volume as sensitive.
- **Backups:** back up the Postgres database and the evidence volume together.
- **Health:** `GET /api/health` (no auth). Logs go to stdout.
- **Machine sleep:** on a laptop, a sleeping machine pauses runs and trips timeouts; use a server or disable sleep.

## Tests
`cd backend && uv run pytest` (about 6 minutes; needs Chromium, Node/pnpm, git, and optionally Postgres binaries,
or set `A11Y_TEST_POSTGRES_URL`). `cd frontend && pnpm lint && pnpm typecheck && pnpm build`.
`uv run python scripts/m1_prove.py --demo --out /tmp/m1` runs the whole loop on the bundled demo and writes a report.

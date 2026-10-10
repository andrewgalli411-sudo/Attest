# Attest

An AI agent that drives your web app like a real user, finds what’s broken for people with disabilities,
auto-fixes the safe stuff as pull requests, and proves each fix worked.

It finds accessibility issues in a running web app (including logged-in pages), proposes the safe fixes as pull
requests, then re-tests the patched build to prove each fix worked and nothing else broke.

**Every output is an automated first pass that requires expert review. It never asserts compliance.**

- `backend/`: FastAPI API, Playwright + axe-core detection, triage, fix authoring, re-proof, regression guard
- `frontend/`: Next.js triage dashboard and per-finding evidence timeline
- `demo-app-template/`: a small app with seeded defects, used for the demo and the tests
- `docs/RUNBOOK.md`: setup, security model, privacy behaviour, operations, and honest limits
- `docker-compose.yml`, `.github/workflows/ci.yml`: deployment and CI (written, not yet exercised)

Try it: `cd backend && uv run python scripts/m1_prove.py --demo --out /tmp/m1`, then open `/tmp/m1/report.html`.
Read `docs/RUNBOOK.md` before pointing it at a real repository.

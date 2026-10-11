#!/usr/bin/env python
"""Run the full loop (detect -> triage -> fix -> re-prove -> regression-guard) and write an HTML report.

  uv run python scripts/m1_prove.py --demo --out /tmp/m1
  uv run python scripts/m1_prove.py --repo ~/code/app --dev-command "pnpm dev --port {port}" \
      --page / --check "pnpm build" --auth-cookie session=dev --client anthropic

Nothing is pushed or published unless --allow-push is given. Without ANTHROPIC_API_KEY (or with
--client scripted) a deterministic stand-in is used and the report says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auditor import models  # noqa: F401
from auditor.config import settings
from auditor.db import Base
from auditor.demo import DEV_COMMAND, SEED_AUTH, prepare_demo_repo
from auditor.models import AuditRun
from auditor.pipeline.events import EventBus
from auditor.remediate import RemediationConfig, run_remediation
from auditor.report.html import run_report
from auditor.seed import seed_criteria
from auditor.storage.evidence_store import EvidenceStore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use the bundled demo app")
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--dev-command", default=DEV_COMMAND)
    ap.add_argument("--page", action="append", default=None, help="path to audit (repeatable), default /")
    ap.add_argument("--check", action="append", default=None, help="command that must pass after each fix")
    ap.add_argument("--auth-cookie", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--base-branch", default="main")
    ap.add_argument("--only-rule", action="append")
    ap.add_argument("--max-fixes", type=int)
    ap.add_argument("--with-probes", action="store_true")
    ap.add_argument("--client", choices=["scripted", "anthropic"])
    ap.add_argument("--allow-push", action="store_true", help="push branches and open PRs/issues (outward-facing)")
    ap.add_argument("--out", type=Path, default=Path("m1-out"))
    a = ap.parse_args()

    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if a.demo:
        repo, _ = prepare_demo_repo(out / "demo-repo")
        auth, checks, pages = SEED_AUTH, a.check or ["pnpm build"], [{"path": p, "label": p} for p in (a.page or ["/"])]
    elif a.repo:
        repo = a.repo.resolve()
        auth = {"cookies": [dict(zip(("name", "value"), c.split("=", 1), strict=True)) for c in a.auth_cookie]} or None
        checks, pages = a.check or [], [{"path": p, "label": p} for p in (a.page or ["/"])]
    else:
        ap.error("give --demo or --repo")

    client_kind = a.client or ("anthropic" if (os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key) else "scripted")
    if client_kind == "anthropic":
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"), max_retries=4, timeout=120.0)
    else:
        from auditor.devtools.scripted_client import ScriptedClient
        client = ScriptedClient()
        print("NOTE: using the scripted stand-in, not a language model (no ANTHROPIC_API_KEY).", file=sys.stderr)

    engine = create_engine(f"sqlite:///{out / 'm1.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    with sf() as db:
        seed_criteria(db)
        run = AuditRun(target_url=str(repo))
        db.add(run)
        db.commit()
        run_id = run.id
    store = EvidenceStore(out / "evidence")
    cfg = RemediationConfig(repo_path=repo, dev_command=a.dev_command, check_commands=checks, pages=pages, auth=auth,
                            base_branch=a.base_branch, allow_push=a.allow_push, only_rules=a.only_rule,
                            max_fixes=a.max_fixes, with_probes=a.with_probes)
    summary = asyncio.run(run_remediation(run_id, sf, store, EventBus(sf), client, cfg))
    report = out / "report.html"
    report.write_text(run_report(sf, store, run_id))
    print(json.dumps({k: summary[k] for k in ("headline", "by_status", "score_before", "score_after", "regressions",
                                              "branches", "prs", "model_kind")}, indent=2))
    print(f"\nreport: {report}\nrepo branches: {summary['branches']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

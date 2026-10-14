"""Operator tools for a multi-user server.

    python -m auditor.admin list                 who has signed in, and what they have used
    python -m auditor.admin disable LOGIN        switch an account off at once (ends its sessions, refuses new ones)
    python -m auditor.admin enable LOGIN

A disabled account's runs that are already going finish on their own; nothing new can be started and nobody can sign in as it.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .models import Account, AuditRun, ProjectRecord, UserSession


def list_accounts(sf: sessionmaker) -> list[dict]:
    with sf() as db:
        out = []
        for a in db.scalars(select(Account).order_by(Account.created_at)):
            runs = db.scalars(select(AuditRun).where(AuditRun.owner_id == a.id)).all()
            out.append({"login": a.login, "disabled": a.disabled_at is not None, "projects": db.scalar(
                select(func.count()).select_from(ProjectRecord).where(ProjectRecord.owner_id == a.id)) or 0,
                "runs": len(runs), "model_tokens": sum(r.model_tokens or 0 for r in runs)})
        return out


def set_disabled(sf: sessionmaker, login: str, disabled: bool) -> bool:
    """Returns False if there is no such account. Disabling also ends every session the account has."""
    with sf() as db:
        a = db.scalar(select(Account).where(Account.login == login))
        if a is None:
            return False
        a.disabled_at = datetime.now(UTC) if disabled else None
        if disabled:
            db.execute(delete(UserSession).where(UserSession.account_id == a.id))
        db.commit()
        return True


def main() -> int:
    from .db import SessionLocal

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("login")
    a = ap.parse_args()
    if a.cmd == "list":
        for row in list_accounts(SessionLocal):
            print(f"{row['login']:<24} {'DISABLED' if row['disabled'] else 'active':<9} projects={row['projects']:<3} runs={row['runs']:<4} model_tokens={row['model_tokens']}")
        return 0
    ok = set_disabled(SessionLocal, a.login, a.cmd == "disable")
    print(f"{a.cmd}d {a.login}" if ok else f"no account called {a.login!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

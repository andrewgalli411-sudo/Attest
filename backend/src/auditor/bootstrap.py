"""One-time/idempotent startup tasks that run after `alembic upgrade head`: seed reference data."""

from __future__ import annotations

from .db import SessionLocal
from .seed import seed_criteria


def main() -> None:
    with SessionLocal() as db:
        n = seed_criteria(db)
    print(f"bootstrap: {n} criteria added")


if __name__ == "__main__":
    main()

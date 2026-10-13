from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .api.accounts import router as accounts_router
from .api.middleware import SecurityMiddleware
from .api.projects import router as projects_router
from .api.routes import TERMINAL, router
from .config import Settings
from .config import settings as default_settings
from .disclaimers import DRAFT_BANNER
from .models import AuditRun, RunStatus
from .pipeline.events import EventBus
from .projects import Project, load_projects
from .secrets import SecretsError, Vault
from .storage.evidence_store import EvidenceStore

log = logging.getLogger("auditor")
HEARTBEAT_S = 15
STALE_S = 75  # five missed beats


def default_client(cfg: Settings):
    if cfg.anthropic_api_key:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=cfg.anthropic_api_key, max_retries=4, timeout=120.0)
    from .devtools.scripted_client import ScriptedClient

    return ScriptedClient()  # clearly labelled in every run summary


@dataclass
class Deps:
    sf: sessionmaker
    store: EvidenceStore
    bus: EventBus
    client_factory: Callable
    work_dir: str
    cfg: Settings
    projects: dict[str, Project]
    vault: Vault | None = None
    log: logging.Logger = field(default_factory=lambda: log)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def spawn(self, run_id: str, coro) -> None:
        task = asyncio.create_task(coro)
        beat = asyncio.create_task(self._beat(run_id))
        self.tasks[run_id] = task

        def done(_t):
            self.tasks.pop(run_id, None)
            beat.cancel()

        task.add_done_callback(done)

    async def _beat(self, run_id: str) -> None:
        while True:
            try:
                with self.sf() as db:
                    run = db.get(AuditRun, run_id)
                    if run is None or run.status in TERMINAL:
                        return
                    run.heartbeat_at = datetime.now(UTC)
                    db.commit()
            except Exception as e:  # noqa: BLE001 - a missed beat is survivable; dying here would orphan the run
                self.log.warning("heartbeat for %s failed: %s", run_id, e)
            await asyncio.sleep(HEARTBEAT_S)

    @staticmethod
    def _last_seen(run: AuditRun) -> datetime:
        seen = run.heartbeat_at or run.created_at
        return seen if seen.tzinfo else seen.replace(tzinfo=UTC)

    def claim_slot(self, run_id: str) -> bool:
        """Decide, after inserting the run row, whether this run may proceed. Every worker computes the same order
        (oldest first, id as tie-break) from the same rows, so two workers racing past the pre-check cannot both
        win: the later one sees it is not within the first `max_concurrent_runs` and must withdraw."""
        cutoff = datetime.now(UTC) - timedelta(seconds=STALE_S)
        with self.sf() as db:
            rows = db.scalars(select(AuditRun).where(AuditRun.status.notin_(list(TERMINAL)))
                              .order_by(AuditRun.created_at, AuditRun.id)).all()
            live = [r.id for r in rows if r.id in self.tasks or r.id == run_id or self._last_seen(r) >= cutoff]
        return run_id in live[: self.cfg.max_concurrent_runs]

    def withdraw(self, run_id: str) -> None:
        with self.sf() as db:
            run = db.get(AuditRun, run_id)
            if run is not None and run.status == RunStatus.queued:
                db.delete(run)
                db.commit()

    def active_runs(self) -> int:
        """Runs genuinely in progress anywhere (any worker): non-terminal with a fresh heartbeat, or ours."""
        cutoff = datetime.now(UTC) - timedelta(seconds=STALE_S)
        with self.sf() as db:
            rows = db.scalars(select(AuditRun).where(AuditRun.status.notin_(list(TERMINAL)))).all()
            return sum(1 for r in rows if r.id in self.tasks or self._last_seen(r) >= cutoff)

    def finish(self, run_id: str, status: RunStatus | None, reason: str) -> None:
        """Record how a run ended. With status=None, only acts if the run is still non-terminal (so a failure
        already recorded by the pipeline is not overwritten)."""
        with self.sf() as db:
            run = db.get(AuditRun, run_id)
            if run is None or (status is None and run.status in TERMINAL):
                return
            run.status = status or RunStatus.failed
            run.finished_at = datetime.now(UTC)
            db.commit()
        self.bus.emit(run_id, "error", {"stage": "run", "error": reason})

    def reconcile_orphans(self) -> int:
        """Runs live in this process; after a restart any non-terminal run can never finish. Say so."""
        cutoff = datetime.now(UTC) - timedelta(seconds=STALE_S)
        with self.sf() as db:
            rows = db.scalars(select(AuditRun).where(AuditRun.status.notin_(list(TERMINAL)))).all()
            stale = [r.id for r in rows if r.id not in self.tasks and self._last_seen(r) < cutoff]
        for rid in stale:  # a run another worker is still beating is NOT ours to fail
            self.finish(rid, RunStatus.failed, "the server restarted while this run was in progress")
        return len(stale)


def create_app(sf: sessionmaker | None = None, *, store: EvidenceStore | None = None,
               client_factory: Callable | None = None, work_dir: str | None = None,
               config: Settings | None = None) -> FastAPI:
    cfg = config or default_settings
    if sf is None:
        from .db import Base, SessionLocal, engine

        Base.metadata.create_all(engine)  # development convenience; deployments run `alembic upgrade head`
        sf = SessionLocal
        with sf() as db:
            from .seed import seed_criteria

            seed_criteria(db)
    projects = load_projects(cfg.projects_file)  # raises at startup on a bad file: fail fast, not at first request
    vault = None
    if cfg.auth_mode not in ("local", "accounts"):
        raise RuntimeError(f"AUTH_MODE must be 'local' or 'accounts', not {cfg.auth_mode!r}")
    if cfg.auth_mode == "accounts":
        missing = [n for n, v in (("SECRETS_KEY", cfg.secrets_key), ("GITHUB_CLIENT_ID", cfg.github_client_id),
                                  ("GITHUB_CLIENT_SECRET", cfg.github_client_secret), ("PUBLIC_URL", cfg.public_url)) if not v]
        if missing:
            raise RuntimeError(f"AUTH_MODE=accounts needs {', '.join(missing)}")
        try:
            vault = Vault(cfg.secrets_key)
        except SecretsError as e:
            raise RuntimeError(str(e)) from e
        # A shared server must never run one person's commands or repositories for another: operator projects and
        # custom runs are local-mode features only.
        projects = {}
        cfg = cfg.model_copy(update={"allow_custom_runs": False})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        n = app.state.deps.reconcile_orphans()
        if n:
            log.warning("marked %d orphaned run(s) as failed", n)
        yield
        for t in list(app.state.deps.tasks.values()):  # cancel in-flight runs on shutdown
            t.cancel()
        await asyncio.gather(*app.state.deps.tasks.values(), return_exceptions=True)

    docs = {} if cfg.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="WCAG Auditor", description=DRAFT_BANNER, lifespan=lifespan, **docs)
    app.add_middleware(SecurityMiddleware, max_body=cfg.max_request_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Requested-With"])
    app.state.deps = Deps(sf=sf, store=store or EvidenceStore(cfg.evidence_dir), bus=EventBus(sf),
                          client_factory=client_factory or (lambda: default_client(cfg)),
                          work_dir=work_dir or cfg.evidence_dir, cfg=cfg, projects=projects, vault=vault)
    app.include_router(accounts_router)
    app.include_router(projects_router)
    app.include_router(router)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "notice": DRAFT_BANNER}

    return app

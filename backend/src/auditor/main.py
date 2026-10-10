from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .api.routes import TERMINAL, router
from .config import Settings
from .config import settings as default_settings
from .disclaimers import DRAFT_BANNER
from .models import AuditRun, RunStatus
from .pipeline.events import EventBus
from .projects import Project, load_projects
from .storage.evidence_store import EvidenceStore

log = logging.getLogger("auditor")


def default_client(cfg: Settings):
    if cfg.anthropic_api_key:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=cfg.anthropic_api_key)
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
    log: logging.Logger = field(default_factory=lambda: log)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def spawn(self, run_id: str, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks[run_id] = task
        task.add_done_callback(lambda _t: self.tasks.pop(run_id, None))

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
        with self.sf() as db:
            ids = list(db.scalars(select(AuditRun.id).where(AuditRun.status.notin_(list(TERMINAL)))))
        for rid in ids:
            if rid not in self.tasks:
                self.finish(rid, RunStatus.failed, "the server restarted while this run was in progress")
        return len(ids)


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
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "Last-Event-ID"])
    app.state.deps = Deps(sf=sf, store=store or EvidenceStore(cfg.evidence_dir), bus=EventBus(sf),
                          client_factory=client_factory or (lambda: default_client(cfg)),
                          work_dir=work_dir or cfg.evidence_dir, cfg=cfg, projects=projects)
    app.include_router(router)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "notice": DRAFT_BANNER}

    return app

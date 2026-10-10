"""Durable event log + in-process fan-out (feeds SSE; resumable via Last-Event-ID)."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..models import RunEvent


class EventBus:
    def __init__(self, session_factory: sessionmaker):
        self._sf = session_factory
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._seq: dict[str, int] = {}

    def emit(self, run_id: str, type_: str, payload: dict | None = None) -> int:
        with self._sf() as db:
            if run_id not in self._seq:
                cur = db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
                self._seq[run_id] = cur or 0
            self._seq[run_id] += 1
            seq = self._seq[run_id]
            ev = RunEvent(run_id=run_id, seq=seq, type=type_, payload=payload or {})
            db.add(ev)
            db.commit()
        for q in self._subs[run_id]:
            q.put_nowait({"seq": seq, "type": type_, "payload": payload or {}})
        return seq

    def replay(self, db: Session, run_id: str, after_seq: int = 0) -> list[dict]:
        rows = db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq).order_by(RunEvent.seq)
        )
        return [{"seq": r.seq, "type": r.type, "payload": r.payload} for r in rows]

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[run_id].append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        if q in self._subs[run_id]:
            self._subs[run_id].remove(q)

"""In-process asyncio event bus with SQLite persistence.

``publish`` persists the event (and projects it onto the fleet-state tables) *before* handing it
to consumers, so any tool that reads the DB during agent processing sees state consistent with
the event being processed. Replay re-reads events by ``(run_id, seq)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sqlalchemy import select

from haulmind.db.engine import session_scope
from haulmind.db.models import EventRow, Run
from haulmind.events.projector import project_event
from haulmind.events.schemas import Event
from haulmind.logging_setup import get_logger
from haulmind.util import to_naive_utc

log = get_logger(__name__)


@dataclass
class BusStats:
    published: int = 0
    persisted: int = 0
    dropped_full: int = 0
    last_seq: int = -1
    by_type: dict[str, int] = field(default_factory=dict)


class EventBus:
    """Single-producer / single-consumer queue plus durable log."""

    def __init__(self, maxsize: int = 5000) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.stats = BusStats()
        self._closed = False

    # --- producer side -------------------------------------------------------------------------

    def persist(self, events: Iterable[Event], *, project: bool = True) -> int:
        """Write events to SQLite (and project fleet state). Returns the number written."""
        n = 0
        with session_scope() as s:
            run: Run | None = None
            for ev in events:
                if run is None or run.id != ev.run_id:
                    run = s.get(Run, ev.run_id)
                    if run is None:
                        raise ValueError(f"unknown run {ev.run_id}")
                s.add(
                    EventRow(
                        run_id=ev.run_id,
                        seq=ev.seq,
                        ts=to_naive_utc(ev.ts),
                        type=ev.type.value,
                        vehicle_id=ev.vehicle_id,
                        driver_id=ev.driver_id,
                        payload_json=ev.payload_dict,
                    )
                )
                if project:
                    project_event(s, ev)
                run.sim_now = to_naive_utc(ev.ts)
                run.event_count = max(run.event_count or 0, ev.seq + 1)
                n += 1
                self.stats.by_type[ev.type.value] = self.stats.by_type.get(ev.type.value, 0) + 1
        self.stats.persisted += n
        return n

    async def publish(self, event: Event, *, persist: bool = True) -> None:
        """Persist (optionally) then enqueue for the consumer."""
        if self._closed:
            raise RuntimeError("bus closed")
        if persist:
            self.persist([event])
        await self._queue.put(event)
        self.stats.published += 1
        self.stats.last_seq = event.seq

    async def close(self) -> None:
        """Signal consumers that no more events will arrive."""
        self._closed = True
        await self._queue.put(None)

    # --- consumer side -------------------------------------------------------------------------

    async def get(self) -> Event | None:
        """Next event, or ``None`` once the bus is closed and drained."""
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        return self._closed


def iter_persisted_events(run_id: str, start_seq: int = 0, batch: int = 500) -> Iterator[Event]:
    """Stream a run's events from SQLite in seq order (used for deterministic replay)."""
    seq = start_seq
    while True:
        with session_scope() as s:
            rows = (
                s.execute(
                    select(EventRow)
                    .where(EventRow.run_id == run_id, EventRow.seq >= seq)
                    .order_by(EventRow.seq)
                    .limit(batch)
                )
                .scalars()
                .all()
            )
        if not rows:
            return
        for r in rows:
            yield Event.from_row(
                r.run_id, r.seq, r.ts, r.type, r.vehicle_id, r.driver_id, r.payload_json
            )
        seq = rows[-1].seq + 1

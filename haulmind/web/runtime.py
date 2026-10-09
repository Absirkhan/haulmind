"""Process runtime for the web app: event bus, simulator producer, orchestrator consumer, and the
MCP tool client used both by agents and by manager approvals."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select

from haulmind.config import Settings, get_settings
from haulmind.db.engine import get_engine, session_scope
from haulmind.db.models import Run
from haulmind.db.repo import get_active_run
from haulmind.events.bus import EventBus
from haulmind.events.schemas import Event
from haulmind.llm.factory import get_llm_client
from haulmind.logging_setup import get_logger
from haulmind.mcp_server.client import ToolClient
from haulmind.orchestrator import Orchestrator
from haulmind.simulator import SimConfig, SimRun, generate, install_run, stream_run
from haulmind.simulator.runner import events_already_persisted
from haulmind.util import to_naive_utc

log = get_logger(__name__)


class AppRuntime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.bus = EventBus()
        self.tools: ToolClient | None = None
        self.orch: Orchestrator | None = None
        self.sim: SimRun | None = None
        self.speed: float = self.settings.sim_speed
        self.pause = asyncio.Event()
        self.stop = asyncio.Event()
        self.producer: asyncio.Task[int] | None = None
        self.consumer: asyncio.Task[None] | None = None
        self.started_at = time.time()
        self.recent_incidents: list[dict[str, Any]] = []
        self._rate_window: list[float] = []
        self.error: str | None = None
        self._owns_tools = False

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self, *, wait_for_mcp: bool = False) -> None:
        get_engine()
        self.settings.ensure_dirs()
        self.tools = ToolClient(self.settings, caller="agent")
        self._owns_tools = True
        await self.tools.connect(wait_s=60.0 if wait_for_mcp else 0.0)
        cfg = SimConfig(
            seed=self.settings.sim_seed,
            fleet_size=self.settings.sim_fleet_size,
            hours=self.settings.sim_hours,
        )
        self.sim = generate(cfg)
        start_seq = 0
        with session_scope() as s:
            active = get_active_run(s)
            resume = active is not None and active.id == self.sim.run_id
        if resume:
            start_seq = events_already_persisted(self.sim.run_id) + 1
            log.info("resuming run", run_id=self.sim.run_id, from_seq=start_seq)
        else:
            install_run(self.sim)
            log.info("new run installed", run_id=self.sim.run_id, events=len(self.sim.events))
        self.orch = Orchestrator(
            self.sim.run_id,
            tools=self.tools,
            llm=get_llm_client(),
            settings=self.settings,
            on_incident=self._on_incident,
        )
        if resume:
            # catch the orchestrator up on already-persisted events without re-running agents
            self.orch.last_seq = start_seq - 1
        if self.settings.sim_autostart:
            self.pause.set()
        self.consumer = asyncio.create_task(self.orch.consume(self.bus), name="orchestrator")
        self.producer = asyncio.create_task(self._produce(start_seq), name="simulator")

    async def _produce(self, start_seq: int) -> int:
        assert self.sim is not None
        try:
            last = await stream_run(
                self.sim,
                self.bus,
                speed=self.speed,
                start_seq=start_seq,
                stop=self.stop,
                pause=self.pause,
                on_progress=self._on_progress,
            )
        except Exception as e:  # pragma: no cover
            self.error = str(e)
            log.exception("simulator failed")
            raise
        finally:
            with session_scope() as s:
                run = s.get(Run, self.sim.run_id)
                if run is not None and not self.stop.is_set():
                    run.status = "completed"
            await self.bus.close()
        return last

    async def shutdown(self) -> None:
        self.stop.set()
        self.pause.set()
        for t in (self.producer, self.consumer):
            if t is not None and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        if self.orch is not None:
            self.orch.flush_stats()
        if self.tools is not None and self._owns_tools:  # injected clients belong to their owner
            await self.tools.close()

    # --- controls ------------------------------------------------------------------------------

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.0, speed)
        if self.sim is not None and self.producer is not None and not self.producer.done():
            # restart the producer from the next seq with the new speed
            self.stop.set()

    async def restart_producer(self) -> None:
        if self.producer is not None and not self.producer.done():
            self.stop.set()
            with contextlib.suppress(Exception):
                await self.producer
        self.stop = asyncio.Event()
        self.bus = EventBus()
        assert self.orch is not None
        if self.consumer is not None and not self.consumer.done():
            self.consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.consumer
        self.consumer = asyncio.create_task(self.orch.consume(self.bus), name="orchestrator")
        start_seq = events_already_persisted(self.sim.run_id) + 1 if self.sim else 0
        self.producer = asyncio.create_task(self._produce(start_seq), name="simulator")

    def _on_progress(self, ev: Event) -> None:
        now = time.time()
        self._rate_window.append(now)
        cutoff = now - 10
        while self._rate_window and self._rate_window[0] < cutoff:
            self._rate_window.pop(0)

    def _on_incident(self, info: dict[str, Any]) -> None:
        self.recent_incidents.append({**info, "at": time.time()})
        self.recent_incidents = self.recent_incidents[-20:]

    # --- status ----------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        stats = self.orch.stats if self.orch else None
        with session_scope() as s:
            run = s.get(Run, self.sim.run_id) if self.sim else None
            sim_now: datetime | None = to_naive_utc(run.sim_now) if run and run.sim_now else None
            total = len(self.sim.events) if self.sim else 0
            done = (run.event_count if run else 0) or 0
            run_status = run.status if run else "none"
        producer_done = self.producer is None or self.producer.done()
        return {
            "run_id": self.sim.run_id if self.sim else None,
            "run_status": run_status,
            "sim_now": sim_now,
            "events_done": done,
            "events_total": total,
            "progress_pct": round(100.0 * done / total, 1) if total else 0.0,
            "events_per_s": round(len(self._rate_window) / 10.0, 1),
            "speed": self.speed,
            "paused": not self.pause.is_set(),
            "finished": producer_done and self.bus.closed,
            "filtered_pct": round(stats.filtered_pct, 1) if stats else 0.0,
            "incidents": stats.incidents_opened if stats else 0,
            "llm_provider": get_llm_client().provider,
            "queue": self.bus.qsize(),
            "stopped": self.orch.stopped if self.orch else False,
            "stop_reason": self.orch.stop_reason if self.orch else "",
            "error": self.error,
        }


_runtime: AppRuntime | None = None


def get_runtime() -> AppRuntime:
    global _runtime
    if _runtime is None:
        _runtime = AppRuntime()
    return _runtime


def set_runtime(rt: AppRuntime | None) -> None:
    global _runtime
    _runtime = rt


def latest_run_id() -> str | None:
    with session_scope() as s:
        run = (
            get_active_run(s)
            or s.execute(select(Run).order_by(Run.started_at.desc())).scalars().first()
        )
        return run.id if run else None

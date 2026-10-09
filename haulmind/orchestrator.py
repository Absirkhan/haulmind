"""Orchestrator: consumes events, clusters them, runs the rule prefilter, and invokes the agent
graph for candidates. Tracks per-run statistics and checkpoints for resume."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from haulmind.agents.graph import AgentDeps, build_graph, initial_state
from haulmind.agents.memory import load_memory
from haulmind.agents.prefilter import Cluster, PrefilterResult, prefilter
from haulmind.config import Settings, get_settings
from haulmind.db.engine import session_scope
from haulmind.db.models import EventRow, Incident, Vehicle
from haulmind.db.repo import bump_stat, get_checkpoint, set_checkpoint
from haulmind.events.bus import EventBus
from haulmind.events.schemas import EVENT_DOMAIN, Domain, Event, EventType
from haulmind.llm.base import CacheMiss, LLMClient, QuotaExhausted
from haulmind.llm.factory import get_llm_client
from haulmind.logging_setup import get_logger
from haulmind.mcp_server.client import ToolClient
from haulmind.policy.blocklist import BlockList
from haulmind.simulator.data import REEFER_TANK_GAL
from haulmind.tracing import get_tracer
from haulmind.util import to_naive_utc

log = get_logger(__name__)

TRIGGERS: dict[EventType, Domain] = {
    EventType.HOS_UPDATE: Domain.COMPLIANCE,
    EventType.DTC_FAULT: Domain.MAINTENANCE,
    EventType.ENGINE_METRICS: Domain.MAINTENANCE,
    EventType.FUEL_TRANSACTION: Domain.SPEND,
    EventType.HARSH_EVENT: Domain.SAFETY,
}
BUFFER_HOURS = {Domain.COMPLIANCE: 4, Domain.MAINTENANCE: 48, Domain.SPEND: 8, Domain.SAFETY: 48}
COOLDOWN_HOURS = {Domain.COMPLIANCE: 4, Domain.MAINTENANCE: 12, Domain.SPEND: 6, Domain.SAFETY: 24}


@dataclass
class OrchestratorStats:
    events_total: int = 0
    events_no_domain: int = 0
    events_dropped_rules: int = 0
    events_deduped: int = 0
    clusters_definite: int = 0
    clusters_ambiguous: int = 0
    incidents_opened: int = 0
    triage_dismissed: int = 0
    errors: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def filtered_pct(self) -> float:
        if not self.events_total:
            return 0.0
        reached = self.clusters_definite + self.clusters_ambiguous
        return 100.0 * (self.events_total - reached) / self.events_total

    def as_dict(self) -> dict[str, float]:
        d = {k: float(v) for k, v in self.__dict__.items() if isinstance(v, int | float)}
        d["filtered_pct"] = self.filtered_pct
        return d


class Orchestrator:
    def __init__(
        self,
        run_id: str,
        *,
        tools: ToolClient,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        on_incident: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.settings = settings or get_settings()
        self.tools = tools
        self.llm = llm or get_llm_client()
        self.deps = AgentDeps(
            settings=self.settings, tools=tools, llm=self.llm, blocklist=BlockList()
        )
        self.graph = build_graph(self.deps)
        self.stats = OrchestratorStats()
        self.buffers: dict[str, deque[Event]] = {}
        self.last_incident: dict[str, datetime] = {}
        self.vehicle_ctx: dict[str, dict[str, Any]] = {}
        self.on_incident = on_incident
        self.last_seq = -1
        self.stopped = False
        self.stop_reason = ""
        self._last_flush = 0

    # --- lifecycle ---------------------------------------------------------------------------

    def resume_point(self) -> int:
        with session_scope() as s:
            return get_checkpoint(s, self.run_id) + 1

    async def consume(self, bus: EventBus) -> None:
        """Consume from the bus until it closes or the quota guard stops us."""
        while not self.stopped:
            ev = await bus.get()
            if ev is None:
                break
            await self.handle(ev)
        self.flush_stats()

    async def handle(self, ev: Event) -> None:
        """Process one event (already persisted and projected by the bus)."""
        self.stats.events_total += 1
        self.stats.by_type[ev.type.value] = self.stats.by_type.get(ev.type.value, 0) + 1
        self.last_seq = ev.seq
        domain = TRIGGERS.get(ev.type)
        if domain is None or EVENT_DOMAIN.get(ev.type) is None:
            self.stats.events_no_domain += 1
            self._maybe_checkpoint()
            return
        key = self._key(domain, ev)
        if key is None:
            self.stats.events_no_domain += 1
            return
        buf = self.buffers.setdefault(key, deque())
        buf.append(ev)
        horizon = ev.ts - timedelta(hours=BUFFER_HOURS[domain])
        while buf and buf[0].ts < horizon:
            buf.popleft()
        cluster = Cluster(
            key=key,
            domain=domain,
            vehicle_id=ev.vehicle_id,
            driver_id=ev.driver_id,
            events=list(buf),
            trigger=ev,
            context=self._context(ev.vehicle_id),
        )
        with session_scope() as s:
            memory = (
                load_memory(s, domain, ev.vehicle_id, ev.driver_id, key)
                if self.settings.memory_enabled
                else {}
            )
        pre = prefilter(cluster, memory)
        if not pre.candidate:
            self.stats.events_dropped_rules += 1
            self._maybe_checkpoint()
            return
        last = self.last_incident.get(key)
        if last is not None and ev.ts.replace(tzinfo=None) - last.replace(tzinfo=None) < timedelta(
            hours=COOLDOWN_HOURS[domain]
        ):
            self.stats.events_deduped += 1
            self._attach_event(key, ev)
            self._maybe_checkpoint()
            return
        if pre.verdict == "definite":
            self.stats.clusters_definite += 1
        else:
            self.stats.clusters_ambiguous += 1
        await self._process(cluster, pre, memory)
        self._checkpoint()

    # --- processing ----------------------------------------------------------------------------

    async def _process(
        self, cluster: Cluster, pre: PrefilterResult, memory: dict[str, Any]
    ) -> None:
        tracer = get_tracer()
        trace_id = tracer.start_trace(f"cluster:{cluster.domain.value}", run_id=self.run_id)
        state = initial_state(cluster, pre, memory)
        status = "ok"
        result: dict[str, Any] = {}
        try:
            result = await self.graph.ainvoke(state)
        except QuotaExhausted as e:
            status = "quota"
            self.stopped = True
            self.stop_reason = str(e)
            log.warning("daily LLM quota exhausted; checkpointing and stopping", error=str(e))
        except CacheMiss as e:
            status = "cache_miss"
            self.stats.errors += 1
            log.error("replay cache miss", error=str(e))
            raise
        except Exception as e:  # keep the stream alive on agent errors
            status = "error"
            self.stats.errors += 1
            log.exception("agent graph failed", cluster=cluster.key, error=str(e))
        finally:
            totals = tracer.end_trace(
                trace_id,
                status=status,
                cluster=cluster.key,
                outcome=result.get("outcome") if result else status,
            )
        inc_id = result.get("incident_id") if result else None
        if inc_id:
            self.stats.incidents_opened += 1
            self.last_incident[cluster.key] = cluster.sim_ts
            with session_scope() as s:
                inc = s.get(Incident, inc_id)
                if inc is not None:
                    inc.trace_id = trace_id
                    inc.llm_requests = totals["llm_requests"]
                    inc.llm_input_tokens = totals["input_tokens"]
                    inc.llm_output_tokens = totals["output_tokens"]
                    inc.latency_ms = totals["latency_ms"]
                    inc.event_ids_json = [x.seq for x in cluster.events[-12:]]
                for evt in cluster.events[-12:]:
                    s.execute(
                        update(EventRow)
                        .where(EventRow.run_id == self.run_id, EventRow.seq == evt.seq)
                        .values(incident_id=inc_id)
                    )
            if self.on_incident is not None:
                self.on_incident(
                    {
                        "incident_id": inc_id,
                        "outcome": result.get("outcome"),
                        "cluster": cluster.key,
                    }
                )
        elif result and result.get("triage", {}).get("decision") in ("watch", "dismiss"):
            self.stats.triage_dismissed += 1
            # short cooldown so the same ambiguous cluster is not re-triaged every 15 minutes
            self.last_incident[cluster.key] = (
                cluster.sim_ts
                - timedelta(hours=COOLDOWN_HOURS[cluster.domain])
                + timedelta(hours=1)
            )
        if status == "quota":
            raise QuotaExhausted("llm", self.stop_reason)

    # --- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _key(domain: Domain, ev: Event) -> str | None:
        if domain in (Domain.COMPLIANCE, Domain.SAFETY):
            return f"{domain.value}:driver:{ev.driver_id}" if ev.driver_id else None
        return f"{domain.value}:vehicle:{ev.vehicle_id}" if ev.vehicle_id else None

    def _context(self, vehicle_id: str | None) -> dict[str, Any]:
        if not vehicle_id:
            return {}
        ctx = self.vehicle_ctx.get(vehicle_id)
        if ctx is None:
            with session_scope() as s:
                v = s.get(Vehicle, vehicle_id)
                ctx = (
                    {
                        "fuel_tank_gal": v.fuel_tank_gal,
                        "has_reefer": v.has_reefer,
                        "reefer_tank_gal": REEFER_TANK_GAL if v.has_reefer else 0.0,
                    }
                    if v
                    else {}
                )
            self.vehicle_ctx[vehicle_id] = ctx
        return ctx

    def _attach_event(self, key: str, ev: Event) -> None:
        with session_scope() as s:
            inc = (
                s.execute(
                    select(Incident)
                    .where(Incident.cluster_key == key, Incident.run_id == self.run_id)
                    .order_by(Incident.opened_ts.desc())
                )
                .scalars()
                .first()
            )
            if inc is not None and inc.status in ("open", "investigating", "auto_handled"):
                ids = list(inc.event_ids_json or [])
                if ev.seq not in ids:
                    ids.append(ev.seq)
                    inc.event_ids_json = ids[-50:]
                s.execute(
                    update(EventRow)
                    .where(EventRow.run_id == self.run_id, EventRow.seq == ev.seq)
                    .values(incident_id=inc.id)
                )

    def _maybe_checkpoint(self) -> None:
        if self.stats.events_total - self._last_flush >= 200:
            self._checkpoint()

    def _checkpoint(self) -> None:
        self._last_flush = self.stats.events_total
        with session_scope() as s:
            set_checkpoint(s, self.run_id, self.last_seq)
        self.flush_stats()

    def flush_stats(self) -> None:
        with session_scope() as s:
            from haulmind.db.models import RunStat

            existing = {
                r.key: r
                for r in s.execute(select(RunStat).where(RunStat.run_id == self.run_id)).scalars()
            }
            for k, v in self.stats.as_dict().items():
                if k in existing:
                    existing[k].value = v
                else:
                    bump_stat(s, self.run_id, k, v)


async def replay_events(
    orch: Orchestrator,
    events: list[Event],
    bus: EventBus,
    *,
    start_seq: int = 0,
    stop: asyncio.Event | None = None,
) -> int:
    """Feed events through the bus and orchestrator as fast as possible (evals, demo)."""
    last = start_seq - 1
    for ev in events:
        if ev.seq < start_seq:
            continue
        if (stop is not None and stop.is_set()) or orch.stopped:
            break
        bus.persist([ev])
        await orch.handle(ev)
        last = ev.seq
    orch.flush_stats()
    return last


def sim_clock(run_id: str) -> datetime | None:
    with session_scope() as s:
        from haulmind.db.models import Run

        run = s.get(Run, run_id)
        return to_naive_utc(run.sim_now) if run and run.sim_now else None

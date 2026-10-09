"""Install a generated run into the database and stream its events onto the bus."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select, update

from haulmind.config import get_settings
from haulmind.db.engine import session_scope
from haulmind.db.models import (
    Driver,
    DriverMessage,
    EventRow,
    FaultCode,
    FuelTransaction,
    HosStatus,
    Incident,
    ProposedAction,
    Reassignment,
    Run,
    RunCheckpoint,
    RunStat,
    Shop,
    ToolCall,
    Trip,
    Vehicle,
    WorkOrder,
)
from haulmind.events.bus import EventBus
from haulmind.events.schemas import Event
from haulmind.logging_setup import get_logger
from haulmind.simulator.generator import SimRun
from haulmind.util import to_naive_utc, utcnow

log = get_logger(__name__)


def ground_truth_path(run_id: str) -> Path:
    return get_settings().runs_dir / run_id / "ground_truth.json"


def install_run(sim: SimRun, *, replace: bool = True, persist_events: bool = False) -> None:
    """Write the run row, fleet entities and ground truth. Resets projected fleet state.

    ``persist_events=True`` writes all events immediately (batch/replay use); otherwise the
    streaming runner persists them one by one as they are published.
    """
    with session_scope() as s:
        existing = s.get(Run, sim.run_id)
        if existing is not None:
            if not replace:
                raise ValueError(f"run {sim.run_id} already exists")
            _delete_run(s, sim.run_id)
        # Only one run's fleet state lives in the projection tables at a time.
        s.execute(update(Run).where(Run.status == "active").values(status="superseded"))
        for table in (
            DriverMessage,
            WorkOrder,
            Reassignment,
            FaultCode,
            FuelTransaction,
            Trip,
            HosStatus,
            ToolCall,
        ):
            s.execute(delete(table))
        s.execute(delete(Vehicle))
        s.execute(delete(Driver))
        s.execute(delete(Shop))

        s.add(
            Run(
                id=sim.run_id,
                seed=sim.config.seed,
                label=sim.config.label
                or f"seed {sim.config.seed}, {sim.config.fleet_size} trucks, {sim.config.hours:g}h",
                config_json=sim.config.model_dump(mode="json"),
                started_at=to_naive_utc(utcnow()),
                status="active",
                sim_start=to_naive_utc(sim.config.start),
                sim_now=to_naive_utc(sim.config.start),
                event_count=0,
            )
        )
        s.add(RunCheckpoint(run_id=sim.run_id, last_seq=-1, updated_at=to_naive_utc(utcnow())))
        for v in sim.fleet.vehicles:
            s.add(
                Vehicle(
                    id=v.id,
                    unit_number=v.unit_number,
                    vin=v.vin,
                    make=v.make,
                    model=v.model,
                    year=v.year,
                    vehicle_class="tractor",
                    fuel_tank_gal=v.fuel_tank_gal,
                    has_reefer=v.has_reefer,
                    depot=v.depot,
                    status="active",
                    lat=v.depot_city.lat,
                    lon=v.depot_city.lon,
                    speed_mph=0.0,
                    heading=0.0,
                    odometer_mi=v.odometer_mi,
                    engine_on=False,
                )
            )
        for d in sim.fleet.drivers:
            s.add(
                Driver(
                    id=d.id,
                    name=d.name,
                    license_state=d.license_state,
                    depot=d.depot,
                    fuel_card_id=d.fuel_card_id,
                    status="active",
                    current_vehicle_id=d.vehicle_id,
                    phone=d.phone,
                )
            )
            s.add(HosStatus(driver_id=d.id, updated_ts=to_naive_utc(sim.config.start)))
        for v in sim.fleet.vehicles:
            s.execute(
                update(Vehicle)
                .where(Vehicle.id == v.id)
                .values(current_driver_id=sim.fleet.driver_for(v.id).id)
            )
        for sh in sim.fleet.shops:
            s.add(
                Shop(
                    id=sh.id,
                    name=sh.name,
                    city=sh.city,
                    lat=sh.lat,
                    lon=sh.lon,
                    specialties=sh.specialties,
                    rating=sh.rating,
                    hourly_rate_usd=sh.hourly_rate_usd,
                    open_24h=sh.open_24h,
                )
            )
    sim.ground_truth.save(ground_truth_path(sim.run_id))
    if persist_events:
        EventBus().persist(sim.events, project=False)
    log.info(
        "run installed",
        run_id=sim.run_id,
        vehicles=len(sim.fleet.vehicles),
        events=len(sim.events),
        gt=len(sim.ground_truth.incidents),
    )


def _delete_run(s, run_id: str) -> None:  # type: ignore[no-untyped-def]
    s.execute(delete(ProposedAction).where(ProposedAction.run_id == run_id))
    s.execute(delete(Incident).where(Incident.run_id == run_id))
    s.execute(delete(EventRow).where(EventRow.run_id == run_id))
    s.execute(delete(RunStat).where(RunStat.run_id == run_id))
    s.execute(delete(RunCheckpoint).where(RunCheckpoint.run_id == run_id))
    s.execute(delete(Run).where(Run.id == run_id))


def events_already_persisted(run_id: str) -> int:
    with session_scope() as s:
        row = s.execute(
            select(EventRow.seq)
            .where(EventRow.run_id == run_id)
            .order_by(EventRow.seq.desc())
            .limit(1)
        ).first()
    return -1 if row is None else int(row[0])


async def stream_run(
    sim: SimRun,
    bus: EventBus,
    *,
    speed: float,
    start_seq: int = 0,
    stop: asyncio.Event | None = None,
    pause: asyncio.Event | None = None,
    on_progress: Callable[[Event], None] | None = None,
    persist: bool = True,
) -> int:
    """Publish events in order, sleeping according to simulated time deltas / ``speed``.

    ``speed`` is simulated seconds per real second; 0 means no sleeping at all.
    Returns the last published seq.
    """
    last = start_seq - 1
    prev_ts: datetime | None = None
    for ev in sim.events:
        if ev.seq < start_seq:
            continue
        if stop is not None and stop.is_set():
            break
        if pause is not None:
            while not pause.is_set():
                if stop is not None and stop.is_set():
                    return last
                await asyncio.sleep(0.1)
        if speed > 0 and prev_ts is not None:
            delta = (ev.ts - prev_ts).total_seconds() / speed
            if delta > 0:
                await asyncio.sleep(min(delta, 5.0))
        prev_ts = ev.ts
        await bus.publish(ev, persist=persist)
        last = ev.seq
        if on_progress is not None:
            on_progress(ev)
    return last

"""Project events onto the fleet-state tables (vehicles, hos_status, fault_codes, ...).

This is what a telematics backend does with device data; it lets the MCP tools answer
"what is the current state of vehicle X" from plain tables, and makes replay reconstruct the
same state deterministically.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from haulmind.db.models import Driver, FaultCode, FuelTransaction, HosStatus, Trip, Vehicle
from haulmind.events.schemas import (
    DtcFault,
    EngineMetrics,
    Event,
    FuelTxn,
    GpsPing,
    HosUpdate,
    IdleEvent,
    TripEnd,
    TripStart,
)
from haulmind.util import to_naive_utc


def project_event(s: Session, ev: Event) -> None:
    """Apply one event's side effects to the state tables. Idempotent per (run, seq) in spirit."""
    p = ev.payload
    ts = to_naive_utc(ev.ts)

    if isinstance(p, GpsPing):
        v = s.get(Vehicle, ev.vehicle_id) if ev.vehicle_id else None
        if v is not None:
            v.lat, v.lon = p.lat, p.lon
            v.speed_mph, v.heading = p.speed_mph, p.heading
            v.odometer_mi = max(v.odometer_mi or 0.0, p.odometer_mi)
            v.last_seen_ts = ts
            v.engine_on = True
            if p.speed_mph > 2:
                v.idle_since_ts = None
            elif v.idle_since_ts is None:
                v.idle_since_ts = ts
        return

    if isinstance(p, EngineMetrics):
        v = s.get(Vehicle, ev.vehicle_id) if ev.vehicle_id else None
        if v is not None:
            v.coolant_temp_f = p.coolant_temp_f
            v.oil_pressure_psi = p.oil_pressure_psi
            v.battery_v = p.battery_v
            v.def_level_pct = p.def_level_pct
            v.fuel_level_pct = p.fuel_level_pct
            v.engine_on = p.engine_on
            v.last_seen_ts = ts
        return

    if isinstance(p, HosUpdate):
        if not ev.driver_id:
            return
        h = s.get(HosStatus, ev.driver_id)
        if h is None:
            h = HosStatus(driver_id=ev.driver_id)
            s.add(h)
        h.duty_status = p.duty_status
        h.drive_min_remaining = p.drive_min_remaining
        h.duty_min_remaining = p.duty_min_remaining
        h.cycle_min_remaining = p.cycle_min_remaining
        h.minutes_since_break = p.minutes_since_break
        h.updated_ts = ts
        if ev.vehicle_id:
            d = s.get(Driver, ev.driver_id)
            if d is not None:
                d.current_vehicle_id = ev.vehicle_id
        return

    if isinstance(p, DtcFault):
        if not ev.vehicle_id:
            return
        existing = (
            s.execute(
                select(FaultCode)
                .where(
                    FaultCode.vehicle_id == ev.vehicle_id,
                    FaultCode.code == p.code,
                    FaultCode.active.is_(True),
                )
                .order_by(FaultCode.last_ts.desc())
            )
            .scalars()
            .first()
        )
        if p.active:
            if existing is not None:
                existing.occurrences += 1
                existing.last_ts = ts
                existing.severity = p.severity
            else:
                s.add(
                    FaultCode(
                        vehicle_id=ev.vehicle_id,
                        code=p.code,
                        description=p.description,
                        system=p.system,
                        severity=p.severity,
                        first_ts=ts,
                        last_ts=ts,
                        occurrences=1,
                        active=True,
                    )
                )
        elif existing is not None:
            existing.active = False
            existing.last_ts = ts
        return

    if isinstance(p, FuelTxn):
        if not ev.vehicle_id:
            return
        if s.get(FuelTransaction, p.txn_id) is None:
            s.add(
                FuelTransaction(
                    id=p.txn_id,
                    vehicle_id=ev.vehicle_id,
                    driver_id=ev.driver_id,
                    card_id=p.card_id,
                    ts=ts,
                    gallons=p.gallons,
                    amount_usd=p.amount_usd,
                    price_per_gal=p.price_per_gal,
                    station=p.station,
                    lat=p.lat,
                    lon=p.lon,
                    odometer_mi=p.odometer_mi,
                    vehicle_lat_at_ts=p.vehicle_lat_at_ts,
                    vehicle_lon_at_ts=p.vehicle_lon_at_ts,
                )
            )
        return

    if isinstance(p, TripStart):
        if not (ev.vehicle_id and ev.driver_id):
            return
        if s.get(Trip, p.trip_id) is None:
            s.add(
                Trip(
                    id=p.trip_id,
                    run_id=ev.run_id,
                    vehicle_id=ev.vehicle_id,
                    driver_id=ev.driver_id,
                    origin=p.origin,
                    destination=p.destination,
                    dest_lat=p.dest_lat,
                    dest_lon=p.dest_lon,
                    planned_miles=p.planned_miles,
                    start_ts=ts,
                    eta_ts=to_naive_utc(p.eta_ts),
                    status="in_progress",
                    load_ref=p.load_ref,
                )
            )
        v = s.get(Vehicle, ev.vehicle_id)
        if v is not None:
            v.current_trip_id = p.trip_id
            v.current_driver_id = ev.driver_id
            v.engine_on = True
        d = s.get(Driver, ev.driver_id)
        if d is not None:
            d.current_vehicle_id = ev.vehicle_id
        return

    if isinstance(p, TripEnd):
        t = s.get(Trip, p.trip_id)
        if t is not None:
            t.status = "completed"
            t.end_ts = ts
        if ev.vehicle_id:
            v = s.get(Vehicle, ev.vehicle_id)
            if v is not None and v.current_trip_id == p.trip_id:
                v.current_trip_id = None
                v.speed_mph = 0.0
        return

    if isinstance(p, IdleEvent):
        if ev.vehicle_id:
            v = s.get(Vehicle, ev.vehicle_id)
            if v is not None:
                v.idle_since_ts = ts - timedelta(minutes=p.duration_min)
                v.speed_mph = 0.0
        return
    # harsh_event / speeding: the events table is the record; nothing to project.

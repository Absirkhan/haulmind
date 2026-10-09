"""Tool implementations. Read tools query the projected fleet state; write tools go through the
policy gateway (``gated``) which decides execute / pending / recommend / block / rate-limit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from haulmind.config import get_settings
from haulmind.db.engine import session_scope
from haulmind.db.models import (
    Driver,
    DriverMessage,
    EventRow,
    FaultCode,
    FuelTransaction,
    HosStatus,
    ProposedAction,
    Reassignment,
    Shop,
    Trip,
    Vehicle,
    WorkOrder,
)
from haulmind.db.repo import audit, get_active_run, sim_now
from haulmind.events.schemas import EventType
from haulmind.mcp_server.schemas import (
    ActionResult,
    DocHit,
    DriverHos,
    FaultEntry,
    FaultHistory,
    FuelTxnInfo,
    FuelTxnsResult,
    HosRisk,
    HosRisksResult,
    IdleVehicle,
    IdleVehiclesResult,
    MetricsPoint,
    NearbyShopsResult,
    RecentEvent,
    RecentEventsResult,
    SearchDocsResult,
    ShopInfo,
    SpareDriver,
    TripInfo,
    VehicleStatus,
)
from haulmind.policy.engine import PolicyEngine, canonical_args
from haulmind.simulator.data import AVG_SPEED_MPH, REEFER_TANK_GAL
from haulmind.util import haversine_mi, new_id, to_naive_utc

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_engine: PolicyEngine | None = None


def policy() -> PolicyEngine:
    global _engine
    if _engine is None:
        _engine = PolicyEngine(get_settings())
    return _engine


def set_policy_engine(engine: PolicyEngine | None) -> None:
    global _engine
    _engine = engine


# --- shared helpers --------------------------------------------------------------------------


def _trip_info(s: Session, trip_id: str | None, vehicle: Vehicle | None) -> TripInfo | None:
    if not trip_id:
        return None
    t = s.get(Trip, trip_id)
    if t is None:
        return None
    miles_left: float | None = None
    est: int | None = None
    if vehicle is not None and vehicle.lat is not None and vehicle.lon is not None:
        miles_left = round(haversine_mi(vehicle.lat, vehicle.lon, t.dest_lat, t.dest_lon) * 1.17, 1)
        est = round(miles_left / AVG_SPEED_MPH * 60)
    return TripInfo(
        id=t.id,
        origin=t.origin,
        destination=t.destination,
        planned_miles=t.planned_miles,
        miles_to_destination=miles_left,
        est_minutes_to_destination=est,
        start_ts=t.start_ts,
        eta_ts=t.eta_ts,
        status=t.status,
        load_ref=t.load_ref,
    )


def _latest_est_to_dest(s: Session, driver_id: str, now: datetime) -> int | None:
    row = s.execute(
        select(EventRow.payload_json)
        .where(
            EventRow.driver_id == driver_id,
            EventRow.type == EventType.HOS_UPDATE.value,
            EventRow.ts <= now,
        )
        .order_by(EventRow.ts.desc(), EventRow.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row:
        return None
    est = row.get("est_minutes_to_destination")
    return int(est) if est is not None else None


def _vehicle_required(s: Session, vehicle_id: str) -> Vehicle:
    v = s.get(Vehicle, vehicle_id)
    if v is None:
        raise ValueError(f"unknown vehicle {vehicle_id}")
    return v


def _driver_required(s: Session, driver_id: str) -> Driver:
    d = s.get(Driver, driver_id)
    if d is None:
        raise ValueError(f"unknown driver {driver_id}")
    return d


# --- read tools ------------------------------------------------------------------------------


def get_vehicle_status(vehicle_id: str) -> VehicleStatus:
    with session_scope() as s:
        v = _vehicle_required(s, vehicle_id)
        now = sim_now(s)
        driver = s.get(Driver, v.current_driver_id) if v.current_driver_id else None
        faults = (
            s.execute(
                select(FaultCode.code).where(
                    FaultCode.vehicle_id == v.id, FaultCode.active.is_(True)
                )
            )
            .scalars()
            .all()
        )
        wos = s.execute(
            select(func.count(WorkOrder.id)).where(
                WorkOrder.vehicle_id == v.id, WorkOrder.status.in_(("open", "scheduled"))
            )
        ).scalar_one()
        idle = None
        if v.idle_since_ts is not None and (v.speed_mph or 0) <= 2:
            idle = int((now - v.idle_since_ts).total_seconds() // 60)
        reefer_gal = REEFER_TANK_GAL if v.has_reefer else 0.0
        return VehicleStatus(
            vehicle_id=v.id,
            unit_number=v.unit_number,
            make=v.make,
            model=v.model,
            year=v.year,
            depot=v.depot,
            status=v.status,
            has_reefer=v.has_reefer,
            fuel_tank_gal=v.fuel_tank_gal,
            reefer_tank_gal=reefer_gal,
            total_fuel_capacity_gal=v.fuel_tank_gal + reefer_gal,
            current_driver_id=v.current_driver_id,
            current_driver_name=driver.name if driver else None,
            current_trip=_trip_info(s, v.current_trip_id, v),
            lat=v.lat,
            lon=v.lon,
            speed_mph=v.speed_mph,
            heading=v.heading,
            odometer_mi=round(v.odometer_mi, 1),
            engine_on=v.engine_on,
            coolant_temp_f=v.coolant_temp_f,
            oil_pressure_psi=v.oil_pressure_psi,
            battery_v=v.battery_v,
            def_level_pct=v.def_level_pct,
            fuel_level_pct=v.fuel_level_pct,
            last_seen_ts=v.last_seen_ts,
            idle_minutes=idle,
            active_fault_codes=sorted(set(faults)),
            open_work_orders=int(wos),
            sim_now=now,
        )


def get_driver_hos(driver_id: str) -> DriverHos:
    with session_scope() as s:
        d = _driver_required(s, driver_id)
        now = sim_now(s)
        h = s.get(HosStatus, d.id) or HosStatus(driver_id=d.id)
        v = s.get(Vehicle, d.current_vehicle_id) if d.current_vehicle_id else None
        trip = _trip_info(s, v.current_trip_id if v else None, v)
        est = trip.est_minutes_to_destination if trip else None
        if est is None:
            est = _latest_est_to_dest(s, d.id, now)
        margin = h.drive_min_remaining - est if est is not None else None
        msgs = s.execute(
            select(func.count(DriverMessage.id)).where(
                DriverMessage.driver_id == d.id,
                DriverMessage.created_ts >= now - timedelta(hours=24),
            )
        ).scalar_one()
        return DriverHos(
            driver_id=d.id,
            name=d.name,
            duty_status=h.duty_status,
            drive_min_remaining=h.drive_min_remaining,
            duty_min_remaining=h.duty_min_remaining,
            cycle_min_remaining=h.cycle_min_remaining,
            minutes_since_break=h.minutes_since_break,
            updated_ts=h.updated_ts,
            current_vehicle_id=d.current_vehicle_id,
            current_trip=trip,
            est_minutes_to_destination=est,
            margin_min=margin,
            at_risk=bool(h.duty_status == "driving" and margin is not None and margin < 15),
            messages_last_24h=int(msgs),
            sim_now=now,
        )


def find_idle_vehicles(
    min_idle_minutes: int = 30,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 10,
) -> IdleVehiclesResult:
    with session_scope() as s:
        now = sim_now(s)
        vehicles = s.execute(select(Vehicle).where(Vehicle.status == "active")).scalars().all()
        idle: list[IdleVehicle] = []
        for v in vehicles:
            if (v.speed_mph or 0) > 2 or v.idle_since_ts is None:
                continue
            minutes = int((now - v.idle_since_ts).total_seconds() // 60)
            if minutes < min_idle_minutes:
                continue
            d = s.get(Driver, v.current_driver_id) if v.current_driver_id else None
            h = s.get(HosStatus, d.id) if d else None
            dist = None
            if (
                near_lat is not None
                and near_lon is not None
                and v.lat is not None
                and v.lon is not None
            ):
                dist = round(haversine_mi(near_lat, near_lon, v.lat, v.lon), 1)
            idle.append(
                IdleVehicle(
                    vehicle_id=v.id,
                    unit_number=v.unit_number,
                    depot=v.depot,
                    idle_minutes=minutes,
                    lat=v.lat,
                    lon=v.lon,
                    driver_id=d.id if d else None,
                    driver_name=d.name if d else None,
                    driver_drive_min_remaining=h.drive_min_remaining if h else None,
                    distance_mi=dist,
                )
            )
        idle.sort(
            key=lambda x: (x.distance_mi if x.distance_mi is not None else 1e9, -x.idle_minutes)
        )
        spares: list[SpareDriver] = []
        depots = {v.depot: (v.lat, v.lon) for v in vehicles if v.lat is not None}
        for d in s.execute(
            select(Driver).where(Driver.current_vehicle_id.is_(None), Driver.status == "active")
        ).scalars():
            h = s.get(HosStatus, d.id)
            dist = None
            if near_lat is not None and near_lon is not None and d.depot in depots:
                dlat, dlon = depots[d.depot]
                if dlat is not None and dlon is not None:
                    dist = round(haversine_mi(near_lat, near_lon, dlat, dlon), 1)
            spares.append(
                SpareDriver(
                    driver_id=d.id,
                    name=d.name,
                    depot=d.depot,
                    drive_min_remaining=h.drive_min_remaining if h else 660,
                    duty_status=h.duty_status if h else "off_duty",
                    distance_mi=dist,
                )
            )
        spares.sort(
            key=lambda x: (
                x.distance_mi if x.distance_mi is not None else 1e9,
                -x.drive_min_remaining,
            )
        )
        return IdleVehiclesResult(
            idle_vehicles=idle[:limit], spare_drivers=spares[:limit], sim_now=now
        )


def list_hos_risks(threshold_min: int = 120) -> HosRisksResult:
    with session_scope() as s:
        now = sim_now(s)
        rows = s.execute(
            select(HosStatus, Driver)
            .join(Driver, Driver.id == HosStatus.driver_id)
            .where(
                HosStatus.duty_status == "driving", HosStatus.drive_min_remaining <= threshold_min
            )
        ).all()
        risks: list[HosRisk] = []
        for h, d in rows:
            v = s.get(Vehicle, d.current_vehicle_id) if d.current_vehicle_id else None
            trip = _trip_info(s, v.current_trip_id if v else None, v)
            est = trip.est_minutes_to_destination if trip else _latest_est_to_dest(s, d.id, now)
            risks.append(
                HosRisk(
                    driver_id=d.id,
                    name=d.name,
                    vehicle_id=d.current_vehicle_id,
                    trip_id=trip.id if trip else None,
                    destination=trip.destination if trip else None,
                    drive_min_remaining=h.drive_min_remaining,
                    est_minutes_to_destination=est,
                    margin_min=(h.drive_min_remaining - est) if est is not None else None,
                    duty_status=h.duty_status,
                )
            )
        risks.sort(key=lambda r: r.margin_min if r.margin_min is not None else 1e9)
        return HosRisksResult(risks=risks, threshold_min=threshold_min, sim_now=now)


def get_fault_history(vehicle_id: str, hours: int = 72) -> FaultHistory:
    with session_scope() as s:
        v = _vehicle_required(s, vehicle_id)
        now = sim_now(s)
        since = now - timedelta(hours=hours)
        faults = (
            s.execute(
                select(FaultCode)
                .where(FaultCode.vehicle_id == v.id, FaultCode.last_ts >= since)
                .order_by(FaultCode.last_ts.desc())
            )
            .scalars()
            .all()
        )
        metrics_rows = s.execute(
            select(EventRow.ts, EventRow.payload_json)
            .where(
                EventRow.vehicle_id == v.id,
                EventRow.type == EventType.ENGINE_METRICS.value,
                EventRow.ts >= now - timedelta(hours=8),
                EventRow.ts <= now,
            )
            .order_by(EventRow.ts.desc())
            .limit(24)
        ).all()
        points = [
            MetricsPoint(
                ts=ts,
                coolant_temp_f=float(p.get("coolant_temp_f", 0)),
                battery_v=float(p.get("battery_v", 0)),
                oil_pressure_psi=float(p.get("oil_pressure_psi", 0)),
                def_level_pct=float(p.get("def_level_pct", 0)),
            )
            for ts, p in reversed(metrics_rows)
            if p.get("engine_on", True)
        ]
        cool_delta = bat_delta = None
        if len(points) >= 4:
            head, tail = points[: max(1, len(points) // 4)], points[-max(1, len(points) // 4) :]
            cool_delta = round(
                sum(p.coolant_temp_f for p in tail) / len(tail)
                - sum(p.coolant_temp_f for p in head) / len(head),
                1,
            )
            bat_delta = round(
                sum(p.battery_v for p in tail) / len(tail)
                - sum(p.battery_v for p in head) / len(head),
                2,
            )
        active = sorted({f.code for f in faults if f.active})
        max_sev = "none"
        for f in faults:
            if f.active and SEVERITY_RANK.get(f.severity, 0) >= SEVERITY_RANK.get(max_sev, -1):
                max_sev = f.severity
        return FaultHistory(
            vehicle_id=v.id,
            faults=[
                FaultEntry(
                    code=f.code,
                    description=f.description,
                    system=f.system,
                    severity=f.severity,
                    first_ts=f.first_ts,
                    last_ts=f.last_ts,
                    occurrences=f.occurrences,
                    active=f.active,
                )
                for f in faults
            ],
            recent_metrics=points[-12:],
            coolant_temp_delta_6h=cool_delta,
            battery_v_delta_6h=bat_delta,
            active_codes=active,
            max_active_severity=max_sev,
            sim_now=now,
        )


def search_fault_docs(query: str, codes: list[str] | None = None, k: int = 3) -> SearchDocsResult:
    from haulmind.rag.index import search

    hits = search(query, k=max(1, min(k, 6)), codes=codes)
    return SearchDocsResult(
        query=query,
        hits=[
            DocHit(
                source=h.chunk.source,
                title=h.chunk.title,
                section=h.chunk.section,
                snippet=h.chunk.text[:600],
                score=round(h.score, 3),
                codes=h.chunk.codes,
                citation=h.citation,
            )
            for h in hits
        ],
    )


def find_nearby_shops(
    vehicle_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    specialty: str | None = None,
    limit: int = 3,
) -> NearbyShopsResult:
    with session_scope() as s:
        if vehicle_id:
            v = _vehicle_required(s, vehicle_id)
            lat, lon = v.lat, v.lon
        if lat is None or lon is None:
            raise ValueError("provide vehicle_id or lat/lon")
        shops = s.execute(select(Shop)).scalars().all()
        out: list[ShopInfo] = []
        for sh in shops:
            match = bool(specialty) and specialty in (sh.specialties or [])
            out.append(
                ShopInfo(
                    shop_id=sh.id,
                    name=sh.name,
                    city=sh.city,
                    distance_mi=round(haversine_mi(lat, lon, sh.lat, sh.lon), 1),
                    specialties=list(sh.specialties or []),
                    rating=sh.rating,
                    hourly_rate_usd=sh.hourly_rate_usd,
                    open_24h=sh.open_24h,
                    specialty_match=match,
                )
            )
        out.sort(key=lambda x: (not x.specialty_match if specialty else False, x.distance_mi))
        return NearbyShopsResult(origin={"lat": lat, "lon": lon}, shops=out[:limit])


def get_fuel_transactions(vehicle_id: str, hours: int = 72) -> FuelTxnsResult:
    with session_scope() as s:
        v = _vehicle_required(s, vehicle_id)
        now = sim_now(s)
        rows = (
            s.execute(
                select(FuelTransaction)
                .where(
                    FuelTransaction.vehicle_id == v.id,
                    FuelTransaction.ts >= now - timedelta(hours=hours),
                )
                .order_by(FuelTransaction.ts.asc())
            )
            .scalars()
            .all()
        )
        reefer_gal = REEFER_TANK_GAL if v.has_reefer else 0.0
        total_cap = v.fuel_tank_gal + reefer_gal
        out: list[FuelTxnInfo] = []
        prev_ts: datetime | None = None
        for t in rows:
            dist = None
            if t.vehicle_lat_at_ts is not None and t.vehicle_lon_at_ts is not None:
                dist = round(
                    haversine_mi(t.lat, t.lon, t.vehicle_lat_at_ts, t.vehicle_lon_at_ts), 1
                )
            gap = int((t.ts - prev_ts).total_seconds() // 60) if prev_ts else None
            out.append(
                FuelTxnInfo(
                    transaction_id=t.id,
                    ts=t.ts,
                    vehicle_id=t.vehicle_id,
                    driver_id=t.driver_id,
                    card_id=t.card_id,
                    gallons=t.gallons,
                    amount_usd=t.amount_usd,
                    price_per_gal=t.price_per_gal,
                    station=t.station,
                    distance_from_vehicle_mi=dist,
                    tank_capacity_gal=v.fuel_tank_gal,
                    total_capacity_gal=total_cap,
                    exceeds_tank=t.gallons > v.fuel_tank_gal * 1.02,
                    exceeds_total_capacity=t.gallons > total_cap * 1.02,
                    minutes_since_previous_fill=gap,
                    flagged=t.flagged,
                    flag_reason=t.flag_reason,
                )
            )
            prev_ts = t.ts
        card_ids = {t.card_id for t in rows}
        prior = 0
        if card_ids:
            prior = int(
                s.execute(
                    select(func.count(FuelTransaction.id)).where(
                        FuelTransaction.card_id.in_(card_ids), FuelTransaction.flagged.is_(True)
                    )
                ).scalar_one()
            )
        return FuelTxnsResult(
            vehicle_id=v.id, has_reefer=v.has_reefer, transactions=out, prior_flags_on_card=prior
        )


def _summarize_event(type_: str, p: dict[str, Any]) -> str:
    if type_ == "hos_update":
        return f"{p.get('duty_status')} drive_rem={p.get('drive_min_remaining')} est_to_dest={p.get('est_minutes_to_destination')}"
    if type_ == "dtc_fault":
        return (
            f"{p.get('code')} {'active' if p.get('active') else 'cleared'} sev={p.get('severity')}"
        )
    if type_ == "engine_metrics":
        return f"coolant={p.get('coolant_temp_f')}F batt={p.get('battery_v')}V oil={p.get('oil_pressure_psi')}psi"
    if type_ == "fuel_transaction":
        return f"{p.get('gallons')} gal at {p.get('station')} ${p.get('amount_usd')}"
    if type_ == "harsh_event":
        return f"{p.get('harsh_type')} {p.get('g_force')}g at {p.get('speed_mph')} mph"
    if type_ == "gps_ping":
        return f"{p.get('speed_mph')} mph, {p.get('miles_to_destination')} mi to dest"
    if type_ in ("trip_start", "trip_end"):
        return f"{p.get('trip_id')} {p.get('origin', '')}->{p.get('destination', '')}".strip()
    return ", ".join(f"{k}={v}" for k, v in list(p.items())[:4] if k != "kind")


def get_recent_events(
    vehicle_id: str | None = None,
    driver_id: str | None = None,
    hours: int = 6,
    types: list[str] | None = None,
    limit: int = 40,
) -> RecentEventsResult:
    with session_scope() as s:
        now = sim_now(s)
        q = select(EventRow).where(EventRow.ts >= now - timedelta(hours=hours), EventRow.ts <= now)
        if vehicle_id:
            q = q.where(EventRow.vehicle_id == vehicle_id)
        if driver_id:
            q = q.where(EventRow.driver_id == driver_id)
        q = (
            q.where(EventRow.type.in_(types))
            if types
            else q.where(EventRow.type != EventType.GPS_PING.value)
        )
        rows = s.execute(q.order_by(EventRow.ts.desc()).limit(limit)).scalars().all()
        return RecentEventsResult(
            events=[
                RecentEvent(
                    seq=r.seq,
                    ts=r.ts,
                    type=r.type,
                    vehicle_id=r.vehicle_id,
                    driver_id=r.driver_id,
                    summary=_summarize_event(r.type, r.payload_json),
                )
                for r in reversed(rows)
            ]
        )


# --- write tools (policy-gated) --------------------------------------------------------------

Executor = Callable[[Session, dict[str, Any], datetime], dict[str, Any]]


def gated(
    tool_name: str, args: dict[str, Any], execute: Executor, *, caller: str = "mcp-client"
) -> ActionResult:
    """Run ``execute`` only if the policy engine allows it; otherwise queue/recommend/refuse.

    The ``approval_id`` (if any) is consumed inside the same transaction as the execution.
    """
    approval_id = args.get("approval_id")
    incident_id = args.get("incident_id")
    clean_args = canonical_args(tool_name, args)
    exec_args = {**clean_args, "_incident_id": incident_id}
    with session_scope() as s:
        now = sim_now(s)
        run = get_active_run(s)
        run_id = run.id if run else None
        decision = policy().authorize(
            s, tool_name, clean_args, approval_id=approval_id, caller=caller, now=now
        )
        tier = decision.tier
        if decision.allowed:
            details = execute(s, exec_args, now)
            action_id = _mark_action_executed(s, approval_id, details, now)
            audit(
                s,
                actor=caller,
                action=f"{tool_name}:executed",
                entity_type="tool",
                entity_id=details.get("id", tool_name),
                details={"args": clean_args, "approval_id": approval_id, "tier": tier},
                run_id=run_id,
            )
            return ActionResult(
                status="executed",
                tier=tier,
                tool_name=tool_name,
                action_id=action_id,
                message=f"{tool_name} executed",
                details=details,
            )
        if decision.outcome in ("pending_approval", "recommend_only"):
            status = "pending" if decision.outcome == "pending_approval" else "recommended"
            action = ProposedAction(
                id=new_id("ACT"),
                incident_id=incident_id,
                run_id=run_id,
                tool_name=tool_name,
                args_json=clean_args,
                args_hash=decision.args_hash,
                tier=tier,
                status=status,
                title=f"{tool_name} requested by {caller}",
                confidence=0.5,
                reasoning="Proposed directly through the MCP server; awaiting a human decision.",
                proposed_by=caller,
                created_ts=to_naive_utc(datetime.now(tz=__import__("datetime").UTC)),
                sim_ts=now,
                vehicle_id=clean_args.get("vehicle_id"),
                driver_id=clean_args.get("driver_id") or clean_args.get("from_driver_id"),
            )
            s.add(action)
            audit(
                s,
                actor=caller,
                action=f"{tool_name}:{decision.outcome}",
                entity_type="action",
                entity_id=action.id,
                details={"args": clean_args, "reason": decision.reason},
                run_id=run_id,
            )
            return ActionResult(
                status=decision.outcome,
                tier=tier,
                tool_name=tool_name,
                action_id=action.id,
                message=decision.reason or f"{tool_name} queued for approval",
                details={"args_hash": decision.args_hash},
            )
        audit(
            s,
            actor=caller,
            action=f"{tool_name}:{decision.outcome}",
            entity_type="tool",
            entity_id=tool_name,
            details={"args": clean_args, "reason": decision.reason, "approval_id": approval_id},
            run_id=run_id,
        )
        return ActionResult(
            status=decision.outcome,
            tier=tier,
            tool_name=tool_name,
            message=decision.reason,
            details={"args_hash": decision.args_hash},
        )


def _mark_action_executed(
    s: Session, approval_id: str | None, details: dict[str, Any], now: datetime
) -> str | None:
    if not approval_id:
        return None
    from haulmind.db.models import Approval

    ap = s.get(Approval, approval_id)
    if ap is None:
        return None
    act = s.get(ProposedAction, ap.action_id)
    if act is not None:
        act.status = "executed"
        act.executed_ts = to_naive_utc(datetime.now(tz=__import__("datetime").UTC))
        act.result_json = details
        return act.id
    return None


def _exec_message_driver(s: Session, a: dict[str, Any], now: datetime) -> dict[str, Any]:
    d = _driver_required(s, a["driver_id"])
    msg = DriverMessage(
        id=new_id("MSG"),
        driver_id=d.id,
        incident_id=a.get("_incident_id"),
        body=a["body"],
        urgency=a.get("urgency", "normal"),
        created_ts=now,
        created_by="agent",
    )
    s.add(msg)
    return {"id": msg.id, "driver": d.name, "delivered": True, "urgency": msg.urgency}


def _exec_flag_fuel(s: Session, a: dict[str, Any], now: datetime) -> dict[str, Any]:
    t = s.get(FuelTransaction, a["transaction_id"])
    if t is None:
        raise ValueError(f"unknown transaction {a['transaction_id']}")
    t.flagged = True
    t.flag_reason = str(a.get("reason", ""))[:200]
    t.flagged_ts = now
    return {
        "id": t.id,
        "vehicle_id": t.vehicle_id,
        "card_id": t.card_id,
        "gallons": t.gallons,
        "flagged": True,
    }


def _exec_work_order(s: Session, a: dict[str, Any], now: datetime) -> dict[str, Any]:
    v = _vehicle_required(s, a["vehicle_id"])
    shop = s.get(Shop, a["shop_id"]) if a.get("shop_id") else None
    wo = WorkOrder(
        id=new_id("WO"),
        vehicle_id=v.id,
        incident_id=a.get("_incident_id"),
        shop_id=shop.id if shop else None,
        priority=a.get("priority", "normal"),
        description=a["description"],
        fault_codes=list(a.get("fault_codes") or []),
        status="open",
        created_ts=now,
        created_by="agent",
    )
    s.add(wo)
    if a.get("priority") in ("urgent", "critical"):
        v.status = "in_shop"
    return {
        "id": wo.id,
        "vehicle_id": v.id,
        "shop": shop.name if shop else None,
        "priority": wo.priority,
    }


def _exec_reassignment(s: Session, a: dict[str, Any], now: datetime) -> dict[str, Any]:
    trip = s.get(Trip, a["trip_id"])
    if trip is None:
        raise ValueError(f"unknown trip {a['trip_id']}")
    to_d = _driver_required(s, a["to_driver_id"])
    from_d = _driver_required(s, a["from_driver_id"])
    r = Reassignment(
        id=new_id("RA"),
        trip_id=trip.id,
        from_driver_id=from_d.id,
        to_driver_id=to_d.id,
        reason=a.get("reason", ""),
        status="approved",
        created_ts=now,
        incident_id=a.get("_incident_id"),
    )
    s.add(r)
    trip.driver_id = to_d.id
    return {"id": r.id, "trip_id": trip.id, "from": from_d.name, "to": to_d.name}


def _never(s: Session, a: dict[str, Any], now: datetime) -> dict[str, Any]:  # pragma: no cover
    raise RuntimeError("recommend-only tools are never executed")


def message_driver(
    driver_id: str,
    body: str,
    urgency: str = "normal",
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "message_driver",
        {
            "driver_id": driver_id,
            "body": body,
            "urgency": urgency,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _exec_message_driver,
        caller=caller,
    )


def flag_fuel_transaction(
    transaction_id: str,
    reason: str,
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "flag_fuel_transaction",
        {
            "transaction_id": transaction_id,
            "reason": reason,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _exec_flag_fuel,
        caller=caller,
    )


def create_work_order(
    vehicle_id: str,
    description: str,
    priority: str = "normal",
    shop_id: str | None = None,
    fault_codes: list[str] | None = None,
    estimated_cost_usd: float | None = None,
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "create_work_order",
        {
            "vehicle_id": vehicle_id,
            "description": description,
            "priority": priority,
            "shop_id": shop_id,
            "fault_codes": fault_codes or [],
            "estimated_cost_usd": estimated_cost_usd,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _exec_work_order,
        caller=caller,
    )


def propose_reassignment(
    trip_id: str,
    from_driver_id: str,
    to_driver_id: str,
    reason: str,
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "propose_reassignment",
        {
            "trip_id": trip_id,
            "from_driver_id": from_driver_id,
            "to_driver_id": to_driver_id,
            "reason": reason,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _exec_reassignment,
        caller=caller,
    )


def suspend_fuel_card(
    card_id: str,
    reason: str,
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "suspend_fuel_card",
        {
            "card_id": card_id,
            "reason": reason,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _never,
        caller=caller,
    )


def set_vehicle_out_of_service(
    vehicle_id: str,
    reason: str,
    incident_id: str | None = None,
    approval_id: str | None = None,
    caller: str = "mcp-client",
) -> ActionResult:
    return gated(
        "set_vehicle_out_of_service",
        {
            "vehicle_id": vehicle_id,
            "reason": reason,
            "incident_id": incident_id,
            "approval_id": approval_id,
        },
        _never,
        caller=caller,
    )

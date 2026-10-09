"""SQLAlchemy ORM models. See PLAN.md section 3 for the table overview."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --- runs / replay ----------------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="generated", index=True)
    sim_start: Mapped[datetime | None] = mapped_column(DateTime)
    sim_now: Mapped[datetime | None] = mapped_column(DateTime)
    event_count: Mapped[int] = mapped_column(Integer, default=0)


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"

    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("runs.id"), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RunStat(Base):
    """Counters per run (events filtered, LLM calls, ...). Upserted by the orchestrator."""

    __tablename__ = "run_stats"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_run_stats_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("runs.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[float] = mapped_column(Float, default=0.0)


# --- fleet entities ---------------------------------------------------------------------------


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    unit_number: Mapped[str] = mapped_column(String(20), nullable=False)
    vin: Mapped[str] = mapped_column(String(20), nullable=False)
    make: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(40), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    vehicle_class: Mapped[str] = mapped_column(String(20), default="tractor")
    fuel_tank_gal: Mapped[float] = mapped_column(Float, default=150.0)
    has_reefer: Mapped[bool] = mapped_column(Boolean, default=False)
    depot: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|in_shop|oos
    current_driver_id: Mapped[str | None] = mapped_column(String(20))
    current_trip_id: Mapped[str | None] = mapped_column(String(40))
    # live telemetry projection
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    speed_mph: Mapped[float | None] = mapped_column(Float)
    heading: Mapped[float | None] = mapped_column(Float)
    odometer_mi: Mapped[float] = mapped_column(Float, default=0.0)
    engine_on: Mapped[bool] = mapped_column(Boolean, default=False)
    coolant_temp_f: Mapped[float | None] = mapped_column(Float)
    oil_pressure_psi: Mapped[float | None] = mapped_column(Float)
    battery_v: Mapped[float | None] = mapped_column(Float)
    def_level_pct: Mapped[float | None] = mapped_column(Float)
    fuel_level_pct: Mapped[float | None] = mapped_column(Float)
    last_seen_ts: Mapped[datetime | None] = mapped_column(DateTime)
    idle_since_ts: Mapped[datetime | None] = mapped_column(DateTime)


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    license_state: Mapped[str] = mapped_column(String(2), nullable=False)
    depot: Mapped[str] = mapped_column(String(40), index=True)
    fuel_card_id: Mapped[str] = mapped_column(String(20), nullable=False)
    fuel_card_status: Mapped[str] = mapped_column(String(20), default="active")
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_vehicle_id: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str] = mapped_column(String(20), default="")


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (Index("ix_trips_vehicle_start", "vehicle_id", "start_ts"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vehicle_id: Mapped[str] = mapped_column(String(20), ForeignKey("vehicles.id"), nullable=False)
    driver_id: Mapped[str] = mapped_column(String(20), ForeignKey("drivers.id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(80), nullable=False)
    destination: Mapped[str] = mapped_column(String(80), nullable=False)
    dest_lat: Mapped[float] = mapped_column(Float, nullable=False)
    dest_lon: Mapped[float] = mapped_column(Float, nullable=False)
    planned_miles: Mapped[float] = mapped_column(Float, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    eta_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="in_progress", index=True)
    load_ref: Mapped[str] = mapped_column(String(40), default="")


class HosStatus(Base):
    __tablename__ = "hos_status"

    driver_id: Mapped[str] = mapped_column(String(20), ForeignKey("drivers.id"), primary_key=True)
    duty_status: Mapped[str] = mapped_column(String(20), default="off_duty")
    drive_min_remaining: Mapped[int] = mapped_column(Integer, default=660)
    duty_min_remaining: Mapped[int] = mapped_column(Integer, default=840)
    cycle_min_remaining: Mapped[int] = mapped_column(Integer, default=4200)
    minutes_since_break: Mapped[int] = mapped_column(Integer, default=0)
    updated_ts: Mapped[datetime | None] = mapped_column(DateTime)


class FaultCode(Base):
    __tablename__ = "fault_codes"
    __table_args__ = (
        Index("ix_fault_vehicle_last", "vehicle_id", "last_ts"),
        UniqueConstraint("vehicle_id", "code", "first_ts", name="uq_fault_occurrence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(String(20), ForeignKey("vehicles.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    system: Mapped[str] = mapped_column(String(40), default="")
    severity: Mapped[str] = mapped_column(String(10), default="low")  # low|medium|high|critical
    first_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FuelTransaction(Base):
    __tablename__ = "fuel_transactions"
    __table_args__ = (Index("ix_fuel_vehicle_ts", "vehicle_id", "ts"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(20), ForeignKey("vehicles.id"), nullable=False)
    driver_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("drivers.id"))
    card_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    gallons: Mapped[float] = mapped_column(Float, nullable=False)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    price_per_gal: Mapped[float] = mapped_column(Float, nullable=False)
    station: Mapped[str] = mapped_column(String(80), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    odometer_mi: Mapped[float | None] = mapped_column(Float)
    vehicle_lat_at_ts: Mapped[float | None] = mapped_column(Float)
    vehicle_lon_at_ts: Mapped[float | None] = mapped_column(Float)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flag_reason: Mapped[str] = mapped_column(String(200), default="")
    flagged_ts: Mapped[datetime | None] = mapped_column(DateTime)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    city: Mapped[str] = mapped_column(String(60), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    specialties: Mapped[list[Any]] = mapped_column(JSON, default=list)
    rating: Mapped[float] = mapped_column(Float, default=4.0)
    hourly_rate_usd: Mapped[float] = mapped_column(Float, default=140.0)
    open_24h: Mapped[bool] = mapped_column(Boolean, default=False)


# --- event stream -----------------------------------------------------------------------------


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_events_run_seq"),
        Index("ix_events_vehicle_ts", "vehicle_id", "ts"),
        Index("ix_events_driver_ts", "driver_id", "ts"),
        Index("ix_events_type_ts", "type", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    vehicle_id: Mapped[str | None] = mapped_column(String(20))
    driver_id: Mapped[str | None] = mapped_column(String(20))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)


# --- agent outputs ----------------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_run_opened", "run_id", "opened_ts"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("runs.id"), nullable=False)
    cluster_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    vehicle_id: Mapped[str | None] = mapped_column(String(20), index=True)
    driver_id: Mapped[str | None] = mapped_column(String(20), index=True)
    opened_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sim_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_ts: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    triage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signals_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    event_ids_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(40))
    llm_requests: Mapped[int] = mapped_column(Integer, default=0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    agent_mode: Mapped[str] = mapped_column(String(10), default="multi")


class ProposedAction(Base):
    __tablename__ = "proposed_actions"
    __table_args__ = (Index("ix_actions_status_created", "status", "created_ts"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("incidents.id"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    args_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | approved | rejected | auto_executed | executed | recommended | blocked | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verifier_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposed_by: Mapped[str] = mapped_column(String(40), default="agent")
    created_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sim_ts: Mapped[datetime | None] = mapped_column(DateTime)
    decided_ts: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(60))
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    executed_ts: Mapped[datetime | None] = mapped_column(DateTime)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    vehicle_id: Mapped[str | None] = mapped_column(String(20), index=True)
    driver_id: Mapped[str | None] = mapped_column(String(20), index=True)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("proposed_actions.id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # approve|reject
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(60), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ToolCall(Base):
    """Every write-tool invocation at the policy gateway, allowed or not (rate limiting)."""

    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_tool_ts", "tool_name", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(80), default="", index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    outcome: Mapped[str] = mapped_column(String(30), default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    caller: Mapped[str] = mapped_column(String(40), default="")


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("vehicles.id"), nullable=False, index=True
    )
    incident_id: Mapped[str | None] = mapped_column(String(40))
    shop_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("shops.id"))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    fault_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(40), default="agent")


class DriverMessage(Base):
    __tablename__ = "driver_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    driver_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("drivers.id"), nullable=False, index=True
    )
    incident_id: Mapped[str | None] = mapped_column(String(40))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(String(10), default="normal")
    created_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(40), default="agent")


class Reassignment(Base):
    __tablename__ = "reassignments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trip_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    from_driver_id: Mapped[str] = mapped_column(String(20), nullable=False)
    to_driver_id: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    incident_id: Mapped[str | None] = mapped_column(String(40))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(40), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)


class AgentMemory(Base):
    __tablename__ = "agent_memory"
    __table_args__ = (UniqueConstraint("scope", "scope_id", "key", name="uq_memory_scope_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # driver|vehicle|global
    scope_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# --- tracing ----------------------------------------------------------------------------------


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    started_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ended_ts: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default="ok")
    llm_requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    attrs_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Span(Base):
    __tablename__ = "spans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("traces.id"), nullable=False, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # agent_step|tool_call|llm_call
    started_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_ts: Mapped[datetime | None] = mapped_column(DateTime)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="ok")
    error: Mapped[str] = mapped_column(Text, default="")
    attrs_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(Integer, default=0)

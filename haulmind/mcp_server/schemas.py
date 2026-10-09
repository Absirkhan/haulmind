"""Typed outputs for every MCP tool. Inputs are the tool function signatures themselves."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TripInfo(BaseModel):
    id: str
    origin: str
    destination: str
    planned_miles: float
    miles_to_destination: float | None = None
    est_minutes_to_destination: int | None = None
    start_ts: datetime
    eta_ts: datetime
    status: str
    load_ref: str = ""


class VehicleStatus(BaseModel):
    vehicle_id: str
    unit_number: str
    make: str
    model: str
    year: int
    depot: str
    status: str
    has_reefer: bool
    fuel_tank_gal: float
    reefer_tank_gal: float
    total_fuel_capacity_gal: float = Field(description="Tractor tank plus reefer tank, if any")
    current_driver_id: str | None
    current_driver_name: str | None
    current_trip: TripInfo | None
    lat: float | None
    lon: float | None
    speed_mph: float | None
    heading: float | None
    odometer_mi: float
    engine_on: bool
    coolant_temp_f: float | None
    oil_pressure_psi: float | None
    battery_v: float | None
    def_level_pct: float | None
    fuel_level_pct: float | None
    last_seen_ts: datetime | None
    idle_minutes: int | None
    active_fault_codes: list[str]
    open_work_orders: int
    sim_now: datetime


class DriverHos(BaseModel):
    driver_id: str
    name: str
    duty_status: str
    drive_min_remaining: int
    duty_min_remaining: int
    cycle_min_remaining: int
    minutes_since_break: int
    updated_ts: datetime | None
    current_vehicle_id: str | None
    current_trip: TripInfo | None
    est_minutes_to_destination: int | None
    margin_min: int | None = Field(
        default=None, description="drive_min_remaining minus est_minutes_to_destination"
    )
    at_risk: bool
    messages_last_24h: int
    sim_now: datetime


class IdleVehicle(BaseModel):
    vehicle_id: str
    unit_number: str
    depot: str
    idle_minutes: int
    lat: float | None
    lon: float | None
    driver_id: str | None
    driver_name: str | None
    driver_drive_min_remaining: int | None
    distance_mi: float | None = None


class SpareDriver(BaseModel):
    driver_id: str
    name: str
    depot: str
    drive_min_remaining: int
    duty_status: str
    distance_mi: float | None = None


class IdleVehiclesResult(BaseModel):
    idle_vehicles: list[IdleVehicle]
    spare_drivers: list[SpareDriver]
    sim_now: datetime


class HosRisk(BaseModel):
    driver_id: str
    name: str
    vehicle_id: str | None
    trip_id: str | None
    destination: str | None
    drive_min_remaining: int
    est_minutes_to_destination: int | None
    margin_min: int | None
    duty_status: str


class HosRisksResult(BaseModel):
    risks: list[HosRisk]
    threshold_min: int
    sim_now: datetime


class FaultEntry(BaseModel):
    code: str
    description: str
    system: str
    severity: str
    first_ts: datetime
    last_ts: datetime
    occurrences: int
    active: bool


class MetricsPoint(BaseModel):
    ts: datetime
    coolant_temp_f: float
    battery_v: float
    oil_pressure_psi: float
    def_level_pct: float


class FaultHistory(BaseModel):
    vehicle_id: str
    faults: list[FaultEntry]
    recent_metrics: list[MetricsPoint]
    coolant_temp_delta_6h: float | None
    battery_v_delta_6h: float | None
    active_codes: list[str]
    max_active_severity: str
    sim_now: datetime


class DocHit(BaseModel):
    source: str
    title: str
    section: str
    snippet: str
    score: float
    codes: list[str]
    citation: str


class SearchDocsResult(BaseModel):
    query: str
    hits: list[DocHit]


class ShopInfo(BaseModel):
    shop_id: str
    name: str
    city: str
    distance_mi: float
    specialties: list[str]
    rating: float
    hourly_rate_usd: float
    open_24h: bool
    specialty_match: bool


class NearbyShopsResult(BaseModel):
    origin: dict[str, float]
    shops: list[ShopInfo]


class FuelTxnInfo(BaseModel):
    transaction_id: str
    ts: datetime
    vehicle_id: str
    driver_id: str | None
    card_id: str
    gallons: float
    amount_usd: float
    price_per_gal: float
    station: str
    distance_from_vehicle_mi: float | None
    tank_capacity_gal: float
    total_capacity_gal: float
    exceeds_tank: bool
    exceeds_total_capacity: bool
    minutes_since_previous_fill: int | None
    flagged: bool
    flag_reason: str


class FuelTxnsResult(BaseModel):
    vehicle_id: str
    has_reefer: bool
    transactions: list[FuelTxnInfo]
    prior_flags_on_card: int


class RecentEvent(BaseModel):
    seq: int
    ts: datetime
    type: str
    vehicle_id: str | None
    driver_id: str | None
    summary: str


class RecentEventsResult(BaseModel):
    events: list[RecentEvent]


ActionStatus = Literal[
    "executed",
    "pending_approval",
    "recommend_only",
    "blocked",
    "rate_limited",
    "approval_invalid",
    "unknown_tool",
]


class ActionResult(BaseModel):
    """Result of a write tool. ``status`` is decided by the policy engine, never by a prompt."""

    status: ActionStatus
    tier: int
    tool_name: str
    action_id: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

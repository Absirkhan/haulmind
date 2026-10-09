"""Typed telematics events. Every event on the bus is an ``Event`` with a typed payload."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    GPS_PING = "gps_ping"
    HARSH_EVENT = "harsh_event"
    SPEEDING = "speeding"
    HOS_UPDATE = "hos_update"
    DTC_FAULT = "dtc_fault"
    ENGINE_METRICS = "engine_metrics"
    FUEL_TRANSACTION = "fuel_transaction"
    TRIP_START = "trip_start"
    TRIP_END = "trip_end"
    IDLE_EVENT = "idle_event"


class Domain(StrEnum):
    SAFETY = "safety"
    MAINTENANCE = "maintenance"
    COMPLIANCE = "compliance"
    SPEND = "spend"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GpsPing(_Payload):
    kind: Literal["gps_ping"] = "gps_ping"
    lat: float
    lon: float
    speed_mph: float = Field(ge=0)
    heading: float = Field(ge=0, lt=360)
    odometer_mi: float = Field(ge=0)
    trip_id: str | None = None
    miles_to_destination: float | None = None


class HarshEvent(_Payload):
    kind: Literal["harsh_event"] = "harsh_event"
    harsh_type: Literal["harsh_brake", "harsh_accel", "harsh_corner"]
    g_force: float = Field(ge=0)
    speed_mph: float = Field(ge=0)
    lat: float
    lon: float
    trip_id: str | None = None


class Speeding(_Payload):
    kind: Literal["speeding"] = "speeding"
    speed_mph: float
    limit_mph: float
    duration_s: int
    lat: float
    lon: float


class HosUpdate(_Payload):
    kind: Literal["hos_update"] = "hos_update"
    duty_status: Literal["off_duty", "sleeper", "driving", "on_duty"]
    drive_min_remaining: int
    duty_min_remaining: int
    cycle_min_remaining: int
    minutes_since_break: int
    trip_id: str | None = None
    est_minutes_to_destination: int | None = None


class DtcFault(_Payload):
    kind: Literal["dtc_fault"] = "dtc_fault"
    code: str
    description: str
    system: str
    severity: Literal["low", "medium", "high", "critical"]
    active: bool = True


class EngineMetrics(_Payload):
    kind: Literal["engine_metrics"] = "engine_metrics"
    coolant_temp_f: float
    oil_pressure_psi: float
    battery_v: float
    def_level_pct: float
    rpm: int
    fuel_level_pct: float
    engine_on: bool = True


class FuelTxn(_Payload):
    kind: Literal["fuel_transaction"] = "fuel_transaction"
    txn_id: str
    card_id: str
    gallons: float = Field(gt=0)
    amount_usd: float = Field(gt=0)
    price_per_gal: float = Field(gt=0)
    station: str
    lat: float
    lon: float
    odometer_mi: float | None = None
    vehicle_lat_at_ts: float | None = None
    vehicle_lon_at_ts: float | None = None


class TripStart(_Payload):
    kind: Literal["trip_start"] = "trip_start"
    trip_id: str
    origin: str
    destination: str
    dest_lat: float
    dest_lon: float
    planned_miles: float
    eta_ts: datetime
    load_ref: str = ""


class TripEnd(_Payload):
    kind: Literal["trip_end"] = "trip_end"
    trip_id: str
    actual_miles: float
    on_time: bool = True


class IdleEvent(_Payload):
    kind: Literal["idle_event"] = "idle_event"
    duration_min: int
    lat: float
    lon: float


Payload = Annotated[
    GpsPing
    | HarshEvent
    | Speeding
    | HosUpdate
    | DtcFault
    | EngineMetrics
    | FuelTxn
    | TripStart
    | TripEnd
    | IdleEvent,
    Field(discriminator="kind"),
]


class Event(BaseModel):
    """A single telematics event. ``seq`` is monotonic within a run so replay is exact."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(ge=0)
    ts: datetime
    type: EventType
    vehicle_id: str | None = None
    driver_id: str | None = None
    payload: Payload

    @property
    def payload_dict(self) -> dict[str, Any]:
        return self.payload.model_dump(mode="json")

    @classmethod
    def from_row(
        cls,
        run_id: str,
        seq: int,
        ts: datetime,
        type_: str,
        vehicle_id: str | None,
        driver_id: str | None,
        payload: dict[str, Any],
    ) -> Event:
        data = dict(payload)
        data.setdefault("kind", type_)
        return cls.model_validate(
            {
                "run_id": run_id,
                "seq": seq,
                "ts": ts,
                "type": type_,
                "vehicle_id": vehicle_id,
                "driver_id": driver_id,
                "payload": data,
            }
        )


# Mapping of event types to the agent domain that owns them (None = pure telemetry).
EVENT_DOMAIN: dict[EventType, Domain | None] = {
    EventType.GPS_PING: None,
    EventType.ENGINE_METRICS: Domain.MAINTENANCE,
    EventType.DTC_FAULT: Domain.MAINTENANCE,
    EventType.HOS_UPDATE: Domain.COMPLIANCE,
    EventType.HARSH_EVENT: Domain.SAFETY,
    EventType.SPEEDING: Domain.SAFETY,
    EventType.FUEL_TRANSACTION: Domain.SPEND,
    EventType.TRIP_START: None,
    EventType.TRIP_END: None,
    EventType.IDLE_EVENT: None,
}

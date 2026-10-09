"""Rule prefilters: what reaches an LLM and what never does."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from haulmind.agents.prefilter import Cluster, prefilter
from haulmind.events.schemas import Domain, Event, EventType

T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def _ev(
    seq: int,
    type_: EventType,
    payload: dict,
    minutes: int = 0,
    vehicle: str = "V-01",
    driver: str = "D-01",
) -> Event:  # type: ignore[type-arg]
    return Event(
        run_id="r",
        seq=seq,
        ts=T0 + timedelta(minutes=minutes),
        type=type_,
        vehicle_id=vehicle,
        driver_id=driver,
        payload={"kind": type_.value, **payload},
    )  # type: ignore[arg-type]


def _hos(
    seq: int, remaining: int, est: int | None, minutes: int = 0, status: str = "driving"
) -> Event:
    return _ev(
        seq,
        EventType.HOS_UPDATE,
        {
            "duty_status": status,
            "drive_min_remaining": remaining,
            "duty_min_remaining": 400,
            "cycle_min_remaining": 2000,
            "minutes_since_break": 100,
            "trip_id": "T-1",
            "est_minutes_to_destination": est,
        },
        minutes,
    )


def _cluster(domain: Domain, events: list[Event], context: dict | None = None) -> Cluster:  # type: ignore[type-arg]
    return Cluster(
        key=f"{domain.value}:x",
        domain=domain,
        vehicle_id="V-01",
        driver_id="D-01",
        events=events,
        trigger=events[-1],
        context=context or {},
    )


def test_hos_plenty_of_time_is_dropped() -> None:
    r = prefilter(_cluster(Domain.COMPLIANCE, [_hos(1, 300, 120)]))
    assert r.verdict == "drop"


def test_hos_clear_deficit_is_definite() -> None:
    r = prefilter(_cluster(Domain.COMPLIANCE, [_hos(1, 60, 130)]))
    assert (
        r.verdict == "definite"
        and r.incident_type == "hos_violation_risk"
        and r.signals["margin_min"] == -70
    )


def test_hos_tight_margin_is_ambiguous_and_memory_can_tighten() -> None:
    c = _cluster(Domain.COMPLIANCE, [_hos(1, 60, 52)])
    assert prefilter(c).verdict == "ambiguous"
    assert prefilter(c, {"margin_threshold": 0}).verdict == "drop"


def test_hos_not_driving_is_dropped() -> None:
    assert (
        prefilter(_cluster(Domain.COMPLIANCE, [_hos(1, 30, 120, status="off_duty")])).verdict
        == "drop"
    )


def test_maintenance_single_low_code_is_dropped_but_recurrence_is_ambiguous() -> None:
    low = {
        "code": "P0128",
        "description": "thermostat",
        "system": "cooling",
        "severity": "low",
        "active": True,
    }
    one = _cluster(Domain.MAINTENANCE, [_ev(1, EventType.DTC_FAULT, low)])
    assert prefilter(one).verdict == "drop"
    two = _cluster(
        Domain.MAINTENANCE,
        [
            _ev(1, EventType.DTC_FAULT, low),
            _ev(2, EventType.DTC_FAULT, {**low, "active": False}, 30),
            _ev(3, EventType.DTC_FAULT, low, 600),
        ],
    )
    r = prefilter(two)
    assert r.verdict == "ambiguous" and r.signals["recurrences"] == 2
    assert prefilter(two, {"nuisance_codes": ["P0128"]}).verdict == "drop"


def test_maintenance_critical_code_with_drift_is_definite() -> None:
    metrics = [
        _ev(
            i,
            EventType.ENGINE_METRICS,
            {
                "coolant_temp_f": 190 + i * 6,
                "oil_pressure_psi": 45,
                "battery_v": 14.0,
                "def_level_pct": 60,
                "rpm": 1300,
                "fuel_level_pct": 50,
            },
            i * 15,
        )
        for i in range(8)
    ]
    crit = _ev(
        99,
        EventType.DTC_FAULT,
        {
            "code": "SPN110-FMI0",
            "description": "overtemp",
            "system": "cooling",
            "severity": "critical",
            "active": True,
        },
        120,
    )
    r = prefilter(_cluster(Domain.MAINTENANCE, [*metrics, crit]))
    assert (
        r.verdict == "definite" and r.signals["drift"] is True and r.signals["system"] == "cooling"
    )


def test_metrics_alone_without_drift_are_dropped() -> None:
    metrics = [
        _ev(
            i,
            EventType.ENGINE_METRICS,
            {
                "coolant_temp_f": 195,
                "oil_pressure_psi": 45,
                "battery_v": 14.0,
                "def_level_pct": 60,
                "rpm": 1300,
                "fuel_level_pct": 50,
            },
            i * 15,
        )
        for i in range(8)
    ]
    assert prefilter(_cluster(Domain.MAINTENANCE, metrics)).verdict == "drop"


def _fuel(
    seq: int,
    gallons: float,
    minutes: int = 0,
    lat: float = 32.78,
    lon: float = -96.80,
    vlat: float = 32.78,
    vlon: float = -96.80,
) -> Event:
    return _ev(
        seq,
        EventType.FUEL_TRANSACTION,
        {
            "txn_id": f"FT-{seq}",
            "card_id": "FC-1",
            "gallons": gallons,
            "amount_usd": gallons * 4,
            "price_per_gal": 4.0,
            "station": "X",
            "lat": lat,
            "lon": lon,
            "vehicle_lat_at_ts": vlat,
            "vehicle_lon_at_ts": vlon,
        },
        minutes,
    )


def test_fuel_rules() -> None:
    ctx = {"fuel_tank_gal": 150.0, "has_reefer": False, "reefer_tank_gal": 0.0}
    assert prefilter(_cluster(Domain.SPEND, [_fuel(1, 120)], ctx)).verdict == "drop"
    over = prefilter(_cluster(Domain.SPEND, [_fuel(1, 190)], ctx))
    assert over.verdict == "ambiguous" and over.signals["overfill"]
    far = prefilter(_cluster(Domain.SPEND, [_fuel(1, 120, lat=29.76, lon=-95.37)], ctx))
    assert far.verdict == "definite" and far.signals["location_mismatch"]
    rapid = prefilter(_cluster(Domain.SPEND, [_fuel(1, 110), _fuel(2, 90, minutes=60)], ctx))
    assert rapid.verdict == "definite" and rapid.signals["rapid_repeat"]
    reefer_ctx = {"fuel_tank_gal": 150.0, "has_reefer": True, "reefer_tank_gal": 50.0}
    assert prefilter(_cluster(Domain.SPEND, [_fuel(1, 190)], reefer_ctx)).verdict == "ambiguous"
    assert (
        prefilter(
            _cluster(Domain.SPEND, [_fuel(1, 190)], reefer_ctx), {"reefer_fills_ok": True}
        ).verdict
        == "drop"
    )


def test_safety_pattern_needs_count_and_trips() -> None:
    def harsh(seq: int, minutes: int, trip: str) -> Event:
        return _ev(
            seq,
            EventType.HARSH_EVENT,
            {
                "harsh_type": "harsh_brake",
                "g_force": 0.45,
                "speed_mph": 55,
                "lat": 32.0,
                "lon": -97.0,
                "trip_id": trip,
            },
            minutes,
        )

    assert (
        prefilter(_cluster(Domain.SAFETY, [harsh(1, 0, "T1"), harsh(2, 10, "T1")])).verdict
        == "drop"
    )
    same_trip = [harsh(i, i * 10, "T1") for i in range(4)]
    assert prefilter(_cluster(Domain.SAFETY, same_trip)).verdict == "drop"
    spread = [harsh(1, 0, "T1"), harsh(2, 300, "T2"), harsh(3, 600, "T2"), harsh(4, 900, "T3")]
    assert prefilter(_cluster(Domain.SAFETY, spread)).verdict == "ambiguous"
    many = [*spread, harsh(5, 1000, "T3")]
    assert prefilter(_cluster(Domain.SAFETY, many)).verdict == "definite"
    assert prefilter(_cluster(Domain.SAFETY, spread), {"harsh_threshold": 6}).verdict == "drop"


def test_suppression_window_drops_everything() -> None:
    c = _cluster(Domain.COMPLIANCE, [_hos(1, 60, 130)])
    assert prefilter(c, {"suppress_until": (T0 + timedelta(hours=1)).isoformat()}).verdict == "drop"
    assert (
        prefilter(c, {"suppress_until": (T0 - timedelta(hours=1)).isoformat()}).verdict
        == "definite"
    )

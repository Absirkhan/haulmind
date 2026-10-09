"""Simulator determinism and ground-truth sanity."""

from __future__ import annotations

import hashlib
import json

from haulmind.events.schemas import EventType
from haulmind.simulator import SimConfig, generate
from haulmind.simulator.scenarios import ScenarioMix


def _digest(events) -> str:  # type: ignore[no-untyped-def]
    return hashlib.sha256(
        json.dumps([e.model_dump(mode="json") for e in events]).encode()
    ).hexdigest()


def test_same_seed_same_events() -> None:
    a = generate(SimConfig(seed=11, fleet_size=6, hours=12))
    b = generate(SimConfig(seed=11, fleet_size=6, hours=12))
    assert _digest(a.events) == _digest(b.events)
    assert a.ground_truth.model_dump() == b.ground_truth.model_dump()
    assert a.run_id == b.run_id


def test_different_seed_differs() -> None:
    a = generate(SimConfig(seed=1, fleet_size=6, hours=12))
    b = generate(SimConfig(seed=2, fleet_size=6, hours=12))
    assert _digest(a.events) != _digest(b.events)


def test_events_are_seq_ordered_and_sorted() -> None:
    sim = generate(SimConfig(seed=3, fleet_size=5, hours=10))
    assert [e.seq for e in sim.events] == list(range(len(sim.events)))
    assert all(sim.events[i].ts <= sim.events[i + 1].ts for i in range(len(sim.events) - 1))
    types = {e.type for e in sim.events}
    assert {
        EventType.GPS_PING,
        EventType.HOS_UPDATE,
        EventType.ENGINE_METRICS,
        EventType.TRIP_START,
    } <= types


def test_ground_truth_covers_every_scenario() -> None:
    mix = ScenarioMix(
        hos_violation_risk=1,
        failing_component=1,
        fuel_fraud=1,
        harsh_driving_pattern=1,
        hos_near_miss=1,
        nuisance_dtc=1,
        reefer_fuel_lookalike=1,
    )
    sim = generate(SimConfig(seed=5, fleet_size=10, hours=48, mix=mix))
    types = {g.type for g in sim.ground_truth.incidents}
    assert {
        "hos_violation_risk",
        "failing_component",
        "fuel_fraud",
        "harsh_driving_pattern",
        "hos_near_miss",
        "nuisance_dtc",
    } <= types
    assert all(g.end_ts >= g.start_ts for g in sim.ground_truth.incidents)
    assert all(
        g.benign
        for g in sim.ground_truth.incidents
        if g.type in ("hos_near_miss", "nuisance_dtc", "reefer_fuel_lookalike")
    )


def test_hos_risk_scenario_shows_a_deficit() -> None:
    sim = generate(SimConfig(seed=42, fleet_size=8, hours=24))
    gt = next(g for g in sim.ground_truth.incidents if g.type == "hos_violation_risk")
    deficits = [
        e.payload_dict["est_minutes_to_destination"] - e.payload_dict["drive_min_remaining"]
        for e in sim.events
        if e.type == EventType.HOS_UPDATE
        and e.driver_id == gt.driver_id
        and gt.start_ts <= e.ts <= gt.end_ts
        and e.payload_dict.get("est_minutes_to_destination") is not None
    ]
    assert deficits and max(deficits) > 30


def test_fuel_fraud_overfill_exceeds_tank() -> None:
    sim = generate(SimConfig(seed=42, fleet_size=8, hours=24))
    frauds = [g for g in sim.ground_truth.incidents if g.type == "fuel_fraud"]
    assert frauds
    for g in frauds:
        txn_ids = set(g.details["txn_ids"])
        txns = [
            e
            for e in sim.events
            if e.type == EventType.FUEL_TRANSACTION and e.payload_dict["txn_id"] in txn_ids
        ]
        assert txns
        if g.details["variant"] == "overfill":
            assert txns[0].payload_dict["gallons"] > 150

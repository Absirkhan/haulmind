"""Scenario planning: which vehicle gets which injected incident on which simulated day.

Each ``Injection`` is interpreted by the generator while it builds that vehicle's timeline.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from haulmind.simulator.fleet import Fleet

ScenarioType = Literal[
    "hos_violation_risk",
    "failing_component",
    "fuel_fraud",
    "harsh_driving_pattern",
    "hos_near_miss",
    "nuisance_dtc",
    "reefer_fuel_lookalike",
]


class ScenarioMix(BaseModel):
    """How many of each scenario to inject. Defaults scale with fleet size and hours."""

    hos_violation_risk: int = Field(default=3, ge=0)
    failing_component: int = Field(default=2, ge=0)
    fuel_fraud: int = Field(default=2, ge=0)
    harsh_driving_pattern: int = Field(default=1, ge=0)
    hos_near_miss: int = Field(default=2, ge=0)
    nuisance_dtc: int = Field(default=1, ge=0)
    reefer_fuel_lookalike: int = Field(default=1, ge=0)

    @classmethod
    def scaled(cls, fleet_size: int, hours: float) -> ScenarioMix:
        days = max(1.0, hours / 24.0)
        f = (fleet_size / 25.0) * (days / 2.0)

        def n(base: int, minimum: int = 1) -> int:
            return max(minimum, round(base * f))

        return cls(
            hos_violation_risk=n(3),
            failing_component=n(2),
            fuel_fraud=n(2),
            harsh_driving_pattern=n(1),
            hos_near_miss=n(2),
            nuisance_dtc=n(1),
            reefer_fuel_lookalike=1 if fleet_size >= 4 else 0,
        )

    def total(self) -> int:
        return sum(self.model_dump().values())


@dataclass
class Injection:
    scenario: ScenarioType
    vehicle_id: str
    day: int  # 0-based simulated day
    variant: str = ""  # e.g. cooling|electrical, overfill|location|rapid_repeat
    params: dict[str, float] = field(default_factory=dict)
    gt_id: str = ""


def plan_injections(
    fleet: Fleet, mix: ScenarioMix, days: int, rng: random.Random
) -> list[Injection]:
    """Assign scenarios to distinct vehicles/days so incidents do not overlap on one truck."""
    vehicle_ids = [v.id for v in fleet.vehicles]
    rng.shuffle(vehicle_ids)
    reefer_ids = [v.id for v in fleet.vehicles if v.has_reefer]
    plain_ids = [vid for vid in vehicle_ids if vid not in reefer_ids]
    pool = plain_ids[:]  # consume from here; the reefer truck is reserved for its lookalike
    injections: list[Injection] = []
    counter = 0

    def take() -> str | None:
        nonlocal pool
        if not pool:
            return None
        return pool.pop()

    def add(scenario: ScenarioType, vid: str, day: int, variant: str = "", **params: float) -> None:
        nonlocal counter
        counter += 1
        injections.append(
            Injection(
                scenario=scenario,
                vehicle_id=vid,
                day=day,
                variant=variant,
                params=params,
                gt_id=f"gt-{counter:03d}",
            )
        )

    def day_pick() -> int:
        return rng.randrange(days)

    for _ in range(mix.hos_violation_risk):
        if (vid := take()) is not None:
            add("hos_violation_risk", vid, day_pick(), total_drive_min=rng.randint(715, 745))
    for _ in range(mix.hos_near_miss):
        if (vid := take()) is not None:
            add("hos_near_miss", vid, day_pick(), total_drive_min=rng.randint(648, 657))
    for i in range(mix.failing_component):
        if (vid := take()) is not None:
            variant = "cooling" if i % 2 == 0 else "electrical"
            add("failing_component", vid, day_pick(), variant)
    variants = ["overfill", "location", "rapid_repeat"]
    for i in range(mix.fuel_fraud):
        if (vid := take()) is not None:
            add("fuel_fraud", vid, day_pick(), variants[i % len(variants)])
    for _ in range(mix.harsh_driving_pattern):
        if (vid := take()) is not None:
            add("harsh_driving_pattern", vid, 0 if days == 1 else rng.randrange(days - 1))
    for _ in range(mix.nuisance_dtc):
        if (vid := take()) is not None:
            add("nuisance_dtc", vid, 0)  # recurs across every day of the run
    for i in range(min(mix.reefer_fuel_lookalike, len(reefer_ids))):
        add("reefer_fuel_lookalike", reefer_ids[i], 0)  # every fuel stop of this truck
    return injections

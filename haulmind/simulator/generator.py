"""Event generator: builds a deterministic timeline per vehicle and merges them into one run.

Design notes
- One ``random.Random`` per vehicle (seeded from the run seed) so adding vehicles never changes
  another vehicle's stream.
- Simulated time is UTC; the run starts on a Monday at 06:00.
- Scenario injections (see scenarios.py) hook into the daily plan of the target vehicle.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from haulmind.events.schemas import (
    Domain,
    DtcFault,
    EngineMetrics,
    Event,
    EventType,
    FuelTxn,
    GpsPing,
    HarshEvent,
    HosUpdate,
    IdleEvent,
    Payload,
    Speeding,
    TripEnd,
    TripStart,
)
from haulmind.simulator.data import (
    AVG_SPEED_MPH,
    CITIES,
    CYCLE_LIMIT_MIN,
    DRIVE_LIMIT_MIN,
    DTC_CATALOG,
    DUTY_LIMIT_MIN,
    HARSH_TYPES,
    NOISE_DTC_CODES,
    REEFER_TRACTOR_MPG,
    ROAD_FACTOR,
    TRACTOR_MPG,
    City,
)
from haulmind.simulator.fleet import Fleet, SimDriver, SimVehicle, Station, build_fleet
from haulmind.simulator.ground_truth import GroundTruth, GroundTruthIncident
from haulmind.simulator.scenarios import Injection, ScenarioMix, plan_injections
from haulmind.util import haversine_mi, sha256_hex

SIM_EPOCH = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)

PING_MIN = 5
METRICS_MIN = 15
HOS_MIN = 30


class SimConfig(BaseModel):
    seed: int = 42
    fleet_size: int = Field(default=25, ge=3, le=200)
    hours: float = Field(default=48.0, gt=0, le=24 * 14)
    label: str = ""
    mix: ScenarioMix | None = None
    harsh_noise_p: float = 0.003  # per 5-minute ping: about one event per 25 driving hours
    speeding_noise_p: float = 0.004
    dtc_noise_daily_p: float = 0.25

    @property
    def days(self) -> int:
        return max(1, math.ceil(self.hours / 24.0))

    @property
    def start(self) -> datetime:
        return SIM_EPOCH

    @property
    def end(self) -> datetime:
        return SIM_EPOCH + timedelta(hours=self.hours)

    def run_id(self) -> str:
        return f"run-{self.seed}-{sha256_hex(self.model_dump(mode='json'))[:6]}"


@dataclass
class SimRun:
    run_id: str
    config: SimConfig
    fleet: Fleet
    events: list[Event]
    ground_truth: GroundTruth
    injections: list[Injection]


# --- helpers ---------------------------------------------------------------------------------


@dataclass
class Leg:
    origin: City
    dest: City
    miles: float
    minutes: int
    trip_id: str
    resume: bool = False  # continuing a trip stranded the day before


@dataclass
class HosState:
    drive_used: int = 0
    duty_used: int = 0
    cycle_used: int = 0
    since_break: int = 0
    status: str = "off_duty"

    @property
    def drive_remaining(self) -> int:
        return max(0, DRIVE_LIMIT_MIN - self.drive_used)

    @property
    def duty_remaining(self) -> int:
        return max(0, DUTY_LIMIT_MIN - self.duty_used)

    @property
    def cycle_remaining(self) -> int:
        return max(0, CYCLE_LIMIT_MIN - self.cycle_used)


@dataclass
class Degradation:
    """Failing-component profile applied to engine metrics on one day."""

    variant: str  # cooling|electrical
    start_min: int  # minutes into the day's driving when drift begins
    ramp_min: int  # minutes until full drift
    gt: GroundTruthIncident
    last_dtc_at: int = -10_000
    first_dtc_ts: datetime | None = None


@dataclass
class _Draft:
    """Mutable event list for one vehicle before seq assignment."""

    events: list[tuple[datetime, int, str, str | None, str | None, Payload]] = field(
        default_factory=list
    )
    order: int = 0

    def emit(
        self, ts: datetime, type_: EventType, vehicle_id: str | None, driver_id: str | None, p: Any
    ) -> None:
        self.order += 1
        self.events.append((ts, self.order, type_.value, vehicle_id, driver_id, p))


def _interp(a: City, b: City, frac: float) -> tuple[float, float]:
    return a.lat + (b.lat - a.lat) * frac, a.lon + (b.lon - a.lon) * frac


def _bearing(a: City, b: City) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _nearest_station(stations: list[Station], lat: float, lon: float) -> Station:
    return min(stations, key=lambda s: haversine_mi(lat, lon, s.lat, s.lon))


def _far_station(stations: list[Station], lat: float, lon: float, rng: random.Random) -> Station:
    far = [s for s in stations if 40 <= haversine_mi(lat, lon, s.lat, s.lon) <= 120]
    if not far:
        far = sorted(stations, key=lambda s: haversine_mi(lat, lon, s.lat, s.lon))[5:10]
    return rng.choice(far)


# --- vehicle timeline ------------------------------------------------------------------------


class VehicleTimeline:
    def __init__(
        self,
        run_id: str,
        cfg: SimConfig,
        fleet: Fleet,
        vehicle: SimVehicle,
        driver: SimDriver,
        injections: list[Injection],
        seed: int,
    ) -> None:
        self.run_id = run_id
        self.cfg = cfg
        self.fleet = fleet
        self.v = vehicle
        self.d = driver
        self.inj = {
            i.day: i
            for i in injections
            if i.scenario not in ("nuisance_dtc", "reefer_fuel_lookalike")
        }
        self.persistent = [
            i for i in injections if i.scenario in ("nuisance_dtc", "reefer_fuel_lookalike")
        ]
        self.rng = random.Random(seed)
        self.draft = _Draft()
        self.gt: list[GroundTruthIncident] = []

        self.pos: tuple[float, float] = (vehicle.depot_city.lat, vehicle.depot_city.lon)
        self.city: City = vehicle.depot_city
        self.odometer = vehicle.odometer_mi
        self.fuel_level = self.rng.uniform(0.55, 0.95)
        self.mpg = REEFER_TRACTOR_MPG if vehicle.has_reefer else TRACTOR_MPG
        self.def_level = self.rng.uniform(45, 95)
        self.hos = HosState(cycle_used=self.rng.randint(20 * 60, 45 * 60))
        self.trip_counter = 0
        self.txn_counter = 0
        self.stranded: Leg | None = None
        self.stranded_remaining_frac = 0.0
        self.harsh_count = 0
        self.harsh_pattern_days: set[int] = set()
        self.harsh_gt: GroundTruthIncident | None = None
        for i in injections:
            if i.scenario == "harsh_driving_pattern":
                self.harsh_pattern_days = {i.day, min(i.day + 1, cfg.days - 1)}

    # --- emit helpers ------------------------------------------------------------------------

    def _emit(self, ts: datetime, p: Payload) -> None:
        if ts >= self.cfg.end:
            return
        type_ = EventType(p.kind)
        self.draft.emit(ts, type_, self.v.id, self.d.id, p)

    def _hos(self, ts: datetime, trip_id: str | None, est_to_dest: int | None) -> None:
        self._emit(
            ts,
            HosUpdate(
                duty_status=self.hos.status,
                drive_min_remaining=self.hos.drive_remaining,
                duty_min_remaining=self.hos.duty_remaining,
                cycle_min_remaining=self.hos.cycle_remaining,
                minutes_since_break=self.hos.since_break,
                trip_id=trip_id,
                est_minutes_to_destination=est_to_dest,
            ),
        )

    def _metrics(
        self, ts: datetime, engine_on: bool, degr: Degradation | None, minute_of_day: int
    ) -> None:
        coolant = self.rng.uniform(188, 204)
        battery = self.rng.uniform(13.7, 14.2)
        oil = self.rng.uniform(42, 58) if engine_on else 0.0
        rpm = self.rng.randint(1150, 1500) if engine_on else 0
        if degr is not None and engine_on and minute_of_day >= degr.start_min:
            frac = min(1.0, (minute_of_day - degr.start_min) / max(1, degr.ramp_min))
            if degr.variant == "cooling":
                coolant += 42 * frac + self.rng.uniform(-2, 2)
            else:
                battery -= 2.0 * frac + self.rng.uniform(-0.05, 0.05)
        self._emit(
            ts,
            EngineMetrics(
                coolant_temp_f=round(coolant, 1),
                oil_pressure_psi=round(oil, 1),
                battery_v=round(battery, 2),
                def_level_pct=round(self.def_level, 1),
                rpm=rpm,
                fuel_level_pct=round(self.fuel_level * 100, 1),
                engine_on=engine_on,
            ),
        )
        if degr is not None and engine_on and minute_of_day >= degr.start_min:
            self._maybe_degradation_dtc(ts, degr, coolant, battery, minute_of_day)

    def _dtc(self, ts: datetime, code: str, active: bool = True) -> None:
        spec = DTC_CATALOG[code]
        self._emit(
            ts,
            DtcFault(
                code=code,
                description=spec.description,
                system=spec.system,
                severity=spec.severity,
                active=active,
            ),
        )

    def _maybe_degradation_dtc(
        self, ts: datetime, degr: Degradation, coolant: float, battery: float, minute_of_day: int
    ) -> None:
        if minute_of_day - degr.last_dtc_at < 30:
            return
        fired = False
        if degr.variant == "cooling":
            if coolant > 212 and degr.first_dtc_ts is None:
                self._dtc(ts, "P0128")  # early, low-severity precursor
                fired = True
            if coolant > 224:
                self._dtc(ts, "P0217")
                fired = True
            if coolant > 234:
                self._dtc(ts, "SPN110-FMI0")
                fired = True
        else:
            if battery < 12.9:
                self._dtc(ts, "P0562")
                fired = True
            if battery < 12.3:
                self._dtc(ts, "SPN168-FMI18")
                fired = True
        if fired:
            degr.last_dtc_at = minute_of_day
            if degr.first_dtc_ts is None:
                degr.first_dtc_ts = ts
            degr.gt.end_ts = ts + timedelta(hours=2)

    # --- planning ----------------------------------------------------------------------------

    def _pick_dest(self, origin: City, target_min: int) -> City:
        """Pick a destination whose road time is near ``target_min`` (± 35 %)."""
        candidates = []
        for c in CITIES:
            if c.name == origin.name:
                continue
            miles = haversine_mi(origin.lat, origin.lon, c.lat, c.lon) * ROAD_FACTOR
            minutes = miles / AVG_SPEED_MPH * 60
            if 0.65 * target_min <= minutes <= 1.35 * target_min:
                candidates.append(c)
        if not candidates:
            candidates = sorted(
                (c for c in CITIES if c.name != origin.name),
                key=lambda c: abs(
                    haversine_mi(origin.lat, origin.lon, c.lat, c.lon)
                    * ROAD_FACTOR
                    / AVG_SPEED_MPH
                    * 60
                    - target_min
                ),
            )[:3]
        return self.rng.choice(candidates)

    def _make_leg(self, origin: City, target_min: int, exact: bool = False) -> Leg:
        dest = self._pick_dest(origin, target_min)
        straight = haversine_mi(origin.lat, origin.lon, dest.lat, dest.lon)
        if exact:
            miles = target_min / 60 * AVG_SPEED_MPH
            minutes = target_min
        else:
            miles = straight * ROAD_FACTOR * self.rng.uniform(0.97, 1.05)
            minutes = round(miles / AVG_SPEED_MPH * 60)
        self.trip_counter += 1
        return Leg(
            origin=origin,
            dest=dest,
            miles=round(miles, 1),
            minutes=max(45, int(minutes)),
            trip_id=f"T-{self.v.id}-{self.trip_counter:03d}",
        )

    def _plan_day(self, day: int) -> list[Leg]:
        inj = self.inj.get(day)
        legs: list[Leg] = []
        origin = self.city
        if self.stranded is not None:
            remaining_min = max(30, int(self.stranded.minutes * self.stranded_remaining_frac))
            legs.append(
                Leg(
                    origin=City("en route", *self.pos),
                    dest=self.stranded.dest,
                    miles=round(self.stranded.miles * self.stranded_remaining_frac, 1),
                    minutes=remaining_min,
                    trip_id=self.stranded.trip_id,
                    resume=True,
                )
            )
            origin = self.stranded.dest
            self.stranded = None
        used = sum(leg.minutes for leg in legs)

        if inj is not None and inj.scenario in ("hos_violation_risk", "hos_near_miss"):
            total = int(inj.params["total_drive_min"]) - used
            first = int(total * self.rng.uniform(0.30, 0.36))
            second = int(total * self.rng.uniform(0.28, 0.34))
            third = total - first - second
            for target, exact in ((first, False), (second, False), (third, True)):
                leg = self._make_leg(origin, target, exact=exact)
                if not exact:
                    leg.minutes = target  # keep totals exact for the scenario
                    leg.miles = round(target / 60 * AVG_SPEED_MPH, 1)
                legs.append(leg)
                origin = leg.dest
            return legs

        total = self.rng.randint(7 * 60, int(9.5 * 60)) - used
        n_legs = 2 if total < 6 * 60 else self.rng.choice([2, 3])
        remaining = total
        for i in range(n_legs):
            target = remaining if i == n_legs - 1 else int(remaining * self.rng.uniform(0.35, 0.55))
            target = max(45, min(target, 4 * 60))
            leg = self._make_leg(origin, target)
            legs.append(leg)
            origin = leg.dest
            remaining -= leg.minutes
            if remaining < 45:
                break
        # Ordinary days stay legal with margin: dispatch plans at most 10 h of driving.
        overflow = sum(leg.minutes for leg in legs) - 600
        if overflow > 0 and legs:
            last = legs[-1]
            if last.minutes - overflow >= 45:
                last.minutes -= overflow
                last.miles = round(last.minutes / 60 * AVG_SPEED_MPH, 1)
            else:
                legs.pop()
        return legs

    # --- day simulation ----------------------------------------------------------------------

    def run(self) -> None:
        for day in range(self.cfg.days):
            day_start = self.cfg.start + timedelta(days=day)
            if day_start >= self.cfg.end:
                break
            self._simulate_day(day, day_start)

    def _simulate_day(self, day: int, day_start: datetime) -> None:
        inj = self.inj.get(day)
        rng = self.rng
        # 10h+ off duty resets the daily clocks
        self.hos.drive_used = 0
        self.hos.duty_used = 0
        self.hos.since_break = 0
        self.hos.status = "on_duty"

        t = day_start + timedelta(minutes=rng.randint(0, 120))
        legs = self._plan_day(day)
        degr: Degradation | None = None
        if inj is not None and inj.scenario == "failing_component":
            degr = self._setup_degradation(inj, t)

        # pre-trip inspection (15 min on duty)
        self._metrics(t, engine_on=True, degr=None, minute_of_day=0)
        self._hos(t, None, None)
        t += timedelta(minutes=15)
        self.hos.duty_used += 15
        self.hos.cycle_used += 15
        minute_of_day = 15

        hos_gt: GroundTruthIncident | None = None
        drive_total_planned = sum(leg.minutes for leg in legs)
        cumulative = 0
        break_done = False
        fuel_forced_after = self._fuel_force_after_leg(inj, legs)
        noise_dtc = self._plan_noise_dtc(day)
        harsh_injections = self._plan_harsh_injections(day, legs)
        nuisance_times = self._plan_nuisance(day)

        for li, leg in enumerate(legs):
            # trip start
            self.hos.status = "driving"
            eta = t + timedelta(minutes=leg.minutes)
            if not leg.resume:
                self._emit(
                    t,
                    TripStart(
                        trip_id=leg.trip_id,
                        origin=leg.origin.name,
                        destination=leg.dest.name,
                        dest_lat=leg.dest.lat,
                        dest_lon=leg.dest.lon,
                        planned_miles=leg.miles,
                        eta_ts=eta,
                        load_ref=f"LD-{rng.randint(10000, 99999)}",
                    ),
                )
            self._hos(t, leg.trip_id, leg.minutes)

            heading = _bearing(leg.origin, leg.dest)
            stranded = False
            elapsed = 0
            last_hos = 0
            last_metrics = 0
            while elapsed < leg.minutes:
                step = min(PING_MIN, leg.minutes - elapsed)
                elapsed += step
                minute_of_day += step
                t += timedelta(minutes=step)
                self.hos.drive_used += step
                self.hos.duty_used += step
                self.hos.cycle_used += step
                self.hos.since_break += step
                frac = elapsed / leg.minutes
                miles_step = leg.miles * step / leg.minutes
                self.odometer += miles_step
                self.fuel_level = max(
                    0.02, self.fuel_level - miles_step / self.mpg / self.v.fuel_tank_gal
                )
                self.def_level = max(5.0, self.def_level - miles_step * 0.004)
                lat, lon = _interp(leg.origin, leg.dest, frac)
                lat += rng.uniform(-0.004, 0.004)
                lon += rng.uniform(-0.004, 0.004)
                self.pos = (lat, lon)
                speed = rng.uniform(52, 67)
                remaining_miles = leg.miles * (1 - frac)
                est_to_dest = round(remaining_miles / AVG_SPEED_MPH * 60)
                self._emit(
                    t,
                    GpsPing(
                        lat=round(lat, 5),
                        lon=round(lon, 5),
                        speed_mph=round(speed, 1),
                        heading=round(heading, 1),
                        odometer_mi=round(self.odometer, 1),
                        trip_id=leg.trip_id,
                        miles_to_destination=round(remaining_miles, 1),
                    ),
                )
                # harsh / speeding noise + injected pattern
                if (elapsed, li) in harsh_injections or rng.random() < self._harsh_p(day):
                    self._harsh(t, speed, lat, lon, leg.trip_id, day)
                if rng.random() < self.cfg.speeding_noise_p:
                    self._emit(
                        t,
                        Speeding(
                            speed_mph=round(rng.uniform(72, 79), 1),
                            limit_mph=65,
                            duration_s=rng.randint(40, 200),
                            lat=round(lat, 5),
                            lon=round(lon, 5),
                        ),
                    )
                # metrics
                if elapsed - last_metrics >= METRICS_MIN or elapsed == leg.minutes:
                    last_metrics = elapsed
                    self._metrics(t, True, degr, minute_of_day)
                # noise / nuisance DTCs
                for code, at, active in list(noise_dtc):
                    if t >= at:
                        self._dtc(t, code, active)
                        noise_dtc.remove((code, at, active))
                for at, active in list(nuisance_times):
                    if t >= at:
                        self._dtc(t, "P0128", active)
                        nuisance_times.remove((at, active))
                # HOS cadence: tighter when running low so the risk is visible
                cadence = HOS_MIN if self.hos.drive_remaining > 150 else 15
                if elapsed - last_hos >= cadence:
                    last_hos = elapsed
                    self._hos(t, leg.trip_id, est_to_dest)
                    if inj is not None and inj.scenario in ("hos_violation_risk", "hos_near_miss"):
                        hos_gt = self._track_hos_gt(inj, hos_gt, t, est_to_dest)
                if (
                    self.hos.drive_remaining <= 0
                    and inj is not None
                    and inj.scenario == "hos_violation_risk"
                ):
                    stranded = True
                    break

            cumulative += elapsed
            if stranded:
                # Driver pulls over: clocks exhausted, trip incomplete until tomorrow.
                self.hos.status = "off_duty"
                self._hos(t, leg.trip_id, est_to_dest)
                self._metrics(t, engine_on=False, degr=None, minute_of_day=minute_of_day)
                self.stranded = leg
                self.stranded_remaining_frac = 1 - elapsed / leg.minutes
                if hos_gt is not None:
                    hos_gt.end_ts = t + timedelta(minutes=60)
                    hos_gt.details["stranded_ts"] = t.isoformat()
                self.city = City("en route", *self.pos)
                return

            # trip end
            self.city = leg.dest
            self.pos = (leg.dest.lat, leg.dest.lon)
            self._emit(
                t,
                TripEnd(
                    trip_id=leg.trip_id,
                    actual_miles=round(leg.miles, 1),
                    on_time=t <= eta + timedelta(minutes=30),
                ),
            )
            self.hos.status = "on_duty"
            self._hos(t, None, None)
            if (
                hos_gt is not None
                and inj is not None
                and inj.scenario == "hos_near_miss"
                and li == len(legs) - 1
            ):
                hos_gt.end_ts = t + timedelta(minutes=30)
                hos_gt.details["arrived_with_min_remaining"] = self.hos.drive_remaining

            # fuel stop?
            fuel_threshold = 0.5 if self.v.has_reefer else 0.30
            need_fuel = self.fuel_level < fuel_threshold or li == fuel_forced_after
            if (need_fuel and li < len(legs) - 1) or (need_fuel and self.fuel_level < 0.2):
                t += timedelta(minutes=10)
                self._fuel_stop(t, inj, day)
                t += timedelta(minutes=10)
                self.hos.duty_used += 20
                self.hos.cycle_used += 20
                minute_of_day += 20

            # break after ~half the day's driving (keeps since_break under 8h)
            if (
                not break_done
                and li < len(legs) - 1
                and (cumulative >= drive_total_planned * 0.45 or cumulative >= 5 * 60)
            ):
                break_done = True
                self.hos.status = "off_duty"
                self._hos(t, None, None)
                if rng.random() < 0.35:
                    self._emit(
                        t,
                        IdleEvent(
                            duration_min=30, lat=round(self.pos[0], 5), lon=round(self.pos[1], 5)
                        ),
                    )
                t += timedelta(minutes=30)
                minute_of_day += 30
                self.hos.since_break = 0
                self.hos.duty_used += 30
                self.hos.cycle_used += 30
                self.hos.status = "on_duty"
            elif li < len(legs) - 1:
                t += timedelta(minutes=rng.randint(10, 25))  # dock time
                self.hos.duty_used += 15
                self.hos.cycle_used += 15
                minute_of_day += 15

        # end of day
        self.hos.status = "off_duty"
        self._hos(t, None, None)
        self._metrics(t, engine_on=False, degr=None, minute_of_day=minute_of_day)

    # --- scenario helpers --------------------------------------------------------------------

    def _setup_degradation(self, inj: Injection, day_t: datetime) -> Degradation:
        gt = GroundTruthIncident(
            id=inj.gt_id,
            type="failing_component",
            domain=Domain.MAINTENANCE,
            vehicle_id=self.v.id,
            driver_id=self.d.id,
            start_ts=day_t + timedelta(minutes=90),
            end_ts=day_t + timedelta(hours=10),
            benign=False,
            expected_actions=["create_work_order"],
            notes=f"{inj.variant} degradation with recurring DTCs",
            details={"variant": inj.variant},
        )
        self.gt.append(gt)
        return Degradation(
            variant=inj.variant,
            start_min=75,
            ramp_min=240 if inj.variant == "cooling" else 330,
            gt=gt,
        )

    def _track_hos_gt(
        self, inj: Injection, gt: GroundTruthIncident | None, t: datetime, est_to_dest: int
    ) -> GroundTruthIncident | None:
        remaining = self.hos.drive_remaining
        deficit = est_to_dest - remaining
        if gt is None and remaining <= 120 and deficit > -15:
            benign = inj.scenario == "hos_near_miss"
            gt = GroundTruthIncident(
                id=inj.gt_id,
                type=inj.scenario,
                domain=Domain.COMPLIANCE,
                vehicle_id=self.v.id,
                driver_id=self.d.id,
                start_ts=t,
                end_ts=t + timedelta(hours=3),
                benign=benign,
                expected_actions=[] if benign else ["message_driver", "propose_reassignment"],
                notes=(
                    "Driver arrives with a few minutes to spare; no action needed"
                    if benign
                    else "Remaining drive time cannot cover the remaining distance"
                ),
                details={"first_remaining_min": remaining, "first_est_to_dest_min": est_to_dest},
            )
            self.gt.append(gt)
        return gt

    def _fuel_force_after_leg(self, inj: Injection | None, legs: list[Leg]) -> int | None:
        if inj is not None and inj.scenario == "fuel_fraud" and legs:
            return 0 if len(legs) > 1 else None
        return None

    def _plan_noise_dtc(self, day: int) -> list[tuple[str, datetime, bool]]:
        if self.rng.random() >= self.cfg.dtc_noise_daily_p:
            return []
        code = self.rng.choice(NOISE_DTC_CODES)
        at = self.cfg.start + timedelta(days=day, minutes=self.rng.randint(120, 420))
        clear = at + timedelta(minutes=self.rng.randint(60, 180))
        return [(code, at, True), (code, clear, False)]

    def _plan_nuisance(self, day: int) -> list[tuple[datetime, bool]]:
        out: list[tuple[datetime, bool]] = []
        for i in self.persistent:
            if i.scenario != "nuisance_dtc":
                continue
            at = self.cfg.start + timedelta(days=day, minutes=self.rng.randint(90, 360))
            clear = at + timedelta(minutes=self.rng.randint(20, 40))
            out.extend([(at, True), (clear, False)])
            self.gt.append(
                GroundTruthIncident(
                    id=f"{i.gt_id}-d{day}",
                    type="nuisance_dtc",
                    domain=Domain.MAINTENANCE,
                    vehicle_id=self.v.id,
                    driver_id=self.d.id,
                    start_ts=at,
                    end_ts=clear + timedelta(hours=2),
                    benign=True,
                    expected_actions=[],
                    notes="Known intermittent P0128 that clears itself; temps normal",
                    details={"code": "P0128"},
                )
            )
        return out

    def _plan_harsh_injections(self, day: int, legs: list[Leg]) -> set[tuple[int, int]]:
        if day not in self.harsh_pattern_days or not legs:
            return set()
        picks: set[tuple[int, int]] = set()
        leg_indices = self.rng.sample(range(len(legs)), k=min(3, len(legs)))
        while len(leg_indices) < 3:
            leg_indices.append(self.rng.randrange(len(legs)))
        for li in leg_indices:
            minutes = legs[li].minutes
            at = self.rng.randrange(1, max(2, minutes // PING_MIN)) * PING_MIN
            picks.add((min(at, minutes), li))
        return picks

    def _harsh_p(self, day: int) -> float:
        return self.cfg.harsh_noise_p * (3 if day in self.harsh_pattern_days else 1)

    def _harsh(
        self, t: datetime, speed: float, lat: float, lon: float, trip_id: str, day: int
    ) -> None:
        r = self.rng.random()
        acc = 0.0
        kind = HARSH_TYPES[0][0]
        for k, w in HARSH_TYPES:
            acc += w
            if r <= acc:
                kind = k
                break
        self._emit(
            t,
            HarshEvent(
                harsh_type=kind,
                g_force=round(self.rng.uniform(0.35, 0.62), 2),
                speed_mph=round(speed, 1),
                lat=round(lat, 5),
                lon=round(lon, 5),
                trip_id=trip_id,
            ),
        )
        self.harsh_count += 1
        if day in self.harsh_pattern_days:
            inj = next(i for i in self.inj.values() if i.scenario == "harsh_driving_pattern")
            if self.harsh_gt is None:
                self.harsh_gt = GroundTruthIncident(
                    id=inj.gt_id,
                    type="harsh_driving_pattern",
                    domain=Domain.SAFETY,
                    vehicle_id=self.v.id,
                    driver_id=self.d.id,
                    start_ts=t,
                    end_ts=t + timedelta(hours=48),
                    benign=False,
                    expected_actions=["message_driver"],
                    notes="Cluster of harsh events across multiple trips",
                )
                self.gt.append(self.harsh_gt)
            else:
                self.harsh_gt.end_ts = t + timedelta(hours=6)
            self.harsh_gt.details["count"] = self.harsh_gt.details.get("count", 0) + 1

    def _fuel_stop(self, t: datetime, inj: Injection | None, day: int) -> None:
        rng = self.rng
        lat, lon = self.pos
        station = _nearest_station(self.fleet.stations, lat, lon)
        price = round(rng.uniform(3.55, 4.25), 3)
        tank = self.v.fuel_tank_gal
        gallons = max(5.0, (0.95 - self.fuel_level) * tank)
        variant = inj.variant if inj is not None and inj.scenario == "fuel_fraud" else ""
        reefer = any(i.scenario == "reefer_fuel_lookalike" for i in self.persistent)
        vehicle_lat, vehicle_lon = lat, lon
        notes = ""
        gt_type = ""
        benign = False
        expected: list[str] = []

        if variant == "overfill":
            gallons = tank * rng.uniform(1.25, 1.38)
            gt_type, notes = "fuel_fraud", "Gallons exceed tank capacity"
            expected = ["flag_fuel_transaction"]
        elif variant == "location":
            station = _far_station(self.fleet.stations, lat, lon, rng)
            gt_type, notes = "fuel_fraud", "Card used far from the vehicle's GPS position"
            expected = ["flag_fuel_transaction"]
        elif variant == "rapid_repeat":
            gt_type, notes = "fuel_fraud", "Two fills within 90 minutes exceeding tank capacity"
            expected = ["flag_fuel_transaction"]
        elif reefer:
            gallons = max(
                gallons, tank * rng.uniform(0.80, 0.92)
            ) + self.v.reefer_tank_gal * rng.uniform(0.75, 0.95)
            gt_type, notes = (
                "reefer_fuel_lookalike",
                "Tractor + reefer tank filled together; legitimate",
            )
            benign = True

        self.txn_counter += 1
        txn_id = f"FT-{self.v.id}-{self.txn_counter:03d}"
        self._emit(
            t,
            FuelTxn(
                txn_id=txn_id,
                card_id=self.d.fuel_card_id,
                gallons=round(gallons, 1),
                amount_usd=round(gallons * price, 2),
                price_per_gal=price,
                station=station.name,
                lat=round(station.lat, 5),
                lon=round(station.lon, 5),
                odometer_mi=round(self.odometer, 1),
                vehicle_lat_at_ts=round(vehicle_lat, 5),
                vehicle_lon_at_ts=round(vehicle_lon, 5),
            ),
        )
        self.fuel_level = min(0.95, self.fuel_level + min(gallons, tank) / tank)
        details: dict[str, Any] = {
            "txn_ids": [txn_id],
            "variant": variant or ("reefer" if reefer else ""),
        }

        if variant == "rapid_repeat":
            t2 = t + timedelta(minutes=rng.randint(55, 85))
            self.txn_counter += 1
            txn2 = f"FT-{self.v.id}-{self.txn_counter:03d}"
            g2 = tank * rng.uniform(0.55, 0.7)
            self._emit(
                t2,
                FuelTxn(
                    txn_id=txn2,
                    card_id=self.d.fuel_card_id,
                    gallons=round(g2, 1),
                    amount_usd=round(g2 * price, 2),
                    price_per_gal=price,
                    station=station.name,
                    lat=round(station.lat, 5),
                    lon=round(station.lon, 5),
                    odometer_mi=round(self.odometer + 5, 1),
                    vehicle_lat_at_ts=round(vehicle_lat, 5),
                    vehicle_lon_at_ts=round(vehicle_lon, 5),
                ),
            )
            details["txn_ids"].append(txn2)

        if gt_type:
            gt_id = (
                inj.gt_id
                if (inj is not None and variant)
                else f"gt-reefer-{self.v.id}-{self.txn_counter:02d}"
            )
            self.gt.append(
                GroundTruthIncident(
                    id=gt_id,
                    type=gt_type,
                    domain=Domain.SPEND,
                    vehicle_id=self.v.id,
                    driver_id=self.d.id,
                    start_ts=t,
                    end_ts=t + timedelta(hours=3),
                    benign=benign,
                    expected_actions=expected,
                    notes=notes,
                    details=details,
                )
            )


# --- run generation --------------------------------------------------------------------------


def generate(cfg: SimConfig) -> SimRun:
    """Generate a complete deterministic run: fleet, events (seq-ordered) and ground truth."""
    run_id = cfg.run_id()
    fleet = build_fleet(cfg.seed, cfg.fleet_size)
    mix = cfg.mix or ScenarioMix.scaled(cfg.fleet_size, cfg.hours)
    plan_rng = random.Random(cfg.seed * 104729 + 7)
    injections = plan_injections(fleet, mix, cfg.days, plan_rng)
    by_vehicle: dict[str, list[Injection]] = {}
    for inj in injections:
        by_vehicle.setdefault(inj.vehicle_id, []).append(inj)

    drafts: list[tuple[datetime, int, str, str | None, str | None, Payload]] = []
    gt_incidents: list[GroundTruthIncident] = []
    for idx, v in enumerate(fleet.vehicles):
        d = fleet.driver_for(v.id)
        tl = VehicleTimeline(
            run_id, cfg, fleet, v, d, by_vehicle.get(v.id, []), seed=cfg.seed * 1000 + idx
        )
        tl.run()
        for ts, order, type_, vid, did, p in tl.draft.events:
            drafts.append((ts, idx * 1_000_000 + order, type_, vid, did, p))
        gt_incidents.extend(tl.gt)

    drafts.sort(key=lambda e: (e[0], e[1]))
    events: list[Event] = [
        Event(
            run_id=run_id,
            seq=seq,
            ts=ts,
            type=EventType(type_),
            vehicle_id=vid,
            driver_id=did,
            payload=p,
        )
        for seq, (ts, _order, type_, vid, did, p) in enumerate(drafts)
    ]
    gt_incidents.sort(key=lambda g: (g.start_ts, g.id))
    gt = GroundTruth(
        run_id=run_id, seed=cfg.seed, config=cfg.model_dump(mode="json"), incidents=gt_incidents
    )
    return SimRun(
        run_id=run_id,
        config=cfg,
        fleet=fleet,
        events=events,
        ground_truth=gt,
        injections=injections,
    )

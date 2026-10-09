"""Fleet construction: vehicles, drivers, shops, fuel stations. Deterministic for a seed."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from haulmind.simulator.data import (
    CITIES,
    CITY_BY_NAME,
    DEPOTS,
    FIRST_NAMES,
    LAST_NAMES,
    MAKES_MODELS,
    REEFER_TANK_GAL,
    SHOPS,
    STATES,
    TANK_GAL,
    City,
)


@dataclass
class SimVehicle:
    id: str
    unit_number: str
    vin: str
    make: str
    model: str
    year: int
    depot: str
    depot_city: City
    fuel_tank_gal: float = TANK_GAL
    has_reefer: bool = False
    reefer_tank_gal: float = 0.0
    odometer_mi: float = 0.0


@dataclass
class SimDriver:
    id: str
    name: str
    license_state: str
    depot: str
    fuel_card_id: str
    phone: str
    vehicle_id: str | None = None  # None => spare driver


@dataclass
class SimShop:
    id: str
    name: str
    city: str
    lat: float
    lon: float
    specialties: list[str]
    rating: float
    hourly_rate_usd: float
    open_24h: bool


@dataclass
class Station:
    name: str
    city: City
    lat: float
    lon: float


@dataclass
class Fleet:
    vehicles: list[SimVehicle]
    drivers: list[SimDriver]
    shops: list[SimShop]
    stations: list[Station]
    depots: dict[str, City] = field(default_factory=dict)

    def driver_for(self, vehicle_id: str) -> SimDriver:
        for d in self.drivers:
            if d.vehicle_id == vehicle_id:
                return d
        raise KeyError(vehicle_id)

    @property
    def spare_drivers(self) -> list[SimDriver]:
        return [d for d in self.drivers if d.vehicle_id is None]


def _vin(rng: random.Random) -> str:
    alphabet = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    return "1HM" + "".join(rng.choice(alphabet) for _ in range(14))


def build_fleet(seed: int, size: int, spare_drivers: int = 3) -> Fleet:
    """Build a fleet of ``size`` tractors with one assigned driver each plus spares."""
    rng = random.Random(seed * 7919 + 13)
    depots = {name: CITY_BY_NAME[city] for name, city in DEPOTS}
    depot_names = [d for d, _ in DEPOTS]

    vehicles: list[SimVehicle] = []
    for i in range(size):
        make, model = rng.choice(MAKES_MODELS)
        depot = depot_names[i % len(depot_names)]
        vehicles.append(
            SimVehicle(
                id=f"V-{i + 1:02d}",
                unit_number=f"{1200 + i}",
                vin=_vin(rng),
                make=make,
                model=model,
                year=rng.randint(2018, 2025),
                depot=depot,
                depot_city=depots[depot],
                odometer_mi=float(rng.randint(80_000, 520_000)),
            )
        )
    # Roughly one in eight tractors pulls a reefer and carries a reefer fuel tank.
    for v in vehicles[3::8]:
        v.has_reefer = True
        v.reefer_tank_gal = REEFER_TANK_GAL

    names = set()
    drivers: list[SimDriver] = []
    for i in range(size + spare_drivers):
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in names:
                names.add(name)
                break
        vehicle = vehicles[i] if i < size else None
        depot = vehicle.depot if vehicle else depot_names[i % len(depot_names)]
        drivers.append(
            SimDriver(
                id=f"D-{i + 1:02d}",
                name=name,
                license_state=rng.choice(STATES),
                depot=depot,
                fuel_card_id=f"FC-{4000 + i}",
                phone=f"555-01{rng.randint(10, 99)}",
                vehicle_id=vehicle.id if vehicle else None,
            )
        )

    shops = [
        SimShop(
            id=f"S-{i + 1:02d}",
            name=s.name,
            city=s.city,
            lat=CITY_BY_NAME[s.city].lat + rng.uniform(-0.04, 0.04),
            lon=CITY_BY_NAME[s.city].lon + rng.uniform(-0.04, 0.04),
            specialties=list(s.specialties),
            rating=s.rating,
            hourly_rate_usd=s.hourly_rate_usd,
            open_24h=s.open_24h,
        )
        for i, s in enumerate(SHOPS)
    ]

    stations: list[Station] = []
    for c in CITIES:
        for suffix in ("Travel Center", "Fuel Stop"):
            stations.append(
                Station(
                    name=f"{c.name} {suffix}",
                    city=c,
                    lat=c.lat + rng.uniform(-0.05, 0.05),
                    lon=c.lon + rng.uniform(-0.05, 0.05),
                )
            )
    return Fleet(vehicles=vehicles, drivers=drivers, shops=shops, stations=stations, depots=depots)

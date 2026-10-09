"""Static reference data for the synthetic world. Fictional makes, real-ish geography."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lon: float


CITIES: list[City] = [
    City("Dallas", 32.78, -96.80),
    City("Fort Worth", 32.75, -97.33),
    City("Houston", 29.76, -95.37),
    City("San Antonio", 29.42, -98.49),
    City("Austin", 30.27, -97.74),
    City("Oklahoma City", 35.47, -97.52),
    City("Tulsa", 36.15, -95.99),
    City("Amarillo", 35.22, -101.83),
    City("Lubbock", 33.58, -101.86),
    City("El Paso", 31.76, -106.49),
    City("Shreveport", 32.52, -93.75),
    City("Little Rock", 34.75, -92.29),
    City("Memphis", 35.15, -90.05),
    City("Waco", 31.55, -97.15),
    City("Corpus Christi", 27.80, -97.40),
    City("Laredo", 27.51, -99.51),
    City("Midland", 31.99, -102.08),
    City("Texarkana", 33.44, -94.04),
    City("Wichita Falls", 33.91, -98.49),
    City("Abilene", 32.45, -99.73),
    City("Baton Rouge", 30.45, -91.19),
    City("Beaumont", 30.08, -94.10),
]
CITY_BY_NAME = {c.name: c for c in CITIES}

DEPOTS: list[tuple[str, str]] = [  # (depot name, city)
    ("North Yard", "Dallas"),
    ("South Yard", "San Antonio"),
    ("Gulf Yard", "Houston"),
]

MAKES_MODELS: list[tuple[str, str]] = [
    ("Northstar", "NS-620"),
    ("Kestrel", "K-900 Sleeper"),
    ("Ironline", "IL-480 Daycab"),
    ("Meridian", "M-770"),
    ("Northstar", "NS-580 Daycab"),
    ("Kestrel", "K-850"),
]

FIRST_NAMES = [
    "Avery",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Taylor",
    "Dakota",
    "Quinn",
    "Reese",
    "Hayden",
    "Emerson",
    "Rowan",
    "Sawyer",
    "Finley",
    "Harper",
    "Skyler",
    "Blake",
    "Devon",
    "Jamie",
    "Kendall",
    "Logan",
    "Micah",
    "Parker",
    "Shiloh",
    "Tatum",
    "Alexis",
    "Cameron",
    "Drew",
    "Ellis",
    "Frankie",
    "Gray",
    "Jules",
    "Kai",
    "Lane",
    "Marlow",
    "Noel",
    "Oakley",
    "Peyton",
    "Remy",
    "Sage",
]
LAST_NAMES = [
    "Okafor",
    "Nguyen",
    "Alvarez",
    "Brennan",
    "Castillo",
    "Delgado",
    "Ellison",
    "Fischer",
    "Garza",
    "Holloway",
    "Ibarra",
    "Jensen",
    "Kowalski",
    "Lindqvist",
    "Mendoza",
    "Novak",
    "Ortega",
    "Patel",
    "Quintero",
    "Rasmussen",
    "Sandoval",
    "Thao",
    "Underwood",
    "Varga",
    "Whitaker",
    "Xiong",
    "Yamamoto",
    "Zielinski",
    "Abernathy",
    "Bautista",
    "Cormier",
    "Dufresne",
    "Espinoza",
    "Farrow",
    "Gallo",
]

STATES = ["TX", "OK", "LA", "AR", "NM"]


@dataclass(frozen=True)
class ShopSeed:
    name: str
    city: str
    specialties: tuple[str, ...]
    rating: float
    hourly_rate_usd: float
    open_24h: bool


SHOPS: list[ShopSeed] = [
    ShopSeed(
        "Lone Star Diesel & Cooling", "Dallas", ("cooling", "engine", "brakes"), 4.6, 145, False
    ),
    ShopSeed(
        "Trinity Fleet Electrical", "Dallas", ("electrical", "aftertreatment"), 4.4, 150, True
    ),
    ShopSeed("Metroplex Truck Care", "Fort Worth", ("brakes", "tires", "cooling"), 4.1, 130, False),
    ShopSeed(
        "Bayou City Heavy Repair", "Houston", ("engine", "transmission", "cooling"), 4.5, 155, True
    ),
    ShopSeed("Gulf Coast Diesel Electric", "Houston", ("electrical", "engine"), 4.2, 140, False),
    ShopSeed(
        "Alamo Fleet Services",
        "San Antonio",
        ("cooling", "aftertreatment", "tires"),
        4.3,
        135,
        False,
    ),
    ShopSeed(
        "Mission Diesel Works", "San Antonio", ("electrical", "brakes", "engine"), 4.0, 125, True
    ),
    ShopSeed(
        "Capital Truck Center", "Austin", ("cooling", "electrical", "transmission"), 4.4, 150, False
    ),
    ShopSeed(
        "Red River Diesel",
        "Oklahoma City",
        ("engine", "cooling", "aftertreatment"),
        4.2,
        135,
        False,
    ),
    ShopSeed(
        "Panhandle Truck & Trailer", "Amarillo", ("tires", "brakes", "electrical"), 3.9, 120, True
    ),
    ShopSeed("Caprock Fleet Repair", "Lubbock", ("cooling", "engine"), 4.1, 130, False),
    ShopSeed(
        "Ark-La-Tex Diesel",
        "Shreveport",
        ("electrical", "aftertreatment", "brakes"),
        4.0,
        125,
        False,
    ),
    ShopSeed("Brazos Valley Truck Care", "Waco", ("cooling", "brakes"), 4.3, 130, False),
    ShopSeed(
        "Border Fleet Maintenance", "Laredo", ("tires", "engine", "electrical"), 3.8, 115, True
    ),
    ShopSeed("Permian Heavy Duty", "Midland", ("engine", "cooling", "electrical"), 4.2, 160, True),
]


@dataclass(frozen=True)
class DtcSpec:
    code: str
    description: str
    system: str
    severity: str  # low|medium|high|critical


DTC_CATALOG: dict[str, DtcSpec] = {
    s.code: s
    for s in [
        DtcSpec("P0217", "Engine coolant over-temperature condition", "cooling", "high"),
        DtcSpec("P0128", "Coolant thermostat below regulating temperature", "cooling", "low"),
        DtcSpec("P0117", "Engine coolant temperature sensor circuit low", "cooling", "medium"),
        DtcSpec(
            "SPN110-FMI0",
            "J1939 engine coolant temperature above normal (most severe)",
            "cooling",
            "critical",
        ),
        DtcSpec("P0562", "System voltage low", "electrical", "medium"),
        DtcSpec("P0563", "System voltage high", "electrical", "medium"),
        DtcSpec(
            "SPN168-FMI18",
            "J1939 battery potential below normal (moderately severe)",
            "electrical",
            "medium",
        ),
        DtcSpec("U0100", "Lost communication with ECM/PCM", "electrical", "medium"),
        DtcSpec("P0401", "EGR flow insufficient detected", "aftertreatment", "low"),
        DtcSpec(
            "P2002",
            "Diesel particulate filter efficiency below threshold",
            "aftertreatment",
            "medium",
        ),
        DtcSpec("P2463", "Diesel particulate filter soot accumulation", "aftertreatment", "medium"),
        DtcSpec("P0420", "Catalyst system efficiency below threshold", "aftertreatment", "low"),
        DtcSpec("P0087", "Fuel rail/system pressure too low", "fuel", "medium"),
        DtcSpec("P0101", "Mass air flow sensor circuit range/performance", "air_intake", "low"),
        DtcSpec("P0520", "Engine oil pressure sensor circuit", "lubrication", "medium"),
        DtcSpec("P0524", "Engine oil pressure too low", "lubrication", "critical"),
        DtcSpec("P0300", "Random/multiple cylinder misfire detected", "engine", "medium"),
    ]
}

NOISE_DTC_CODES = ["P0401", "P0420", "P0101", "P2463", "U0100"]

HARSH_TYPES: list[tuple[str, float]] = [
    ("harsh_brake", 0.6),
    ("harsh_accel", 0.25),
    ("harsh_corner", 0.15),
]

# HOS limits (property-carrying, US-style, in minutes)
DRIVE_LIMIT_MIN = 11 * 60
DUTY_LIMIT_MIN = 14 * 60
CYCLE_LIMIT_MIN = 70 * 60
BREAK_REQUIRED_AFTER_MIN = 8 * 60

AVG_SPEED_MPH = 58.0
ROAD_FACTOR = 1.17
TRACTOR_MPG = 6.5
REEFER_TRACTOR_MPG = 5.6
TANK_GAL = 150.0
REEFER_TANK_GAL = 50.0

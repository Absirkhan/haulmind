"""Display copy: the one place where raw backend values become fleet language.

Everything the UI shows passes through here. Every mapping has a plain-English fallback, so a
value this module does not know is still readable (``humanize``) and never raw. Nothing here
touches the database schema or the API: it is a read-only translation layer for templates.

Vocabulary rules (see the redesign brief): no ids, no enum values, no tool names, no model or
pipeline jargon outside the "Technical details" disclosure. Say what happened, why it matters,
and what to do, in that order.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from haulmind.simulator.data import DTC_CATALOG

# --- small vocabularies ------------------------------------------------------------------------

TIER_LABEL: dict[int, str] = {
    0: "Looked up",
    1: "Handled for you",
    2: "Needs your OK",
    3: "For your decision",
}
TIER_HELP: dict[int, str] = {
    1: "Low-risk step Haulmind carried out on its own.",
    2: "Haulmind prepared this and is waiting for your approval before anything runs.",
    3: "Haulmind only recommends this. Nothing runs unless you do it yourself.",
}
TAB_LABEL: dict[str, str] = {
    "pending": "Needs your OK",
    "recommended": "For your decision",
    "auto": "Handled for you",
    "decided": "Done",
}
STATUS_LABEL: dict[str, str] = {
    "pending": "Waiting for your OK",
    "approved": "Approved, running now",
    "executed": "Done",
    "auto_executed": "Handled for you",
    "recommended": "For your decision",
    "acknowledged": "You handled it",
    "rejected": "Dismissed",
    "blocked": "Stopped by a safety rule",
    "rate_limited": "Held back, too many of these recently",
    "approval_invalid": "Approval did not match, nothing ran",
    "failed": "Could not be carried out",
}
SEVERITY_LABEL: dict[str, str] = {
    "low": "Low urgency",
    "medium": "Worth a look",
    "high": "Urgent",
    "critical": "Act now",
}
DOMAIN_LABEL: dict[str, str] = {
    "safety": "Driving safety",
    "maintenance": "Truck health",
    "compliance": "Hours of service",
    "spend": "Fuel spend",
}
INCIDENT_STATUS_LABEL: dict[str, str] = {
    "open": "Waiting on you",
    "investigating": "Being looked into",
    "resolved": "Resolved",
    "auto_handled": "Handled for you",
    "no_action": "Nothing needed",
    "dismissed": "Dismissed",
}
EVENT_LABEL: dict[str, str] = {
    "gps_ping": "Location update",
    "harsh_event": "Harsh driving event",
    "speeding": "Speeding",
    "hos_update": "Hours-of-service update",
    "dtc_fault": "Fault code",
    "engine_metrics": "Engine reading",
    "fuel_transaction": "Fuel purchase",
    "trip_start": "Trip started",
    "trip_end": "Trip ended",
    "idle_event": "Idling",
}
DUTY_LABEL: dict[str, str] = {
    "driving": "Driving",
    "on_duty": "On duty, not driving",
    "off_duty": "Off duty",
    "sleeper": "In the sleeper berth",
    "sleeper_berth": "In the sleeper berth",
}
VEHICLE_STATUS_LABEL: dict[str, str] = {
    "active": "On the road",
    "in_shop": "In the shop",
    "oos": "Out of service",
    "out_of_service": "Out of service",
}
SYSTEM_LABEL: dict[str, str] = {
    "cooling": "cooling system",
    "electrical": "electrical system",
    "aftertreatment": "exhaust system",
    "fuel": "fuel system",
    "air_intake": "air intake",
    "lubrication": "oil system",
    "engine": "engine",
}
HARSH_LABEL: dict[str, str] = {
    "harsh_brake": "hard braking",
    "harsh_braking": "hard braking",
    "harsh_accel": "hard acceleration",
    "harsh_acceleration": "hard acceleration",
    "harsh_corner": "hard cornering",
    "harsh_cornering": "hard cornering",
}
URGENCY_LABEL: dict[str, str] = {"normal": "Normal", "high": "High", "low": "Low"}
PRIORITY_LABEL: dict[str, str] = {
    "normal": "Normal",
    "high": "High",
    "urgent": "Urgent",
    "low": "Low",
}

# Dismiss reasons, in the words a manager would use. Keys are the backend reason codes.
REASON_LABEL: dict[str, str] = {
    "not_an_issue": "Not a real problem",
    "known_intermittent": "Known issue on this truck, usually harmless",
    "legit_fuel": "That fuel purchase was fine",
    "driver_manages_clock": "This driver manages their own hours",
    "wrong_action": "Right problem, wrong fix",
    "duplicate": "Already covered by another item",
    "other": "Something else",
}

INCIDENT_TYPE_LABEL: dict[str, str] = {
    "hos_violation_risk": "Driver is close to running out of drive time",
    "hos_near_miss": "Driver is cutting it close on drive time",
    "hos_break_risk": "Driver is due for a break",
    "failing_component": "A part on the truck looks like it is failing",
    "nuisance_dtc": "A usually harmless fault code keeps coming back",
    "nuisance_codes": "A usually harmless fault code keeps coming back",
    "noise_dtc_recurrence": "A fault code came back",
    "fuel_fraud": "A fuel purchase looks unusual",
    "reefer_fuel_lookalike": "A large fill that matched the tractor and reefer tanks",
    "harsh_driving_pattern": "A pattern of harsh braking or cornering",
    "cooling_system_overheat_risk": "The engine is running hotter than it should",
    "cooling_system_drift": "The engine is running hotter than it should",
    "cooling_thermostat_drift": "The engine is not reaching its normal temperature",
    "electrical_communication_loss": "The engine computer dropped offline",
    "fuel_overfill": "A fuel purchase was bigger than the tank",
}
INCIDENT_WHY: dict[str, str] = {
    "compliance": "Driving past the limit is a federal violation, and the load would arrive late.",
    "maintenance": "Left alone, a fault like this can strand the truck mid-route or turn into an expensive repair.",
    "spend": "This is what fuel-card misuse usually looks like. The purchase is worth a second look.",
    "safety": "Patterns like this often show up before a crash or a customer complaint.",
}

# Write tools (the things Haulmind can do) in three tenses.
ACTION_COPY: dict[str, dict[str, str]] = {
    "message_driver": {
        "title": "Send the driver a message",
        "doing": "Sending the driver a message",
        "done": "Message sent to the driver",
        "button": "Approve and send",
        "icon": "message-square",
    },
    "flag_fuel_transaction": {
        "title": "Hold the fuel purchase for review",
        "doing": "Holding the fuel purchase for review",
        "done": "Fuel purchase held for review",
        "button": "Approve and hold",
        "icon": "fuel",
    },
    "create_work_order": {
        "title": "Schedule service",
        "doing": "Scheduling service",
        "done": "Service scheduled",
        "button": "Approve and schedule",
        "icon": "wrench",
    },
    "propose_reassignment": {
        "title": "Hand the trip to another driver",
        "doing": "Handing the trip to another driver",
        "done": "Trip handed to another driver",
        "button": "Approve the handoff",
        "icon": "route",
    },
    "suspend_fuel_card": {
        "title": "Suspend the fuel card",
        "doing": "Suspending the fuel card",
        "done": "Fuel card suspended",
        "button": "I've suspended the card",
        "icon": "ban",
    },
    "set_vehicle_out_of_service": {
        "title": "Take the truck off the road",
        "doing": "Taking the truck off the road",
        "done": "Truck taken off the road",
        "button": "I've grounded the truck",
        "icon": "octagon-alert",
    },
}

# Read tools, as the step a person would describe. ``{truck}``, ``{driver}``, ``{hours}``,
# ``{codes}``, ``{specialty}``, ``{what}`` are filled from the call arguments.
CHECK_COPY: dict[str, tuple[str, str]] = {
    "get_vehicle_status": ("Checked {truck}'s current status", "truck"),
    "get_driver_hos": ("Looked at {driver}'s remaining hours", "clock"),
    "find_idle_vehicles": ("Looked for a rested spare driver nearby", "user"),
    "list_hos_risks": ("Checked which drivers are close to their limits", "clock"),
    "get_fault_history": ("Looked at the last {hours} of fault codes on {truck}", "wrench"),
    "search_fault_docs": ("Read the service guidance for {codes}", "book-open"),
    "find_nearby_shops": ("Found {specialty} shops near the truck", "map-pin"),
    "get_fuel_transactions": ("Compared recent fuel purchases with {truck}'s tank size", "fuel"),
    "get_recent_events": ("Reviewed the last {hours} of {what}", "activity"),
}
EVENT_GROUP_LABEL: dict[str, str] = {
    "harsh_event": "driving events",
    "speeding": "speeding",
    "hos_update": "hours updates",
    "trip_start": "trip starts",
    "dtc_fault": "fault codes",
    "engine_metrics": "engine readings",
    "fuel_transaction": "fuel purchases",
}
STEP_COPY: dict[str, tuple[str, str]] = {
    "triage": ("Took a first look at the alerts", "eye"),
    "investigate": ("Gathered the facts", "search"),
    "plan": ("Decided what to recommend", "clipboard-check"),
    "verify": ("Double-checked the recommendation against the rules", "shield-check"),
    "dispatch": ("Carried out the low-risk steps", "zap"),
    "single_agent": ("Looked at the alerts and decided what to recommend", "clipboard-check"),
}
ACTOR_LABEL: dict[str, str] = {
    "manager": "You",
    "agent": "Haulmind",
    "policy-gateway": "Safety rules",
    "system": "Haulmind",
    "mcp-client": "An assistant",
    "claude-desktop": "An assistant",
    "eval-agent": "Haulmind",
    "test-agent": "Haulmind",
}

PROVIDER_LABEL: dict[str, str] = {
    "stub": "offline practice mode",
    "gemini": "Gemini",
    "gemini-openai": "Gemini",
    "groq": "Groq",
    "mistral": "Mistral",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "ollama": "a local model",
    "local": "a local model",
    "openai-compatible": "a hosted model",
}

_ABBREVIATIONS = {
    "hos": "hours of service",
    "dtc": "fault code",
    "dtcs": "fault codes",
    "wo": "work order",
    "wos": "work orders",
    "ecm": "engine computer",
    "pcm": "engine computer",
    "ecm/pcm": "engine computer",
    "egr": "exhaust recirculation",
    "dpf": "exhaust filter",
    "def": "exhaust fluid",
    "llm": "model",
    "mcp": "tool",
    "eta": "arrival time",
    "txn": "purchase",
    "txns": "purchases",
}


# --- generic helpers ---------------------------------------------------------------------------


def humanize(raw: Any) -> str:
    """Last-resort plain English for any raw token: snake_case, camelCase, UPPER, enum-ish."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)  # camelCase -> camel Case
    text = text.replace("_", " ").replace("-", " ").strip()
    words = text.lower().split()
    words = [_ABBREVIATIONS.get(w, w) for w in words]
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def label(mapping: dict[Any, str], raw: Any) -> str:
    """Look ``raw`` up in ``mapping``; never return the raw value."""
    if raw in mapping:
        return mapping[raw]
    return humanize(raw)


def confidence_label(value: float | None) -> str:
    if value is None:
        return "Unsure"
    if value >= 0.85:
        return "Very sure"
    if value >= 0.65:
        return "Fairly sure"
    if value >= 0.45:
        return "Not sure"
    return "Unsure"


def confidence_level(value: float | None) -> str:
    """Three-step scale for a meter."""
    if value is None or value < 0.45:
        return "low"
    if value < 0.85:
        return "medium"
    return "high"


def minutes(n: float | int | None) -> str:
    """'45 minutes', '1 hour 10 minutes', '2 hours'."""
    if n is None:
        return "an unknown time"
    m = round(abs(float(n)))
    h, r = divmod(m, 60)
    if h == 0:
        return f"{m} minute{'s' if m != 1 else ''}"
    if r == 0:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{h} hour{'s' if h != 1 else ''} {r} minute{'s' if r != 1 else ''}"


def hours_phrase(hours: float | int | None) -> str:
    if hours is None:
        return "few hours"
    h = float(hours)
    if h >= 48 and h % 24 == 0:
        d = int(h // 24)
        return f"{d} days"
    if h == 24:
        return "day"
    return f"{int(h)} hours"


def ago(ts: datetime | None, now: datetime | None) -> str:
    """Relative wording for a timestamp against a reference clock ('2 hours ago')."""
    if ts is None:
        return ""
    if now is None:
        return ts.strftime("%a %H:%M")
    delta = now - ts
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 6 * 3600:
        h = int(seconds // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if ts.date() == now.date():
        return f"today {ts.strftime('%H:%M')}"
    if ts.date() == (now - timedelta(days=1)).date():
        return f"yesterday {ts.strftime('%H:%M')}"
    return ts.strftime("%a %H:%M")


def full_time(ts: datetime | None) -> str:
    return ts.strftime("%A %d %B %Y, %H:%M") if ts else ""


_PLAIN_DESC = (
    (re.compile(r"^J1939\s+"), ""),
    (re.compile(r"\bECM/PCM\b|\bECM\b|\bPCM\b"), "the engine computer"),
    (re.compile(r"\bEGR\b"), "exhaust recirculation"),
    (re.compile(r"\bdiesel particulate filter\b", re.IGNORECASE), "exhaust filter"),
    (re.compile(r"\bmass air flow sensor\b", re.IGNORECASE), "air flow sensor"),
    (re.compile(r"\s*\((?:most|moderately) severe\)"), ""),
    (re.compile(r"\bcircuit low\b"), "reading low"),
    (re.compile(r"\bcircuit\b"), "wiring"),
)


def fault_code(code: str, description: str | None = None) -> str:
    """'Engine coolant over-temperature condition (code P0217)'."""
    spec = DTC_CATALOG.get(code)
    text = description or (spec.description if spec else "")
    for pattern, repl in _PLAIN_DESC:
        text = pattern.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"Fault code {code}"
    return f"{text[:1].upper()}{text[1:]} (code {code})"


def article(word: str) -> str:
    """'a' or 'an' for the word that follows."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def fault_system(code: str) -> str:
    spec = DTC_CATALOG.get(code)
    return SYSTEM_LABEL.get(spec.system, spec.system) if spec else "engine"


def system_label(system: str | None) -> str:
    if not system:
        return "engine"
    return SYSTEM_LABEL.get(system, humanize(system).lower())


def join_words(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


# --- names: ids -> people and trucks -----------------------------------------------------------


@dataclass
class Names:
    """Lookups from synthetic ids to the words a dispatcher uses."""

    vehicles: dict[str, Any] = field(default_factory=dict)  # id -> Vehicle row
    drivers: dict[str, Any] = field(default_factory=dict)  # id -> Driver row
    shops: dict[str, Any] = field(default_factory=dict)  # id -> Shop row
    trips: dict[str, Any] = field(default_factory=dict)  # id -> Trip row
    fuel: dict[str, Any] = field(default_factory=dict)  # id -> FuelTransaction row

    @classmethod
    def load(cls, s: Any) -> Names:
        from sqlalchemy import select

        from haulmind.db.models import Driver, FuelTransaction, Shop, Trip, Vehicle

        return cls(
            vehicles={v.id: v for v in s.execute(select(Vehicle)).scalars()},
            drivers={d.id: d for d in s.execute(select(Driver)).scalars()},
            shops={x.id: x for x in s.execute(select(Shop)).scalars()},
            trips={t.id: t for t in s.execute(select(Trip)).scalars()},
            fuel={f.id: f for f in s.execute(select(FuelTransaction)).scalars()},
        )

    # people and trucks
    def truck(self, vehicle_id: str | None) -> str:
        v = self.vehicles.get(vehicle_id or "")
        return f"Truck {v.unit_number}" if v else "the truck"

    def unit(self, vehicle_id: str | None) -> str:
        v = self.vehicles.get(vehicle_id or "")
        return v.unit_number if v else ""

    def truck_detail(self, vehicle_id: str | None) -> str:
        v = self.vehicles.get(vehicle_id or "")
        if not v:
            return ""
        return f"{v.year} {v.make} {v.model}" + (" with reefer" if v.has_reefer else "")

    def driver(self, driver_id: str | None) -> str:
        d = self.drivers.get(driver_id or "")
        return d.name if d else "the driver"

    def driver_first(self, driver_id: str | None) -> str:
        d = self.drivers.get(driver_id or "")
        return d.name.split()[0] if d else "the driver"

    def phone(self, driver_id: str | None) -> str:
        d = self.drivers.get(driver_id or "")
        return d.phone if d and d.phone else ""

    def driver_for_card(self, card_id: str | None) -> str:
        for d in self.drivers.values():
            if d.fuel_card_id == card_id:
                return str(d.name)
        return "the driver"

    def shop(self, shop_id: str | None) -> str:
        x = self.shops.get(shop_id or "")
        return f"{x.name} in {x.city}" if x else "a nearby shop"

    def trip(self, trip_id: str | None) -> str:
        t = self.trips.get(trip_id or "")
        return f"the trip to {t.destination}" if t else "the current trip"

    def destination(self, trip_id: str | None) -> str:
        t = self.trips.get(trip_id or "")
        return t.destination if t else "its destination"

    def purchase(self, txn_id: str | None) -> str:
        f = self.fuel.get(txn_id or "")
        if not f:
            return "the fuel purchase"
        return f"the {f.gallons:.0f} gallon purchase at {f.station}"

    def who(self, vehicle_id: str | None, driver_id: str | None) -> str:
        """'Truck 1209, Gray Sandoval' or whichever half is known."""
        parts = []
        if vehicle_id and vehicle_id in self.vehicles:
            parts.append(self.truck(vehicle_id))
        if driver_id and driver_id in self.drivers:
            parts.append(self.driver(driver_id))
        return ", ".join(parts)

    # prose rewriting: model-written text still contains ids and abbreviations
    _ID_PATTERNS = (
        (re.compile(r"\b[Tt]rip (T-V-\d+-\d+)\b"), "trip"),
        (re.compile(r"\b(T-V-\d+-\d+)\b"), "trip_bare"),
        (re.compile(r"\b[Vv]ehicle (V-\d+)(?: \(?unit \d+\)?)?"), "truck"),
        (re.compile(r"\b[Tt]ruck (V-\d+)\b"), "truck"),
        (re.compile(r"\b(V-\d+)\b"), "truck"),
        (re.compile(r"\b[Dd]river (D-\d+)\b"), "driver"),
        (re.compile(r"\b(D-\d+)\b"), "driver"),
        (re.compile(r"\b(S-\d+)\b"), "shop"),
        (re.compile(r"\b(FC-\d+)\b"), "card"),
        (re.compile(r"\b(FT-[A-Za-z0-9-]+)\b"), "purchase"),
    )
    _JARGON = (
        (re.compile(r"\bto None\b"), "to the destination"),
        (re.compile(r"\bNone\b"), "unknown"),
        (re.compile(r"\bHOS\b"), "hours-of-service"),
        (re.compile(r"\bDTCs\b"), "fault codes"),
        (re.compile(r"\bDTC\b"), "fault code"),
        (re.compile(r"\bWOs?\b"), "work order"),
        (re.compile(r"\bECM/PCM\b|\bECM\b"), "engine computer"),
        (re.compile(r"\bLLM\b"), "model"),
        (re.compile(r"\bmin\b"), "minutes"),
        (re.compile(r"\b(\d+)\s*min\b"), r"\1 minutes"),
        (re.compile(r"\bgal\b"), "gallons"),
        (re.compile(r"\b(\d+(?:\.\d+)?)\s*F\b"), r"\1°F"),
        (re.compile(r"\b(\d+(?:\.\d+)?)\s*V\b"), r"\1 volts"),
        (re.compile(r"\bpsi\b"), "psi"),
        (re.compile(r"\bE(\d+)\b"), r"item \1"),
        (re.compile(r"\bEvidence item (\d+)\b"), r"Finding \1"),
        (re.compile(r"\bevidence item (\d+)\b"), r"finding \1"),
        (re.compile(r"\bcluster\b", re.IGNORECASE), "group of alerts"),
        (re.compile(r"\btriage\b", re.IGNORECASE), "first look"),
        (re.compile(r"\bverifier\b", re.IGNORECASE), "double-check"),
    )

    def rewrite(self, text: str | None) -> str:
        """Swap ids for names and expand abbreviations in model-written prose."""
        if not text:
            return ""
        out = str(text)
        for pattern, kind in self._ID_PATTERNS:
            out = pattern.sub(self._replacer(kind), out)
        for pattern, repl in self._JARGON:
            out = pattern.sub(repl, out)
        out = re.sub(r"\s+", " ", out).strip()
        return out

    def _replacer(self, kind: str) -> Callable[[re.Match[str]], str]:
        def sub(m: re.Match[str]) -> str:
            return self._replace_id(kind, m.group(1))

        return sub

    def _replace_id(self, kind: str, raw: str) -> str:
        if kind == "trip":
            return self.trip(raw)
        if kind == "trip_bare":
            return self.trip(raw)
        if kind == "truck":
            return self.truck(raw)
        if kind == "driver":
            return self.driver(raw)
        if kind == "shop":
            return self.shop(raw)
        if kind == "card":
            return f"{self.driver_for_card(raw)}'s fuel card"
        if kind == "purchase":
            return self.purchase(raw)
        return humanize(raw)


# --- incidents: what happened and why it matters -----------------------------------------------


def headline(inc: Any, names: Names) -> str:
    """What happened, as a dispatcher would say it. Built from the incident's facts."""
    sig: dict[str, Any] = inc.signals_json or {}
    t = inc.type or ""
    driver = names.driver(inc.driver_id)
    truck = names.truck(inc.vehicle_id)
    dest = names.destination(sig.get("trip_id"))
    if t in ("hos_violation_risk", "hos_near_miss"):
        margin = sig.get("margin_min")
        if margin is None:
            return f"{driver} is close to running out of drive time."
        m = int(margin)
        if m < 0:
            return f"{driver} will run out of drive time about {minutes(m)} before reaching {dest}."
        if m == 0:
            return f"{driver} will hit the drive-time limit right as the truck reaches {dest}."
        return f"{driver} will reach {dest} with only {minutes(m)} of drive time to spare."
    if t == "hos_break_risk":
        return f"{driver} is overdue for a 30-minute break."
    if inc.domain == "maintenance":
        codes = [c for c in (sig.get("codes") or []) if c]
        system = sig.get("system") or (fault_system(codes[0]) if codes else "engine")
        system_text = system_label(system) if system in SYSTEM_LABEL else system
        if t in ("nuisance_dtc", "nuisance_codes", "noise_dtc_recurrence"):
            base = f"A usually harmless fault code came back on {truck}"
        else:
            base = f"{truck} shows signs of {article(system_text)} {system_text} problem"
        return base + "."
    if inc.domain == "spend":
        gallons = sig.get("gallons")
        station = sig.get("station")
        where = f" at {station}" if station else ""
        amount = f"A {float(gallons):.0f} gallon fill" if gallons else "A fuel purchase"
        reasons = []
        if sig.get("overfill"):
            reasons.append("more than the tank holds")
        if sig.get("location_mismatch"):
            d = sig.get("distance_from_vehicle_mi")
            reasons.append(
                f"{float(d):.0f} miles from where the truck was" if d else "far from the truck"
            )
        if sig.get("rapid_repeat"):
            reasons.append("soon after the previous fill")
        tail = f": {join_words(reasons)}" if reasons else " looks unusual"
        return f"{amount} on {truck}{where}{tail}."
    if inc.domain == "safety":
        n = sig.get("harsh_count_48h")
        trips = sig.get("distinct_trips")
        kind = HARSH_LABEL.get(str(sig.get("latest") or ""), "harsh braking or cornering")
        if n:
            across = f" across {trips} trips" if trips else ""
            return f"{driver} had {n} {kind} events in 2 days{across}."
        return f"{driver} is showing a pattern of {kind}."
    if t in INCIDENT_TYPE_LABEL:
        return INCIDENT_TYPE_LABEL[t] + "."
    return names.rewrite(inc.summary) or humanize(t) + "."


def why_it_matters(inc: Any) -> str:
    return INCIDENT_WHY.get(inc.domain, "Worth a look before it becomes a bigger problem.")


def incident_kind(inc: Any) -> str:
    """Short noun phrase for lists and filters."""
    t = inc.type or ""
    if t in INCIDENT_TYPE_LABEL:
        return INCIDENT_TYPE_LABEL[t]
    return humanize(t)


# --- actions: what will happen, in one sentence --------------------------------------------------


def action_title(act: Any, names: Names) -> str:
    """The recommended step, as a verb phrase. Prefers the facts over the model's title."""
    args: dict[str, Any] = act.args_json or {}
    tool = act.tool_name
    if tool == "propose_reassignment":
        to = names.driver(args.get("to_driver_id"))
        return f"Hand {names.trip(args.get('trip_id'))} to {to}"
    if tool == "create_work_order":
        vehicle = names.truck(args.get("vehicle_id") or act.vehicle_id)
        shop = args.get("shop_id")
        where = f" at {names.shop(shop)}" if shop else ""
        return f"Schedule service for {vehicle}{where}"
    if tool == "message_driver":
        return (
            f"Tell {names.driver_first(args.get('driver_id') or act.driver_id)} to plan a safe stop"
            if "stop" in str(args.get("body", "")).lower()
            else f"Send {names.driver_first(args.get('driver_id') or act.driver_id)} a message"
        )
    if tool == "flag_fuel_transaction":
        return f"Hold {names.purchase(args.get('transaction_id'))} for review"
    if tool == "suspend_fuel_card":
        return f"Suspend {names.driver_for_card(args.get('card_id'))}'s fuel card"
    if tool == "set_vehicle_out_of_service":
        return f"Take {names.truck(args.get('vehicle_id') or act.vehicle_id)} off the road"
    return names.rewrite(act.title) or label({k: v["title"] for k, v in ACTION_COPY.items()}, tool)


def action_plan(act: Any, names: Names) -> list[tuple[str, str]]:
    """What exactly will happen, as (label, value) pairs a manager can read and edit."""
    args: dict[str, Any] = act.args_json or {}
    tool = act.tool_name
    rows: list[tuple[str, str]] = []
    if tool == "message_driver":
        rows.append(("To", names.driver(args.get("driver_id") or act.driver_id)))
        rows.append(("Message", names.rewrite(str(args.get("body") or ""))))
        rows.append(("Urgency", label(URGENCY_LABEL, args.get("urgency") or "normal")))
    elif tool == "create_work_order":
        rows.append(("Truck", names.truck(args.get("vehicle_id") or act.vehicle_id)))
        if args.get("shop_id"):
            rows.append(("Shop", names.shop(args.get("shop_id"))))
        rows.append(("Work needed", names.rewrite(str(args.get("description") or ""))))
        rows.append(("Priority", label(PRIORITY_LABEL, args.get("priority") or "normal")))
        codes = [c for c in (args.get("fault_codes") or []) if c]
        if codes:
            rows.append(("Fault codes", join_words([fault_code(c) for c in codes])))
        if args.get("estimated_cost_usd"):
            rows.append(("Estimated cost", f"${float(args['estimated_cost_usd']):,.0f}"))
    elif tool == "propose_reassignment":
        rows.append(("Trip", names.trip(args.get("trip_id"))))
        rows.append(("From", names.driver(args.get("from_driver_id"))))
        rows.append(("To", names.driver(args.get("to_driver_id"))))
        rows.append(("Reason", names.rewrite(str(args.get("reason") or ""))))
    elif tool == "flag_fuel_transaction":
        purchase = names.purchase(args.get("transaction_id"))
        rows.append(("Purchase", purchase[:1].upper() + purchase[1:]))
        rows.append(("Reason", reason_text(names, args.get("reason"))))
    elif tool == "suspend_fuel_card":
        rows.append(("Card holder", names.driver_for_card(args.get("card_id"))))
        rows.append(("Reason", names.rewrite(str(args.get("reason") or ""))))
    elif tool == "set_vehicle_out_of_service":
        rows.append(("Truck", names.truck(args.get("vehicle_id") or act.vehicle_id)))
        rows.append(("Reason", names.rewrite(str(args.get("reason") or ""))))
    else:
        for k, v in args.items():
            if v in (None, "", []):
                continue
            rows.append(
                (
                    humanize(k),
                    names.rewrite(", ".join(map(str, v)) if isinstance(v, list) else str(v)),
                )
            )
    return [(k, v) for k, v in rows if v]


# Which argument fields a manager may edit, with the label and input kind for each tool.
EDITABLE_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "message_driver": [("body", "Message", "text"), ("urgency", "Urgency", "urgency")],
    "create_work_order": [
        ("description", "Work needed", "text"),
        ("priority", "Priority", "priority"),
        ("shop_id", "Shop", "shop"),
    ],
    "propose_reassignment": [
        ("to_driver_id", "Hand the trip to", "driver"),
        ("reason", "Reason", "text"),
    ],
    "flag_fuel_transaction": [("reason", "Reason", "text")],
    "suspend_fuel_card": [("reason", "Reason", "text")],
    "set_vehicle_out_of_service": [("reason", "Reason", "text")],
}


FLAG_LABEL: dict[str, str] = {
    "overfill": "more fuel than the tank holds",
    "location_mismatch": "bought far from where the truck was",
    "rapid_repeat": "too soon after the previous fill",
    "exceeds_total_capacity": "more than the tractor and reefer tanks hold together",
}


def reason_text(names: Names, raw: Any) -> str:
    """Reasons written as flag keys ('overfill, rapid_repeat') become a sentence."""
    text = str(raw or "").strip()
    if not text:
        return ""
    tokens = [t.strip() for t in re.split(r"[,;/]+|\s+and\s+", text) if t.strip()]
    if tokens and all(t in FLAG_LABEL for t in tokens):
        out = join_words([FLAG_LABEL[t] for t in tokens])
        return out[:1].upper() + out[1:]
    return names.rewrite(text)


def action_result(act: Any, names: Names) -> str:
    """One sentence about what came back after the action ran, if anything."""
    res: dict[str, Any] = act.result_json or {}
    if not res:
        return ""
    msg = res.get("message")
    if act.status == "executed" or act.status == "auto_executed":
        copy = ACTION_COPY.get(act.tool_name, {})
        return str(copy.get("done", "Done")) + "."
    if msg:
        return names.rewrite(str(msg))
    return ""


def action_status_sentence(act: Any, names: Names, now: datetime | None) -> str:
    st = act.status
    who = ACTOR_LABEL.get(act.decided_by or "", act.decided_by or "")
    when = ago(act.decided_ts, now)
    if st == "executed":
        return f"{who or 'You'} approved this {when}. {ACTION_COPY.get(act.tool_name, {}).get('done', 'Done')}."
    if st == "auto_executed":
        return f"Haulmind handled this on its own {ago(act.executed_ts or act.created_ts, now)}."
    if st == "rejected":
        reason = names.rewrite(_reason_text(act.decision_reason))
        return f"{who or 'You'} dismissed this {when}" + (f": {reason}" if reason else ".")
    if st == "acknowledged":
        return f"{who or 'You'} marked this as handled {when}."
    if st in ("blocked", "rate_limited", "approval_invalid", "failed"):
        return f"{STATUS_LABEL[st]}. {names.rewrite(_gateway_text(act.decision_reason))}".strip()
    return STATUS_LABEL.get(st, humanize(st))


def _reason_text(decision_reason: str | None) -> str:
    """'driver_manages_clock: note' -> 'This driver manages their own hours. note'."""
    if not decision_reason:
        return ""
    text = decision_reason.split(" | ")[0]
    code, _, note = text.partition(": ")
    code_label = REASON_LABEL.get(code.strip())
    if code_label:
        return f"{code_label}" + (f". {note.strip()}" if note.strip() else "")
    return text


def _gateway_text(decision_reason: str | None) -> str:
    if not decision_reason or "gateway:" not in decision_reason:
        return ""
    return decision_reason.split("gateway:", 1)[1].strip()


# --- investigation steps ("What Haulmind checked") ---------------------------------------------


def _as_list(value: Any) -> list[str]:
    """Lists survive JSON; the tracer shortens them to strings like "['a', 'b']"."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.startswith("["):
        return [t.strip(" '\"") for t in value.strip("[]").split(",") if t.strip(" '\"")]
    return []


def check_step(tool: str, args: dict[str, Any], names: Names) -> tuple[str, str]:
    """(sentence, icon) for a read-tool call."""
    template, icon = CHECK_COPY.get(tool, (f"{humanize(tool)}", "search"))
    types = _as_list(args.get("types"))
    what = join_words([EVENT_GROUP_LABEL.get(t, humanize(t).lower()) for t in types]) or "activity"
    codes = [c for c in _as_list(args.get("codes")) if c]
    try:
        text = template.format(
            truck=names.truck(args.get("vehicle_id")),
            driver=names.driver(args.get("driver_id")),
            hours=hours_phrase(args.get("hours")),
            codes=join_words([f"code {c}" for c in codes]) or "these codes",
            specialty=system_label(args.get("specialty")) if args.get("specialty") else "service",
            what=what,
        )
    except (KeyError, IndexError):
        text = humanize(tool)
    return text, icon


def _times(n: Any) -> str:
    try:
        count = int(n)
    except (TypeError, ValueError):
        return ""
    return "seen once" if count == 1 else f"seen {count} times"


def finding(tool: str, data: dict[str, Any], names: Names) -> str:
    """What the check found, in plain words, from the tool's structured result."""
    try:
        if tool == "get_driver_hos":
            trip = data.get("current_trip") or {}
            left = minutes(data.get("drive_min_remaining"))
            duty = label(DUTY_LABEL, data.get("duty_status"))
            parts = [f"{duty.lower()} with {left} of drive time left"]
            if data.get("est_minutes_to_destination") is not None:
                parts.append(
                    f"about {minutes(data['est_minutes_to_destination'])} from "
                    f"{trip.get('destination', 'the destination')}"
                )
            if data.get("minutes_since_break") is not None:
                parts.append(f"{minutes(data['minutes_since_break'])} since the last break")
            return f"{data.get('name', 'The driver')} is {', '.join(parts)}."
        if tool == "get_vehicle_status":
            trip = data.get("current_trip") or {}
            bits = [label(VEHICLE_STATUS_LABEL, data.get("status")).lower()]
            if trip.get("destination"):
                bits.append(f"headed to {trip['destination']}")
            if data.get("coolant_temp_f") is not None:
                bits.append(f"coolant at {float(data['coolant_temp_f']):.0f}°F")
            if data.get("battery_v") is not None:
                bits.append(f"battery at {float(data['battery_v']):.1f} volts")
            if data.get("fuel_level_pct") is not None:
                bits.append(f"fuel at {float(data['fuel_level_pct']):.0f}%")
            codes = data.get("active_fault_codes") or []
            if codes:
                bits.append(f"active fault codes: {join_words([fault_code(c) for c in codes[:3]])}")
            return f"Truck {data.get('unit_number', '')} is {', '.join(bits)}."
        if tool == "find_idle_vehicles":
            spares = data.get("spare_drivers") or []
            if not spares:
                return "No rested spare driver was available nearby."
            s = spares[0]
            return (
                f"{s.get('name', 'A spare driver')} at {s.get('depot', 'the yard')} is rested with "
                f"{minutes(s.get('drive_min_remaining'))} of drive time, about {s.get('distance_mi', '?')} miles away."
            )
        if tool == "get_fault_history":
            faults = data.get("faults") or []
            active = [f for f in faults if f.get("active")]
            parts = []
            if active:
                parts.append(
                    "Active: "
                    + join_words(
                        [
                            f"{fault_code(f['code'])} {_times(f.get('occurrences', 1))}"
                            for f in active[:3]
                        ]
                    )
                )
            cd = data.get("coolant_temp_delta_6h")
            if cd is not None and abs(float(cd)) >= 1:
                parts.append(
                    f"coolant {'up' if float(cd) > 0 else 'down'} {abs(float(cd)):.0f}°F over 6 hours"
                )
            bd = data.get("battery_v_delta_6h")
            if bd is not None and abs(float(bd)) >= 0.2:
                parts.append(
                    f"battery {'down' if float(bd) < 0 else 'up'} {abs(float(bd)):.1f} volts over 6 hours"
                )
            parts = [s[:1].upper() + s[1:] for s in parts]
            return ". ".join(parts) + "." if parts else "No active fault codes in that window."
        if tool == "search_fault_docs":
            hits = data.get("hits") or []
            if not hits:
                return "No matching service guidance."
            return (
                "Guidance found: "
                + join_words([f"{h.get('title', '')} ({h.get('section', '')})" for h in hits[:2]])
                + "."
            )
        if tool == "find_nearby_shops":
            shops = data.get("shops") or []
            if not shops:
                return "No suitable shop nearby."
            s = shops[0]
            return (
                f"Nearest: {s.get('name')} in {s.get('city')}, {s.get('distance_mi')} miles away"
                + (", open 24 hours" if s.get("open_24h") else "")
                + "."
            )
        if tool == "get_fuel_transactions":
            txns = data.get("transactions") or []
            if not txns:
                return "No fuel purchases in that window."
            t = txns[-1]
            flags = []
            if t.get("exceeds_total_capacity"):
                flags.append("more than the tractor and reefer tanks hold together")
            elif t.get("exceeds_tank"):
                flags.append("more than the tractor tank holds")
            if (t.get("distance_from_vehicle_mi") or 0) > 15:
                flags.append(
                    f"{float(t['distance_from_vehicle_mi']):.0f} miles from where the truck was"
                )
            if (
                t.get("minutes_since_previous_fill") is not None
                and t["minutes_since_previous_fill"] <= 90
            ):
                flags.append(
                    f"only {minutes(t['minutes_since_previous_fill'])} after the previous fill"
                )
            base = f"Latest purchase: {float(t.get('gallons', 0)):.0f} gallons at {t.get('station', 'a station')}"
            prior = data.get("prior_flags_on_card")
            tail = (
                f" This card has been flagged {prior} time{'s' if prior != 1 else ''} before."
                if prior
                else ""
            )
            return (
                base
                + (", " + join_words(flags) if flags else ", within normal limits")
                + "."
                + tail
            )
        if tool == "get_recent_events":
            evs = data.get("events") or []
            return f"Found {len(evs)} relevant event{'s' if len(evs) != 1 else ''} in that window."
        if tool == "list_hos_risks":
            risks = data.get("risks") or []
            return f"{len(risks)} driver{'s' if len(risks) != 1 else ''} close to their limits."
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return ""


def step_for_span(span: Any) -> tuple[str, str] | None:
    """Plain-language step for an agent or model span; tool calls use ``check_step``."""
    name = span.name or ""
    key = name.split(":")[0]
    if span.kind == "llm_call":
        if key == "triage":
            return ("Took a first look", "eye")
        if key == "investigate":
            return ("Decided what else to check", "search")
        if key == "plan":
            return ("Drafted the recommendation", "clipboard-check")
        if key == "verify":
            return ("Double-checked the recommendation", "shield-check")
        if key == "single_agent":
            return STEP_COPY["single_agent"]
        return (humanize(key), "sparkles")
    if span.kind == "agent_step":
        if key in STEP_COPY:
            return STEP_COPY[key]
        return (humanize(key), "activity")
    return None


# --- audit log -------------------------------------------------------------------------------


def audit_sentence(row: Any, names: Names) -> str:
    """One sentence per log row, in the past tense."""
    actor = ACTOR_LABEL.get(row.actor, humanize(row.actor))
    action = row.action or ""
    d: dict[str, Any] = row.details_json or {}
    tool = d.get("tool") or ""
    done = ACTION_COPY.get(tool, {}).get("done", "")
    if action == "incident.opened":
        return f"Haulmind found a problem{(' on ' + names.truck(d.get('vehicle_id'))) if d.get('vehicle_id') else ''}."
    if action == "action.approved":
        return (
            f"{actor} approved: {ACTION_COPY.get(tool, {}).get('title', humanize(tool)).lower()}."
        )
    if action == "action.rejected":
        reason = REASON_LABEL.get(d.get("reason_code") or "", "")
        return f"{actor} dismissed it" + (f": {reason.lower()}" if reason else "") + "."
    if action == "action.acknowledged":
        return f"{actor} marked it as handled."
    if action == "action.edited":
        return f"{actor} changed the details before approving."
    if action == "action.executed":
        return f"{done or 'Done'}."
    if action == "action.auto_executed":
        return f"Haulmind handled it on its own: {done.lower() or humanize(tool).lower()}."
    if action == "action.pending":
        return f"Haulmind asked for your OK: {ACTION_COPY.get(tool, {}).get('title', humanize(tool)).lower()}."
    if action == "action.recommended":
        return f"Haulmind recommended: {ACTION_COPY.get(tool, {}).get('title', humanize(tool)).lower()}."
    if action in (
        "action.blocked",
        "action.rate_limited",
        "action.approval_invalid",
        "action.failed",
    ):
        return f"{STATUS_LABEL[action.split('.', 1)[1]]}."
    if ":" in action:
        t, _, outcome = action.partition(":")
        t_copy = ACTION_COPY.get(t, {})
        if outcome == "executed":
            return f"{t_copy.get('done', humanize(t))}."
        return f"{t_copy.get('title', humanize(t))}: {label(STATUS_LABEL, outcome).lower()}."
    return f"{actor}: {humanize(action)}."


# --- events in an incident's story ----------------------------------------------------------


def event_sentence(ev: Any, names: Names) -> str:
    p: dict[str, Any] = ev.payload_json or {}
    t = ev.type
    if t == "hos_update":
        parts = [label(DUTY_LABEL, p.get("duty_status"))]
        if p.get("drive_min_remaining") is not None:
            parts.append(f"{minutes(p['drive_min_remaining'])} of drive time left")
        if p.get("est_minutes_to_destination") is not None:
            parts.append(f"{minutes(p['est_minutes_to_destination'])} to go")
        return ", ".join(parts)
    if t == "dtc_fault":
        state = "appeared" if p.get("active", True) else "cleared"
        return f"{fault_code(str(p.get('code', '')))} {state}"
    if t == "engine_metrics":
        bits = []
        if p.get("coolant_temp_f") is not None:
            bits.append(f"coolant {float(p['coolant_temp_f']):.0f}°F")
        if p.get("battery_v") is not None:
            bits.append(f"battery {float(p['battery_v']):.1f} volts")
        return "Engine reading: " + ", ".join(bits) if bits else "Engine reading"
    if t == "fuel_transaction":
        return f"Fuel purchase: {float(p.get('gallons', 0)):.0f} gallons at {p.get('station', 'a station')}"
    if t == "harsh_event":
        kind = HARSH_LABEL.get(str(p.get("harsh_type") or ""), "harsh driving")
        return f"{kind.capitalize()} ({float(p.get('g_force', 0)):.1f} g)"
    if t == "speeding":
        return f"Speeding, {float(p.get('speed_mph', 0)):.0f} mph"
    if t == "trip_start":
        return f"Trip started to {p.get('destination', 'its destination')}"
    if t == "trip_end":
        return "Trip ended"
    return label(EVENT_LABEL, t)


def event_is_hot(ev: Any) -> bool:
    p: dict[str, Any] = ev.payload_json or {}
    if ev.type in ("dtc_fault", "fuel_transaction", "harsh_event", "speeding"):
        return True
    return (
        ev.type == "hos_update"
        and p.get("drive_min_remaining") is not None
        and int(p["drive_min_remaining"]) <= 120
    )


# --- what Haulmind learned from a decision -----------------------------------------------------

MEMORY_KEY_LABEL: dict[str, str] = {
    "margin_threshold": "the drive-time margin that triggers an alert",
    "hos_margin_alert_max": "the drive-time margin that triggers an alert",
    "nuisance_codes": "the fault codes it treats as usually harmless",
    "harsh_threshold": "how many harsh events it takes before it speaks up",
    "reefer_fills_expected": "whether large combined fills are expected",
    "legit_fuel": "which fuel purchases are considered normal",
}


def memory_sentence(changes: dict[str, Any] | None, names: Names) -> str:
    """'Haulmind will now use a 5 minute margin for Logan Mendoza.'"""
    if not changes:
        return ""
    scope = str(changes.get("scope") or "")
    scope_id = str(changes.get("scope_id") or "")
    about = (
        names.driver(scope_id)
        if scope == "driver"
        else names.truck(scope_id)
        if scope == "vehicle"
        else "the whole fleet"
    )
    parts = []
    for key, value in changes.items():
        if key in ("scope", "scope_id"):
            continue
        what = MEMORY_KEY_LABEL.get(key, humanize(key).lower())
        if isinstance(value, bool):
            shown = "yes" if value else "no"
        elif isinstance(value, list):
            shown = (
                join_words(
                    [
                        fault_code(str(v)) if re.match(r"^[PUBC]\d{4}$|^SPN", str(v)) else str(v)
                        for v in value
                    ]
                )
                or "none"
            )
        elif isinstance(value, int | float) and "margin" in key:
            shown = minutes(value)
        else:
            shown = str(value)
        parts.append(f"{what} is now {shown}")
    if not parts:
        return ""
    return f"Haulmind updated what it remembers about {about}: {join_words(parts)}."


def day_summary(stats: dict[str, float], problems: int) -> str:
    events = int(stats.get("events_total", 0) or 0)
    dismissed = int(stats.get("triage_dismissed", 0) or 0)
    if not events:
        return "Nothing has come in yet."
    text = (
        f"Haulmind has read {events:,} readings from the trucks and found {problems} "
        f"problem{'s' if problems != 1 else ''} worth your time"
    )
    if dismissed:
        text += f", after setting aside {dismissed} that turned out to be nothing"
    return text + "."


OUTCOME_LABEL: dict[str, str] = {
    "watch": "Decided to keep watching",
    "dismiss": "Decided it was nothing",
    "act": "Found a problem",
    "opened": "Found a problem",
    "quota": "Stopped: the daily model budget ran out",
    "error": "Could not finish",
    "ok": "Finished",
    "cache_miss": "Could not finish",
}


def trace_subject(trace: Any, names: Names) -> str:
    """'A look at Truck 1209' from a trace's cluster key when no incident was opened."""
    key = str((trace.attrs_json or {}).get("cluster") or trace.name or "")
    parts = key.split(":")
    if len(parts) >= 3:
        area = label(DOMAIN_LABEL, parts[0].replace("cluster", "").strip(":") or parts[0])
        who = names.driver(parts[2]) if parts[1] == "driver" else names.truck(parts[2])
        return f"{area}: a closer look at {who}"
    return "A closer look at some alerts"


# --- scorecard wording -----------------------------------------------------------------------

SCORE_COPY: dict[str, tuple[str, str]] = {
    "recall": (
        "Real problems caught",
        "Of the problems we know were there, how many Haulmind found.",
    ),
    "precision": (
        "Alerts that were real",
        "Of everything Haulmind raised, how much turned out to be a real problem.",
    ),
    "false_alert_rate": ("False alarms", "Share of alerts that were not a real problem."),
    "action_correctness": (
        "Right fix suggested",
        "How often the recommended step was the one a manager would want.",
    ),
    "latency_p50_ms": (
        "Typical time to notice",
        "How long Haulmind usually takes from the first sign to a recommendation.",
    ),
    "latency_p95_ms": (
        "Slowest time to notice",
        "Nearly every problem was handled within this time.",
    ),
    "policy_violations": (
        "Safety-rule breaches",
        "Times something ran without the approval it needed. Must be zero.",
    ),
    "filtered_pct": (
        "Handled by simple rules",
        "Share of raw telematics that never needed the model at all.",
    ),
    "llm_requests_per_incident": (
        "Checks per problem",
        "How many times the model was consulted for one problem.",
    ),
}


def percent(value: float | None) -> str:
    return "" if value is None else f"{float(value) * 100:.0f}%"


def took(ms: float | None) -> str:
    """How long a step took, for people: 'instant', '0.4 s', '12 seconds'."""
    if ms is None or ms < 50:
        return "instant"
    if ms < 1000:
        return f"{ms / 1000:.1f} s"
    return duration_ms(ms)


def duration_ms(ms: float | None) -> str:
    if ms is None:
        return ""
    s = float(ms) / 1000.0
    if s < 1:
        return "under a second"
    if s < 90:
        return f"{s:.0f} seconds"
    return minutes(s / 60.0)

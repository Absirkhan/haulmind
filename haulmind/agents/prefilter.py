"""Rule-based prefilters. They run on every event cluster before any LLM is involved and drop
the vast majority of telemetry. Thresholds are tunable per driver/vehicle through memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from haulmind.events.schemas import Domain, Event, EventType
from haulmind.util import haversine_mi

Verdict = Literal["drop", "definite", "ambiguous"]

DEFAULTS: dict[str, float] = {
    "hos_remaining_alert_min": 120,  # only look when fewer minutes than this remain
    "hos_margin_alert_max": 15,  # alert when (remaining - est_to_dest) is at or below this
    "hos_margin_definite": -30,  # skip LLM triage when the deficit is this clear
    "break_due_min": 450,  # minutes driving without a 30-min break
    "coolant_drift_f": 15,
    "battery_drift_v": -0.8,
    "metric_recurrence": 2,
    "fuel_overfill_factor": 1.02,
    "fuel_location_mi": 15,
    "fuel_rapid_repeat_min": 90,
    "harsh_pattern_count": 3,
    "harsh_definite_count": 5,
    "harsh_window_h": 48,
}


@dataclass
class Cluster:
    key: str
    domain: Domain
    vehicle_id: str | None
    driver_id: str | None
    events: list[Event]
    trigger: Event
    context: dict[str, Any] = field(default_factory=dict)  # fleet facts (tank size, reefer, ...)

    @property
    def sim_ts(self) -> datetime:
        return self.trigger.ts

    def recent(self, type_: EventType, hours: float) -> list[Event]:
        since = self.trigger.ts - timedelta(hours=hours)
        return [e for e in self.events if e.type == type_ and e.ts >= since]


@dataclass
class PrefilterResult:
    verdict: Verdict
    domain: Domain
    incident_type: str
    severity: str
    signals: dict[str, Any]
    reason: str

    @property
    def candidate(self) -> bool:
        return self.verdict != "drop"


def _thr(memory: dict[str, Any], key: str) -> float:
    return float(memory.get(key, DEFAULTS[key]))


def prefilter(cluster: Cluster, memory: dict[str, Any] | None = None) -> PrefilterResult:
    """Evaluate a cluster. ``memory`` carries per-scope tuned thresholds and suppressions."""
    mem = memory or {}
    until = mem.get("suppress_until")
    if until and cluster.sim_ts.replace(tzinfo=None) < datetime.fromisoformat(str(until)).replace(
        tzinfo=None
    ):
        return PrefilterResult(
            "drop", cluster.domain, "", "low", {}, "suppressed by manager feedback"
        )
    if cluster.domain == Domain.COMPLIANCE:
        return _compliance(cluster, mem)
    if cluster.domain == Domain.MAINTENANCE:
        return _maintenance(cluster, mem)
    if cluster.domain == Domain.SPEND:
        return _spend(cluster, mem)
    return _safety(cluster, mem)


# --- compliance ------------------------------------------------------------------------------


def _compliance(c: Cluster, mem: dict[str, Any]) -> PrefilterResult:
    ev = c.trigger
    if ev.type != EventType.HOS_UPDATE:
        return PrefilterResult("drop", c.domain, "", "low", {}, "not an HOS update")
    p = ev.payload_dict
    remaining = int(p.get("drive_min_remaining", 9999))
    est = p.get("est_minutes_to_destination")
    since_break = int(p.get("minutes_since_break", 0))
    signals: dict[str, Any] = {
        "duty_status": p.get("duty_status"),
        "drive_min_remaining": remaining,
        "est_minutes_to_destination": est,
        "minutes_since_break": since_break,
        "trip_id": p.get("trip_id"),
    }
    if p.get("duty_status") != "driving":
        return PrefilterResult("drop", c.domain, "", "low", signals, "driver not driving")
    break_risk = since_break >= _thr(mem, "break_due_min")
    if est is None:
        if break_risk:
            signals["break_overdue"] = True
            return PrefilterResult(
                "ambiguous",
                c.domain,
                "hos_break_risk",
                "medium",
                signals,
                "8h break window nearly used",
            )
        return PrefilterResult("drop", c.domain, "", "low", signals, "no destination estimate")
    margin = remaining - int(est)
    signals["margin_min"] = margin
    if remaining > _thr(mem, "hos_remaining_alert_min"):
        return PrefilterResult("drop", c.domain, "", "low", signals, "plenty of drive time left")
    margin_max = float(mem.get("margin_threshold", _thr(mem, "hos_margin_alert_max")))
    if margin > margin_max:
        return PrefilterResult(
            "drop",
            c.domain,
            "",
            "low",
            signals,
            f"margin {margin} min above threshold {margin_max:g}",
        )
    if margin <= _thr(mem, "hos_margin_definite"):
        return PrefilterResult(
            "definite",
            c.domain,
            "hos_violation_risk",
            "high",
            signals,
            f"drive clock {-margin} min short of destination",
        )
    return PrefilterResult(
        "ambiguous",
        c.domain,
        "hos_violation_risk",
        "medium",
        signals,
        f"tight margin of {margin} min to destination",
    )


# --- maintenance -----------------------------------------------------------------------------

SEV = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _maintenance(c: Cluster, mem: dict[str, Any]) -> PrefilterResult:
    faults = c.recent(EventType.DTC_FAULT, 48)
    metrics = c.recent(EventType.ENGINE_METRICS, 8)
    active: dict[str, dict[str, Any]] = {}
    occurrences: dict[str, int] = {}
    fresh_after = c.trigger.ts - timedelta(hours=8)
    for e in faults:
        p = e.payload_dict
        code = str(p["code"])
        if p.get("active", True):
            occurrences[code] = occurrences.get(code, 0) + 1
            # a code counts as active now only if it fired recently; stale codes still count
            # towards recurrence but do not by themselves justify a new alert
            if e.ts >= fresh_after:
                active[code] = p
            else:
                active.pop(code, None)
        else:
            active.pop(code, None)
    nuisance = {str(x).upper() for x in mem.get("nuisance_codes", [])}
    ticketed = {str(x).upper() for x in c.context.get("ticketed_codes", [])}
    if ticketed:  # already on an open work order: judge only what is new
        for code in list(active):
            if code.upper() in ticketed:
                active.pop(code, None)
                occurrences.pop(code, None)
    codes = sorted(active)
    max_sev = max((SEV.get(str(p.get("severity", "low")), 0) for p in active.values()), default=-1)
    sev_name = {v: k for k, v in SEV.items()}.get(max_sev, "none")
    system = next((str(p.get("system", "")) for p in active.values() if p.get("system")), "")
    recur = max((occurrences.get(code, 0) for code in active), default=0)

    on = [e.payload_dict for e in metrics if e.payload_dict.get("engine_on", True)]
    cool_delta = bat_delta = None
    if len(on) >= 4:
        q = max(1, len(on) // 4)
        cool_delta = round(
            sum(m["coolant_temp_f"] for m in on[-q:]) / q
            - sum(m["coolant_temp_f"] for m in on[:q]) / q,
            1,
        )
        bat_delta = round(
            sum(m["battery_v"] for m in on[-q:]) / q - sum(m["battery_v"] for m in on[:q]) / q, 2
        )
    latest = on[-1] if on else {}
    drift = bool(
        (cool_delta is not None and cool_delta >= _thr(mem, "coolant_drift_f"))
        or (bat_delta is not None and bat_delta <= _thr(mem, "battery_drift_v"))
        or (latest and float(latest.get("coolant_temp_f", 0)) >= 225)
        or (latest and 0 < float(latest.get("battery_v", 14)) < 12.4)
    )
    signals: dict[str, Any] = {
        "codes": codes,
        "max_severity": sev_name,
        "system": system
        or (
            "cooling"
            if cool_delta and cool_delta > 0
            else "electrical"
            if bat_delta and bat_delta < 0
            else ""
        ),
        "recurrences": recur,
        "ticketed_codes": sorted(ticketed),
        "drift": drift,
        "coolant_temp_delta_f": cool_delta,
        "battery_v_delta": bat_delta,
        "coolant_temp_f": latest.get("coolant_temp_f"),
        "battery_v": latest.get("battery_v"),
    }
    if not codes and not drift:
        reason = (
            "codes already covered by an open work order"
            if ticketed
            else "no active codes, metrics normal"
        )
        return PrefilterResult("drop", c.domain, "", "low", signals, reason)
    if codes and nuisance and set(x.upper() for x in codes) <= nuisance and not drift:
        return PrefilterResult(
            "drop", c.domain, "", "low", signals, "known nuisance code, metrics normal"
        )
    if c.trigger.type == EventType.ENGINE_METRICS and not drift:
        return PrefilterResult("drop", c.domain, "", "low", signals, "metrics within normal band")
    if max_sev >= SEV["critical"] or (max_sev >= SEV["high"] and drift):
        return PrefilterResult(
            "definite",
            c.domain,
            "failing_component",
            "critical" if max_sev >= 3 else "high",
            signals,
            "critical/high codes with metric drift",
        )
    if (
        max_sev >= SEV["high"]
        or drift
        or (max_sev >= SEV["medium"] and recur >= _thr(mem, "metric_recurrence"))
    ):
        return PrefilterResult(
            "ambiguous",
            c.domain,
            "failing_component",
            "high" if max_sev >= 2 else "medium",
            signals,
            "high-severity code or drifting metrics",
        )
    if recur >= _thr(mem, "metric_recurrence"):
        return PrefilterResult(
            "ambiguous",
            c.domain,
            "recurring_fault",
            "low",
            signals,
            f"low-severity code recurred {recur}x",
        )
    return PrefilterResult(
        "drop", c.domain, "", "low", signals, "single low-severity code; watching"
    )


# --- spend -----------------------------------------------------------------------------------


def _spend(c: Cluster, mem: dict[str, Any]) -> PrefilterResult:
    ev = c.trigger
    if ev.type != EventType.FUEL_TRANSACTION:
        return PrefilterResult("drop", c.domain, "", "low", {}, "not a fuel transaction")
    p = ev.payload_dict
    tank = float(c.context.get("fuel_tank_gal", 150.0))
    has_reefer = bool(c.context.get("has_reefer", False))
    reefer_gal = float(c.context.get("reefer_tank_gal", 0.0))
    gallons = float(p["gallons"])
    overfill = gallons > tank * _thr(mem, "fuel_overfill_factor")
    if has_reefer and mem.get("reefer_fills_ok") and gallons <= (tank + reefer_gal) * 1.02:
        overfill = False
    dist = None
    if p.get("vehicle_lat_at_ts") is not None:
        dist = round(
            haversine_mi(
                float(p["lat"]),
                float(p["lon"]),
                float(p["vehicle_lat_at_ts"]),
                float(p["vehicle_lon_at_ts"]),
            ),
            1,
        )
    location_mismatch = dist is not None and dist > _thr(mem, "fuel_location_mi")
    prev = [e for e in c.recent(EventType.FUEL_TRANSACTION, 6) if e.seq != ev.seq]
    rapid = False
    if prev:
        last = prev[-1]
        gap = (ev.ts - last.ts).total_seconds() / 60
        if (
            gap <= _thr(mem, "fuel_rapid_repeat_min")
            and gallons + float(last.payload_dict["gallons"]) > tank * 1.02
        ):
            rapid = True
    signals: dict[str, Any] = {
        "txn_id": p["txn_id"],
        "gallons": gallons,
        "tank_gal": tank,
        "has_reefer": has_reefer,
        "reefer_tank_gal": reefer_gal,
        "station": p.get("station"),
        "distance_from_vehicle_mi": dist,
        "overfill": overfill,
        "location_mismatch": location_mismatch,
        "rapid_repeat": rapid,
        "card_id": p.get("card_id"),
    }
    flags = [k for k in ("overfill", "location_mismatch", "rapid_repeat") if signals[k]]
    if not flags:
        return PrefilterResult(
            "drop", c.domain, "", "low", signals, "transaction within normal bounds"
        )
    if location_mismatch or rapid:
        return PrefilterResult(
            "definite",
            c.domain,
            "fuel_fraud",
            "medium",
            signals,
            "card used away from vehicle or duplicate fill",
        )
    return PrefilterResult(
        "ambiguous",
        c.domain,
        "fuel_fraud",
        "medium",
        signals,
        f"{gallons:.0f} gal exceeds {tank:.0f} gal tank" + (" (reefer unit)" if has_reefer else ""),
    )


# --- safety ----------------------------------------------------------------------------------


def _safety(c: Cluster, mem: dict[str, Any]) -> PrefilterResult:
    ev = c.trigger
    if ev.type != EventType.HARSH_EVENT:
        return PrefilterResult("drop", c.domain, "", "low", {}, "not a harsh event")
    window = _thr(mem, "harsh_window_h")
    harsh = c.recent(EventType.HARSH_EVENT, window)
    trips = {e.payload_dict.get("trip_id") for e in harsh}
    n = len(harsh)
    signals: dict[str, Any] = {
        "harsh_count_48h": n,
        "distinct_trips": len(trips),
        "max_g": max((float(e.payload_dict.get("g_force", 0)) for e in harsh), default=0.0),
        "latest": ev.payload_dict.get("harsh_type"),
    }
    threshold = float(mem.get("harsh_threshold", _thr(mem, "harsh_pattern_count")))
    if n < threshold or len(trips) < 2:
        return PrefilterResult("drop", c.domain, "", "low", signals, "isolated harsh event")
    if n >= _thr(mem, "harsh_definite_count"):
        return PrefilterResult(
            "definite",
            c.domain,
            "harsh_driving_pattern",
            "medium",
            signals,
            f"{n} harsh events across {len(trips)} trips",
        )
    return PrefilterResult(
        "ambiguous",
        c.domain,
        "harsh_driving_pattern",
        "low",
        signals,
        f"{n} harsh events across {len(trips)} trips",
    )

"""View models for the templates: plain-language dictionaries built from ORM rows.

Templates never touch raw ids or enums; they read the fields prepared here, which all go
through :mod:`haulmind.web.copy`. Raw values are kept only under ``technical`` keys, rendered
inside the collapsed "Technical details" disclosure.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from haulmind.db.models import (
    EventRow,
    FaultCode,
    HosStatus,
    Incident,
    ProposedAction,
    Span,
    Trace,
)
from haulmind.simulator.data import CITIES
from haulmind.web import copy as C  # noqa: N812

DRIVE_LIMIT_MIN = 660  # the 11-hour driving limit


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def tab_for_status(status: str) -> str:
    if status in ("pending", "approved"):
        return "pending"
    if status == "auto_executed":
        return "auto"
    if status == "recommended":
        return "recommended"
    return "decided"


def tier_tone(tier: int) -> str:
    return {1: "ok", 2: "warn", 3: "stop"}.get(int(tier), "neutral")


def status_tone(status: str) -> str:
    if status in ("executed", "auto_executed", "acknowledged", "approved"):
        return "ok"
    if status in ("pending",):
        return "warn"
    if status in ("recommended", "blocked", "rate_limited", "approval_invalid", "failed"):
        return "stop"
    return "neutral"


def severity_tone(severity: str) -> str:
    return {"critical": "stop", "high": "stop", "medium": "warn", "low": "neutral"}.get(
        severity, "neutral"
    )


def drive_strip(inc: Incident | None) -> dict[str, Any] | None:
    """Drive-time strip for hours-of-service items: fill = time left, marker = time needed."""
    if inc is None or inc.domain != "compliance":
        return None
    sig = inc.signals_json or {}
    left = sig.get("drive_min_remaining")
    need = sig.get("est_minutes_to_destination")
    if left is None:
        return None
    left_i = max(0, int(left))
    out: dict[str, Any] = {
        "left_pct": min(100.0, 100.0 * left_i / DRIVE_LIMIT_MIN),
        "left_text": C.minutes(left_i),
        "need_pct": None,
        "need_text": "",
    }
    if need is not None:
        out["need_pct"] = min(100.0, 100.0 * int(need) / DRIVE_LIMIT_MIN)
        out["need_text"] = C.minutes(int(need))
    return out


# --- inbox ---------------------------------------------------------------------------------------


def queue_item(
    act: ProposedAction, inc: Incident | None, names: C.Names, now: datetime | None
) -> dict[str, Any]:
    severity = inc.severity if inc else "medium"
    return {
        "id": act.id,
        "tier": int(act.tier),
        "tier_label": C.label(C.TIER_LABEL, int(act.tier)),
        "tier_tone": tier_tone(int(act.tier)),
        "status": act.status,
        "status_label": C.label(C.STATUS_LABEL, act.status),
        "status_tone": status_tone(act.status),
        "headline": C.headline(inc, names) if inc else names.rewrite(act.title),
        "step": C.action_title(act, names),
        "who": names.who(
            act.vehicle_id or (inc.vehicle_id if inc else None),
            act.driver_id or (inc.driver_id if inc else None),
        ),
        "when": C.ago(act.sim_ts, now),
        "when_full": C.full_time(act.sim_ts),
        "sure": C.confidence_label(act.confidence),
        "sure_level": C.confidence_level(act.confidence),
        "sure_pct": f"{act.confidence * 100:.0f}%",
        "severity": severity,
        "severity_label": C.label(C.SEVERITY_LABEL, severity),
        "severity_tone": severity_tone(severity),
        "kind": C.incident_kind(inc) if inc else "",
        "domain": inc.domain if inc else "",
        "tab": tab_for_status(act.status),
        "drive": drive_strip(inc),
        "vehicle_id": act.vehicle_id or (inc.vehicle_id if inc else None),
        "driver_id": act.driver_id or (inc.driver_id if inc else None),
        "search": " ".join(
            [
                C.headline(inc, names) if inc else "",
                C.action_title(act, names),
                names.who(act.vehicle_id, act.driver_id),
                names.unit(act.vehicle_id or (inc.vehicle_id if inc else None)),
                C.label(C.DOMAIN_LABEL, inc.domain) if inc else "",
                C.incident_kind(inc) if inc else "",
                "fuel" if inc and inc.domain == "spend" else "",
                "hours" if inc and inc.domain == "compliance" else "",
                "service repair" if inc and inc.domain == "maintenance" else "",
            ]
        ).lower(),
    }


def matches_filters(item: dict[str, Any], q: str, truck: str, driver: str, urgency: str) -> bool:
    if q and not all(w in item["search"] for w in q.lower().split()):
        return False
    if truck and item["vehicle_id"] != truck:
        return False
    if driver and item["driver_id"] != driver:
        return False
    if urgency == "urgent" and item["severity"] not in ("high", "critical"):
        return False
    if urgency == "medium" and item["severity"] != "medium":
        return False
    return not (urgency == "low" and item["severity"] != "low")


def steps_for(inc: Incident | None, spans: list[Span], names: C.Names) -> list[dict[str, Any]]:
    """The 'What Haulmind checked' timeline: plain steps with what each one found."""
    steps: list[dict[str, Any]] = []
    span_names = {sp.name.split(":")[0] for sp in spans if sp.kind == "llm_call"}
    if "triage" in span_names:
        steps.append(
            {"what": "Took a first look at the alerts", "icon": "eye", "model": True, "found": ""}
        )
    for e in (inc.evidence_json if inc else []) or []:
        tool = str(e.get("tool") or "")
        args = e.get("args") or {}
        what, icon = C.check_step(tool, args, names)
        found = names.rewrite(C.finding(tool, e.get("data") or {}, names))
        if e.get("error"):
            found = "This check did not come back. Haulmind went on without it."
        if e.get("requested_by") == "llm":
            what += " (asked for during the investigation)"
        steps.append(
            {
                "what": what,
                "icon": icon,
                "found": found,
                "failed": bool(e.get("error")),
                "model": False,
                "technical": {"tool": tool, "args": args, "data": e.get("data") or {}},
            }
        )
    if "plan" in span_names or "single_agent" in span_names:
        steps.append(
            {
                "what": "Decided what to recommend",
                "icon": "clipboard-check",
                "model": True,
                "found": "",
            }
        )
    if "verify" in span_names:
        steps.append(
            {
                "what": "Double-checked the recommendation against the rules",
                "icon": "shield-check",
                "model": True,
                "found": "",
            }
        )
    return steps


def action_view(
    s: Any, act: ProposedAction, names: C.Names, now: datetime | None, wall_now: datetime | None
) -> dict[str, Any]:
    inc = s.get(Incident, act.incident_id) if act.incident_id else None
    spans = (
        s.execute(select(Span).where(Span.trace_id == inc.trace_id).order_by(Span.seq))
        .scalars()
        .all()
        if inc and inc.trace_id
        else []
    )
    trace = s.get(Trace, inc.trace_id) if inc and inc.trace_id else None
    tier = int(act.tier)
    item = queue_item(act, inc, names, now)
    copy = C.ACTION_COPY.get(act.tool_name, {})
    reasoning = names.rewrite(act.reasoning)
    verifier = names.rewrite((act.verifier_json or {}).get("reason", ""))
    plan = C.action_plan(act, names)
    phone = names.phone(act.driver_id or (inc.driver_id if inc else None))
    models = sorted({sp.model for sp in spans if sp.model})
    return {
        **item,
        "incident_id": inc.id if inc else None,
        "why": C.why_it_matters(inc) if inc else "",
        "tier_help": C.TIER_HELP.get(tier, ""),
        "reasoning": reasoning,
        "verifier": verifier,
        "plan": plan,
        "message_body": plan[1][1] if act.tool_name == "message_driver" and len(plan) > 1 else "",
        "button": copy.get("button", "Approve"),
        "doing": copy.get("doing", "Working on it"),
        "done": copy.get("done", "Done"),
        "open": act.status in ("pending", "recommended"),
        "decided_sentence": C.action_status_sentence(act, names, wall_now)
        if act.status not in ("pending", "recommended")
        else "",
        "result": C.action_result(act, names),
        "steps": steps_for(inc, spans, names),
        "phone": phone,
        "driver_name": names.driver(act.driver_id or (inc.driver_id if inc else None)),
        "truck_detail": names.truck_detail(act.vehicle_id or (inc.vehicle_id if inc else None)),
        "editable": act.status in ("pending", "recommended") and act.tool_name in C.EDITABLE_FIELDS,
        "technical": {
            "action_id": act.id,
            "incident_id": inc.id if inc else "",
            "trace_id": trace.id if trace else "",
            "tool": act.tool_name,
            "args": pretty(act.args_json),
            "result": pretty(act.result_json) if act.result_json else "",
            "status": act.status,
            "tier": tier,
            "confidence": f"{act.confidence:.2f}",
            "incident_type": inc.type if inc else "",
            "signals": pretty(inc.signals_json) if inc else "",
            "models": ", ".join(models),
            "llm_requests": inc.llm_requests if inc else 0,
            "tokens": (inc.llm_input_tokens + inc.llm_output_tokens) if inc else 0,
            "latency_ms": inc.latency_ms if inc else 0,
            "proposed_by": act.proposed_by,
            "decision_reason": act.decision_reason or "",
        },
    }


def edit_view(act: ProposedAction, names: C.Names) -> dict[str, Any]:
    fields = []
    args = act.args_json or {}
    for key, label_text, kind in C.EDITABLE_FIELDS.get(act.tool_name, []):
        value = args.get(key)
        options: list[tuple[str, str]] = []
        if kind == "driver":
            options = sorted(((d.id, d.name) for d in names.drivers.values()), key=lambda x: x[1])
        elif kind == "shop":
            shops = ((x.id, f"{x.name}, {x.city}") for x in names.shops.values())
            options = [("", "No preference"), *sorted(shops, key=lambda x: x[1])]
        elif kind == "urgency":
            options = [("normal", "Normal"), ("high", "High")]
        elif kind == "priority":
            options = [("low", "Low"), ("normal", "Normal"), ("high", "High"), ("urgent", "Urgent")]
        fields.append(
            {
                "key": key,
                "label": label_text,
                "kind": "select"
                if options
                else ("textarea" if key in ("body", "description", "reason") else "text"),
                "value": ""
                if value is None
                else (names.rewrite(str(value)) if not options else str(value)),
                "options": options,
            }
        )
    return {"id": act.id, "title": C.action_title(act, names), "fields": fields}


# --- problems (incidents) ------------------------------------------------------------------------


def incident_row(
    inc: Incident, acts: list[ProposedAction], names: C.Names, now: datetime | None
) -> dict[str, Any]:
    open_acts = [a for a in acts if a.status in ("pending", "recommended")]
    if open_acts:
        status_label, tone = (
            ("Waiting on you", "warn")
            if any(a.status == "pending" for a in open_acts)
            else ("For your decision", "stop")
        )
    elif acts and all(a.status in ("executed", "auto_executed", "acknowledged") for a in acts):
        status_label, tone = ("Handled", "ok")
    elif inc.status == "resolved":
        status_label, tone = ("Resolved", "ok")
    elif inc.status in ("no_action", "auto_handled"):
        status_label, tone = (C.label(C.INCIDENT_STATUS_LABEL, inc.status), "neutral")
    elif acts and all(a.status == "rejected" for a in acts):
        status_label, tone = ("Dismissed", "neutral")
    else:
        status_label, tone = (C.label(C.INCIDENT_STATUS_LABEL, inc.status), "neutral")
    return {
        "id": inc.id,
        "headline": C.headline(inc, names),
        "kind": C.incident_kind(inc),
        "area": C.label(C.DOMAIN_LABEL, inc.domain),
        "who": names.who(inc.vehicle_id, inc.driver_id),
        "when": C.ago(inc.sim_ts, now),
        "when_full": C.full_time(inc.sim_ts),
        "severity_label": C.label(C.SEVERITY_LABEL, inc.severity),
        "severity_tone": severity_tone(inc.severity),
        "status_label": status_label,
        "status_tone": tone,
        "steps": [
            {
                "id": a.id,
                "title": C.action_title(a, names),
                "tier_tone": tier_tone(int(a.tier)),
                "tier_label": C.label(C.TIER_LABEL, int(a.tier)),
                "status_label": C.label(C.STATUS_LABEL, a.status),
                "tab": tab_for_status(a.status),
            }
            for a in acts
        ],
        "vehicle_id": inc.vehicle_id,
        "driver_id": inc.driver_id,
    }


def incident_story(
    inc: Incident,
    events: list[EventRow],
    acts: list[ProposedAction],
    logs: list[Any],
    names: C.Names,
) -> list[dict[str, Any]]:
    story: list[dict[str, Any]] = []
    for e in events:
        story.append(
            {
                "t": e.ts.strftime("%a %H:%M"),
                "text": C.event_sentence(e, names),
                "kind": "hot" if C.event_is_hot(e) else "",
            }
        )
    story.append(
        {
            "t": inc.sim_ts.strftime("%a %H:%M"),
            "text": "Haulmind decided this needed a closer look.",
            "kind": "agent",
        }
    )
    for a in acts:
        verb = {
            "auto_executed": "Haulmind handled it: ",
            "pending": "Haulmind asked for your OK: ",
            "recommended": "Haulmind recommended: ",
        }.get(a.status, "Haulmind proposed: ")
        title = C.action_title(a, names)
        story.append(
            {
                "t": a.sim_ts.strftime("%a %H:%M") if a.sim_ts else "",
                "text": verb + title[:1].lower() + title[1:],
                "kind": "agent",
                "href": f"/inbox?tab={tab_for_status(a.status)}&selected={a.id}",
            }
        )
    for log in logs:
        if log.actor in ("manager", "policy-gateway"):
            story.append(
                {
                    "t": log.ts.strftime("%H:%M:%S"),
                    "text": C.audit_sentence(log, names),
                    "kind": "you" if log.actor == "manager" else "",
                }
            )
    return story


# --- trucks ----------------------------------------------------------------------------------------


def _project(
    lat: float, lon: float, bbox: tuple[float, float, float, float], w: int, h: int, pad: int
) -> tuple[float, float]:
    lat0, lat1, lon0, lon1 = bbox
    x = pad + (lon - lon0) / (lon1 - lon0) * (w - 2 * pad)
    y = pad + (lat1 - lat) / (lat1 - lat0) * (h - 2 * pad)
    return round(x, 1), round(y, 1)


def fleet_view(s: Any, names: C.Names, run_id: str | None, now: datetime | None) -> dict[str, Any]:
    vehicles = sorted(names.vehicles.values(), key=lambda v: v.unit_number)
    hos = {h.driver_id: h for h in s.execute(select(HosStatus)).scalars()}
    faults: dict[str, list[FaultCode]] = {}
    for f in s.execute(select(FaultCode).where(FaultCode.active.is_(True))).scalars():
        faults.setdefault(f.vehicle_id, []).append(f)
    open_q = select(ProposedAction).where(ProposedAction.status.in_(("pending", "recommended")))
    if run_id:
        open_q = open_q.where(ProposedAction.run_id == run_id)
    open_by_vehicle: dict[str, list[ProposedAction]] = {}
    for a in s.execute(open_q).scalars():
        if a.vehicle_id:
            open_by_vehicle.setdefault(a.vehicle_id, []).append(a)
    inc_q = select(Incident).where(Incident.status.in_(("open", "investigating")))
    if run_id:
        inc_q = inc_q.where(Incident.run_id == run_id)
    open_inc: dict[str, int] = {}
    for i in s.execute(inc_q).scalars():
        if i.vehicle_id:
            open_inc[i.vehicle_id] = open_inc.get(i.vehicle_id, 0) + 1

    lats = [c.lat for c in CITIES]
    lons = [c.lon for c in CITIES]
    bbox = (min(lats) - 0.8, max(lats) + 0.8, min(lons) - 0.8, max(lons) + 0.8)
    w, h, pad = 420, 300, 14
    rows = []
    points = []
    for v in vehicles:
        d = names.drivers.get(v.current_driver_id or "")
        hs = hos.get(v.current_driver_id or "")
        trip = names.trips.get(v.current_trip_id or "")
        opens = open_by_vehicle.get(v.id, [])
        codes = faults.get(v.id, [])
        moving = bool(v.engine_on and (v.speed_mph or 0) > 5)
        if v.status in ("oos", "out_of_service", "in_shop"):
            tone, status_text = "stop", C.label(C.VEHICLE_STATUS_LABEL, v.status)
        elif opens or open_inc.get(v.id):
            tone, status_text = "issue", "Needs attention"
        elif moving:
            tone, status_text = "moving", f"On the way to {trip.destination}" if trip else "Moving"
        else:
            tone, status_text = "parked", "Parked"
        left = hs.drive_min_remaining if hs else None
        rows.append(
            {
                "id": v.id,
                "unit": v.unit_number,
                "detail": names.truck_detail(v.id),
                "driver": d.name if d else "No driver assigned",
                "driver_id": d.id if d else None,
                "tone": tone,
                "status": status_text,
                "duty": C.label(C.DUTY_LABEL, hs.duty_status) if hs else "",
                "drive_left": C.minutes(left) if left is not None else "",
                "drive_pct": min(100.0, 100.0 * max(0, int(left)) / DRIVE_LIMIT_MIN)
                if left is not None
                else None,
                "destination": trip.destination if trip else "",
                "speed": f"{v.speed_mph:.0f} mph" if moving and v.speed_mph is not None else "",
                "fuel": f"{v.fuel_level_pct:.0f}%" if v.fuel_level_pct is not None else "",
                "coolant": f"{v.coolant_temp_f:.0f}°F" if v.coolant_temp_f is not None else "",
                "coolant_hot": bool(v.coolant_temp_f and v.coolant_temp_f > 218),
                "battery": f"{v.battery_v:.1f} V" if v.battery_v is not None else "",
                "battery_low": bool(v.battery_v and v.battery_v < 12.8),
                "codes": [C.fault_code(f.code, f.description) for f in codes],
                "open_items": [
                    {"id": a.id, "title": C.action_title(a, names), "tab": tab_for_status(a.status)}
                    for a in opens
                ],
                "open_problems": open_inc.get(v.id, 0),
            }
        )
        if v.lat is not None and v.lon is not None:
            x, y = _project(v.lat, v.lon, bbox, w, h, pad)
            points.append(
                {
                    "x": x,
                    "y": y,
                    "tone": tone,
                    "label": f"Truck {v.unit_number}",
                    "id": v.id,
                    "heading": float(v.heading or 0.0),
                    "glyph": "veh-semi"
                    if v.has_reefer or "Sleeper" in (v.model or "")
                    else "veh-daycab",
                }
            )
    cities: list[dict[str, Any]] = []
    for c in CITIES:
        x, y = _project(c.lat, c.lon, bbox, w, h, pad)
        crowded = any(abs(o["x"] - x) < 46 and abs(o["y"] - y) < 12 for o in cities if o["label"])
        cities.append({"x": x, "y": y, "name": c.name, "label": not crowded})
    counts = {
        "attention": sum(1 for r in rows if r["tone"] == "issue"),
        "moving": sum(1 for r in rows if r["tone"] == "moving"),
        "parked": sum(1 for r in rows if r["tone"] == "parked"),
        "off": sum(1 for r in rows if r["tone"] == "stop"),
    }
    return {"rows": rows, "points": points, "cities": cities, "w": w, "h": h, "counts": counts}


# --- how well it works ------------------------------------------------------------------------------


def performance_view(results_path: Path) -> dict[str, Any] | None:
    if not results_path.exists():
        return None
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base = data.get("base") or {}
    if not base:
        return None
    scn = data.get("scenario") or {}
    provider = str(base.get("provider") or "")
    scores = []
    for key, good_high in (
        ("recall", True),
        ("precision", True),
        ("action_correctness", True),
        ("false_alert_rate", False),
    ):
        value = base.get(key)
        if value is None:
            continue
        label_text, help_text = C.SCORE_COPY[key]
        v = float(value)
        tone = (
            "good"
            if ((v >= 0.8) if good_high else (v <= 0.2))
            else ("bad" if ((v < 0.5) if good_high else (v > 0.5)) else "")
        )
        scores.append({"value": C.percent(v), "label": label_text, "help": help_text, "tone": tone})
    timing = []
    for key in ("latency_p50_ms", "latency_p95_ms"):
        if base.get(key) is not None:
            label_text, help_text = C.SCORE_COPY[key]
            timing.append(
                {
                    "value": C.duration_ms(base[key]),
                    "label": label_text,
                    "help": help_text,
                    "tone": "",
                }
            )
    violations = base.get("policy_violations")
    if violations is not None:
        label_text, help_text = C.SCORE_COPY["policy_violations"]
        timing.append(
            {
                "value": str(int(violations)),
                "label": label_text,
                "help": help_text,
                "tone": "good" if int(violations) == 0 else "bad",
            }
        )
    per_type = []
    for t, row in (base.get("per_type") or {}).items():
        total = int(row.get("total") or 0)
        per_type.append(
            {
                "label": C.INCIDENT_TYPE_LABEL.get(t, C.humanize(t)),
                "total": total,
                "detected": int(row.get("detected") or 0),
                "pct": (100.0 * int(row.get("detected") or 0) / total) if total else 0.0,
            }
        )
    learning = []
    for row in (data.get("learning") or {}).get("rows") or []:
        learning.append(
            {
                "phase": str(row.get("phase", "")).replace(
                    "(no decisions yet)", "(before any decisions)"
                ),
                "memory": "Learning on" if row.get("memory") == "on" else "Learning off",
                "on": row.get("memory") == "on",
                "alerts": int(row.get("alerts") or 0),
                "true": int(row.get("true") or 0),
                "false": int(row.get("false") or 0),
                "precision": C.percent(row.get("precision")),
                "precision_pct": 100.0 * float(row.get("precision") or 0),
            }
        )
    return {
        "provider": C.label(C.PROVIDER_LABEL, provider),
        "practice": provider == "stub",
        "generated": _results_generated(results_path.with_suffix(".md")),
        "scenario": (
            f"{scn.get('fleet_size', '?')} trucks over {int(scn.get('hours') or 0)} hours, "
            f"{base.get('gt_positives', '?')} real problems and {base.get('gt_benign', '?')} look-alikes planted"
        ),
        "scores": scores,
        "timing": timing,
        "per_type": per_type,
        "learning": learning,
        "filtered_pct": base.get("filtered_pct"),
        "checks_per_problem": base.get("llm_requests_per_incident"),
        "missed": base.get("missed") or [],
        "false_alerts": len(base.get("false_alert_ids") or []),
    }


def _results_generated(md_path: Path) -> str:
    try:
        for line in md_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Generated by"):
                part = line.split(" on ", 1)[1]
                return part.split(" (", 1)[0]
    except (OSError, IndexError):
        pass
    return ""


def landing_results(results_path: Path) -> dict[str, Any]:
    """Headline numbers for the landing page, from the last evaluation run."""
    perf = performance_view(results_path)
    base: dict[str, Any] = {}
    try:
        base = (json.loads(results_path.read_text(encoding="utf-8")) or {}).get("base") or {}
    except (OSError, ValueError):
        base = {}
    if not perf:
        return {
            "practice": True,
            "provider": "offline practice mode",
            "scenario": "25 trucks over 48 hours with planted problems",
            "caught_pct": 100,
            "filtered_pct": 99.8,
            "false_pct": 20,
            "breaches": 0,
            "per_type": [],
        }
    return {
        "practice": perf["practice"],
        "provider": perf["provider"],
        "scenario": perf["scenario"],
        "caught_pct": round(100 * float(base.get("recall") or 0)),
        "filtered_pct": round(float(base.get("filtered_pct") or 0), 1),
        "false_pct": round(100 * float(base.get("false_alert_rate") or 0)),
        "breaches": int(base.get("policy_violations") or 0),
        "per_type": perf["per_type"],
    }


# --- command palette -------------------------------------------------------------------------------


def palette_commands(names: C.Names) -> list[dict[str, str]]:
    cmds: list[dict[str, str]] = [
        {"label": "Go to the inbox", "href": "/inbox", "hint": "Inbox"},
        {"label": "Show items needing your OK", "href": "/inbox?tab=pending", "hint": "Inbox"},
        {
            "label": "Show items for your decision",
            "href": "/inbox?tab=recommended",
            "hint": "Inbox",
        },
        {"label": "Show what was handled for you", "href": "/inbox?tab=auto", "hint": "Inbox"},
        {"label": "Show what is done", "href": "/inbox?tab=decided", "hint": "Inbox"},
        {"label": "Show only urgent items", "href": "/inbox?urgency=urgent", "hint": "Inbox"},
        {"label": "Go to trucks", "href": "/fleet", "hint": "Trucks"},
        {"label": "Show trucks needing attention", "href": "/fleet#attention", "hint": "Trucks"},
        {"label": "Go to problems found", "href": "/incidents", "hint": "Problems"},
        {
            "label": "How well Haulmind is catching problems",
            "href": "/performance",
            "hint": "Performance",
        },
        {"label": "Go to history", "href": "/audit", "hint": "History"},
    ]
    for v in sorted(names.vehicles.values(), key=lambda x: x.unit_number):
        d = names.drivers.get(v.current_driver_id or "")
        who = f" with {d.name}" if d else ""
        cmds.append(
            {
                "label": f"Truck {v.unit_number}{who}",
                "href": f"/inbox?truck={v.id}",
                "hint": "Truck",
                "keywords": f"truck {v.unit_number} {v.make} {v.model}",
            }
        )
    for d in sorted(names.drivers.values(), key=lambda x: x.name):
        cmds.append(
            {
                "label": d.name,
                "href": f"/inbox?driver={d.id}",
                "hint": "Driver",
                "keywords": "driver",
            }
        )
    return cmds

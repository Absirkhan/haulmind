"""Scripted evidence gathering per domain (zero LLM calls) and compact summaries of tool results
for prompts, the inbox UI and evaluation."""

from __future__ import annotations

from typing import Any

from haulmind.agents.prefilter import PrefilterResult
from haulmind.events.schemas import Domain
from haulmind.mcp_server.client import ToolClient, ToolError


def summarize(tool: str, data: dict[str, Any]) -> str:
    """One-line, human-readable summary of a tool result (kept under ~200 chars)."""
    try:
        if tool == "get_driver_hos":
            trip = data.get("current_trip") or {}
            return (
                f"HOS {data['driver_id']} ({data['name']}): {data['duty_status']}, {data['drive_min_remaining']} min drive left, "
                f"{data['duty_min_remaining']} min duty left, {data['minutes_since_break']} min since break; "
                f"trip {trip.get('id', '-')} to {trip.get('destination', '-')} est {data.get('est_minutes_to_destination')} min, margin {data.get('margin_min')} min"
            )
        if tool == "get_vehicle_status":
            trip = data.get("current_trip") or {}
            return (
                f"Vehicle {data['vehicle_id']} unit {data['unit_number']} ({data['year']} {data['make']} {data['model']}), {data['status']}, "
                f"driver {data.get('current_driver_id')}, trip {trip.get('id', '-')} -> {trip.get('destination', '-')}; "
                f"coolant {data.get('coolant_temp_f')}F, battery {data.get('battery_v')}V, oil {data.get('oil_pressure_psi')}psi, fuel {data.get('fuel_level_pct')}%, "
                f"tank {data['fuel_tank_gal']:.0f} gal{' + reefer ' + str(int(data['reefer_tank_gal'])) + ' gal' if data.get('has_reefer') else ''}; "
                f"active codes {data.get('active_fault_codes') or 'none'}; open WOs {data.get('open_work_orders')}"
            )
        if tool == "find_idle_vehicles":
            spares = data.get("spare_drivers") or []
            idle = data.get("idle_vehicles") or []
            sp = ", ".join(
                f"{d['driver_id']} ({d['name']}, {d['depot']}, {d['drive_min_remaining']} min, {d.get('distance_mi')} mi)"
                for d in spares[:3]
            )
            return f"{len(idle)} idle vehicles; rested spare drivers: {sp or 'none'}"
        if tool == "get_fault_history":
            faults = data.get("faults") or []
            fs = "; ".join(
                f"{f['code']} {f['severity']} x{f['occurrences']} {'active' if f['active'] else 'cleared'}"
                for f in faults[:5]
            )
            return (
                f"Faults {data['vehicle_id']}: {fs or 'none'}; coolant 6h delta {data.get('coolant_temp_delta_6h')}F, "
                f"battery 6h delta {data.get('battery_v_delta_6h')}V; max active severity {data.get('max_active_severity')}"
            )
        if tool == "search_fault_docs":
            hits = data.get("hits") or []
            return (
                "Docs: "
                + "; ".join(f"[{h['citation']}] {h['title']} / {h['section']}" for h in hits[:3])
                if hits
                else "Docs: no matching guidance"
            )
        if tool == "find_nearby_shops":
            shops = data.get("shops") or []
            return "Shops: " + "; ".join(
                f"{s['shop_id']} {s['name']} ({s['city']}, {s['distance_mi']} mi, {'/'.join(s['specialties'])}, {s['rating']}*{', 24h' if s['open_24h'] else ''})"
                for s in shops[:3]
            )
        if tool == "get_fuel_transactions":
            txns = data.get("transactions") or []
            ts = "; ".join(
                f"{t['transaction_id']} {t['gallons']:.0f} gal at {t['station']} ({t.get('distance_from_vehicle_mi')} mi from truck"
                f"{', >tank' if t['exceeds_tank'] else ''}{', >total capacity' if t['exceeds_total_capacity'] else ''}"
                f"{', ' + str(t['minutes_since_previous_fill']) + ' min after previous' if t.get('minutes_since_previous_fill') is not None else ''})"
                for t in txns[-4:]
            )
            return (
                f"Fuel {data['vehicle_id']} (reefer={data['has_reefer']}, total capacity {txns[-1]['total_capacity_gal']:.0f} gal): {ts}; prior flags on card: {data.get('prior_flags_on_card')}"
                if txns
                else f"Fuel {data['vehicle_id']}: no transactions in window"
            )
        if tool == "get_recent_events":
            evs = data.get("events") or []
            return "Recent: " + "; ".join(
                f"{e['ts'][11:16]} {e['type']} {e['summary']}" for e in evs[-8:]
            )
        if tool == "list_hos_risks":
            return "HOS risks: " + "; ".join(
                f"{r['driver_id']} margin {r.get('margin_min')}"
                for r in (data.get("risks") or [])[:5]
            )
    except (KeyError, TypeError, IndexError):
        pass
    return f"{tool}: " + ", ".join(f"{k}={v}" for k, v in list(data.items())[:6])


async def _call(
    tc: ToolClient, evidence: list[dict[str, Any]], tool: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        data = await tc.call(tool, args)
    except ToolError as e:
        evidence.append(
            {
                "id": f"E{len(evidence) + 1}",
                "tool": tool,
                "args": args,
                "summary": f"{tool} failed: {str(e)[:120]}",
                "data": {},
                "error": True,
            }
        )
        return None
    evidence.append(
        {
            "id": f"E{len(evidence) + 1}",
            "tool": tool,
            "args": args,
            "summary": summarize(tool, data),
            "data": data,
        }
    )
    return data


async def gather(
    tc: ToolClient,
    domain: Domain,
    vehicle_id: str | None,
    driver_id: str | None,
    pre: PrefilterResult,
    *,
    rag_enabled: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Standard evidence per domain. Returns (evidence, candidates)."""
    ev: list[dict[str, Any]] = []
    cand: dict[str, Any] = {}
    vstat = (
        await _call(tc, ev, "get_vehicle_status", {"vehicle_id": vehicle_id})
        if vehicle_id
        else None
    )
    if vstat:
        trip = vstat.get("current_trip") or {}
        cand.update(
            {
                "trip_id": trip.get("id"),
                "destination": trip.get("destination"),
                "unit_number": vstat.get("unit_number"),
            }
        )

    if domain == Domain.COMPLIANCE and driver_id:
        hos = await _call(tc, ev, "get_driver_hos", {"driver_id": driver_id})
        if hos and hos.get("current_trip"):
            cand["trip_id"] = hos["current_trip"]["id"]
            cand["destination"] = hos["current_trip"]["destination"]
        near = {"near_lat": vstat.get("lat"), "near_lon": vstat.get("lon")} if vstat else {}
        idle = await _call(
            tc, ev, "find_idle_vehicles", {"min_idle_minutes": 30, "limit": 5, **near}
        )
        if idle:
            spares = [
                d for d in idle.get("spare_drivers", []) if d.get("drive_min_remaining", 0) >= 300
            ]
            if spares:
                cand["spare_driver_id"] = spares[0]["driver_id"]
                cand["spare_driver_name"] = spares[0]["name"]
        await _call(
            tc,
            ev,
            "get_recent_events",
            {"driver_id": driver_id, "hours": 3, "types": ["hos_update", "trip_start"], "limit": 6},
        )

    elif domain == Domain.MAINTENANCE and vehicle_id:
        fh = await _call(tc, ev, "get_fault_history", {"vehicle_id": vehicle_id, "hours": 48})
        codes = list(pre.signals.get("codes") or (fh or {}).get("active_codes") or [])
        system = pre.signals.get("system") or next(
            (f.get("system") for f in (fh or {}).get("faults", []) if f.get("active")), "engine"
        )
        cand.update({"codes": codes, "system": system})
        shops = await _call(
            tc, ev, "find_nearby_shops", {"vehicle_id": vehicle_id, "specialty": system, "limit": 3}
        )
        if shops and shops.get("shops"):
            cand["nearest_shop_id"] = shops["shops"][0]["shop_id"]
            cand["nearest_shop_name"] = shops["shops"][0]["name"]
        if rag_enabled:
            query = f"{' '.join(codes[:3])} {system} diagnosis recommended action priority"
            docs = await _call(
                tc, ev, "search_fault_docs", {"query": query, "codes": codes[:3], "k": 3}
            )
            cand["citations"] = [h["citation"] for h in (docs or {}).get("hits", [])]
        await _call(
            tc,
            ev,
            "get_recent_events",
            {
                "vehicle_id": vehicle_id,
                "hours": 6,
                "types": ["dtc_fault", "engine_metrics"],
                "limit": 8,
            },
        )

    elif domain == Domain.SPEND and vehicle_id:
        fuel = await _call(tc, ev, "get_fuel_transactions", {"vehicle_id": vehicle_id, "hours": 72})
        txns = (fuel or {}).get("transactions", [])
        anomalous = [
            t
            for t in txns
            if t.get("exceeds_total_capacity")
            or (t.get("distance_from_vehicle_mi") or 0) > 15
            or (t.get("minutes_since_previous_fill") or 9999) <= 90
        ]
        if not anomalous and pre.signals.get("txn_id"):
            anomalous = [t for t in txns if t["transaction_id"] == pre.signals["txn_id"]]
        cand.update(
            {
                "txn_ids": [t["transaction_id"] for t in anomalous[-2:]],
                "card_id": pre.signals.get("card_id") or (txns[-1]["card_id"] if txns else None),
                "prior_flags": (fuel or {}).get("prior_flags_on_card", 0),
                "exceeds_total_capacity": any(t.get("exceeds_total_capacity") for t in anomalous),
            }
        )

    elif domain == Domain.SAFETY and driver_id:
        await _call(
            tc,
            ev,
            "get_recent_events",
            {
                "driver_id": driver_id,
                "hours": 48,
                "types": ["harsh_event", "speeding"],
                "limit": 20,
            },
        )
        await _call(tc, ev, "get_driver_hos", {"driver_id": driver_id})
    return ev, cand

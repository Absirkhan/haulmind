"""Per-driver / per-vehicle memory built from manager decisions, and the learning rules that
turn approve/reject feedback into tuned thresholds and suppressions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from haulmind.db.models import ProposedAction
from haulmind.db.repo import memory_all, memory_get, memory_set
from haulmind.events.schemas import Domain
from haulmind.util import to_naive_utc

SCOPE_FOR_DOMAIN = {
    Domain.COMPLIANCE: "driver",
    Domain.SAFETY: "driver",
    Domain.MAINTENANCE: "vehicle",
    Domain.SPEND: "vehicle",
}

REASON_CODES: dict[str, str] = {
    "not_an_issue": "Not an issue",
    "known_intermittent": "Known intermittent / nuisance code",
    "legit_fuel": "Legitimate fuel purchase (reefer, shared card, ...)",
    "driver_manages_clock": "Driver manages the clock; do not alert at this margin",
    "wrong_action": "Right incident, wrong action",
    "duplicate": "Duplicate of an existing item",
    "other": "Other",
}


def scope_for(domain: Domain, vehicle_id: str | None, driver_id: str | None) -> tuple[str, str]:
    scope = SCOPE_FOR_DOMAIN[domain]
    scope_id = driver_id if scope == "driver" else vehicle_id
    return scope, scope_id or ""


def load_memory(
    s: Session, domain: Domain, vehicle_id: str | None, driver_id: str | None, cluster_key: str
) -> dict[str, Any]:
    """Flatten the memory rows relevant to a cluster into one dict for prefilter and prompts."""
    scope, scope_id = scope_for(domain, vehicle_id, driver_id)
    rows = memory_all(s, scope, scope_id) if scope_id else {}
    out: dict[str, Any] = {"scope": scope, "scope_id": scope_id}
    counts: dict[str, int] = {}
    notes: list[str] = []
    for key, val in rows.items():
        if key.startswith(("fp_count:", "tp_count:")):
            counts[key] = int(val.get("n", 0))
        elif key == "notes":
            notes = list(val.get("items", []))[-3:]
        elif key.startswith("threshold:"):
            out[key.split(":", 1)[1]] = val.get("value")
        elif key == "nuisance_codes":
            out["nuisance_codes"] = list(val.get("codes", []))
        elif key == "reefer_fills_ok":
            out["reefer_fills_ok"] = bool(val.get("value"))
    # fleet-wide fallbacks (a per-driver/vehicle value always wins)
    for key, val in memory_all(s, "global", "").items():
        if key.startswith("threshold:"):
            out.setdefault(key.split(":", 1)[1], val.get("value"))
        elif key.startswith(("fp_count:", "tp_count:")):
            counts.setdefault(f"global:{key}", int(val.get("n", 0)))
    sup = memory_get(s, "cluster", cluster_key, "suppress_until")
    if sup:
        out["suppress_until"] = sup.get("until")
    out["counts"] = counts
    out["notes"] = notes
    return out


def prompt_notes(memory: dict[str, Any]) -> str:
    """Compact memory summary for prompts (kept short to save tokens)."""
    parts: list[str] = []
    counts = memory.get("counts", {})
    fps = sum(v for k, v in counts.items() if k.startswith("fp_count:"))
    tps = sum(v for k, v in counts.items() if k.startswith("tp_count:"))
    if fps or tps:
        parts.append(
            f"manager history for this {memory.get('scope')}: {tps} approved, {fps} rejected"
        )
    if memory.get("nuisance_codes"):
        parts.append("known nuisance codes: " + ", ".join(memory["nuisance_codes"]))
    if memory.get("reefer_fills_ok"):
        parts.append("large combined tractor+reefer fills are legitimate for this unit")
    if "margin_threshold" in memory:
        parts.append(f"only alert HOS when margin <= {memory['margin_threshold']:g} min")
    for n in memory.get("notes", []):
        parts.append(f"manager note: {n}")
    return "; ".join(parts)


# --- learning rules --------------------------------------------------------------------------


def apply_decision(
    s: Session,
    action: ProposedAction,
    *,
    decision: str,
    reason_code: str,
    reason_text: str,
    domain: Domain,
    incident_type: str,
    sim_ts: datetime,
    cluster_key: str,
) -> dict[str, Any]:
    """Update memory from a manager decision. Returns a summary of what changed."""
    scope, scope_id = scope_for(domain, action.vehicle_id, action.driver_id)
    changes: dict[str, Any] = {"scope": scope, "scope_id": scope_id}
    if not scope_id:
        return changes
    key = f"{'tp' if decision == 'approve' else 'fp'}_count:{incident_type}"
    cur = memory_get(s, scope, scope_id, key) or {"n": 0}
    cur["n"] = int(cur.get("n", 0)) + 1
    memory_set(s, scope, scope_id, key, cur)
    changes[key] = cur["n"]
    fp = (
        cur["n"]
        if decision == "reject"
        else int((memory_get(s, scope, scope_id, f"fp_count:{incident_type}") or {}).get("n", 0))
    )

    if reason_text.strip():
        notes = memory_get(s, scope, scope_id, "notes") or {"items": []}
        notes["items"] = [*notes.get("items", []), reason_text.strip()[:160]][-5:]
        memory_set(s, scope, scope_id, "notes", notes)

    if decision == "reject":
        if domain == Domain.COMPLIANCE:
            # tighten: require a smaller (or negative) margin before alerting this driver again
            current = float(
                (memory_get(s, scope, scope_id, "threshold:margin_threshold") or {}).get(
                    "value", 15
                )
            )
            new = 0.0 if fp == 1 else max(-15.0, current - 10.0)
            if reason_code == "driver_manages_clock":
                new = min(new, -5.0)
            memory_set(s, scope, scope_id, "threshold:margin_threshold", {"value": new})
            changes["margin_threshold"] = new
            # repeated near-miss rejections across drivers tighten the fleet-wide default too
            g = memory_get(s, "global", "", f"fp_count:{incident_type}") or {"n": 0}
            g["n"] = int(g.get("n", 0)) + 1
            memory_set(s, "global", "", f"fp_count:{incident_type}", g)
            if g["n"] >= 2:
                gval = 0.0 if g["n"] < 4 else -10.0
                memory_set(s, "global", "", "threshold:margin_threshold", {"value": gval})
                changes["global_margin_threshold"] = gval
        elif domain == Domain.MAINTENANCE:
            codes = [str(c).upper() for c in (action.args_json.get("fault_codes") or [])]
            if codes and (reason_code in ("known_intermittent", "not_an_issue") or fp >= 2):
                cur_codes = memory_get(s, scope, scope_id, "nuisance_codes") or {"codes": []}
                merged = sorted(set(cur_codes.get("codes", [])) | set(codes))
                memory_set(s, scope, scope_id, "nuisance_codes", {"codes": merged})
                changes["nuisance_codes"] = merged
        elif domain == Domain.SPEND:
            if reason_code in ("legit_fuel", "not_an_issue"):
                memory_set(s, scope, scope_id, "reefer_fills_ok", {"value": True})
                changes["reefer_fills_ok"] = True
        elif domain == Domain.SAFETY:
            current = float(
                (memory_get(s, scope, scope_id, "threshold:harsh_threshold") or {}).get("value", 3)
            )
            new = min(8.0, current + 1.0)
            memory_set(s, scope, scope_id, "threshold:harsh_threshold", {"value": new})
            changes["harsh_threshold"] = new
        # suppress the same cluster for a while regardless of domain
        until = to_naive_utc(sim_ts) + timedelta(hours=24)
        memory_set(s, "cluster", cluster_key, "suppress_until", {"until": until.isoformat()})
        changes["suppress_until"] = until.isoformat()
    elif decision == "approve" and domain == Domain.COMPLIANCE:
        row = memory_get(s, scope, scope_id, "threshold:margin_threshold")
        if row is not None:
            new = min(15.0, float(row.get("value", 0)) + 5.0)
            memory_set(s, scope, scope_id, "threshold:margin_threshold", {"value": new})
            changes["margin_threshold"] = new
    return changes

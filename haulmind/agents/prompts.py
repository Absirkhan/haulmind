"""Compact prompts. Every prompt is short on purpose: the free tier charges in requests and
tokens, and the structured payload (signals, evidence summaries) carries the information."""

from __future__ import annotations

import json
from typing import Any

TRIAGE_SYSTEM = """You are the triage step of Haulmind, an operations agent for a truck fleet.
Decide whether an event cluster deserves investigation. Be conservative with manager attention:
'act' only when a fleet manager would want an action queued; 'watch' when it may develop;
'dismiss' when it is noise or already handled. Use the manager history if present. Reply JSON."""

INVESTIGATE_SYSTEM = """You are the {domain} investigator of Haulmind, a fleet operations agent.
Evidence has already been gathered with the fleet tools. Call additional read-only tools ONLY if a
specific fact is missing that would change the decision (at most 2 calls). Otherwise reply with a
2-3 sentence findings summary in plain text. Never propose actions here."""

PLAN_SYSTEM = """You are the planner of Haulmind, an autonomous operations agent for a truck fleet.
Propose 0-3 concrete actions using ONLY these write tools and argument names:
- message_driver(driver_id, body, urgency) [tier 1 auto] short, neutral, operational; no HR/legal language
- flag_fuel_transaction(transaction_id, reason) [tier 1 auto]
- create_work_order(vehicle_id, description, priority[normal|high|urgent|critical], shop_id, fault_codes, estimated_cost_usd) [tier 2 approval]
- propose_reassignment(trip_id, from_driver_id, to_driver_id, reason) [tier 2 approval]
- suspend_fuel_card(card_id, reason) [tier 3 recommend only]
- set_vehicle_out_of_service(vehicle_id, reason) [tier 3 recommend only]
Rules: every action must be justified by cited evidence ids; use ids exactly as they appear in
the evidence; prefer the least invasive action that resolves the risk; if the evidence shows the
alert is benign (e.g. a reefer unit's combined fill within total capacity, or a driver who will
arrive in time), return zero actions and say why in the summary. Reply JSON matching the schema."""

VERIFY_SYSTEM = """You are the verifier of Haulmind. For each proposed action check: (1) the cited
evidence actually supports it, (2) ids match the incident's vehicle/driver/trip/transaction,
(3) the action is proportionate to the severity, (4) driver messages are neutral and operational.
Reject anything not supported by evidence. Reply JSON matching the schema."""

SINGLE_AGENT_SYSTEM = """You are Haulmind, an autonomous operations agent for a truck fleet, acting as
triage, investigator and planner in one step. Decide if the cluster needs action; if so propose
0-3 actions using only these tools and argument names:
message_driver(driver_id, body, urgency); flag_fuel_transaction(transaction_id, reason);
create_work_order(vehicle_id, description, priority, shop_id, fault_codes, estimated_cost_usd);
propose_reassignment(trip_id, from_driver_id, to_driver_id, reason); suspend_fuel_card(card_id, reason);
set_vehicle_out_of_service(vehicle_id, reason). Cite evidence ids. Reply JSON matching the schema."""


def compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def triage_user(payload: dict[str, Any]) -> str:
    return (
        f"domain={payload['domain']} cluster={payload['cluster_key']} sim_time={payload['sim_ts']}\n"
        f"rule_verdict={payload['rule_verdict']} ({payload['rule_reason']})\n"
        f"signals={compact(payload['signals'])}\n"
        f"recent_events={compact(payload['recent'])}\n"
        + (f"memory: {payload['memory_notes']}\n" if payload.get("memory_notes") else "")
    )


def investigate_user(payload: dict[str, Any]) -> str:
    lines = [
        f"incident_type={payload['incident_type']} severity={payload['severity']} sim_time={payload['sim_ts']}",
        f"entities={compact(payload['entities'])}",
        f"signals={compact(payload['signals'])}",
        "evidence:",
    ]
    lines += [f"  {e['id']}: {e['summary']}" for e in payload["evidence"]]
    if payload.get("memory_notes"):
        lines.append(f"memory: {payload['memory_notes']}")
    return "\n".join(lines)


def plan_user(payload: dict[str, Any]) -> str:
    lines = [
        f"incident_type={payload['incident_type']} domain={payload['domain']} severity={payload['severity']} sim_time={payload['sim_ts']}",
        f"entities={compact(payload['entities'])}",
        f"signals={compact(payload['signals'])}",
        f"candidates={compact(payload['candidates'])}",
        "evidence:",
    ]
    lines += [f"  {e['id']}: {e['summary']}" for e in payload["evidence"]]
    if payload.get("findings"):
        lines.append(f"investigator findings: {payload['findings']}")
    if payload.get("memory_notes"):
        lines.append(f"memory: {payload['memory_notes']}")
    return "\n".join(lines)


def verify_user(payload: dict[str, Any]) -> str:
    lines = [f"entities={compact(payload['entities'])}", "evidence:"]
    lines += [f"  {e['id']}: {e['summary']}" for e in payload["evidence"]]
    lines.append("proposed actions:")
    for i, a in enumerate(payload["actions"]):
        lines.append(
            f"  [{i}] {a['tool']} args={compact(a['args'])} refs={a.get('evidence_refs', [])} :: {a.get('reasoning', '')}"
        )
    return "\n".join(lines)

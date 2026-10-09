"""Offline, deterministic stand-in for the LLM.

It reads the structured ``req.meta["payload"]`` that the agents attach to every request and
applies transparent heuristics, so the whole system (demo, tests, evals) runs with no key and
no network. Outputs are labelled ``provider="stub"`` so eval tables never confuse them with
real model results.
"""

from __future__ import annotations

import json
from typing import Any

from haulmind.llm.base import LLMRequest, LLMResponse, Role, ToolCallReq, Usage


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class StubLLMClient:
    provider = "stub"

    def __init__(self) -> None:
        self.models: dict[Role, str] = {"triage": "stub-triage", "planner": "stub-planner"}
        self.requests = 0

    def model_for(self, role: Role) -> str:
        return self.models[role]

    async def generate(self, req: LLMRequest) -> LLMResponse:
        self.requests += 1
        payload: dict[str, Any] = req.meta.get("payload", {})
        task = req.task or req.meta.get("task", "")
        tool_calls: list[ToolCallReq] = []
        out: Any
        if task == "triage":
            out = self._triage(payload)
        elif task == "investigate":
            out, tool_calls = self._investigate(req, payload)
        elif task == "plan":
            out = self._plan(payload)
        elif task == "verify":
            out = self._verify(payload)
        elif task == "single_agent":
            out = self._single(payload)
        else:
            out = {"text": "ok"}
        text = json.dumps(out) if not isinstance(out, str) else out
        prompt_chars = len(req.system) + sum(len(m.text or "") for m in req.messages)
        return LLMResponse(
            text=text,
            parsed=out if not isinstance(out, str) else None,
            tool_calls=tool_calls,
            usage=Usage(input_tokens=_tokens(" " * prompt_chars), output_tokens=_tokens(text)),
            model=self.model_for(req.role),
            provider=self.provider,
            latency_ms=1,
        )

    # --- heuristics --------------------------------------------------------------------------

    @staticmethod
    def _triage(p: dict[str, Any]) -> dict[str, Any]:
        domain = p.get("domain", "")
        sig = p.get("signals", {})
        mem = p.get("memory", {})
        decision, itype, severity, reason = "dismiss", "", "low", "no actionable signal"
        if domain == "compliance":
            margin = float(sig.get("margin_min", 999))
            threshold = float(mem.get("margin_threshold", 10))
            itype = "hos_violation_risk"
            if margin <= threshold:
                decision = "act"
                severity = "high" if margin < -30 else "medium"
                reason = (
                    f"remaining drive time short of destination by {-margin:.0f} min"
                    if margin < 0
                    else f"margin to destination only {margin:.0f} min"
                )
            else:
                decision, reason = (
                    "dismiss",
                    f"margin {margin:.0f} min is above threshold {threshold:.0f}",
                )
        elif domain == "maintenance":
            itype = "failing_component"
            codes = [c.upper() for c in sig.get("codes", [])]
            nuisance = {c.upper() for c in mem.get("nuisance_codes", [])}
            sev = str(sig.get("max_severity", "low"))
            drift = bool(sig.get("drift"))
            recur = int(sig.get("recurrences", 0))
            if codes and set(codes) <= nuisance and not drift:
                decision, reason = "dismiss", "known nuisance code with normal metrics"
            elif sev in ("high", "critical") or drift or recur >= 2:
                decision, severity = "act", "critical" if sev == "critical" else "high"
                reason = (
                    "recurring/high-severity faults with metric drift"
                    if drift
                    else "high-severity fault codes"
                )
            else:
                decision, reason = "watch", "single low-severity code; monitoring"
        elif domain == "spend":
            itype = "fuel_fraud"
            flags = [k for k in ("overfill", "location_mismatch", "rapid_repeat") if sig.get(k)]
            if flags:
                decision, severity, reason = "act", "medium", "anomaly flags: " + ", ".join(flags)
            else:
                reason = "transaction within normal bounds"
        elif domain == "safety":
            itype = "harsh_driving_pattern"
            n = int(sig.get("harsh_count_48h", 0))
            if n >= 4:
                decision, severity, reason = (
                    "act",
                    "medium",
                    f"{n} harsh events in 48h across trips",
                )
            else:
                decision, reason = "watch", f"{n} harsh events; below pattern threshold"
        return {
            "decision": decision,
            "domain": domain,
            "severity": severity,
            "incident_type": itype,
            "reason": reason,
        }

    @staticmethod
    def _investigate(req: LLMRequest, p: dict[str, Any]) -> tuple[Any, list[ToolCallReq]]:
        tool_names = {t.name for t in req.tools}
        already = {m.name for msg in req.messages for m in msg.tool_results}
        calls: list[ToolCallReq] = []
        if (
            p.get("domain") == "maintenance"
            and "search_fault_docs" in tool_names
            and "search_fault_docs" not in already
            and not p.get("has_docs")
        ):
            codes = p.get("codes", [])
            query = (
                " ".join(codes[:3]) + " diagnosis and service guidance"
                if codes
                else "engine fault diagnosis"
            )
            calls.append(
                ToolCallReq(
                    id="call_docs",
                    name="search_fault_docs",
                    args={"query": query, "codes": codes[:3], "k": 3},
                )
            )
        if (
            p.get("domain") == "compliance"
            and "find_idle_vehicles" in tool_names
            and "find_idle_vehicles" not in already
            and not p.get("has_spare")
        ):
            calls.append(
                ToolCallReq(
                    id="call_idle", name="find_idle_vehicles", args={"min_idle_minutes": 30}
                )
            )
        if calls:
            return "", calls
        facts = [str(e.get("summary", ""))[:160] for e in p.get("evidence", [])][:6]
        return {
            "findings": "; ".join(f for f in facts if f) or "evidence gathered",
            "key_facts": facts,
        }, []

    @staticmethod
    def _plan(p: dict[str, Any]) -> dict[str, Any]:
        domain = p.get("domain")
        ent = p.get("entities", {})
        cand = p.get("candidates", {})
        sig = p.get("signals", {})
        refs = [str(e.get("id")) for e in p.get("evidence", []) if e.get("id")]
        actions: list[dict[str, Any]] = []
        vehicle, driver = ent.get("vehicle_id"), ent.get("driver_id")
        summary = ""
        severity = "medium"
        if domain == "compliance":
            margin = float(sig.get("margin_min", 0))
            summary = f"Driver {driver} will run out of drive time about {max(0, -margin):.0f} min before reaching the destination."
            severity = "high" if margin < -30 else "medium"
            actions.append(
                {
                    "tool": "message_driver",
                    "args": {
                        "driver_id": driver,
                        "body": f"Dispatch: your drive clock shows ~{int(sig.get('drive_min_remaining', 0))} min left and ~{int(sig.get('est_minutes_to_destination', 0))} min to {cand.get('destination', 'destination')}. Please plan a safe stop before the clock runs out; dispatch is arranging a handoff.",
                        "urgency": "high",
                    },
                    "title": "Tell the driver to plan a safe stop",
                    "reasoning": "Remaining drive minutes are below the estimated time to destination; the driver needs to know now.",
                    "confidence": 0.9 if margin < -15 else 0.7,
                    "evidence_refs": refs[:3],
                }
            )
            if cand.get("spare_driver_id") and cand.get("trip_id") and margin < -15:
                actions.append(
                    {
                        "tool": "propose_reassignment",
                        "args": {
                            "trip_id": cand["trip_id"],
                            "from_driver_id": driver,
                            "to_driver_id": cand["spare_driver_id"],
                            "reason": f"Driver {driver} cannot legally complete {cand['trip_id']} ({-margin:.0f} min short).",
                        },
                        "title": f"Hand off {cand['trip_id']} to spare driver {cand['spare_driver_id']}",
                        "reasoning": "A rested spare driver near the route can finish the load on time without an HOS violation.",
                        "confidence": 0.75,
                        "evidence_refs": refs[:3],
                    }
                )
        elif domain == "maintenance":
            codes = sig.get("codes", [])
            system = sig.get("system", "engine")
            code_text = ", ".join(codes[:3]) if codes else "no stored codes yet"
            summary = f"Vehicle {vehicle} shows {system} degradation ({code_text}; metrics trending abnormal)."
            severity = "critical" if sig.get("max_severity") == "critical" else "high"
            shop = cand.get("nearest_shop_id")
            actions.append(
                {
                    "tool": "create_work_order",
                    "args": {
                        "vehicle_id": vehicle,
                        "description": f"Inspect {system} system: {('codes ' + ', '.join(codes[:4])) if codes else 'metric drift without stored codes'}; metrics trending abnormal. See cited service guidance.",
                        "priority": "urgent" if severity == "critical" else "high",
                        "shop_id": shop,
                        "fault_codes": codes[:4],
                        "estimated_cost_usd": 1800.0 if system == "cooling" else 1200.0,
                    },
                    "title": f"Open a {system} work order for {vehicle}",
                    "reasoning": "Recurring high-severity codes plus metric drift indicate a component that will fail if the truck keeps running.",
                    "confidence": 0.85,
                    "evidence_refs": refs[:4],
                }
            )
            if severity == "critical":
                actions.append(
                    {
                        "tool": "set_vehicle_out_of_service",
                        "args": {
                            "vehicle_id": vehicle,
                            "reason": "Critical overheat/electrical codes; risk of roadside failure.",
                        },
                        "title": f"Recommend grounding {vehicle} until repaired",
                        "reasoning": "Critical codes recorded; continuing to operate risks engine damage.",
                        "confidence": 0.6,
                        "evidence_refs": refs[:2],
                    }
                )
        elif domain == "spend":
            txns = cand.get("txn_ids", [])
            flags = [k for k in ("overfill", "location_mismatch", "rapid_repeat") if sig.get(k)]
            if (
                flags == ["overfill"]
                and sig.get("has_reefer")
                and cand.get("exceeds_total_capacity") is False
            ):
                return {
                    "summary": f"Fill on {vehicle} exceeds the tractor tank but not tractor + reefer capacity; evidence shows a legitimate combined fill.",
                    "severity": "low",
                    "actions": [],
                }
            summary = f"Fuel transaction(s) on {vehicle} show {', '.join(flags) or 'anomalies'}."
            for t in txns[:2]:
                actions.append(
                    {
                        "tool": "flag_fuel_transaction",
                        "args": {
                            "transaction_id": t,
                            "reason": "; ".join(flags) or "anomalous fuel purchase",
                        },
                        "title": f"Flag fuel transaction {t} for review",
                        "reasoning": "Gallons/location/timing are inconsistent with the vehicle's tank and GPS trail.",
                        "confidence": 0.85 if "overfill" in flags else 0.7,
                        "evidence_refs": refs[:3],
                    }
                )
            if int(sig.get("prior_flags", 0)) >= 1 and cand.get("card_id"):
                actions.append(
                    {
                        "tool": "suspend_fuel_card",
                        "args": {
                            "card_id": cand["card_id"],
                            "reason": "Repeated anomalous fuel purchases on this card.",
                        },
                        "title": f"Recommend suspending fuel card {cand['card_id']}",
                        "reasoning": "Second anomaly on the same card; a human should decide on suspension.",
                        "confidence": 0.55,
                        "evidence_refs": refs[:2],
                    }
                )
        elif domain == "safety":
            n = int(sig.get("harsh_count_48h", 0))
            summary = f"Driver {driver} logged {n} harsh events in 48h across multiple trips."
            actions.append(
                {
                    "tool": "message_driver",
                    "args": {
                        "driver_id": driver,
                        "body": f"Safety: {n} harsh braking/acceleration events were recorded in the last two days. Please leave more following distance and smooth out braking; a coach will check in this week.",
                        "urgency": "normal",
                    },
                    "title": "Send a coaching message about harsh events",
                    "reasoning": "A pattern across trips is a coaching opportunity, not an emergency.",
                    "confidence": 0.7,
                    "evidence_refs": refs[:3],
                }
            )
        return {"summary": summary, "severity": severity, "actions": actions}

    @staticmethod
    def _verify(p: dict[str, Any]) -> dict[str, Any]:
        ent = p.get("entities", {})
        results = []
        for i, a in enumerate(p.get("actions", [])):
            args = a.get("args", {})
            ok, reason = True, "consistent with evidence"
            if (
                "vehicle_id" in args
                and ent.get("vehicle_id")
                and args["vehicle_id"] != ent["vehicle_id"]
            ):
                ok, reason = False, "targets a different vehicle than the incident"
            if (
                "driver_id" in args
                and ent.get("driver_id")
                and args["driver_id"] != ent["driver_id"]
            ):
                ok, reason = False, "targets a different driver than the incident"
            if a.get("tool") == "message_driver" and len(str(args.get("body", ""))) < 20:
                ok, reason = False, "message too short to be useful"
            results.append({"index": i, "approved": ok, "reason": reason})
        return {"results": results}

    def _single(self, p: dict[str, Any]) -> dict[str, Any]:
        tri = self._triage(p)
        plan = (
            self._plan(p)
            if tri["decision"] == "act"
            else {"summary": tri["reason"], "severity": tri["severity"], "actions": []}
        )
        return {**tri, **plan}

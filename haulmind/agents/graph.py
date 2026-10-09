"""LangGraph wiring. Nodes are closures over ``AgentDeps`` so the graph is easy to build in
tests with an in-memory MCP server and the stub LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from haulmind.agents import prompts
from haulmind.agents.evidence import gather, summarize
from haulmind.agents.memory import prompt_notes
from haulmind.agents.prefilter import Cluster, PrefilterResult
from haulmind.agents.schemas import (
    ActionProposal,
    PlanOutput,
    SingleAgentOutput,
    TriageOutput,
    VerifyOutput,
    json_schema,
)
from haulmind.config import Settings
from haulmind.db.engine import session_scope
from haulmind.db.models import Incident, ProposedAction
from haulmind.db.repo import audit
from haulmind.events.schemas import Domain
from haulmind.llm.base import LLMClient, LLMRequest, LLMResponse, Message, ToolResult
from haulmind.logging_setup import get_logger
from haulmind.mcp_server.client import ToolClient, ToolError
from haulmind.policy.blocklist import BlockList
from haulmind.policy.engine import args_hash
from haulmind.policy.tiers import WRITE_TOOLS, Tier, tier_for
from haulmind.tracing import get_tracer
from haulmind.util import iso, new_id, to_naive_utc

log = get_logger(__name__)

REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "message_driver": ("driver_id", "body"),
    "flag_fuel_transaction": ("transaction_id", "reason"),
    "create_work_order": ("vehicle_id", "description"),
    "propose_reassignment": ("trip_id", "from_driver_id", "to_driver_id", "reason"),
    "suspend_fuel_card": ("card_id", "reason"),
    "set_vehicle_out_of_service": ("vehicle_id", "reason"),
}


class AgentState(TypedDict, total=False):
    run_id: str
    cluster_key: str
    domain: str
    vehicle_id: str | None
    driver_id: str | None
    sim_ts: str
    recent: list[dict[str, Any]]
    prefilter: dict[str, Any]
    memory: dict[str, Any]
    triage: dict[str, Any]
    incident_id: str | None
    incident_type: str
    severity: str
    evidence: list[dict[str, Any]]
    candidates: dict[str, Any]
    findings: str
    plan: dict[str, Any]
    verified: list[dict[str, Any]]
    dispatched: list[dict[str, Any]]
    outcome: str
    error: str | None


@dataclass
class AgentDeps:
    settings: Settings
    tools: ToolClient
    llm: LLMClient
    blocklist: BlockList


def initial_state(cluster: Cluster, pre: PrefilterResult, memory: dict[str, Any]) -> AgentState:
    recent = [
        {
            "ts": iso(e.ts),
            "type": e.type.value,
            **{k: v for k, v in e.payload_dict.items() if k != "kind"},
        }
        for e in cluster.events[-6:]
    ]
    return AgentState(
        run_id=cluster.trigger.run_id,
        cluster_key=cluster.key,
        domain=cluster.domain.value,
        vehicle_id=cluster.vehicle_id,
        driver_id=cluster.driver_id,
        sim_ts=iso(cluster.sim_ts) or "",
        recent=recent,
        prefilter={
            "verdict": pre.verdict,
            "reason": pre.reason,
            "signals": pre.signals,
            "incident_type": pre.incident_type,
            "severity": pre.severity,
        },
        memory=memory,
        incident_type=pre.incident_type,
        severity=pre.severity,
        evidence=[],
        candidates={},
        findings="",
        verified=[],
        dispatched=[],
        error=None,
    )


async def _llm(deps: AgentDeps, req: LLMRequest, span_name: str) -> LLMResponse:
    tracer = get_tracer()
    async with tracer.span(span_name, kind="llm_call", task=req.task, role=req.role) as sp:
        resp = await deps.llm.generate(req)
        sp.record_llm(
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens + resp.usage.thought_tokens,
            cached=resp.cached,
        )
        sp.set(
            provider=resp.provider,
            retries=resp.retries,
            tool_calls=len(resp.tool_calls),
            chars=len(resp.text),
        )
        return resp


def _parse(model: type[BaseModel], resp: LLMResponse) -> BaseModel | None:
    data = resp.parsed
    if data is None and resp.text:
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


async def _structured(
    deps: AgentDeps, model: type[BaseModel], req: LLMRequest, span: str
) -> tuple[BaseModel | None, LLMResponse]:
    """Call with schema; on invalid JSON retry once with the validation error appended."""
    resp = await _llm(deps, req, span)
    parsed = _parse(model, resp)
    if parsed is not None:
        return parsed, resp
    retry = req.model_copy(deep=True)
    retry.messages.append(Message(role="model", text=resp.text[:2000]))
    retry.messages.append(
        Message(
            role="user",
            text="That was not valid JSON for the schema "
            f"{json.dumps(json_schema(model))[:1500]}. Reply with JSON only.",
        )
    )
    resp2 = await _llm(deps, retry, span + ":retry")
    return _parse(model, resp2), resp2


def build_graph(deps: AgentDeps) -> Any:
    s = deps.settings

    # --- nodes ---------------------------------------------------------------------------------

    async def triage(state: AgentState) -> dict[str, Any]:
        pre = state["prefilter"]
        if pre["verdict"] == "definite":
            return {
                "triage": {
                    "decision": "act",
                    "domain": state["domain"],
                    "severity": pre["severity"],
                    "incident_type": pre["incident_type"],
                    "reason": pre["reason"],
                    "source": "rules",
                }
            }
        payload = {
            "domain": state["domain"],
            "cluster_key": state["cluster_key"],
            "sim_ts": state["sim_ts"],
            "rule_verdict": pre["verdict"],
            "rule_reason": pre["reason"],
            "signals": pre["signals"],
            "recent": state["recent"][-4:],
            "memory_notes": prompt_notes(state.get("memory", {})),
            "memory": {
                k: v
                for k, v in state.get("memory", {}).items()
                if k in ("margin_threshold", "nuisance_codes", "reefer_fills_ok", "harsh_threshold")
            },
        }
        req = LLMRequest(
            role="triage",
            system=prompts.TRIAGE_SYSTEM,
            messages=[Message(role="user", text=prompts.triage_user(payload))],
            response_schema=json_schema(TriageOutput),
            max_output_tokens=256,
            task="triage",
            meta={"payload": payload},
        )
        parsed, _ = await _structured(deps, TriageOutput, req, "triage")
        if parsed is None:
            return {
                "triage": {
                    "decision": "watch",
                    "domain": state["domain"],
                    "severity": pre["severity"],
                    "incident_type": pre["incident_type"],
                    "reason": "triage output unparseable; watching",
                    "source": "fallback",
                },
                "error": "triage_parse",
            }
        t = parsed.model_dump()
        t["source"] = "llm"
        return {
            "triage": t,
            "incident_type": t["incident_type"] or pre["incident_type"],
            "severity": t["severity"],
        }

    def route_after_triage(state: AgentState) -> str:
        return "open_incident" if state["triage"]["decision"] == "act" else "dismiss"

    async def dismiss(state: AgentState) -> dict[str, Any]:
        return {"outcome": state["triage"]["decision"], "incident_id": None}

    async def open_incident(state: AgentState) -> dict[str, Any]:
        inc_id = new_id("INC")
        now = to_naive_utc(datetime.now(UTC))
        with session_scope() as db:
            db.add(
                Incident(
                    id=inc_id,
                    run_id=state["run_id"],
                    cluster_key=state["cluster_key"],
                    domain=state["domain"],
                    type=state["incident_type"] or "unclassified",
                    severity=state["severity"],
                    vehicle_id=state.get("vehicle_id"),
                    driver_id=state.get("driver_id"),
                    opened_ts=now,
                    sim_ts=to_naive_utc(
                        datetime.fromisoformat(state["sim_ts"].replace("Z", "+00:00"))
                    ),
                    status="investigating",
                    summary=state["triage"].get("reason", ""),
                    triage_json=state["triage"],
                    signals_json=state["prefilter"]["signals"],
                    event_ids_json=[],
                    agent_mode=s.agent_mode,
                )
            )
            audit(
                db,
                actor="agent:triage",
                action="incident.opened",
                entity_type="incident",
                entity_id=inc_id,
                details={"type": state["incident_type"], "source": state["triage"].get("source")},
                run_id=state["run_id"],
            )
        get_tracer().set_incident(get_tracer_trace_id(), inc_id)
        return {"incident_id": inc_id}

    async def investigate(state: AgentState) -> dict[str, Any]:
        domain = Domain(state["domain"])
        pre = PrefilterResult(
            verdict=state["prefilter"]["verdict"],
            domain=domain,
            incident_type=state["incident_type"],
            severity=state["severity"],
            signals=state["prefilter"]["signals"],
            reason=state["prefilter"]["reason"],
        )
        async with get_tracer().span(f"investigate:{domain.value}", kind="agent_step"):
            evidence, candidates = await gather(
                deps.tools,
                domain,
                state.get("vehicle_id"),
                state.get("driver_id"),
                pre,
                rag_enabled=s.rag_enabled,
            )
            findings = ""
            specs = [
                t
                for t in await deps.tools.read_tool_specs()
                if s.rag_enabled or t.name != "search_fault_docs"
            ]
            payload = {
                "domain": domain.value,
                "incident_type": state["incident_type"],
                "severity": state["severity"],
                "sim_ts": state["sim_ts"],
                "entities": {
                    "vehicle_id": state.get("vehicle_id"),
                    "driver_id": state.get("driver_id"),
                    "trip_id": candidates.get("trip_id"),
                },
                "signals": pre.signals,
                "evidence": [{"id": e["id"], "summary": e["summary"]} for e in evidence],
                "codes": candidates.get("codes", []),
                "has_docs": any(e["tool"] == "search_fault_docs" for e in evidence),
                "has_spare": bool(candidates.get("spare_driver_id")),
                "memory_notes": prompt_notes(state.get("memory", {})),
            }
            messages = [Message(role="user", text=prompts.investigate_user(payload))]
            for round_ in range(2):
                req = LLMRequest(
                    role="planner",
                    system=prompts.INVESTIGATE_SYSTEM.format(domain=domain.value),
                    messages=messages,
                    tools=specs,
                    max_output_tokens=400,
                    task="investigate",
                    meta={"payload": payload},
                )
                resp = await _llm(deps, req, f"investigate:llm:{round_}")
                if not resp.tool_calls:
                    findings = resp.text.strip()[:800]
                    break
                messages.append(
                    Message(role="model", text=resp.text or None, tool_calls=resp.tool_calls)
                )
                results: list[ToolResult] = []
                for call in resp.tool_calls[:3]:
                    try:
                        data = await deps.tools.call(call.name, call.args)
                    except ToolError as e:
                        data = {"error": str(e)[:200]}
                    evidence.append(
                        {
                            "id": f"E{len(evidence) + 1}",
                            "tool": call.name,
                            "args": call.args,
                            "summary": summarize(call.name, data),
                            "data": data,
                            "requested_by": "llm",
                        }
                    )
                    results.append(ToolResult(call_id=call.id, name=call.name, result=_trim(data)))
                    if call.name == "find_idle_vehicles":
                        spares = [
                            d
                            for d in data.get("spare_drivers", [])
                            if d.get("drive_min_remaining", 0) >= 300
                        ]
                        if spares and not candidates.get("spare_driver_id"):
                            candidates["spare_driver_id"] = spares[0]["driver_id"]
                    if call.name == "search_fault_docs":
                        candidates.setdefault("citations", [])
                        candidates["citations"] += [h["citation"] for h in data.get("hits", [])]
                messages.append(Message(role="tool", tool_results=results))
                payload["evidence"] = [{"id": e["id"], "summary": e["summary"]} for e in evidence]
                payload["has_docs"] = payload["has_docs"] or any(
                    c.name == "search_fault_docs" for c in resp.tool_calls
                )
                payload["has_spare"] = bool(candidates.get("spare_driver_id"))
            if not findings:
                findings = "; ".join(e["summary"] for e in evidence[:3])[:800]
        return {"evidence": evidence, "candidates": candidates, "findings": findings}

    async def plan(state: AgentState) -> dict[str, Any]:
        payload = _plan_payload(state)
        req = LLMRequest(
            role="planner",
            system=prompts.PLAN_SYSTEM,
            messages=[Message(role="user", text=prompts.plan_user(payload))],
            response_schema=json_schema(PlanOutput),
            max_output_tokens=900,
            task="plan",
            meta={"payload": payload},
        )
        parsed, _ = await _structured(deps, PlanOutput, req, "plan")
        if parsed is None:
            return {
                "plan": {
                    "summary": "planner output unparseable",
                    "severity": state["severity"],
                    "actions": [],
                },
                "error": "plan_parse",
            }
        return {"plan": parsed.model_dump()}

    async def verify(state: AgentState) -> dict[str, Any]:
        actions = [ActionProposal.model_validate(a) for a in state["plan"].get("actions", [])]
        entities = {
            "vehicle_id": state.get("vehicle_id"),
            "driver_id": state.get("driver_id"),
            **{
                k: v
                for k, v in state.get("candidates", {}).items()
                if k in ("trip_id", "spare_driver_id", "nearest_shop_id", "txn_ids", "card_id")
            },
        }
        checked: list[dict[str, Any]] = []
        for a in actions:
            ok, why = _code_check(a, entities, deps.blocklist)
            checked.append({**a.model_dump(), "code_ok": ok, "code_reason": why})
        to_llm = [c for c in checked if c["code_ok"]]
        if to_llm:
            payload = {
                "entities": entities,
                "evidence": [{"id": e["id"], "summary": e["summary"]} for e in state["evidence"]],
                "actions": to_llm,
            }
            req = LLMRequest(
                role="planner",
                system=prompts.VERIFY_SYSTEM,
                messages=[Message(role="user", text=prompts.verify_user(payload))],
                response_schema=json_schema(VerifyOutput),
                max_output_tokens=400,
                task="verify",
                meta={"payload": payload},
            )
            parsed, _ = await _structured(deps, VerifyOutput, req, "verify")
            verdicts = (
                {r.index: r for r in parsed.results} if isinstance(parsed, VerifyOutput) else {}
            )
            for i, c in enumerate(to_llm):
                v = verdicts.get(i)
                c["approved"] = v.approved if v else True
                c["verifier_reason"] = v.reason if v else "verifier unavailable; code checks passed"
        for c in checked:
            if not c["code_ok"]:
                c["approved"] = False
                c["verifier_reason"] = c["code_reason"]
        return {"verified": checked}

    async def dispatch(state: AgentState) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        inc_id = state.get("incident_id")
        now = to_naive_utc(datetime.now(UTC))
        sim_ts = to_naive_utc(datetime.fromisoformat(state["sim_ts"].replace("Z", "+00:00")))
        queued = auto = 0
        for a in state["verified"]:
            tier = tier_for(a["tool"])
            status = "rejected"
            result: dict[str, Any] = {}
            if a.get("approved"):
                if tier == Tier.AUTO:
                    try:
                        r = await deps.tools.call(a["tool"], {**a["args"], "incident_id": inc_id})
                        result = r
                        status = (
                            "auto_executed"
                            if r.get("status") == "executed"
                            else str(r.get("status", "failed"))
                        )
                        if status == "auto_executed":
                            auto += 1
                    except ToolError as e:
                        status, result = "failed", {"error": str(e)[:300]}
                elif tier == Tier.APPROVAL:
                    status = "pending"
                    queued += 1
                elif tier == Tier.RECOMMEND:
                    status = "recommended"
                    queued += 1
            act_id = new_id("ACT")
            with session_scope() as db:
                db.add(
                    ProposedAction(
                        id=act_id,
                        incident_id=inc_id,
                        run_id=state["run_id"],
                        tool_name=a["tool"],
                        args_json=a["args"],
                        args_hash=args_hash(a["tool"], a["args"]),
                        tier=int(tier) if tier is not None else -1,
                        status=status,
                        title=a.get("title") or a["tool"],
                        confidence=float(a.get("confidence", 0.5)),
                        reasoning=a.get("reasoning", ""),
                        evidence_json=[
                            e
                            for e in state["evidence"]
                            if e["id"] in set(a.get("evidence_refs", []))
                        ]
                        or state["evidence"][:2],
                        verifier_json={
                            "approved": a.get("approved"),
                            "reason": a.get("verifier_reason"),
                            "code_ok": a.get("code_ok"),
                        },
                        proposed_by="agent:planner",
                        created_ts=now,
                        sim_ts=sim_ts,
                        decided_ts=now if status in ("rejected", "auto_executed") else None,
                        decided_by="agent:verifier"
                        if status == "rejected"
                        else ("policy" if status == "auto_executed" else None),
                        decision_reason=a.get("verifier_reason", "")
                        if status == "rejected"
                        else "",
                        executed_ts=now if status == "auto_executed" else None,
                        result_json=result,
                        vehicle_id=a["args"].get("vehicle_id") or state.get("vehicle_id"),
                        driver_id=a["args"].get("driver_id")
                        or a["args"].get("from_driver_id")
                        or state.get("driver_id"),
                    )
                )
                audit(
                    db,
                    actor="agent:dispatch",
                    action=f"action.{status}",
                    entity_type="action",
                    entity_id=act_id,
                    details={
                        "tool": a["tool"],
                        "tier": int(tier) if tier is not None else -1,
                        "incident_id": inc_id,
                    },
                    run_id=state["run_id"],
                )
            results.append(
                {
                    "action_id": act_id,
                    "tool": a["tool"],
                    "tier": int(tier) if tier is not None else -1,
                    "status": status,
                    "result": result,
                }
            )
        outcome = "queued" if queued else ("auto_handled" if auto else "no_action")
        if inc_id:
            with session_scope() as db:
                inc = db.get(Incident, inc_id)
                if inc is not None:
                    inc.status = "open" if queued else ("auto_handled" if auto else "no_action")
                    inc.summary = state["plan"].get("summary") or inc.summary
                    inc.severity = state["plan"].get("severity") or inc.severity
                    inc.evidence_json = state["evidence"]
                    inc.triage_json = {
                        **inc.triage_json,
                        "findings": state.get("findings", ""),
                        "plan": state["plan"],
                        "verified": state["verified"],
                    }
                    if not queued and not auto:
                        inc.closed_ts = now
        return {"dispatched": results, "outcome": outcome}

    async def single_agent(state: AgentState) -> dict[str, Any]:
        """Ablation: one LLM call does triage + plan over the scripted evidence."""
        domain = Domain(state["domain"])
        pre = PrefilterResult(
            verdict=state["prefilter"]["verdict"],
            domain=domain,
            incident_type=state["incident_type"],
            severity=state["severity"],
            signals=state["prefilter"]["signals"],
            reason=state["prefilter"]["reason"],
        )
        evidence, candidates = await gather(
            deps.tools,
            domain,
            state.get("vehicle_id"),
            state.get("driver_id"),
            pre,
            rag_enabled=s.rag_enabled,
        )
        st: AgentState = {**state, "evidence": evidence, "candidates": candidates}
        payload = _plan_payload(st)
        payload["rule_verdict"] = pre.verdict
        payload["rule_reason"] = pre.reason
        payload["memory"] = {
            k: v
            for k, v in state.get("memory", {}).items()
            if k in ("margin_threshold", "nuisance_codes", "reefer_fills_ok", "harsh_threshold")
        }
        req = LLMRequest(
            role="planner",
            system=prompts.SINGLE_AGENT_SYSTEM,
            messages=[Message(role="user", text=prompts.plan_user(payload))],
            response_schema=json_schema(SingleAgentOutput),
            max_output_tokens=900,
            task="single_agent",
            meta={"payload": payload},
        )
        parsed, _ = await _structured(deps, SingleAgentOutput, req, "single_agent")
        if parsed is None or not isinstance(parsed, SingleAgentOutput):
            return {
                "evidence": evidence,
                "candidates": candidates,
                "triage": {"decision": "watch", "reason": "unparseable", "source": "fallback"},
                "plan": {"summary": "", "severity": state["severity"], "actions": []},
            }
        triage_out = {
            "decision": parsed.decision,
            "domain": domain.value,
            "severity": parsed.severity,
            "incident_type": parsed.incident_type or pre.incident_type,
            "reason": parsed.reason,
            "source": "llm",
        }
        plan_out = {
            "summary": parsed.summary or parsed.reason,
            "severity": parsed.severity,
            "actions": [a.model_dump() for a in parsed.actions],
        }
        return {
            "evidence": evidence,
            "candidates": candidates,
            "triage": triage_out,
            "incident_type": triage_out["incident_type"],
            "severity": parsed.severity,
            "plan": plan_out,
            "findings": parsed.summary,
        }

    def route_single(state: AgentState) -> str:
        return "open_incident_single" if state["triage"]["decision"] == "act" else "dismiss"

    async def verify_code_only(state: AgentState) -> dict[str, Any]:
        entities = {
            "vehicle_id": state.get("vehicle_id"),
            "driver_id": state.get("driver_id"),
            **{
                k: v
                for k, v in state.get("candidates", {}).items()
                if k in ("trip_id", "spare_driver_id", "nearest_shop_id", "txn_ids", "card_id")
            },
        }
        checked = []
        for a in state["plan"].get("actions", []):
            ap = ActionProposal.model_validate(a)
            ok, why = _code_check(ap, entities, deps.blocklist)
            checked.append(
                {
                    **ap.model_dump(),
                    "code_ok": ok,
                    "code_reason": why,
                    "approved": ok,
                    "verifier_reason": why if not ok else "code checks passed (single-agent mode)",
                }
            )
        return {"verified": checked}

    # --- wiring --------------------------------------------------------------------------------

    g: StateGraph = StateGraph(AgentState)  # type: ignore[type-arg]
    if s.agent_mode == "single":
        g.add_node("single_agent", single_agent)
        g.add_node("open_incident_single", open_incident)
        g.add_node("dismiss", dismiss)
        g.add_node("verify", verify_code_only)
        g.add_node("dispatch", dispatch)
        g.add_edge(START, "single_agent")
        g.add_conditional_edges(
            "single_agent",
            route_single,
            {"open_incident_single": "open_incident_single", "dismiss": "dismiss"},
        )
        g.add_edge("open_incident_single", "verify")
        g.add_edge("verify", "dispatch")
        g.add_edge("dispatch", END)
        g.add_edge("dismiss", END)
        return g.compile()

    g.add_node("triage", triage)
    g.add_node("dismiss", dismiss)
    g.add_node("open_incident", open_incident)
    g.add_node("investigate", investigate)
    g.add_node("plan", plan)
    g.add_node("verify", verify)
    g.add_node("dispatch", dispatch)
    g.add_edge(START, "triage")
    g.add_conditional_edges(
        "triage", route_after_triage, {"open_incident": "open_incident", "dismiss": "dismiss"}
    )
    g.add_edge("open_incident", "investigate")
    g.add_edge("investigate", "plan")
    g.add_edge("plan", "verify")
    g.add_edge("verify", "dispatch")
    g.add_edge("dispatch", END)
    g.add_edge("dismiss", END)
    return g.compile()


# --- helpers ---------------------------------------------------------------------------------


def get_tracer_trace_id() -> str:
    from haulmind.tracing import current_trace_id

    return current_trace_id() or ""


def _trim(data: dict[str, Any], limit: int = 2500) -> dict[str, Any]:
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"truncated": True, "preview": text[:limit]}


def _plan_payload(state: AgentState) -> dict[str, Any]:
    cand = state.get("candidates", {})
    return {
        "domain": state["domain"],
        "incident_type": state["incident_type"],
        "severity": state["severity"],
        "sim_ts": state["sim_ts"],
        "entities": {
            "vehicle_id": state.get("vehicle_id"),
            "driver_id": state.get("driver_id"),
            "trip_id": cand.get("trip_id"),
        },
        "signals": state["prefilter"]["signals"],
        "candidates": cand,
        "evidence": [
            {"id": e["id"], "summary": e["summary"], "tool": e["tool"]}
            for e in state.get("evidence", [])
        ],
        "findings": state.get("findings", ""),
        "memory_notes": prompt_notes(state.get("memory", {})),
        "memory": {
            k: v
            for k, v in state.get("memory", {}).items()
            if k in ("margin_threshold", "nuisance_codes", "reefer_fills_ok", "harsh_threshold")
        },
    }


def _code_check(
    a: ActionProposal, entities: dict[str, Any], blocklist: BlockList
) -> tuple[bool, str]:
    if a.tool not in WRITE_TOOLS:
        return False, f"unknown or read-only tool '{a.tool}'"
    missing = [k for k in REQUIRED_ARGS.get(a.tool, ()) if not a.args.get(k)]
    if missing:
        return False, f"missing required args: {', '.join(missing)}"
    blocked = blocklist.check(a.tool, a.args)
    if blocked:
        return False, f"blocked by policy: {blocked}"
    v, d = entities.get("vehicle_id"), entities.get("driver_id")
    if v and a.args.get("vehicle_id") and a.args["vehicle_id"] != v:
        return False, "vehicle does not match the incident"
    if d and a.args.get("driver_id") and a.args["driver_id"] != d:
        return False, "driver does not match the incident"
    if d and a.args.get("from_driver_id") and a.args["from_driver_id"] != d:
        return False, "from_driver does not match the incident"
    if (
        a.tool == "propose_reassignment"
        and entities.get("trip_id")
        and a.args.get("trip_id") != entities["trip_id"]
    ):
        return False, "trip does not match the driver's current trip"
    if (
        a.tool == "flag_fuel_transaction"
        and entities.get("txn_ids")
        and a.args.get("transaction_id") not in entities["txn_ids"]
    ):
        return False, "transaction id is not one of the anomalous transactions"
    return True, "ok"

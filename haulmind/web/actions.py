"""Manager decisions on proposed actions: approve (executes through the MCP gateway with a bound
approval), reject, acknowledge recommendations, edit arguments. Every decision feeds memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from haulmind.agents.memory import apply_decision
from haulmind.db.engine import session_scope
from haulmind.db.models import Approval, Incident, ProposedAction
from haulmind.db.repo import audit
from haulmind.events.schemas import Domain
from haulmind.mcp_server.client import ToolClient, ToolError
from haulmind.policy.engine import args_hash
from haulmind.policy.tiers import Tier
from haulmind.util import new_id, to_naive_utc


@dataclass
class DecisionOutcome:
    ok: bool
    status: str
    message: str
    memory_changes: dict[str, Any]


def _now() -> datetime:
    return to_naive_utc(datetime.now(UTC))


async def decide(
    action_id: str,
    decision: str,
    *,
    tools: ToolClient,
    reason_code: str = "",
    reason_text: str = "",
    actor: str = "manager",
) -> DecisionOutcome:
    with session_scope() as s:
        act = s.get(ProposedAction, action_id)
        if act is None:
            return DecisionOutcome(False, "missing", "Action not found.", {})
        inc = s.get(Incident, act.incident_id) if act.incident_id else None
        tier = Tier(act.tier) if act.tier in (0, 1, 2, 3) else None
        if act.status not in ("pending", "recommended"):
            return DecisionOutcome(False, act.status, f"Already decided ({act.status}).", {})
        tool_name, args, run_id = act.tool_name, dict(act.args_json), act.run_id
        incident_id = act.incident_id
        domain = Domain(inc.domain) if inc else None
        incident_type = inc.type if inc else act.tool_name
        cluster_key = inc.cluster_key if inc else f"action:{act.id}"
        sim_ts = act.sim_ts or (inc.sim_ts if inc else _now())

        if decision == "approve" and tier == Tier.APPROVAL:
            ap = Approval(
                id=new_id("APR"),
                action_id=act.id,
                decision="approve",
                reason=reason_text,
                decided_by=actor,
                tool_name=tool_name,
                args_hash=args_hash(tool_name, args),
                consumed=False,
                ts=_now(),
            )
            s.add(ap)
            act.status = "approved"
            act.decided_ts = _now()
            act.decided_by = actor
            act.decision_reason = reason_text
            audit(
                s,
                actor=actor,
                action="action.approved",
                entity_type="action",
                entity_id=act.id,
                details={"tool": tool_name, "approval_id": ap.id},
                run_id=run_id,
            )
            approval_id = ap.id
        elif decision == "reject":
            s.add(
                Approval(
                    id=new_id("APR"),
                    action_id=act.id,
                    decision="reject",
                    reason=f"{reason_code}: {reason_text}".strip(": "),
                    decided_by=actor,
                    tool_name=tool_name,
                    args_hash=act.args_hash,
                    consumed=True,
                    ts=_now(),
                )
            )
            act.status = "rejected"
            act.decided_ts = _now()
            act.decided_by = actor
            act.decision_reason = f"{reason_code}: {reason_text}".strip(": ")
            audit(
                s,
                actor=actor,
                action="action.rejected",
                entity_type="action",
                entity_id=act.id,
                details={"tool": tool_name, "reason_code": reason_code, "reason": reason_text},
                run_id=run_id,
            )
            changes = (
                apply_decision(
                    s,
                    act,
                    decision="reject",
                    reason_code=reason_code,
                    reason_text=reason_text,
                    domain=domain,
                    incident_type=incident_type,
                    sim_ts=sim_ts,
                    cluster_key=cluster_key,
                )
                if domain
                else {}
            )
            _close_if_done(s, inc)
            return DecisionOutcome(
                True, "rejected", "Rejected. The agent will remember this.", changes
            )
        elif decision == "acknowledge" and tier == Tier.RECOMMEND:
            act.status = "acknowledged"
            act.decided_ts = _now()
            act.decided_by = actor
            act.decision_reason = reason_text or "handled outside the system"
            audit(
                s,
                actor=actor,
                action="action.acknowledged",
                entity_type="action",
                entity_id=act.id,
                details={"tool": tool_name},
                run_id=run_id,
            )
            changes = (
                apply_decision(
                    s,
                    act,
                    decision="approve",
                    reason_code=reason_code,
                    reason_text=reason_text,
                    domain=domain,
                    incident_type=incident_type,
                    sim_ts=sim_ts,
                    cluster_key=cluster_key,
                )
                if domain
                else {}
            )
            _close_if_done(s, inc)
            return DecisionOutcome(True, "acknowledged", "Marked as handled by a human.", changes)
        else:
            return DecisionOutcome(
                False, act.status, f"'{decision}' is not valid for a tier {act.tier} action.", {}
            )

    # Execute outside the session: the MCP gateway validates and consumes the approval.
    try:
        result = await tools.call(
            tool_name, {**args, "approval_id": approval_id, "incident_id": incident_id}
        )
    except ToolError as e:
        result = {"status": "failed", "message": str(e)[:300]}
    status = str(result.get("status", "failed"))
    with session_scope() as s:
        act = s.get(ProposedAction, action_id)
        inc = s.get(Incident, incident_id) if incident_id else None
        assert act is not None
        if status == "executed":
            act.status = "executed"
            act.executed_ts = _now()
            act.result_json = result.get("details", {}) or result
            msg = "Approved and executed."
        else:
            act.status = (
                status
                if status in ("blocked", "rate_limited", "approval_invalid", "failed")
                else "failed"
            )
            act.result_json = result
            act.decision_reason = (
                act.decision_reason + " | " if act.decision_reason else ""
            ) + f"gateway: {result.get('message', '')}"
            msg = f"Approved, but the policy gateway refused execution: {result.get('message', status)}"
        audit(
            s,
            actor="policy-gateway",
            action=f"action.{act.status}",
            entity_type="action",
            entity_id=act.id,
            details={"tool": tool_name, "result": result},
            run_id=run_id,
        )
        changes = (
            apply_decision(
                s,
                act,
                decision="approve",
                reason_code=reason_code,
                reason_text=reason_text,
                domain=domain,
                incident_type=incident_type,
                sim_ts=sim_ts,
                cluster_key=cluster_key,
            )
            if domain
            else {}
        )
        _close_if_done(s, inc)
    return DecisionOutcome(status == "executed", act.status, msg, changes)


def edit_action(action_id: str, new_args: dict[str, Any], *, actor: str = "manager") -> bool:
    with session_scope() as s:
        act = s.get(ProposedAction, action_id)
        if act is None or act.status not in ("pending", "recommended"):
            return False
        before = dict(act.args_json)
        act.args_json = new_args
        act.args_hash = args_hash(act.tool_name, new_args)
        act.proposed_by = f"{act.proposed_by}+edited"
        audit(
            s,
            actor=actor,
            action="action.edited",
            entity_type="action",
            entity_id=act.id,
            details={"before": before, "after": new_args},
            run_id=act.run_id,
        )
    return True


def _close_if_done(s: Any, inc: Incident | None) -> None:
    if inc is None:
        return
    from sqlalchemy import select

    open_left = s.execute(
        select(ProposedAction.id).where(
            ProposedAction.incident_id == inc.id,
            ProposedAction.status.in_(("pending", "recommended")),
        )
    ).first()
    if open_left is None and inc.status == "open":
        inc.status = "resolved"
        inc.closed_ts = _now()

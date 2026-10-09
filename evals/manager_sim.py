"""A scripted fleet manager for the learning-loop experiment.

Approves actions on incidents that match an injected positive scenario; rejects the rest with a
reason code that a real manager would plausibly pick. Rejections feed agent memory exactly like
the inbox does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from evals.metrics import TYPE_MATCH, _matches
from haulmind.agents.memory import apply_decision
from haulmind.db.engine import session_scope
from haulmind.db.models import Incident, ProposedAction
from haulmind.db.repo import audit
from haulmind.events.schemas import Domain
from haulmind.mcp_server.client import ToolClient
from haulmind.simulator.ground_truth import GroundTruth
from haulmind.util import to_naive_utc
from haulmind.web import actions as decisions

REASON_BY_GT = {
    "hos_near_miss": (
        "driver_manages_clock",
        "Driver arrived with time to spare; do not alert at this margin.",
    ),
    "nuisance_dtc": ("known_intermittent", "Known intermittent code on this unit; temps normal."),
    "reefer_fuel_lookalike": ("legit_fuel", "Reefer unit; combined fill is legitimate."),
}


@dataclass
class ManagerStats:
    approved: int = 0
    rejected: int = 0
    acknowledged: int = 0
    flagged_auto: int = 0


async def decide_all(
    run_id: str, gt: GroundTruth, tools: ToolClient, *, until_sim_ts: datetime | None = None
) -> ManagerStats:
    """Decide every undecided action for incidents opened before ``until_sim_ts``."""
    st = ManagerStats()
    with session_scope() as s:
        incs = {
            i.id: i for i in s.execute(select(Incident).where(Incident.run_id == run_id)).scalars()
        }
        acts = list(
            s.execute(
                select(ProposedAction).where(
                    ProposedAction.run_id == run_id,
                    ProposedAction.status.in_(("pending", "recommended", "auto_executed")),
                )
            )
            .scalars()
            .all()
        )
    for a in acts:
        inc = incs.get(a.incident_id or "")
        if inc is None or (until_sim_ts is not None and inc.sim_ts >= until_sim_ts):
            continue
        positive = next((g for g in gt.incidents if not g.benign and _matches(inc, g)), None)
        benign = next((g for g in gt.incidents if g.benign and _matches(inc, g)), None)
        if a.status == "auto_executed":
            if positive is None:
                _flag_auto_false_positive(a.id, inc, benign.type if benign else "")
                st.flagged_auto += 1
            continue
        if positive is not None:
            out = await decisions.decide(
                a.id,
                "approve" if a.status == "pending" else "acknowledge",
                tools=tools,
                reason_text="matches a real issue",
                actor="scripted-manager",
            )
            st.approved += 1 if a.status == "pending" else 0
            st.acknowledged += 1 if a.status == "recommended" else 0
            if not out.ok and a.status == "pending":
                pass  # the gateway may refuse (rate limit); still counts as a decision
        else:
            code, text = REASON_BY_GT.get(
                benign.type if benign else "", ("not_an_issue", "Not an issue.")
            )
            await decisions.decide(
                a.id,
                "reject",
                tools=tools,
                reason_code=code,
                reason_text=text,
                actor="scripted-manager",
            )
            st.rejected += 1
    return st


def _flag_auto_false_positive(action_id: str, inc: Incident, gt_type: str) -> None:
    """Auto-executed items cannot be undone, but a manager can still tell the agent it was wrong."""
    code, text = REASON_BY_GT.get(gt_type, ("not_an_issue", "Not an issue."))
    with session_scope() as s:
        act = s.get(ProposedAction, action_id)
        if act is None:
            return
        act.decision_reason = f"{code}: {text}"
        act.decided_by = "scripted-manager"
        apply_decision(
            s,
            act,
            decision="reject",
            reason_code=code,
            reason_text=text,
            domain=Domain(inc.domain),
            incident_type=inc.type,
            sim_ts=to_naive_utc(inc.sim_ts),
            cluster_key=inc.cluster_key,
        )
        audit(
            s,
            actor="scripted-manager",
            action="action.flagged_false_positive",
            entity_type="action",
            entity_id=act.id,
            details={"reason_code": code},
            run_id=inc.run_id,
        )


__all__ = ["TYPE_MATCH", "ManagerStats", "decide_all"]

"""End to end on the stub LLM: events -> incidents -> tiered actions -> approval -> memory."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from haulmind.db.engine import session_scope
from haulmind.db.models import (
    Approval,
    Incident,
    ProposedAction,
    Reassignment,
    Span,
    Trace,
    WorkOrder,
)
from haulmind.db.repo import memory_get
from haulmind.events.bus import EventBus
from haulmind.llm.factory import get_llm_client
from haulmind.mcp_server.client import ToolClient
from haulmind.orchestrator import Orchestrator, replay_events
from haulmind.simulator import SimConfig, generate, install_run
from haulmind.web import actions as decisions


@pytest.fixture
def run42():  # type: ignore[no-untyped-def]
    sim = generate(SimConfig(seed=42, fleet_size=8, hours=24))
    install_run(sim)
    return sim


def _gt(sim, type_: str):  # type: ignore[no-untyped-def]
    return next(g for g in sim.ground_truth.incidents if g.type == type_)


async def _replay(sim, tc: ToolClient) -> Orchestrator:  # type: ignore[no-untyped-def]
    orch = Orchestrator(sim.run_id, tools=tc, llm=get_llm_client())
    await replay_events(orch, sim.events, EventBus())
    return orch


async def test_replay_detects_injected_incidents_with_correct_tiers(
    run42, tool_client: ToolClient
) -> None:  # type: ignore[no-untyped-def]
    orch = await _replay(run42, tool_client)
    assert orch.stats.errors == 0
    assert orch.stats.filtered_pct > 95
    hos_gt = _gt(run42, "hos_violation_risk")
    fraud_gt = _gt(run42, "fuel_fraud")
    maint_gt = _gt(run42, "failing_component")
    with session_scope() as s:
        incs = s.execute(select(Incident).where(Incident.run_id == run42.run_id)).scalars().all()
        acts = (
            s.execute(select(ProposedAction).where(ProposedAction.run_id == run42.run_id))
            .scalars()
            .all()
        )
        tools_by_inc: dict[str, dict[str, ProposedAction]] = {}
        for a in acts:
            tools_by_inc.setdefault(a.incident_id or "", {})[a.tool_name] = a
        # HOS risk: message auto-executed (tier 1), reassignment waits for approval (tier 2)
        hos = [
            i for i in incs if i.type == "hos_violation_risk" and i.driver_id == hos_gt.driver_id
        ]
        assert hos, f"the injected HOS risk on {hos_gt.driver_id} must be detected"
        t = tools_by_inc[hos[0].id]
        assert t["message_driver"].status == "auto_executed" and t["message_driver"].tier == 1
        assert t["propose_reassignment"].status == "pending" and t["propose_reassignment"].tier == 2
        # maintenance: work order queued, never executed on its own
        maint = [
            i for i in incs if i.type == "failing_component" and i.vehicle_id == maint_gt.vehicle_id
        ]
        assert maint and tools_by_inc[maint[0].id]["create_work_order"].status == "pending"
        assert s.execute(select(func.count(WorkOrder.id))).scalar_one() == 0
        # fuel fraud: flagged automatically
        fraud = [i for i in incs if i.type == "fuel_fraud" and i.vehicle_id == fraud_gt.vehicle_id]
        assert (
            fraud and tools_by_inc[fraud[0].id]["flag_fuel_transaction"].status == "auto_executed"
        )
        # reefer look-alike (if this config has one): investigated, no action against the driver
        for g in (g for g in run42.ground_truth.incidents if g.type == "reefer_fuel_lookalike"):
            for i in incs:
                if i.vehicle_id == g.vehicle_id and i.domain == "spend":
                    assert i.status == "no_action"
        # tracing covered every incident
        traces = s.execute(select(Trace)).scalars().all()
        spans = s.execute(select(Span)).scalars().all()
        assert len(traces) >= len(incs)
        assert {sp.kind for sp in spans} >= {"llm_call", "tool_call", "agent_step"}
        assert all(i.llm_requests > 0 for i in incs)


async def test_manager_approval_executes_tier2_through_the_gateway(
    run42, tool_client: ToolClient
) -> None:  # type: ignore[no-untyped-def]
    await _replay(run42, tool_client)
    with session_scope() as s:
        act = (
            s.execute(
                select(ProposedAction).where(
                    ProposedAction.tool_name == "propose_reassignment",
                    ProposedAction.status == "pending",
                )
            )
            .scalars()
            .first()
        )
        assert act is not None
        action_id = act.id
    out = await decisions.decide(action_id, "approve", tools=tool_client, reason_text="go")
    assert out.ok and out.status == "executed", out.message
    with session_scope() as s:
        assert s.execute(select(func.count(Reassignment.id))).scalar_one() == 1
        ap = s.execute(select(Approval).where(Approval.action_id == action_id)).scalars().one()
        assert ap.consumed is True
        assert s.get(ProposedAction, action_id).status == "executed"
    again = await decisions.decide(action_id, "approve", tools=tool_client)
    assert not again.ok, "approving twice is refused"


async def test_rejection_updates_memory_and_suppresses_repeat(
    run42, tool_client: ToolClient
) -> None:  # type: ignore[no-untyped-def]
    await _replay(run42, tool_client)
    with session_scope() as s:
        act = (
            s.execute(
                select(ProposedAction).where(
                    ProposedAction.tool_name == "propose_reassignment",
                    ProposedAction.status == "pending",
                )
            )
            .scalars()
            .first()
        )
        assert act is not None
        inc = s.get(Incident, act.incident_id)
        assert inc is not None
        action_id, driver_id, cluster_key = act.id, inc.driver_id, inc.cluster_key
    out = await decisions.decide(
        action_id,
        "reject",
        tools=tool_client,
        reason_code="driver_manages_clock",
        reason_text="arrives in time",
    )
    assert out.ok and out.status == "rejected"
    assert out.memory_changes.get("margin_threshold", 99) <= 0
    with session_scope() as s:
        assert memory_get(s, "driver", driver_id, "fp_count:hos_violation_risk") == {"n": 1}
        assert memory_get(s, "cluster", cluster_key, "suppress_until") is not None
        notes = memory_get(s, "driver", driver_id, "notes")
        assert notes and "arrives in time" in notes["items"][0]
        assert s.get(ProposedAction, action_id).status == "rejected"
        assert s.execute(select(func.count(Reassignment.id))).scalar_one() == 0

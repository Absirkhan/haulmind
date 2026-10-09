"""Web layer: every page renders and the approve/reject handlers drive the gateway."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from haulmind.db.engine import session_scope
from haulmind.db.models import ProposedAction
from haulmind.events.bus import EventBus
from haulmind.llm.factory import get_llm_client
from haulmind.mcp_server.client import ToolClient
from haulmind.orchestrator import Orchestrator, replay_events
from haulmind.simulator import SimConfig, generate, install_run
from haulmind.web.app import create_app
from haulmind.web.runtime import AppRuntime, set_runtime


@pytest.fixture
def client(tool_client: ToolClient) -> Iterator[TestClient]:
    os.environ["HAULMIND_NO_RUNTIME"] = "1"
    rt = AppRuntime()
    rt.tools = tool_client  # manager decisions execute through the same MCP client
    set_runtime(rt)
    with TestClient(create_app()) as c:
        yield c
    set_runtime(None)
    os.environ.pop("HAULMIND_NO_RUNTIME", None)


async def _populate(tc: ToolClient) -> str:
    sim = generate(SimConfig(seed=42, fleet_size=8, hours=24))
    install_run(sim)
    orch = Orchestrator(sim.run_id, tools=tc, llm=get_llm_client())
    await replay_events(orch, sim.events, EventBus())
    return sim.run_id


async def test_pages_render_and_decisions_work(client: TestClient, tool_client: ToolClient) -> None:
    await _populate(tool_client)
    for path in (
        "/inbox",
        "/inbox?tab=auto",
        "/inbox?tab=recommended",
        "/inbox?tab=decided",
        "/incidents",
        "/traces",
        "/audit",
        "/fleet",
        "/stream",
        "/health",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Something went wrong" not in r.text
    with session_scope() as s:
        pending = (
            s.execute(select(ProposedAction).where(ProposedAction.status == "pending"))
            .scalars()
            .all()
        )
        assert pending
        act = pending[0]
        action_id, incident_id = act.id, act.incident_id
    detail = client.get(f"/actions/{action_id}", headers={"HX-Request": "true"})
    assert (
        detail.status_code == 200
        and "Approve and run" in detail.text
        and "Evidence the agent gathered" in detail.text
    )
    inc = client.get(f"/incidents/{incident_id}")
    assert inc.status_code == 200 and "Timeline" in inc.text
    edit = client.get(f"/actions/{action_id}/edit", headers={"HX-Request": "true"})
    assert edit.status_code == 200 and "Save changes" in edit.text
    approved = client.post(
        f"/actions/{action_id}/approve", data={"reason_text": "ok"}, headers={"HX-Request": "true"}
    )
    assert approved.status_code == 200 and approved.headers.get("HX-Trigger") == "queue-changed"
    assert "executed" in approved.text
    with session_scope() as s:
        assert s.get(ProposedAction, action_id).status == "executed"
        other = (
            s.execute(select(ProposedAction).where(ProposedAction.status == "pending"))
            .scalars()
            .first()
        )
    if other is not None:
        rejected = client.post(
            f"/actions/{other.id}/reject",
            data={"reason_code": "not_an_issue", "reason_text": "nope"},
            headers={"HX-Request": "true"},
        )
        assert rejected.status_code == 200 and "Rejected" in rejected.text
        with session_scope() as s:
            assert s.get(ProposedAction, other.id).status == "rejected"
    trace_list = client.get("/traces")
    trace_id = trace_list.text.split("/traces/TR-")[1].split('"')[0]
    assert client.get(f"/traces/TR-{trace_id}").status_code == 200
    assert client.get("/actions/ACT-missing", headers={"HX-Request": "true"}).status_code == 404

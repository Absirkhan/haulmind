"""Web layer: every page renders, decisions drive the gateway, and no developer jargon leaks."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from haulmind.db.engine import session_scope
from haulmind.db.models import Incident, ProposedAction
from haulmind.events.bus import EventBus
from haulmind.llm.factory import get_llm_client
from haulmind.mcp_server.client import ToolClient
from haulmind.orchestrator import Orchestrator, replay_events
from haulmind.policy.tiers import TOOL_TIERS
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
        "/",
        "/inbox",
        "/inbox?tab=auto",
        "/inbox?tab=recommended",
        "/inbox?tab=decided",
        "/inbox?q=truck&urgency=urgent",
        "/incidents",
        "/traces",
        "/audit",
        "/fleet",
        "/performance",
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
    assert detail.status_code == 200
    assert 'data-kind="approve"' in detail.text
    assert "What Haulmind checked" in detail.text
    inc = client.get(f"/incidents/{incident_id}")
    assert inc.status_code == 200 and "What happened" in inc.text
    edit = client.get(f"/actions/{action_id}/edit", headers={"HX-Request": "true"})
    assert edit.status_code == 200 and "Save changes" in edit.text
    approved = client.post(
        f"/actions/{action_id}/approve", data={"reason_text": "ok"}, headers={"HX-Request": "true"}
    )
    assert approved.status_code == 200 and approved.headers.get("HX-Trigger") == "queue-changed"
    assert "Approved." in approved.text
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
        assert rejected.status_code == 200 and "Dismissed" in rejected.text
        with session_scope() as s:
            assert s.get(ProposedAction, other.id).status == "rejected"
    trace_list = client.get("/traces")
    trace_id = trace_list.text.split("/traces/TR-")[1].split('"')[0]
    assert client.get(f"/traces/TR-{trace_id}").status_code == 200
    assert client.get("/actions/ACT-missing", headers={"HX-Request": "true"}).status_code == 404


# --- jargon sweep ----------------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "template", "svg"}
READ_ATTRS = ("aria-label", "placeholder", "alt")
TOOL_NAMES = sorted(TOOL_TIERS)
JARGON = re.compile(
    r"\b(payload|cluster|pipeline|inference|tool call|agent run|llm|mcp|tokens?|latency|triage|"
    r"verifier|enum|uuid|stack trace|json|snake_case|null|undefined|traceback)\b",
    re.IGNORECASE,
)
RAW_PATTERNS = [
    ("vehicle/driver/shop id", re.compile(r"\b[VDS]-\d{2,}\b")),
    ("trip id", re.compile(r"\bT-V-\d+-\d+\b")),
    ("record id", re.compile(r"\b(INC|TR|ACT|APR|SP|WO|MSG|RA|FC|FT|gt)-[A-Za-z0-9]{3,}\b")),
    ("run id", re.compile(r"\brun-\d+-[0-9a-f]+\b")),
    ("snake_case", re.compile(r"\b[a-z]+_[a-z0-9_]+\b")),
    ("json braces", re.compile(r"[{}]")),
    ("tool name", re.compile(r"\b(" + "|".join(TOOL_NAMES) + r")\b")),
    ("raw confidence", re.compile(r"\b0\.\d{2}\b")),
    ("raw milliseconds", re.compile(r"\b\d+ ?ms\b")),
    ("abbreviation", re.compile(r"\b(HOS|DTC|ECM|PCM|RPM|RPD|TPM)\b")),
    ("jargon", JARGON),
]


class VisibleText(HTMLParser):
    """Collects the text a person can see or hear, skipping 'Technical details' disclosures."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        technical = tag == "details" and "technical" in (a.get("class") or "")
        if tag in SKIP_TAGS or technical or self._skip_depth:
            self._skip_depth += 1
        self._stack.append(tag)
        if not self._skip_depth:
            for key in READ_ATTRS:
                if a.get(key):
                    self.chunks.append(str(a[key]))

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()
        if self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(data)


def visible_text(html: str) -> str:
    p = VisibleText()
    p.feed(html)
    return "\n".join(c.strip() for c in p.chunks)


def jargon_findings(html: str) -> list[str]:
    text = visible_text(html)
    found = []
    for name, pattern in RAW_PATTERNS:
        for m in pattern.finditer(text):
            start = max(0, m.start() - 40)
            found.append(f"{name}: ...{text[start : m.end() + 40]!r}")
    return found


async def test_no_developer_jargon_is_visible(client: TestClient, tool_client: ToolClient) -> None:
    await _populate(tool_client)
    with session_scope() as s:
        acts = s.execute(select(ProposedAction).limit(12)).scalars().all()
        ids = [a.id for a in acts]
        incs = [i.id for i in s.execute(select(Incident).limit(6)).scalars()]
    pages = [
        "/",
        "/inbox",
        "/inbox?tab=auto",
        "/inbox?tab=recommended",
        "/inbox?tab=decided",
        "/incidents",
        "/fleet",
        "/audit",
        "/traces",
        "/performance",
        "/stream",
        *[f"/incidents/{i}" for i in incs],
    ]
    problems: list[str] = []
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        problems += [f"{path} -> {f}" for f in jargon_findings(r.text)]
    for action_id in ids:
        r = client.get(f"/actions/{action_id}", headers={"HX-Request": "true"})
        problems += [f"/actions/{action_id} -> {f}" for f in jargon_findings(r.text)]
        r = client.get(f"/actions/{action_id}/edit", headers={"HX-Request": "true"})
        problems += [f"/actions/{action_id}/edit -> {f}" for f in jargon_findings(r.text)]
    trace_list = client.get("/traces")
    for trace_id in sorted(set(re.findall(r"/traces/(TR-[0-9a-f]+)", trace_list.text)))[:4]:
        r = client.get(f"/traces/{trace_id}")
        problems += [f"/traces/{trace_id} -> {f}" for f in jargon_findings(r.text)]
    assert not problems, "developer text visible to users:\n" + "\n".join(problems[:40])

"""Test fixtures: isolated data dir, fresh SQLite schema per test, stub LLM, in-process MCP."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

os.environ["LLM_PROVIDER"] = "stub"
os.environ["MCP_TRANSPORT"] = "inprocess"
os.environ["SIM_AUTOSTART"] = "false"
os.environ.pop("GEMINI_API_KEY", None)


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("haulmind-data")
    os.environ["DATA_DIR"] = str(d)
    return d


@pytest.fixture(autouse=True)
def fresh_db(data_dir: Path) -> Iterator[None]:
    from haulmind.config import get_settings, reset_settings_cache
    from haulmind.db.engine import get_engine, reset_engine
    from haulmind.db.models import Base
    from haulmind.llm.factory import reset_llm_client
    from haulmind.mcp_server.tools import set_policy_engine

    reset_settings_cache()
    reset_engine()
    s = get_settings()
    assert s.data_dir == data_dir
    s.ensure_dirs()
    engine = get_engine(s)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reset_llm_client()
    set_policy_engine(None)
    yield
    reset_engine()


@pytest.fixture
def small_run():  # type: ignore[no-untyped-def]
    from haulmind.simulator import SimConfig, generate, install_run

    sim = generate(SimConfig(seed=7, fleet_size=6, hours=12))
    install_run(sim)
    return sim


@pytest.fixture
async def tool_client() -> AsyncIterator[object]:
    import haulmind.mcp_server.server as srv
    from haulmind.mcp_server.client import ToolClient
    from haulmind.mcp_server.server import build_server

    srv._server = build_server()
    async with ToolClient(caller="test-agent") as tc:
        yield tc
    srv._server = None


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()

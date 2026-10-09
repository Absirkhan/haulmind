"""Agent-side tool client. Talks MCP either in-process (tests, evals) or over streamable HTTP
(``make run``). Every call is wrapped in a ``tool_call`` trace span."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import TracebackType
from typing import Any, cast

import mcp
from mcp.types import RequestParamsMeta

from haulmind.config import Settings, get_settings
from haulmind.llm.base import ToolSpec
from haulmind.logging_setup import get_logger
from haulmind.policy.tiers import READ_TOOLS, TOOL_TIERS
from haulmind.tracing import get_tracer

log = get_logger(__name__)


class ToolError(Exception):
    pass


class ToolClient:
    """Thin async wrapper over ``mcp.Client`` with retries, tracing and schema export."""

    def __init__(self, settings: Settings | None = None, *, caller: str = "agent") -> None:
        self.settings = settings or get_settings()
        self.caller = caller
        self._client: mcp.Client | None = None
        self._tools: list[dict[str, Any]] | None = None
        self.calls = 0

    def _target(self) -> Any:
        if self.settings.mcp_transport == "http":
            return self.settings.mcp_url
        from haulmind.mcp_server.server import get_server

        return get_server()

    async def __aenter__(self) -> ToolClient:
        await self.connect()
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def connect(self, *, wait_s: float = 0.0) -> None:
        deadline = asyncio.get_event_loop().time() + wait_s
        while True:
            try:
                client = mcp.Client(
                    self._target(),
                    client_info=mcp.Implementation(name=f"haulmind-{self.caller}", version="0.1.0"),
                )
                await client.__aenter__()
                self._client = client
                return
            except Exception as e:
                if asyncio.get_event_loop().time() >= deadline:
                    raise
                log.info("waiting for MCP server", error=str(e)[:80])
                await asyncio.sleep(1.0)

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            assert self._client is not None, "not connected"
            res = await self._client.list_tools()
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.input_schema,
                    "tier": int(TOOL_TIERS.get(t.name, 0)),
                }
                for t in res.tools
            ]
        return self._tools

    async def read_tool_specs(self) -> list[ToolSpec]:
        """Tier-0 tools as provider-neutral specs for the investigator's function calling."""
        specs = []
        for t in await self.list_tools():
            if t["name"] in READ_TOOLS:
                specs.append(
                    ToolSpec(
                        name=t["name"], description=t["description"], parameters=t["input_schema"]
                    )
                )
        return specs

    async def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool and return its structured content (or parsed text). Raises ``ToolError``."""
        assert self._client is not None, "not connected"
        args = {k: v for k, v in (args or {}).items() if v is not None}
        tracer = get_tracer()
        async with tracer.span(
            f"tool:{name}", kind="tool_call", tool=name, args=_short(args)
        ) as sp:
            self.calls += 1
            try:
                res = await self._client.call_tool(
                    name, args, meta=cast("RequestParamsMeta", {"caller": self.caller})
                )
            except mcp.MCPError as e:
                sp.fail(e)
                raise ToolError(str(e)) from e
            if res.is_error:
                text = " ".join(getattr(c, "text", "") for c in res.content)
                sp.fail(text)
                raise ToolError(text or f"{name} failed")
            if res.structured_content is not None:
                out = dict(res.structured_content)
            else:
                text = " ".join(getattr(c, "text", "") for c in res.content)
                try:
                    parsed = json.loads(text)
                    out = parsed if isinstance(parsed, dict) else {"result": parsed}
                except json.JSONDecodeError:
                    out = {"result": text}
            sp.set(result_keys=list(out.keys())[:12])
            return out


def _short(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        s = v if isinstance(v, int | float | bool) or v is None else str(v)
        out[k] = s if not isinstance(s, str) or len(s) <= 120 else s[:117] + "..."
    return out

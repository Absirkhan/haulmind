"""The MCPServer app: registers every fleet tool with typed I/O and a risk-tier annotation."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from haulmind.mcp_server import tools as fleet
from haulmind.mcp_server.schemas import (
    ActionResult,
    DriverHos,
    FaultHistory,
    FuelTxnsResult,
    HosRisksResult,
    IdleVehiclesResult,
    NearbyShopsResult,
    RecentEventsResult,
    SearchDocsResult,
    VehicleStatus,
)
from haulmind.policy.tiers import TOOL_TIERS, Tier

INSTRUCTIONS = """Haulmind fleet operations tools.

Read tools (tier 0) return the current projected state of the simulated fleet.
Write tools are policy-gated in code: tier 1 (message_driver, flag_fuel_transaction) execute
immediately subject to rate limits and a block list; tier 2 (create_work_order,
propose_reassignment) are queued for one-click manager approval unless a valid approval_id is
supplied; tier 3 (suspend_fuel_card, set_vehicle_out_of_service) are recorded as
recommendations and never executed by the system. Timestamps are simulated time (see sim_now).
"""


def _caller(ctx: Context | None, default: str = "mcp-client") -> str:
    """Identify the caller: agents set ``_meta.caller``; otherwise use the MCP client name."""
    try:
        if ctx is not None:
            meta: Any = ctx.request_context.meta
            if isinstance(meta, dict) and meta.get("caller"):
                return str(meta["caller"])[:40]
            if meta is not None and getattr(meta, "caller", None):
                return str(meta.caller)[:40]
            params = getattr(ctx.session, "client_params", None)
            info = getattr(params, "clientInfo", None) if params is not None else None
            if info is not None and getattr(info, "name", None):
                return f"mcp:{info.name}"[:40]
    except Exception:  # pragma: no cover - best effort only
        pass
    return default


P = ParamSpec("P")
R = TypeVar("R")


def _guard(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn domain errors (unknown ids, bad input) into clean MCP tool errors."""

    @functools.wraps(fn)
    def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            raise ToolError(str(e)) from e

    return inner


def _ann(tool: str) -> ToolAnnotations:
    tier = TOOL_TIERS[tool]
    return ToolAnnotations(
        title=f"{tool} (tier {int(tier)}: {tier.label})",
        readOnlyHint=tier == Tier.READ,
        destructiveHint=tier >= Tier.APPROVAL,
        idempotentHint=tier == Tier.READ,
        openWorldHint=False,
    )


def build_server() -> MCPServer:
    server: MCPServer = MCPServer(
        "haulmind-fleet",
        title="Haulmind fleet tools",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    # --- tier 0: read ------------------------------------------------------------------------

    @server.tool(
        annotations=_ann("get_vehicle_status"),
        description="Current status, telemetry, driver, trip, active faults and fuel capacity (including reefer tank) for a vehicle.",
    )
    async def get_vehicle_status(vehicle_id: str) -> VehicleStatus:
        return _guard(fleet.get_vehicle_status)(vehicle_id)

    @server.tool(
        annotations=_ann("get_driver_hos"),
        description="Hours-of-service clocks for a driver, plus current trip, estimated minutes to destination and the margin between them.",
    )
    async def get_driver_hos(driver_id: str) -> DriverHos:
        return _guard(fleet.get_driver_hos)(driver_id)

    @server.tool(
        annotations=_ann("find_idle_vehicles"),
        description="Vehicles idle for at least min_idle_minutes and rested spare drivers, optionally sorted by distance to a point.",
    )
    async def find_idle_vehicles(
        min_idle_minutes: int = 30,
        near_lat: float | None = None,
        near_lon: float | None = None,
        limit: int = 10,
    ) -> IdleVehiclesResult:
        return _guard(fleet.find_idle_vehicles)(min_idle_minutes, near_lat, near_lon, limit)

    @server.tool(
        annotations=_ann("list_hos_risks"),
        description="Drivers currently driving with drive_min_remaining at or below threshold_min, with their margin to destination.",
    )
    async def list_hos_risks(threshold_min: int = 120) -> HosRisksResult:
        return _guard(fleet.list_hos_risks)(threshold_min)

    @server.tool(
        annotations=_ann("get_fault_history"),
        description="Fault codes for a vehicle over the last N hours with recent engine metrics and 6h coolant/battery trends.",
    )
    async def get_fault_history(vehicle_id: str, hours: int = 72) -> FaultHistory:
        return _guard(fleet.get_fault_history)(vehicle_id, hours)

    @server.tool(
        annotations=_ann("search_fault_docs"),
        description="Semantic search over DTC fault-code and service guidance documents. Pass codes to boost matching docs. Cite results by their citation string.",
    )
    async def search_fault_docs(
        query: str, codes: list[str] | None = None, k: int = 3
    ) -> SearchDocsResult:
        return _guard(fleet.search_fault_docs)(query, codes, k)

    @server.tool(
        annotations=_ann("find_nearby_shops"),
        description="Repair shops nearest a vehicle (or lat/lon), optionally preferring a specialty such as cooling or electrical.",
    )
    async def find_nearby_shops(
        vehicle_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        specialty: str | None = None,
        limit: int = 3,
    ) -> NearbyShopsResult:
        return _guard(fleet.find_nearby_shops)(vehicle_id, lat, lon, specialty, limit)

    @server.tool(
        annotations=_ann("get_fuel_transactions"),
        description="Recent fuel transactions for a vehicle with tank-capacity checks, distance between card location and vehicle GPS, and time since previous fill.",
    )
    async def get_fuel_transactions(vehicle_id: str, hours: int = 72) -> FuelTxnsResult:
        return _guard(fleet.get_fuel_transactions)(vehicle_id, hours)

    @server.tool(
        annotations=_ann("get_recent_events"),
        description="Recent non-GPS telematics events for a vehicle or driver (hos_update, dtc_fault, engine_metrics, fuel_transaction, harsh_event, ...).",
    )
    async def get_recent_events(
        vehicle_id: str | None = None,
        driver_id: str | None = None,
        hours: int = 6,
        types: list[str] | None = None,
        limit: int = 40,
    ) -> RecentEventsResult:
        return _guard(fleet.get_recent_events)(vehicle_id, driver_id, hours, types, limit)

    # --- tier 1: auto ------------------------------------------------------------------------

    @server.tool(
        annotations=_ann("message_driver"),
        description="Send a short operational message to a driver (tier 1, auto). Rate limited; HR/legal language and links are blocked.",
    )
    async def message_driver(
        driver_id: str,
        body: str,
        urgency: str = "normal",
        incident_id: str | None = None,
        ctx: Context | None = None,
    ) -> ActionResult:
        return _guard(fleet.message_driver)(
            driver_id, body, urgency, incident_id, None, caller=_caller(ctx)
        )

    @server.tool(
        annotations=_ann("flag_fuel_transaction"),
        description="Flag a fuel transaction for finance review (tier 1, auto).",
    )
    async def flag_fuel_transaction(
        transaction_id: str, reason: str, incident_id: str | None = None, ctx: Context | None = None
    ) -> ActionResult:
        return _guard(fleet.flag_fuel_transaction)(
            transaction_id, reason, incident_id, None, caller=_caller(ctx)
        )

    # --- tier 2: approval ---------------------------------------------------------------------

    @server.tool(
        annotations=_ann("create_work_order"),
        description="Create a maintenance work order (tier 2). Without a valid approval_id the request is queued for manager approval.",
    )
    async def create_work_order(
        vehicle_id: str,
        description: str,
        priority: str = "normal",
        shop_id: str | None = None,
        fault_codes: list[str] | None = None,
        estimated_cost_usd: float | None = None,
        incident_id: str | None = None,
        approval_id: str | None = None,
        ctx: Context | None = None,
    ) -> ActionResult:
        return _guard(fleet.create_work_order)(
            vehicle_id,
            description,
            priority,
            shop_id,
            fault_codes,
            estimated_cost_usd,
            incident_id,
            approval_id,
            caller=_caller(ctx),
        )

    @server.tool(
        annotations=_ann("propose_reassignment"),
        description="Reassign a trip to another driver (tier 2). Without a valid approval_id the request is queued for manager approval.",
    )
    async def propose_reassignment(
        trip_id: str,
        from_driver_id: str,
        to_driver_id: str,
        reason: str,
        incident_id: str | None = None,
        approval_id: str | None = None,
        ctx: Context | None = None,
    ) -> ActionResult:
        return _guard(fleet.propose_reassignment)(
            trip_id,
            from_driver_id,
            to_driver_id,
            reason,
            incident_id,
            approval_id,
            caller=_caller(ctx),
        )

    # --- tier 3: recommend only ----------------------------------------------------------------

    @server.tool(
        annotations=_ann("suspend_fuel_card"),
        description="Recommend suspending a fuel card (tier 3). Always recorded as a recommendation; never executed by the system.",
    )
    async def suspend_fuel_card(
        card_id: str, reason: str, incident_id: str | None = None, ctx: Context | None = None
    ) -> ActionResult:
        return _guard(fleet.suspend_fuel_card)(
            card_id, reason, incident_id, None, caller=_caller(ctx)
        )

    @server.tool(
        annotations=_ann("set_vehicle_out_of_service"),
        description="Recommend taking a vehicle out of service (tier 3). Always recorded as a recommendation; never executed by the system.",
    )
    async def set_vehicle_out_of_service(
        vehicle_id: str, reason: str, incident_id: str | None = None, ctx: Context | None = None
    ) -> ActionResult:
        return _guard(fleet.set_vehicle_out_of_service)(
            vehicle_id, reason, incident_id, None, caller=_caller(ctx)
        )

    return server


_server: MCPServer | None = None


def get_server() -> MCPServer:
    global _server
    if _server is None:
        _server = build_server()
    return _server


def tool_tier_table() -> list[dict[str, Any]]:
    return [{"tool": t, "tier": int(tier), "label": tier.label} for t, tier in TOOL_TIERS.items()]

"""Autonomy tiers per tool. This table is the single source of truth; prompts never decide."""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    READ = 0  # read-only, always allowed
    AUTO = 1  # executes automatically (rate-limited, block-listed)
    APPROVAL = 2  # needs a one-click manager approval bound to the exact arguments
    RECOMMEND = 3  # recommendation only; the system never executes it

    @property
    def label(self) -> str:
        return {0: "read", 1: "auto", 2: "approval", 3: "recommend"}[int(self)]


TOOL_TIERS: dict[str, Tier] = {
    # tier 0
    "get_vehicle_status": Tier.READ,
    "get_driver_hos": Tier.READ,
    "find_idle_vehicles": Tier.READ,
    "list_hos_risks": Tier.READ,
    "get_fault_history": Tier.READ,
    "search_fault_docs": Tier.READ,
    "find_nearby_shops": Tier.READ,
    "get_fuel_transactions": Tier.READ,
    "get_recent_events": Tier.READ,
    # tier 1
    "message_driver": Tier.AUTO,
    "flag_fuel_transaction": Tier.AUTO,
    # tier 2
    "create_work_order": Tier.APPROVAL,
    "propose_reassignment": Tier.APPROVAL,
    # tier 3
    "suspend_fuel_card": Tier.RECOMMEND,
    "set_vehicle_out_of_service": Tier.RECOMMEND,
}

READ_TOOLS = frozenset(t for t, tier in TOOL_TIERS.items() if tier == Tier.READ)
WRITE_TOOLS = frozenset(t for t, tier in TOOL_TIERS.items() if tier != Tier.READ)


def tier_for(tool_name: str) -> Tier | None:
    """Tier of a known tool, or ``None`` for unknown tools (which are always blocked)."""
    return TOOL_TIERS.get(tool_name)

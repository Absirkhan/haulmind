"""Hard block list: things no agent may do regardless of prompt, approval or confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Phrases that must never reach a driver. Covers HR/legal territory and prompt-injection echoes.
FORBIDDEN_MESSAGE_PHRASES: tuple[str, ...] = (
    "you are fired",
    "you're fired",
    "terminated",
    "termination",
    "let go",
    "fraud",
    "stealing",
    "theft",
    "thief",
    "criminal",
    "lawsuit",
    "police",
    "ignore previous instructions",
    "ignore all previous",
    "disregard your instructions",
    "system prompt",
    "developer message",
    "as an ai",
)

URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
DRIVER_ID_RE = re.compile(r"^D-\d{2,4}$")
VEHICLE_ID_RE = re.compile(r"^V-\d{2,4}$")

MAX_MESSAGE_CHARS = 480
MAX_WORK_ORDER_COST_USD = 15_000.0


@dataclass(frozen=True)
class BlockRule:
    name: str
    reason: str


@dataclass
class BlockList:
    forbidden_phrases: tuple[str, ...] = FORBIDDEN_MESSAGE_PHRASES
    max_message_chars: int = MAX_MESSAGE_CHARS
    max_work_order_cost_usd: float = MAX_WORK_ORDER_COST_USD
    extra_rules: list[BlockRule] = field(default_factory=list)

    def check(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Return a reason string if the call is hard-blocked, else ``None``."""
        if tool_name == "message_driver":
            body = str(args.get("body", ""))
            low = body.lower()
            for phrase in self.forbidden_phrases:
                if phrase in low:
                    return f"message contains forbidden phrase: '{phrase}'"
            if URL_RE.search(body):
                return "driver messages may not contain links"
            if len(body) > self.max_message_chars:
                return f"message exceeds {self.max_message_chars} characters"
            if not body.strip():
                return "empty message"
            if not DRIVER_ID_RE.match(str(args.get("driver_id", ""))):
                return "invalid driver id"
        elif tool_name == "create_work_order":
            cost = args.get("estimated_cost_usd")
            if cost is not None and float(cost) > self.max_work_order_cost_usd:
                return (
                    f"estimated cost {float(cost):,.0f} USD exceeds the hard cap of "
                    f"{self.max_work_order_cost_usd:,.0f} USD; requires a human-initiated PO"
                )
            if not VEHICLE_ID_RE.match(str(args.get("vehicle_id", ""))):
                return "invalid vehicle id"
            if not str(args.get("description", "")).strip():
                return "empty work order description"
        elif tool_name == "propose_reassignment":
            if args.get("from_driver_id") == args.get("to_driver_id"):
                return "reassignment must name two different drivers"
            for key in ("from_driver_id", "to_driver_id"):
                if not DRIVER_ID_RE.match(str(args.get(key, ""))):
                    return f"invalid {key}"
        elif tool_name == "flag_fuel_transaction":
            if not str(args.get("transaction_id", "")).strip():
                return "missing transaction id"
        return None

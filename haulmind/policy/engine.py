"""PolicyEngine: the one gate every write tool passes through.

The engine is pure code: no prompt can change a tier, lift a rate limit, or skip an approval.
It runs inside the MCP server's tool handlers, so even an external MCP client (or a
prompt-injected agent) hits exactly the same checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from haulmind.config import Settings, get_settings
from haulmind.db.models import Approval, ToolCall
from haulmind.policy.blocklist import BlockList
from haulmind.policy.tiers import Tier, tier_for
from haulmind.util import sha256_hex, to_naive_utc

Outcome = Literal[
    "execute",
    "pending_approval",
    "recommend_only",
    "blocked",
    "rate_limited",
    "approval_invalid",
    "unknown_tool",
]


class Decision(BaseModel):
    allowed: bool
    tier: int
    outcome: Outcome
    reason: str = ""
    approval_id: str | None = None
    args_hash: str = ""

    @property
    def tier_label(self) -> str:
        return Tier(self.tier).label if self.tier in (0, 1, 2, 3) else "unknown"


class PolicyViolation(Exception):
    """Raised by executors that are asked to run something the policy did not allow."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"{decision.outcome}: {decision.reason}")
        self.decision = decision


class RateLimit(BaseModel):
    tool_name: str
    limit: int = Field(ge=0)
    window: timedelta = timedelta(hours=1)
    scope_arg: str | None = None  # e.g. "driver_id" => per-driver limit


def default_rate_limits(settings: Settings) -> list[RateLimit]:
    return [
        RateLimit(
            tool_name="message_driver",
            limit=settings.rl_message_driver_per_driver_per_hour,
            scope_arg="driver_id",
        ),
        RateLimit(tool_name="message_driver", limit=settings.rl_message_driver_fleet_per_hour),
        RateLimit(tool_name="flag_fuel_transaction", limit=settings.rl_flags_per_hour),
        RateLimit(tool_name="create_work_order", limit=settings.rl_work_orders_per_hour),
        RateLimit(tool_name="propose_reassignment", limit=settings.rl_reassignments_per_hour),
    ]


TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "message_driver": {"urgency": "normal"},
    "flag_fuel_transaction": {},
    "create_work_order": {"priority": "normal", "fault_codes": []},
    "propose_reassignment": {},
    "suspend_fuel_card": {},
    "set_vehicle_out_of_service": {},
}


def _normalize(value: Any) -> Any:
    """Make numerically equal values hash equally.

    Arguments reach the gateway through JSON and the tool's schema, which turns a planner's
    ``500`` into ``500.0`` for a ``float`` parameter. The approval was bound before that
    coercion, so whole-valued floats are folded back to ints on both sides. Bools are left
    alone (``True`` is not ``1`` here), and nested lists and dicts are normalised recursively.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def canonical_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as the tool will actually see them: defaults filled, control keys dropped,
    numbers normalised so transport coercion cannot break an approval's binding."""
    clean = {
        k: _normalize(v)
        for k, v in args.items()
        if k not in ("approval_id", "incident_id", "caller")
        and not k.startswith("_")
        and v is not None
    }
    for k, v in TOOL_DEFAULTS.get(tool_name, {}).items():
        clean.setdefault(k, v)
    return clean


def args_hash(tool_name: str, args: dict[str, Any]) -> str:
    """Binding hash: an approval is valid only for exactly these (canonical) arguments."""
    return sha256_hex(tool_name, canonical_args(tool_name, args))


class PolicyEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        blocklist: BlockList | None = None,
        rate_limits: list[RateLimit] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.blocklist = blocklist or BlockList()
        self.rate_limits = (
            rate_limits if rate_limits is not None else default_rate_limits(self.settings)
        )

    # --- main entry point ------------------------------------------------------------------

    def authorize(
        self,
        s: Session,
        tool_name: str,
        args: dict[str, Any],
        *,
        approval_id: str | None,
        caller: str,
        now: datetime,
    ) -> Decision:
        """Decide what happens to a write-tool call and record it in ``tool_calls``.

        For Tier 2 with a valid approval, the approval is marked consumed in the same session
        so it cannot be replayed.
        """
        now = to_naive_utc(now)
        h = args_hash(tool_name, args)
        tier = tier_for(tool_name)
        decision: Decision
        if tier is None:
            decision = Decision(
                allowed=False, tier=-1, outcome="unknown_tool", reason="unknown tool", args_hash=h
            )
        elif tier == Tier.READ:
            decision = Decision(allowed=True, tier=0, outcome="execute", args_hash=h)
        else:
            blocked = self.blocklist.check(tool_name, args)
            if blocked:
                decision = Decision(
                    allowed=False, tier=int(tier), outcome="blocked", reason=blocked, args_hash=h
                )
            elif tier == Tier.RECOMMEND:
                decision = Decision(
                    allowed=False,
                    tier=3,
                    outcome="recommend_only",
                    reason="high-risk action: recorded as a recommendation for a human",
                    args_hash=h,
                )
            elif tier == Tier.APPROVAL:
                decision = self._check_approval(s, tool_name, h, approval_id)
            else:  # Tier.AUTO
                limited = self._rate_limited(s, tool_name, args, now)
                decision = (
                    Decision(
                        allowed=False, tier=1, outcome="rate_limited", reason=limited, args_hash=h
                    )
                    if limited
                    else Decision(allowed=True, tier=1, outcome="execute", args_hash=h)
                )
            # Tier 2 executions are also rate limited (defence in depth).
            if decision.allowed and tier == Tier.APPROVAL:
                limited = self._rate_limited(s, tool_name, args, now)
                if limited:
                    decision = Decision(
                        allowed=False, tier=2, outcome="rate_limited", reason=limited, args_hash=h
                    )

        if tier != Tier.READ:
            s.add(
                ToolCall(
                    tool_name=tool_name,
                    scope_key=self._scope_key(tool_name, args),
                    ts=now,
                    allowed=decision.allowed,
                    outcome=decision.outcome,
                    reason=decision.reason[:200],
                    caller=caller[:40],
                )
            )
        return decision

    # --- helpers -----------------------------------------------------------------------------

    def _check_approval(
        self, s: Session, tool_name: str, h: str, approval_id: str | None
    ) -> Decision:
        if not approval_id:
            return Decision(
                allowed=False,
                tier=2,
                outcome="pending_approval",
                reason="requires manager approval",
                args_hash=h,
            )
        ap = s.get(Approval, approval_id)
        if ap is None:
            reason = "approval not found"
        elif ap.decision != "approve":
            reason = "approval was a rejection"
        elif ap.consumed:
            reason = "approval already used"
        elif ap.tool_name != tool_name:
            reason = "approval is for a different tool"
        elif ap.args_hash != h:
            reason = "approval does not match these arguments"
        else:
            ap.consumed = True
            return Decision(
                allowed=True, tier=2, outcome="execute", approval_id=approval_id, args_hash=h
            )
        return Decision(
            allowed=False,
            tier=2,
            outcome="approval_invalid",
            reason=reason,
            approval_id=approval_id,
            args_hash=h,
        )

    def _scope_key(self, tool_name: str, args: dict[str, Any]) -> str:
        for rl in self.rate_limits:
            if rl.tool_name == tool_name and rl.scope_arg:
                return f"{rl.scope_arg}={args.get(rl.scope_arg, '')}"
        return ""

    def _rate_limited(
        self, s: Session, tool_name: str, args: dict[str, Any], now: datetime
    ) -> str | None:
        for rl in self.rate_limits:
            if rl.tool_name != tool_name:
                continue
            since = now - rl.window
            q = select(func.count(ToolCall.id)).where(
                ToolCall.tool_name == tool_name,
                ToolCall.allowed.is_(True),
                ToolCall.outcome == "execute",
                ToolCall.ts > since,
                ToolCall.ts <= now,
            )
            if rl.scope_arg:
                q = q.where(ToolCall.scope_key == f"{rl.scope_arg}={args.get(rl.scope_arg, '')}")
            count = int(s.execute(q).scalar_one())
            if count >= rl.limit:
                scope = f" per {rl.scope_arg}" if rl.scope_arg else ""
                return (
                    f"rate limit: {rl.limit} {tool_name} calls{scope} per "
                    f"{int(rl.window.total_seconds() // 60)} min already used"
                )
        return None

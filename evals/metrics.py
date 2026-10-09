"""Scoring a replayed run against its ground truth."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from haulmind.db.engine import session_scope
from haulmind.db.models import Approval, Incident, ProposedAction, Reassignment, WorkOrder
from haulmind.db.repo import get_stats
from haulmind.simulator.ground_truth import GroundTruth, GroundTruthIncident
from haulmind.util import to_naive_utc

# Which incident types count as detecting a ground-truth scenario.
TYPE_MATCH: dict[str, set[str]] = {
    "hos_violation_risk": {"hos_violation_risk", "hos_break_risk"},
    "failing_component": {"failing_component", "recurring_fault"},
    "fuel_fraud": {"fuel_fraud"},
    "harsh_driving_pattern": {"harsh_driving_pattern"},
    "hos_near_miss": {"hos_violation_risk", "hos_break_risk"},
    "nuisance_dtc": {"failing_component", "recurring_fault"},
    "reefer_fuel_lookalike": {"fuel_fraud"},
    "noise_dtc_recurrence": {"failing_component", "recurring_fault"},
}
# Actions in these states were proposed to (or executed for) the manager: they are "alerts".
ALERT_STATUSES = {
    "pending",
    "approved",
    "executed",
    "auto_executed",
    "recommended",
    "acknowledged",
    "blocked",
    "rate_limited",
    "failed",
}
MATCH_SLACK = timedelta(minutes=45)


@dataclass
class Metrics:
    label: str
    run_id: str
    provider: str
    events_total: int = 0
    filtered_pct: float = 0.0
    clusters_to_agent: int = 0
    incidents: int = 0
    alerts: int = 0
    true_alerts: int = 0
    false_alerts: int = 0
    gt_positives: int = 0
    gt_benign: int = 0
    detected: int = 0
    recall: float = 0.0
    precision: float = 0.0
    false_alert_rate: float = 0.0
    action_correctness: float = 0.0
    policy_violations: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    llm_requests_total: int = 0
    llm_requests_per_incident: float = 0.0
    tokens_per_incident: float = 0.0
    tokens_total: int = 0
    tool_calls: int = 0
    wall_seconds: float = 0.0
    per_type: dict[str, dict[str, int]] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)
    false_alert_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _matches(inc: Incident, gt: GroundTruthIncident) -> bool:
    if inc.type not in TYPE_MATCH.get(gt.type, {gt.type}):
        return False
    same_entity = (gt.vehicle_id and inc.vehicle_id == gt.vehicle_id) or (
        gt.driver_id and inc.driver_id == gt.driver_id
    )
    if not same_entity:
        return False
    start = to_naive_utc(gt.start_ts) - MATCH_SLACK
    end = to_naive_utc(gt.end_ts) + MATCH_SLACK
    return start <= inc.sim_ts <= end


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def score(
    run_id: str,
    gt: GroundTruth,
    *,
    label: str,
    provider: str,
    wall_seconds: float = 0.0,
    tool_calls: int = 0,
    phase_filter: tuple[Any, Any] | None = None,
) -> Metrics:
    """Compute all metrics for a run. ``phase_filter`` restricts incidents/GT to a sim-time window."""
    m = Metrics(
        label=label,
        run_id=run_id,
        provider=provider,
        wall_seconds=wall_seconds,
        tool_calls=tool_calls,
    )
    with session_scope() as s:
        incs = list(s.execute(select(Incident).where(Incident.run_id == run_id)).scalars().all())
        acts = list(
            s.execute(select(ProposedAction).where(ProposedAction.run_id == run_id)).scalars().all()
        )
        stats = get_stats(s, run_id)
        wos = s.execute(select(func.count(WorkOrder.id))).scalar_one()
        ras = s.execute(select(func.count(Reassignment.id))).scalar_one()
        approvals_used = s.execute(
            select(func.count(Approval.id)).where(
                Approval.decision == "approve",
                Approval.consumed.is_(True),
                Approval.tool_name.in_(("create_work_order", "propose_reassignment")),
            )
        ).scalar_one()
    gts = list(gt.incidents)
    if phase_filter is not None:
        lo, hi = phase_filter
        incs = [i for i in incs if lo <= i.sim_ts < hi]
        gts = [g for g in gts if lo <= to_naive_utc(g.start_ts) < hi]
    acts_by_inc: dict[str, list[ProposedAction]] = {}
    for a in acts:
        if a.incident_id:
            acts_by_inc.setdefault(a.incident_id, []).append(a)

    m.events_total = int(stats.get("events_total", 0))
    m.filtered_pct = round(float(stats.get("filtered_pct", 0.0)), 2)
    m.clusters_to_agent = int(
        stats.get("clusters_definite", 0) + stats.get("clusters_ambiguous", 0)
    )
    m.incidents = len(incs)
    positives = [g for g in gts if not g.benign]
    m.gt_positives, m.gt_benign = len(positives), len(gts) - len(positives)

    alerts = [i for i in incs if any(a.status in ALERT_STATUSES for a in acts_by_inc.get(i.id, []))]
    m.alerts = len(alerts)
    matched_alert_ids: set[str] = set()
    correct = 0
    for g in positives:
        hits = [i for i in incs if _matches(i, g)]
        alert_hits = [i for i in hits if i.id in {a.id for a in alerts}]
        key = g.type
        m.per_type.setdefault(key, {"total": 0, "detected": 0, "correct": 0})
        m.per_type[key]["total"] += 1
        if alert_hits:
            m.detected += 1
            m.per_type[key]["detected"] += 1
            matched_alert_ids.update(i.id for i in alert_hits)
            proposed = {
                a.tool_name
                for i in alert_hits
                for a in acts_by_inc.get(i.id, [])
                if a.status in ALERT_STATUSES
            }
            if set(g.expected_actions) <= proposed:
                correct += 1
                m.per_type[key]["correct"] += 1
        else:
            m.missed.append(f"{g.id} {g.type} {g.vehicle_id}")
    m.true_alerts = len([i for i in alerts if i.id in matched_alert_ids])
    m.false_alerts = m.alerts - m.true_alerts
    m.false_alert_ids = [
        f"{i.id} {i.type} {i.vehicle_id}" for i in alerts if i.id not in matched_alert_ids
    ]
    m.recall = round(m.detected / m.gt_positives, 3) if m.gt_positives else 0.0
    m.precision = round(m.true_alerts / m.alerts, 3) if m.alerts else 1.0
    m.false_alert_rate = round(m.false_alerts / m.alerts, 3) if m.alerts else 0.0
    m.action_correctness = round(correct / m.detected, 3) if m.detected else 0.0

    tier3_executed = sum(
        1 for a in acts if a.tier == 3 and a.status in ("executed", "auto_executed")
    )
    tier2_unapproved = sum(
        1
        for a in acts
        if a.tier == 2
        and a.status in ("executed", "auto_executed")
        and not _has_consumed_approval(a.id)
    )
    side_effects_without_approval = max(0, int(wos) + int(ras) - int(approvals_used))
    m.policy_violations = tier3_executed + tier2_unapproved + side_effects_without_approval

    lat = [float(i.latency_ms) for i in incs]
    m.latency_p50_ms = round(_percentile(lat, 0.5), 1)
    m.latency_p95_ms = round(_percentile(lat, 0.95), 1)
    m.llm_requests_total = sum(i.llm_requests for i in incs)
    m.llm_requests_per_incident = (
        round(statistics.mean([i.llm_requests for i in incs]), 2) if incs else 0.0
    )
    m.tokens_total = sum(i.llm_input_tokens + i.llm_output_tokens for i in incs)
    m.tokens_per_incident = round(m.tokens_total / len(incs), 1) if incs else 0.0
    return m


def _has_consumed_approval(action_id: str) -> bool:
    with session_scope() as s:
        return (
            s.execute(
                select(Approval.id).where(
                    Approval.action_id == action_id,
                    Approval.decision == "approve",
                    Approval.consumed.is_(True),
                )
            ).first()
            is not None
        )

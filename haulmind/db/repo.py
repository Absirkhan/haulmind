"""Small repository helpers shared by the web app, orchestrator, policy engine and tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from haulmind.db.models import AgentMemory, AuditLog, Run, RunCheckpoint, RunStat
from haulmind.util import to_naive_utc, utcnow

# --- runs ------------------------------------------------------------------------------------


def get_active_run(s: Session) -> Run | None:
    return (
        s.execute(select(Run).where(Run.status == "active").order_by(Run.started_at.desc()))
        .scalars()
        .first()
    )


def get_latest_run(s: Session) -> Run | None:
    return s.execute(select(Run).order_by(Run.started_at.desc())).scalars().first()


def sim_now(s: Session, run_id: str | None = None) -> datetime:
    """Current simulated time: the latest persisted event timestamp of the (active) run."""
    run = s.get(Run, run_id) if run_id else get_active_run(s) or get_latest_run(s)
    if run is not None and run.sim_now is not None:
        return run.sim_now
    return to_naive_utc(utcnow())


def set_checkpoint(s: Session, run_id: str, last_seq: int) -> None:
    cp = s.get(RunCheckpoint, run_id)
    if cp is None:
        s.add(RunCheckpoint(run_id=run_id, last_seq=last_seq, updated_at=to_naive_utc(utcnow())))
    else:
        cp.last_seq = last_seq
        cp.updated_at = to_naive_utc(utcnow())


def get_checkpoint(s: Session, run_id: str) -> int:
    cp = s.get(RunCheckpoint, run_id)
    return cp.last_seq if cp else -1


def bump_stat(s: Session, run_id: str, key: str, delta: float = 1.0) -> None:
    """Increment a per-run counter (events filtered, llm requests, ...)."""
    row = (
        s.execute(select(RunStat).where(RunStat.run_id == run_id, RunStat.key == key))
        .scalars()
        .first()
    )
    if row is None:
        s.add(RunStat(run_id=run_id, key=key, value=delta))
    else:
        s.execute(update(RunStat).where(RunStat.id == row.id).values(value=RunStat.value + delta))


def get_stats(s: Session, run_id: str) -> dict[str, float]:
    rows = s.execute(select(RunStat).where(RunStat.run_id == run_id)).scalars().all()
    return {r.key: r.value for r in rows}


# --- audit -----------------------------------------------------------------------------------


def audit(
    s: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    s.add(
        AuditLog(
            ts=to_naive_utc(utcnow()),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details_json=details or {},
            run_id=run_id,
        )
    )


# --- memory ----------------------------------------------------------------------------------


def memory_get(s: Session, scope: str, scope_id: str, key: str) -> dict[str, Any] | None:
    row = (
        s.execute(
            select(AgentMemory).where(
                AgentMemory.scope == scope, AgentMemory.scope_id == scope_id, AgentMemory.key == key
            )
        )
        .scalars()
        .first()
    )
    return dict(row.value_json) if row else None


def memory_set(s: Session, scope: str, scope_id: str, key: str, value: dict[str, Any]) -> None:
    row = (
        s.execute(
            select(AgentMemory).where(
                AgentMemory.scope == scope, AgentMemory.scope_id == scope_id, AgentMemory.key == key
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        s.add(
            AgentMemory(
                scope=scope,
                scope_id=scope_id,
                key=key,
                value_json=value,
                updated_ts=to_naive_utc(utcnow()),
            )
        )
    else:
        row.value_json = value
        row.updated_ts = to_naive_utc(utcnow())


def memory_all(s: Session, scope: str, scope_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        s.execute(
            select(AgentMemory).where(AgentMemory.scope == scope, AgentMemory.scope_id == scope_id)
        )
        .scalars()
        .all()
    )
    return {r.key: dict(r.value_json) for r in rows}

"""A small tracer with contextvar-propagated parents. Spans are buffered per trace and flushed
to SQLite when the trace ends (one transaction per incident instead of one per span)."""

from __future__ import annotations

import contextvars
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from haulmind.db.engine import session_scope
from haulmind.db.models import Span, Trace
from haulmind.util import new_id, to_naive_utc, utcnow

_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "haulmind_trace", default=None
)
_current_span: contextvars.ContextVar[SpanHandle | None] = contextvars.ContextVar(
    "haulmind_span", default=None
)


@dataclass
class SpanHandle:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    kind: str
    started: datetime
    seq: int
    _t0: float = field(default_factory=time.perf_counter)
    ended: datetime | None = None
    latency_ms: int = 0
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    status: str = "ok"
    error: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)

    def record_llm(
        self, *, model: str, input_tokens: int, output_tokens: int, cached: bool
    ) -> None:
        self.model = model
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached = cached

    def fail(self, err: BaseException | str) -> None:
        self.status = "error"
        self.error = str(err)[:1000]


@dataclass
class _TraceBuffer:
    id: str
    name: str
    run_id: str | None
    incident_id: str | None
    started: datetime
    spans: list[SpanHandle] = field(default_factory=list)
    seq: int = 0
    attrs: dict[str, Any] = field(default_factory=dict)


class Tracer:
    def __init__(self) -> None:
        self._traces: dict[str, _TraceBuffer] = {}

    # --- trace lifecycle -----------------------------------------------------------------------

    def start_trace(
        self, name: str, *, run_id: str | None = None, incident_id: str | None = None
    ) -> str:
        tid = new_id("TR")
        self._traces[tid] = _TraceBuffer(
            id=tid, name=name, run_id=run_id, incident_id=incident_id, started=utcnow()
        )
        _current_trace.set(tid)
        _current_span.set(None)
        return tid

    def set_incident(self, trace_id: str, incident_id: str) -> None:
        buf = self._traces.get(trace_id)
        if buf is not None:
            buf.incident_id = incident_id

    def end_trace(self, trace_id: str, *, status: str = "ok", **attrs: Any) -> dict[str, int]:
        """Flush the trace to SQLite. Returns aggregate LLM usage for the caller to record."""
        buf = self._traces.pop(trace_id, None)
        if _current_trace.get() == trace_id:
            _current_trace.set(None)
            _current_span.set(None)
        if buf is None:
            return {"llm_requests": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
        ended = utcnow()
        llm_spans = [sp for sp in buf.spans if sp.kind == "llm_call" and sp.status == "ok"]
        totals = {
            "llm_requests": sum(1 for sp in llm_spans if not sp.cached),
            "input_tokens": sum(sp.input_tokens for sp in llm_spans),
            "output_tokens": sum(sp.output_tokens for sp in llm_spans),
            "latency_ms": int((ended - buf.started).total_seconds() * 1000),
        }
        buf.attrs.update(attrs)
        with session_scope() as s:
            s.add(
                Trace(
                    id=buf.id,
                    run_id=buf.run_id,
                    incident_id=buf.incident_id,
                    name=buf.name,
                    started_ts=to_naive_utc(buf.started),
                    ended_ts=to_naive_utc(ended),
                    status=status,
                    llm_requests=totals["llm_requests"],
                    input_tokens=totals["input_tokens"],
                    output_tokens=totals["output_tokens"],
                    attrs_json=_jsonable(buf.attrs),
                )
            )
            s.flush()  # spans reference the trace row
            for sp in buf.spans:
                s.add(
                    Span(
                        id=sp.id,
                        trace_id=sp.trace_id,
                        parent_id=sp.parent_id,
                        name=sp.name,
                        kind=sp.kind,
                        started_ts=to_naive_utc(sp.started),
                        ended_ts=to_naive_utc(sp.ended) if sp.ended else None,
                        latency_ms=sp.latency_ms,
                        model=sp.model,
                        input_tokens=sp.input_tokens,
                        output_tokens=sp.output_tokens,
                        cached=sp.cached,
                        status=sp.status,
                        error=sp.error,
                        attrs_json=_jsonable(sp.attrs),
                        seq=sp.seq,
                    )
                )
        return totals

    # --- spans ---------------------------------------------------------------------------------

    def _open(self, name: str, kind: str, attrs: dict[str, Any]) -> SpanHandle | None:
        tid = _current_trace.get()
        if tid is None or tid not in self._traces:
            return None
        buf = self._traces[tid]
        buf.seq += 1
        parent = _current_span.get()
        sp = SpanHandle(
            id=new_id("SP"),
            trace_id=tid,
            parent_id=parent.id if parent else None,
            name=name,
            kind=kind,
            started=utcnow(),
            seq=buf.seq,
            attrs=dict(attrs),
        )
        buf.spans.append(sp)
        return sp

    @staticmethod
    def _close(sp: SpanHandle) -> None:
        sp.ended = utcnow()
        sp.latency_ms = int((time.perf_counter() - sp._t0) * 1000)

    @asynccontextmanager
    async def span(
        self, name: str, kind: str = "agent_step", **attrs: Any
    ) -> AsyncIterator[SpanHandle]:
        sp = self._open(name, kind, attrs)
        if sp is None:
            yield SpanHandle(
                id="noop",
                trace_id="",
                parent_id=None,
                name=name,
                kind=kind,
                started=utcnow(),
                seq=0,
            )
            return
        token = _current_span.set(sp)
        try:
            yield sp
        except BaseException as e:
            sp.fail(e)
            raise
        finally:
            self._close(sp)
            _current_span.reset(token)

    @contextmanager
    def span_sync(self, name: str, kind: str = "agent_step", **attrs: Any) -> Iterator[SpanHandle]:
        sp = self._open(name, kind, attrs)
        if sp is None:
            yield SpanHandle(
                id="noop",
                trace_id="",
                parent_id=None,
                name=name,
                kind=kind,
                started=utcnow(),
                seq=0,
            )
            return
        token = _current_span.set(sp)
        try:
            yield sp
        except BaseException as e:
            sp.fail(e)
            raise
        finally:
            self._close(sp)
            _current_span.reset(token)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str | int | float | bool) or v is None:
            out[k] = v
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict | list):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def current_trace_id() -> str | None:
    return _current_trace.get()


def current_span() -> SpanHandle | None:
    return _current_span.get()


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = Tracer()
    return _tracer

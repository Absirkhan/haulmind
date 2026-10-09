"""FastAPI application: HTMX pages and partials."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from haulmind.agents.memory import REASON_CODES
from haulmind.config import get_settings
from haulmind.db.engine import session_scope
from haulmind.db.models import (
    AuditLog,
    Driver,
    EventRow,
    Incident,
    ProposedAction,
    Run,
    Span,
    Trace,
    Vehicle,
)
from haulmind.db.repo import get_stats
from haulmind.logging_setup import configure_logging, get_logger
from haulmind.policy.tiers import TOOL_TIERS, Tier
from haulmind.web import actions as decisions
from haulmind.web.runtime import AppRuntime, get_runtime, latest_run_id, set_runtime

log = get_logger(__name__)
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

TIER_LABEL = {1: "ran automatically", 2: "needs your approval", 3: "recommendation only", 0: "read"}
STATUS_LABEL = {
    "pending": "waiting for approval",
    "approved": "approved, executing",
    "executed": "executed",
    "auto_executed": "ran automatically",
    "recommended": "recommended",
    "acknowledged": "handled by a human",
    "rejected": "rejected",
    "blocked": "blocked by policy",
    "rate_limited": "rate limited",
    "approval_invalid": "approval rejected",
    "failed": "failed",
}
TOOL_LABEL = {
    "message_driver": "Message driver",
    "flag_fuel_transaction": "Flag fuel transaction",
    "create_work_order": "Create work order",
    "propose_reassignment": "Reassign trip",
    "suspend_fuel_card": "Suspend fuel card",
    "set_vehicle_out_of_service": "Take vehicle out of service",
}


def _fmt_ts(value: datetime | str | None, fmt: str = "%a %H:%M") -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return parsed.strftime(fmt)
    return value.strftime(fmt)


def _fmt_args(args: dict[str, Any]) -> str:
    lines = []
    for k, v in args.items():
        if v in (None, "", []):
            continue
        val = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        lines.append(f"{k.replace('_', ' ')}: {val}")
    return "\n".join(lines)


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["tier_label"] = lambda t: TIER_LABEL.get(int(t), str(t))
templates.env.filters["status_label"] = lambda st: STATUS_LABEL.get(st, st.replace("_", " "))
templates.env.filters["tool_label"] = lambda t: TOOL_LABEL.get(t, t.replace("_", " "))
templates.env.filters["fmt_args"] = _fmt_args
templates.env.filters["pretty"] = lambda o: json.dumps(o, indent=2, default=str, ensure_ascii=False)
templates.env.filters["pct"] = lambda x: f"{float(x):.0%}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    rt = AppRuntime(settings)
    set_runtime(rt)
    if os.environ.get("HAULMIND_NO_RUNTIME") != "1":
        await rt.start(wait_for_mcp=os.environ.get("HAULMIND_WAIT_FOR_MCP") == "1")
        log.info(
            "web ready",
            url=f"http://{settings.web_host}:{settings.web_port}/inbox",
            mcp=settings.mcp_transport,
        )
    try:
        yield
    finally:
        await rt.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Haulmind", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    # --- helpers -----------------------------------------------------------------------------

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        run_id = latest_run_id()
        with session_scope() as s:
            pending = s.execute(
                select(func.count(ProposedAction.id)).where(
                    ProposedAction.status == "pending", ProposedAction.run_id == run_id
                )
            ).scalar_one()
        base = {
            "request": request,
            "pending_count": pending,
            "run_id": run_id,
            "provider": get_runtime().status().get("llm_provider")
            if get_runtime().orch
            else get_settings().effective_llm_provider,
        }
        return templates.TemplateResponse(request, name, {**base, **ctx})

    def hx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def load_action(s: Any, action_id: str) -> dict[str, Any] | None:
        act = s.get(ProposedAction, action_id)
        if act is None:
            return None
        inc = s.get(Incident, act.incident_id) if act.incident_id else None
        return {"a": act, "inc": inc, "tier": TOOL_TIERS.get(act.tool_name, Tier.READ)}

    def queue_rows(s: Any, tab: str, run_id: str | None) -> list[ProposedAction]:
        status = {
            "pending": ("pending", "approved"),
            "auto": ("auto_executed",),
            "recommended": ("recommended",),
            "decided": (
                "executed",
                "rejected",
                "acknowledged",
                "blocked",
                "failed",
                "rate_limited",
                "approval_invalid",
            ),
        }[tab]
        q = select(ProposedAction).where(ProposedAction.status.in_(status))
        if run_id:
            q = q.where(ProposedAction.run_id == run_id)
        return list(
            s.execute(q.order_by(ProposedAction.created_ts.desc()).limit(80)).scalars().all()
        )

    # --- pages ---------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/inbox", status_code=307)

    @app.get("/health")
    async def health() -> JSONResponse:
        rt = get_runtime()
        return JSONResponse(
            {
                "ok": True,
                **{
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in rt.status().items()
                },
            }
            if rt.orch
            else {"ok": True, "runtime": "not started"}
        )

    @app.get("/stream", response_class=HTMLResponse)
    async def stream(request: Request) -> HTMLResponse:
        rt = get_runtime()
        return templates.TemplateResponse(
            request, "_stream.html", {"st": rt.status() if rt.orch else None, "request": request}
        )

    @app.post("/sim/pause", response_class=HTMLResponse)
    async def sim_pause(request: Request) -> HTMLResponse:
        get_runtime().pause.clear()
        return await stream(request)

    @app.post("/sim/resume", response_class=HTMLResponse)
    async def sim_resume(request: Request) -> HTMLResponse:
        get_runtime().pause.set()
        return await stream(request)

    @app.post("/sim/speed", response_class=HTMLResponse)
    async def sim_speed(request: Request, speed: float = Form(...)) -> HTMLResponse:
        rt = get_runtime()
        rt.speed = max(0.0, speed)
        await rt.restart_producer()
        return await stream(request)

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox(
        request: Request, tab: str = "pending", selected: str | None = None
    ) -> HTMLResponse:
        tab = tab if tab in ("pending", "auto", "recommended", "decided") else "pending"
        run_id = latest_run_id()
        with session_scope() as s:
            rows = queue_rows(s, tab, run_id)
            counts = {
                t: len(queue_rows(s, t, run_id))
                for t in ("pending", "auto", "recommended", "decided")
            }
            sel = (
                load_action(s, selected)
                if selected
                else (load_action(s, rows[0].id) if rows else None)
            )
            ctx = {
                "tab": tab,
                "rows": rows,
                "counts": counts,
                "selected": sel,
                "reasons": REASON_CODES,
                "notice": None,
            }
            if hx(request) and request.headers.get("HX-Target") == "queue":
                return templates.TemplateResponse(
                    request, "_queue.html", {"request": request, **ctx}
                )
            return render(request, "inbox.html", page="inbox", **ctx)

    @app.get("/inbox/queue", response_class=HTMLResponse)
    async def inbox_queue(
        request: Request, tab: str = "pending", selected: str | None = None
    ) -> HTMLResponse:
        run_id = latest_run_id()
        with session_scope() as s:
            rows = queue_rows(
                s,
                tab if tab in ("pending", "auto", "recommended", "decided") else "pending",
                run_id,
            )
            return templates.TemplateResponse(
                request,
                "_queue.html",
                {
                    "request": request,
                    "rows": rows,
                    "tab": tab,
                    "selected": {"a": s.get(ProposedAction, selected)} if selected else None,
                },
            )

    @app.get("/actions/{action_id}", response_class=HTMLResponse)
    async def action_detail(
        request: Request, action_id: str, notice: str | None = None
    ) -> HTMLResponse:
        with session_scope() as s:
            sel = load_action(s, action_id)
            if sel is None:
                return HTMLResponse("<div class='empty'>Action not found.</div>", status_code=404)
            ctx = {
                "request": request,
                "selected": sel,
                "reasons": REASON_CODES,
                "notice": notice,
                "notice_kind": "ok",
            }
            if hx(request):
                return templates.TemplateResponse(request, "_action_detail.html", ctx)
            return RedirectResponse(f"/inbox?selected={action_id}", status_code=307)  # type: ignore[return-value]

    async def _decide(
        request: Request, action_id: str, decision: str, reason_code: str, reason_text: str
    ) -> HTMLResponse:
        rt = get_runtime()
        assert rt.tools is not None, "runtime not started"
        out = await decisions.decide(
            action_id, decision, tools=rt.tools, reason_code=reason_code, reason_text=reason_text
        )
        with session_scope() as s:
            sel = load_action(s, action_id)
            resp = templates.TemplateResponse(
                request,
                "_action_detail.html",
                {
                    "request": request,
                    "selected": sel,
                    "reasons": REASON_CODES,
                    "notice": out.message,
                    "notice_kind": "ok" if out.ok else "bad",
                    "memory_changes": out.memory_changes,
                },
            )
        resp.headers["HX-Trigger"] = "queue-changed"
        return resp

    @app.post("/actions/{action_id}/approve", response_class=HTMLResponse)
    async def approve(
        request: Request, action_id: str, reason_text: str = Form("")
    ) -> HTMLResponse:
        return await _decide(request, action_id, "approve", "", reason_text)

    @app.post("/actions/{action_id}/reject", response_class=HTMLResponse)
    async def reject(
        request: Request,
        action_id: str,
        reason_code: str = Form("not_an_issue"),
        reason_text: str = Form(""),
    ) -> HTMLResponse:
        return await _decide(request, action_id, "reject", reason_code, reason_text)

    @app.post("/actions/{action_id}/acknowledge", response_class=HTMLResponse)
    async def acknowledge(
        request: Request, action_id: str, reason_text: str = Form("")
    ) -> HTMLResponse:
        return await _decide(request, action_id, "acknowledge", "", reason_text)

    @app.get("/actions/{action_id}/edit", response_class=HTMLResponse)
    async def edit_form(request: Request, action_id: str) -> HTMLResponse:
        with session_scope() as s:
            sel = load_action(s, action_id)
            return templates.TemplateResponse(
                request, "_action_edit.html", {"request": request, "selected": sel}
            )

    @app.post("/actions/{action_id}/edit", response_class=HTMLResponse)
    async def edit_submit(request: Request, action_id: str) -> HTMLResponse:
        form = await request.form()
        with session_scope() as s:
            act = s.get(ProposedAction, action_id)
            current = dict(act.args_json) if act else {}
        new_args: dict[str, Any] = {}
        for k, v in current.items():
            raw = form.get(f"arg_{k}")
            if raw is None:
                new_args[k] = v
                continue
            raw_s = str(raw).strip()
            if isinstance(v, list):
                new_args[k] = [x.strip() for x in raw_s.split(",") if x.strip()]
            elif isinstance(v, bool):
                new_args[k] = raw_s.lower() in ("1", "true", "yes", "on")
            elif isinstance(v, int | float) and raw_s:
                try:
                    new_args[k] = type(v)(raw_s)
                except ValueError:
                    new_args[k] = v
            else:
                new_args[k] = raw_s or None
        ok = decisions.edit_action(action_id, new_args)
        with session_scope() as s:
            sel = load_action(s, action_id)
            resp = templates.TemplateResponse(
                request,
                "_action_detail.html",
                {
                    "request": request,
                    "selected": sel,
                    "reasons": REASON_CODES,
                    "notice": "Changes saved. Approve to run with the edited arguments."
                    if ok
                    else "Could not edit this action.",
                    "notice_kind": "ok" if ok else "bad",
                },
            )
        resp.headers["HX-Trigger"] = "queue-changed"
        return resp

    @app.get("/incidents", response_class=HTMLResponse)
    async def incidents(request: Request) -> HTMLResponse:
        run_id = latest_run_id()
        with session_scope() as s:
            q = select(Incident).order_by(Incident.sim_ts.desc()).limit(200)
            if run_id:
                q = q.where(Incident.run_id == run_id)
            incs = s.execute(q).scalars().all()
            counts = dict(
                s.execute(
                    select(ProposedAction.incident_id, func.count(ProposedAction.id)).group_by(
                        ProposedAction.incident_id
                    )
                ).all()
            )
            stats = get_stats(s, run_id) if run_id else {}
            return render(
                request, "incidents.html", page="incidents", incs=incs, counts=counts, stats=stats
            )

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_detail(request: Request, incident_id: str) -> HTMLResponse:
        with session_scope() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                return HTMLResponse("Incident not found", status_code=404)
            acts = (
                s.execute(
                    select(ProposedAction)
                    .where(ProposedAction.incident_id == inc.id)
                    .order_by(ProposedAction.created_ts)
                )
                .scalars()
                .all()
            )
            events = (
                s.execute(
                    select(EventRow)
                    .where(EventRow.run_id == inc.run_id, EventRow.incident_id == inc.id)
                    .order_by(EventRow.seq)
                )
                .scalars()
                .all()
            )
            trace = s.get(Trace, inc.trace_id) if inc.trace_id else None
            spans = (
                s.execute(select(Span).where(Span.trace_id == inc.trace_id).order_by(Span.seq))
                .scalars()
                .all()
                if inc.trace_id
                else []
            )
            logs = (
                s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id.in_([inc.id, *[a.id for a in acts]]))
                    .order_by(AuditLog.ts)
                )
                .scalars()
                .all()
            )
            vehicle = s.get(Vehicle, inc.vehicle_id) if inc.vehicle_id else None
            driver = s.get(Driver, inc.driver_id) if inc.driver_id else None
            return render(
                request,
                "incident_detail.html",
                page="incidents",
                inc=inc,
                acts=acts,
                events=events,
                trace=trace,
                spans=spans,
                logs=logs,
                vehicle=vehicle,
                driver=driver,
            )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request) -> HTMLResponse:
        with session_scope() as s:
            rows = (
                s.execute(select(AuditLog).order_by(AuditLog.ts.desc()).limit(300)).scalars().all()
            )
            return render(request, "audit.html", page="audit", rows=rows)

    @app.get("/traces", response_class=HTMLResponse)
    async def traces(request: Request) -> HTMLResponse:
        run_id = latest_run_id()
        with session_scope() as s:
            q = select(Trace).order_by(Trace.started_ts.desc()).limit(200)
            if run_id:
                q = q.where(Trace.run_id == run_id)
            rows = s.execute(q).scalars().all()
            incs = {
                i.id: i
                for i in s.execute(
                    select(Incident).where(
                        Incident.id.in_([r.incident_id for r in rows if r.incident_id])
                    )
                ).scalars()
            }
            return render(request, "traces.html", page="traces", rows=rows, incs=incs)

    @app.get("/traces/{trace_id}", response_class=HTMLResponse)
    async def trace_detail(request: Request, trace_id: str) -> HTMLResponse:
        with session_scope() as s:
            tr = s.get(Trace, trace_id)
            if tr is None:
                return HTMLResponse("Trace not found", status_code=404)
            spans = (
                s.execute(select(Span).where(Span.trace_id == trace_id).order_by(Span.seq))
                .scalars()
                .all()
            )
            inc = s.get(Incident, tr.incident_id) if tr.incident_id else None
            total_ms = max(
                1, int(((tr.ended_ts or tr.started_ts) - tr.started_ts).total_seconds() * 1000)
            )
            depth: dict[str, int] = {}
            rows = []
            for sp in spans:
                depth[sp.id] = (depth.get(sp.parent_id, -1) + 1) if sp.parent_id else 0
                offset = max(0, int((sp.started_ts - tr.started_ts).total_seconds() * 1000))
                rows.append(
                    {
                        "sp": sp,
                        "depth": depth[sp.id],
                        "left": min(99.0, 100.0 * offset / total_ms),
                        "width": max(0.6, min(100.0, 100.0 * sp.latency_ms / total_ms)),
                    }
                )
            return render(
                request,
                "trace_detail.html",
                page="traces",
                tr=tr,
                rows=rows,
                inc=inc,
                total_ms=total_ms,
            )

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet(request: Request) -> HTMLResponse:
        with session_scope() as s:
            vehicles = s.execute(select(Vehicle).order_by(Vehicle.id)).scalars().all()
            drivers = {d.id: d for d in s.execute(select(Driver)).scalars()}
            from haulmind.db.models import FaultCode, HosStatus

            hos = {h.driver_id: h for h in s.execute(select(HosStatus)).scalars()}
            faults: dict[str, list[str]] = {}
            for f in s.execute(select(FaultCode).where(FaultCode.active.is_(True))).scalars():
                faults.setdefault(f.vehicle_id, []).append(f.code)
            run = s.get(Run, latest_run_id() or "")
            return render(
                request,
                "fleet.html",
                page="fleet",
                vehicles=vehicles,
                drivers=drivers,
                hos=hos,
                faults=faults,
                run=run,
            )

    @app.get("/runs/{run_id}/stats")
    async def run_stats(run_id: str) -> JSONResponse:
        with session_scope() as s:
            return JSONResponse(get_stats(s, run_id))

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> Response:
        log.exception("request failed", path=request.url.path)
        return HTMLResponse(
            f"<div class='notice bad'>Something went wrong: {type(exc).__name__}: {str(exc)[:200]}</div>",
            status_code=500,
        )

    return app

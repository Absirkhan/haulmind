"""FastAPI application: HTMX pages and partials.

Every string a person sees comes from :mod:`haulmind.web.copy` (vocabulary) through
:mod:`haulmind.web.views` (view models). Routes, decision handlers and the API contract are
unchanged from the first version; only what is rendered differs.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from haulmind.config import get_settings
from haulmind.db.engine import session_scope
from haulmind.db.models import (
    AuditLog,
    EventRow,
    Incident,
    ProposedAction,
    Run,
    Span,
    Trace,
)
from haulmind.db.repo import get_stats
from haulmind.logging_setup import configure_logging, get_logger
from haulmind.util import to_naive_utc
from haulmind.web import actions as decisions
from haulmind.web import copy as C  # noqa: N812
from haulmind.web import views as V  # noqa: N812
from haulmind.web.runtime import AppRuntime, get_runtime, latest_run_id, set_runtime

log = get_logger(__name__)
BASE = Path(__file__).parent
RESULTS = BASE.parent.parent / "evals" / "results.json"
templates = Jinja2Templates(directory=str(BASE / "templates"))

TABS = [(k, C.TAB_LABEL[k]) for k in ("pending", "recommended", "auto", "decided")]
TAB_STATUSES = {
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
}


def _clock(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "--:--"


def _dayname(value: datetime | None) -> str:
    return value.strftime("%A") if value else ""


def _daypct(value: datetime | None) -> float:
    if not value:
        return 0.0
    return round(100.0 * (value.hour * 60 + value.minute) / 1440.0, 2)


templates.env.filters["pretty"] = V.pretty
templates.env.filters["clock"] = _clock
templates.env.filters["dayname"] = _dayname
templates.env.filters["daypct"] = _daypct
templates.env.filters["fulltime"] = C.full_time
templates.env.filters["say"] = C.humanize


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    ui_only = os.environ.get("HAULMIND_NO_RUNTIME") == "1"
    rt = get_runtime() if ui_only else AppRuntime(settings)  # tests inject a runtime
    set_runtime(rt)
    if not ui_only:
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


def _wall_now() -> datetime:
    return to_naive_utc(datetime.now(UTC))


def create_app() -> FastAPI:
    app = FastAPI(title="Haulmind", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    # --- helpers -----------------------------------------------------------------------------

    def runtime_status() -> dict[str, Any] | None:
        rt = get_runtime()
        return rt.status() if rt.orch else None

    def sim_now(s: Any, run_id: str | None, st: dict[str, Any] | None) -> datetime | None:
        if st and st.get("sim_now"):
            return st["sim_now"]  # type: ignore[no-any-return]
        run = s.get(Run, run_id) if run_id else None
        return to_naive_utc(run.sim_now) if run and run.sim_now else None

    def base_ctx(request: Request, s: Any, page: str) -> dict[str, Any]:
        run_id = latest_run_id()
        st = runtime_status()
        names = C.Names.load(s)
        pending = s.execute(
            select(func.count(ProposedAction.id)).where(
                ProposedAction.status == "pending", ProposedAction.run_id == run_id
            )
        ).scalar_one()
        return {
            "request": request,
            "page": page,
            "pending_count": pending,
            "run_id": run_id,
            "names": names,
            "now": sim_now(s, run_id, st),
            "wall_now": _wall_now(),
            "paused": bool(st and st.get("paused")),
            "palette_json": json.dumps(V.palette_commands(names)),
        }

    def render(request: Request, name: str, page: str, **ctx: Any) -> HTMLResponse:
        with session_scope() as s:
            base = base_ctx(request, s, page)
            if callable(ctx.get("build")):
                ctx = ctx["build"](s, base)
        return templates.TemplateResponse(request, name, {**base, **ctx})

    def hx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def queue_rows(s: Any, tab: str, run_id: str | None) -> list[ProposedAction]:
        q = select(ProposedAction).where(ProposedAction.status.in_(TAB_STATUSES[tab]))
        if run_id:
            q = q.where(ProposedAction.run_id == run_id)
        return list(
            s.execute(q.order_by(ProposedAction.created_ts.desc()).limit(120)).scalars().all()
        )

    def incidents_for(s: Any, acts: list[ProposedAction]) -> dict[str, Incident]:
        ids = {a.incident_id for a in acts if a.incident_id}
        if not ids:
            return {}
        return {i.id: i for i in s.execute(select(Incident).where(Incident.id.in_(ids))).scalars()}

    def clean_tab(tab: str) -> str:
        return tab if tab in TAB_STATUSES else "pending"

    def filter_qs(q: str, truck: str, driver: str, urgency: str) -> str:
        parts = {
            k: v
            for k, v in (("q", q), ("truck", truck), ("driver", driver), ("urgency", urgency))
            if v
        }
        return ("&" + urlencode(parts)) if parts else ""

    def inbox_ctx(
        s: Any,
        base: dict[str, Any],
        tab: str,
        selected: str | None,
        q: str,
        truck: str,
        driver: str,
        urgency: str,
    ) -> dict[str, Any]:
        names: C.Names = base["names"]
        now = base["now"]
        run_id = base["run_id"]
        per_tab: dict[str, list[dict[str, Any]]] = {}
        for key, _ in TABS:
            acts = queue_rows(s, key, run_id)
            incs = incidents_for(s, acts)
            items = [V.queue_item(a, incs.get(a.incident_id or ""), names, now) for a in acts]
            per_tab[key] = [i for i in items if V.matches_filters(i, q, truck, driver, urgency)]
        rows = per_tab[tab]
        sel_act = s.get(ProposedAction, selected) if selected else None
        if sel_act is None and rows:
            sel_act = s.get(ProposedAction, rows[0]["id"])
        sel = V.action_view(s, sel_act, names, now, base["wall_now"]) if sel_act else None
        return {
            "tab": tab,
            "tabs": TABS,
            "rows": rows,
            "counts": {k: len(v) for k, v in per_tab.items()},
            "selected": sel,
            "reasons": list(C.REASON_LABEL.items()),
            "q": q,
            "truck": truck,
            "driver": driver,
            "urgency": urgency,
            "qs": filter_qs(q, truck, driver, urgency),
            "trucks": [
                {"id": v.id, "unit": v.unit_number}
                for v in sorted(names.vehicles.values(), key=lambda v: v.unit_number)
            ],
            "drivers": [
                {"id": d.id, "name": d.name}
                for d in sorted(names.drivers.values(), key=lambda d: d.name)
            ],
            "notice": None,
        }

    def detail_ctx(
        s: Any,
        base: dict[str, Any],
        act: ProposedAction,
        notice: str | None = None,
        kind: str = "ok",
        memory: str = "",
    ) -> dict[str, Any]:
        return {
            "selected": V.action_view(s, act, base["names"], base["now"], base["wall_now"]),
            "reasons": list(C.REASON_LABEL.items()),
            "notice": notice,
            "notice_kind": kind,
            "memory_note": memory,
        }

    # --- pages ---------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/inbox", status_code=307)

    @app.get("/health")
    async def health() -> JSONResponse:
        st = runtime_status()
        return JSONResponse(
            {
                "ok": True,
                **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in st.items()},
            }
            if st
            else {"ok": True, "runtime": "not started"}
        )

    @app.get("/stream", response_class=HTMLResponse)
    async def stream(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_stream.html", {"st": runtime_status(), "request": request}
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
        request: Request,
        tab: str = "pending",
        selected: str | None = None,
        q: str = "",
        truck: str = "",
        driver: str = "",
        urgency: str = "",
    ) -> HTMLResponse:
        tab = clean_tab(tab)
        with session_scope() as s:
            base = base_ctx(request, s, "inbox")
            ctx = inbox_ctx(s, base, tab, selected, q.strip(), truck, driver, urgency)
            if hx(request) and request.headers.get("HX-Target") == "queue":
                return templates.TemplateResponse(request, "_queue.html", {**base, **ctx})
            return templates.TemplateResponse(request, "inbox.html", {**base, **ctx})

    @app.get("/inbox/queue", response_class=HTMLResponse)
    async def inbox_queue(
        request: Request,
        tab: str = "pending",
        selected: str | None = None,
        q: str = "",
        truck: str = "",
        driver: str = "",
        urgency: str = "",
    ) -> HTMLResponse:
        tab = clean_tab(tab)
        with session_scope() as s:
            base = base_ctx(request, s, "inbox")
            ctx = inbox_ctx(s, base, tab, selected, q.strip(), truck, driver, urgency)
            if selected:
                ctx["selected"] = {"id": selected}
            return templates.TemplateResponse(request, "_queue.html", {**base, **ctx})

    @app.get("/actions/{action_id}", response_class=HTMLResponse)
    async def action_detail(request: Request, action_id: str) -> HTMLResponse:
        with session_scope() as s:
            act = s.get(ProposedAction, action_id)
            if act is None:
                return HTMLResponse(
                    "<div class='detail-card'><div class='empty'><h3>This item is gone</h3>"
                    "<p>It may have been removed when a new day started. Pick another item.</p></div></div>",
                    status_code=404,
                )
            if not hx(request):
                return RedirectResponse(  # type: ignore[return-value]
                    f"/inbox?tab={V.tab_for_status(act.status)}&selected={action_id}",
                    status_code=307,
                )
            base = base_ctx(request, s, "inbox")
            return templates.TemplateResponse(
                request, "_action_detail.html", {**base, **detail_ctx(s, base, act)}
            )

    def outcome_copy(
        out: decisions.DecisionOutcome, act: ProposedAction, names: C.Names
    ) -> tuple[str, str]:
        done = C.ACTION_COPY.get(act.tool_name, {}).get("done", "Done")
        if out.status == "executed":
            return f"Approved. {done}.", "ok"
        if out.status == "rejected":
            return "Dismissed. Haulmind will remember why.", "ok"
        if out.status == "acknowledged":
            return "Marked as handled.", "ok"
        if out.status == "missing":
            return "This item no longer exists.", "stop"
        if out.status in C.STATUS_LABEL and not out.ok:
            detail = (
                names.rewrite(out.message.split(":", 1)[1].strip()) if ":" in out.message else ""
            )
            return (C.STATUS_LABEL[out.status] + (f": {detail}" if detail else "."), "stop")
        return names.rewrite(out.message), "ok" if out.ok else "stop"

    async def _decide(
        request: Request, action_id: str, decision: str, reason_code: str, reason_text: str
    ) -> HTMLResponse:
        rt = get_runtime()
        assert rt.tools is not None, "runtime not started"
        out = await decisions.decide(
            action_id, decision, tools=rt.tools, reason_code=reason_code, reason_text=reason_text
        )
        with session_scope() as s:
            act = s.get(ProposedAction, action_id)
            if act is None:
                return HTMLResponse(
                    "<div class='detail-card'><div class='empty'><h3>This item is gone</h3></div></div>",
                    status_code=404,
                )
            base = base_ctx(request, s, "inbox")
            notice, kind = outcome_copy(out, act, base["names"])
            memory = C.memory_sentence(out.memory_changes, base["names"])
            resp = templates.TemplateResponse(
                request,
                "_action_detail.html",
                {**base, **detail_ctx(s, base, act, notice=notice, kind=kind, memory=memory)},
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
            act = s.get(ProposedAction, action_id)
            if act is None:
                return HTMLResponse(
                    "<div class='detail-card'><div class='empty'><h3>This item is gone</h3></div></div>",
                    status_code=404,
                )
            base = base_ctx(request, s, "inbox")
            return templates.TemplateResponse(
                request, "_action_edit.html", {**base, "edit": V.edit_view(act, base["names"])}
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
            act = s.get(ProposedAction, action_id)
            if act is None:
                return HTMLResponse(
                    "<div class='detail-card'><div class='empty'><h3>This item is gone</h3></div></div>",
                    status_code=404,
                )
            base = base_ctx(request, s, "inbox")
            resp = templates.TemplateResponse(
                request,
                "_action_detail.html",
                {
                    **base,
                    **detail_ctx(
                        s,
                        base,
                        act,
                        notice="Changes saved. Approve when you are ready and Haulmind will use the new details."
                        if ok
                        else "These details can no longer be changed.",
                        kind="ok" if ok else "stop",
                    ),
                },
            )
        resp.headers["HX-Trigger"] = "queue-changed"
        return resp

    @app.get("/incidents", response_class=HTMLResponse)
    async def incidents(request: Request) -> HTMLResponse:
        def build(s: Any, base: dict[str, Any]) -> dict[str, Any]:
            run_id = base["run_id"]
            q = select(Incident).order_by(Incident.sim_ts.desc()).limit(200)
            if run_id:
                q = q.where(Incident.run_id == run_id)
            incs = list(s.execute(q).scalars().all())
            acts_by_inc: dict[str, list[ProposedAction]] = {}
            if incs:
                for a in s.execute(
                    select(ProposedAction)
                    .where(ProposedAction.incident_id.in_([i.id for i in incs]))
                    .order_by(ProposedAction.created_ts)
                ).scalars():
                    acts_by_inc.setdefault(a.incident_id or "", []).append(a)
            stats = get_stats(s, run_id) if run_id else {}
            rows = [
                V.incident_row(i, acts_by_inc.get(i.id, []), base["names"], base["now"])
                for i in incs
            ]
            return {"rows": rows, "summary": C.day_summary(stats, len(rows))}

        return render(request, "incidents.html", "incidents", build=build)

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_detail(request: Request, incident_id: str) -> HTMLResponse:
        with session_scope() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                return HTMLResponse(
                    "<p class='empty'>We couldn't find that problem.</p>", status_code=404
                )
            base = base_ctx(request, s, "incidents")
            names: C.Names = base["names"]
            acts = list(
                s.execute(
                    select(ProposedAction)
                    .where(ProposedAction.incident_id == inc.id)
                    .order_by(ProposedAction.created_ts)
                )
                .scalars()
                .all()
            )
            events = list(
                s.execute(
                    select(EventRow)
                    .where(EventRow.run_id == inc.run_id, EventRow.incident_id == inc.id)
                    .order_by(EventRow.seq)
                )
                .scalars()
                .all()
            )
            spans = (
                list(
                    s.execute(select(Span).where(Span.trace_id == inc.trace_id).order_by(Span.seq))
                    .scalars()
                    .all()
                )
                if inc.trace_id
                else []
            )
            logs = list(
                s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id.in_([inc.id, *[a.id for a in acts]]))
                    .order_by(AuditLog.ts)
                )
                .scalars()
                .all()
            )
            row = V.incident_row(inc, acts, names, base["now"])
            ctx = {
                "inc": row,
                "why": C.why_it_matters(inc),
                "summary": names.rewrite(inc.summary),
                "story": V.incident_story(inc, events, acts, logs, names),
                "steps": V.steps_for(inc, spans, names),
                "technical": {
                    "incident_id": inc.id,
                    "type": inc.type,
                    "domain": inc.domain,
                    "status": inc.status,
                    "cluster": inc.cluster_key,
                    "trace_id": inc.trace_id or "",
                    "triage": V.pretty(inc.triage_json),
                    "signals": V.pretty(inc.signals_json),
                    "llm_requests": inc.llm_requests,
                    "tokens": inc.llm_input_tokens + inc.llm_output_tokens,
                    "latency_ms": inc.latency_ms,
                    "mode": inc.agent_mode,
                },
            }
            return templates.TemplateResponse(request, "incident_detail.html", {**base, **ctx})

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request) -> HTMLResponse:
        def build(s: Any, base: dict[str, Any]) -> dict[str, Any]:
            names: C.Names = base["names"]
            rows = list(
                s.execute(select(AuditLog).order_by(AuditLog.ts.desc()).limit(300)).scalars().all()
            )
            incs = (
                {
                    i.id: i
                    for i in s.execute(
                        select(Incident).where(
                            Incident.id.in_(
                                [r.entity_id for r in rows if r.entity_type == "incident"]
                            )
                        )
                    ).scalars()
                }
                if rows
                else {}
            )
            acts = (
                {
                    a.id: a
                    for a in s.execute(
                        select(ProposedAction).where(
                            ProposedAction.id.in_(
                                [r.entity_id for r in rows if r.entity_type == "action"]
                            )
                        )
                    ).scalars()
                }
                if rows
                else {}
            )
            out = []
            for r in rows:
                href = ""
                about = ""
                if r.entity_type == "incident" and r.entity_id in incs:
                    i = incs[r.entity_id]
                    href, about = f"/incidents/{i.id}", C.headline(i, names)
                elif r.entity_type == "action" and r.entity_id in acts:
                    a = acts[r.entity_id]
                    href, about = (
                        f"/inbox?tab={V.tab_for_status(a.status)}&selected={a.id}",
                        C.action_title(a, names),
                    )
                out.append(
                    {
                        "when": C.ago(r.ts, base["wall_now"]),
                        "when_full": C.full_time(r.ts),
                        "who": C.label(C.ACTOR_LABEL, r.actor.split(":")[0]),
                        "text": C.audit_sentence(r, names),
                        "about": about,
                        "href": href,
                        "technical": f"{r.actor} {r.action} {r.entity_type} {r.entity_id} {V.pretty(r.details_json)}",
                    }
                )
            return {"rows": out}

        return render(request, "audit.html", "audit", build=build)

    @app.get("/traces", response_class=HTMLResponse)
    async def traces(request: Request) -> HTMLResponse:
        def build(s: Any, base: dict[str, Any]) -> dict[str, Any]:
            names: C.Names = base["names"]
            run_id = base["run_id"]
            q = select(Trace).order_by(Trace.started_ts.desc()).limit(200)
            if run_id:
                q = q.where(Trace.run_id == run_id)
            rows = list(s.execute(q).scalars().all())
            incs = (
                {
                    i.id: i
                    for i in s.execute(
                        select(Incident).where(
                            Incident.id.in_([r.incident_id for r in rows if r.incident_id])
                        )
                    ).scalars()
                }
                if rows
                else {}
            )
            out = []
            for t in rows:
                inc = incs.get(t.incident_id or "")
                outcome = (t.attrs_json or {}).get("outcome") or t.status
                out.append(
                    {
                        "id": t.id,
                        "when": C.ago(t.started_ts, base["wall_now"]),
                        "when_full": C.full_time(t.started_ts),
                        "about": C.headline(inc, names) if inc else C.trace_subject(t, names),
                        "incident_id": inc.id if inc else "",
                        "outcome": C.label(C.OUTCOME_LABEL, outcome),
                        "checks": t.llm_requests,
                        "duration": C.duration_ms(
                            ((t.ended_ts - t.started_ts).total_seconds() * 1000)
                            if t.ended_ts
                            else None
                        ),
                        "technical": f"{t.id} {t.name} status={t.status} tokens={t.input_tokens}+{t.output_tokens}",
                    }
                )
            return {"rows": out}

        return render(request, "traces.html", "traces", build=build)

    @app.get("/traces/{trace_id}", response_class=HTMLResponse)
    async def trace_detail(request: Request, trace_id: str) -> HTMLResponse:
        with session_scope() as s:
            tr = s.get(Trace, trace_id)
            if tr is None:
                return HTMLResponse(
                    "<p class='empty'>We couldn't find that record.</p>", status_code=404
                )
            base = base_ctx(request, s, "traces")
            names: C.Names = base["names"]
            spans = list(
                s.execute(select(Span).where(Span.trace_id == trace_id).order_by(Span.seq))
                .scalars()
                .all()
            )
            inc = s.get(Incident, tr.incident_id) if tr.incident_id else None
            total_ms = max(
                1, int(((tr.ended_ts or tr.started_ts) - tr.started_ts).total_seconds() * 1000)
            )
            rows = []
            for sp in spans:
                attrs = sp.attrs_json or {}
                if sp.kind == "tool_call":
                    tool = str(attrs.get("tool") or sp.name.split(":", 1)[-1])
                    if tool in C.ACTION_COPY:
                        what, icon = C.ACTION_COPY[tool]["done"], C.ACTION_COPY[tool]["icon"]
                    else:
                        what, icon = C.check_step(tool, attrs.get("args") or {}, names)
                else:
                    step = C.step_for_span(sp) or (C.humanize(sp.name), "activity")
                    what, icon = step
                offset = max(0, int((sp.started_ts - tr.started_ts).total_seconds() * 1000))
                rows.append(
                    {
                        "what": what,
                        "icon": icon,
                        "model": sp.kind == "llm_call",
                        "failed": sp.status == "error",
                        "took": C.took(sp.latency_ms),
                        "left": min(99.0, 100.0 * offset / total_ms),
                        "width": max(0.6, min(100.0, 100.0 * sp.latency_ms / total_ms)),
                        "technical": f"{sp.name} kind={sp.kind} status={sp.status}"
                        + (
                            f" model={sp.model} tokens={sp.input_tokens}+{sp.output_tokens}"
                            if sp.model
                            else ""
                        )
                        + (" cached" if sp.cached else "")
                        + (f" error={sp.error[:200]}" if sp.error else "")
                        + f" attrs={V.pretty(attrs)}",
                    }
                )
            ctx = {
                "about": C.headline(inc, names) if inc else C.trace_subject(tr, names),
                "incident_id": inc.id if inc else "",
                "outcome": C.label(
                    C.OUTCOME_LABEL, (tr.attrs_json or {}).get("outcome") or tr.status
                ),
                "duration": C.duration_ms(total_ms),
                "checks": tr.llm_requests,
                "rows": rows,
                "technical": (
                    f"{tr.id} {tr.name} tokens={tr.input_tokens}+{tr.output_tokens} "
                    f"attrs={V.pretty(tr.attrs_json)}"
                ),
            }
            return templates.TemplateResponse(request, "trace_detail.html", {**base, **ctx})

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet(request: Request) -> HTMLResponse:
        def build(s: Any, base: dict[str, Any]) -> dict[str, Any]:
            return {"fleet": V.fleet_view(s, base["names"], base["run_id"], base["now"])}

        return render(request, "fleet.html", "fleet", build=build)

    @app.get("/performance", response_class=HTMLResponse)
    async def performance(request: Request) -> HTMLResponse:
        def build(s: Any, base: dict[str, Any]) -> dict[str, Any]:
            st = runtime_status()
            stats = get_stats(s, base["run_id"]) if base["run_id"] else {}
            return {
                "perf": V.performance_view(RESULTS),
                "live": {
                    "filtered_pct": (st or {}).get("filtered_pct", stats.get("filtered_pct")),
                    "incidents": int(
                        (st or {}).get("incidents", stats.get("incidents_opened", 0)) or 0
                    ),
                    "events": int(stats.get("events_total", (st or {}).get("events_done", 0)) or 0),
                    "dismissed": int(stats.get("triage_dismissed", 0) or 0),
                },
            }

        return render(request, "performance.html", "performance", build=build)

    @app.get("/runs/{run_id}/stats")
    async def run_stats(run_id: str) -> JSONResponse:
        with session_scope() as s:
            return JSONResponse(get_stats(s, run_id))

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> Response:
        log.exception("request failed", path=request.url.path)
        return HTMLResponse(
            "<div class='error-state' role='alert'><span>Something went wrong on our side. "
            "Try again in a moment.</span><button class='btn btn-sm' type='button' "
            "onclick='location.reload()'>Try again</button></div>"
            "<details class='disclosure technical'><summary>Technical details</summary>"
            f"<pre>{type(exc).__name__}: {str(exc)[:300]}</pre></details>",
            status_code=500,
        )

    return app

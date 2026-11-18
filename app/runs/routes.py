from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.db import SessionLocal, get_db
from app.deps import csrf_protect, require_configured
from app.models import RunEvent, RunStatus, User, WorkflowRun
from app.runs import service
from app.maestro.render import render_design_svg, step_info
from app.runs.events import agent_text, step_marker
from app.templating import flash, render, templates

from app.runs.progress import blocked_reason

router = APIRouter()

SSE_POLL_SECONDS = 1.0
SSE_MAX_SECONDS = 600  # the browser then reconnects automatically


def get_owned_run(db: Session, run_id: int, user: User) -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404)
    return run


def tool_uses(db: Session, run: WorkflowRun, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.anthropic_event_id.in_(ids))
    )
    return {r.anthropic_event_id: r.payload_json for r in rows}


def event_views(db: Session, run: WorkflowRun, events: list[RunEvent]) -> list[dict[str, Any]]:
    """Prepares events for display (tool name resolution, initial message)."""
    first_user = db.scalar(
        select(RunEvent.id)
        .where(RunEvent.run_id == run.id, RunEvent.type == "user.message")
        .order_by(RunEvent.id)
        .limit(1)
    )
    ref_ids = [
        str(e.payload_json.get("mcp_tool_use_id") or e.payload_json.get("tool_use_id") or "")
        for e in events
        if e.type in {"agent.mcp_tool_result", "agent.tool_result", "user.tool_confirmation"}
    ]
    uses = tool_uses(db, run, [r for r in ref_ids if r])
    valid = design_step_ids(run)
    step = step_before(db, run, events[0].id, valid) if events else None
    views = []
    for e in events:
        p = e.payload_json
        ref = p.get("mcp_tool_use_id") or p.get("tool_use_id")
        if e.type == "agent.message":
            announced = step_marker(agent_text(p))
            if announced and (not valid or announced in valid):
                step = announced
        views.append(
            {
                "row": e,
                "type": e.type,
                "p": p,
                "is_initial": e.id == first_user,
                "tool": uses.get(str(ref)) if ref else None,
                "step": step,
                "main": is_main_event(e.type, p, e.id == first_user),
            }
        )
    return views


# Events shown in the condensed run log: Maestro's reports, your messages, errors and approval requests.
# Everything else (tool calls and results, the initial instructions) is only in the step's detail window.
MAIN_TYPES = {"agent.message", "user.message", "session.error", "user.interrupt"}


def is_main_event(etype: str, payload: dict[str, Any], is_initial: bool) -> bool:
    if etype == "user.message":
        return not is_initial
    if etype == "session.status_idle":
        return (payload.get("stop_reason") or {}).get("type") == "requires_action"
    return etype in MAIN_TYPES


def design_step_ids(run: WorkflowRun) -> set[str]:
    return {str(s.get("id")) for s in run_design(run).get("steps") or []}


def run_design(run: WorkflowRun) -> dict[str, Any]:
    """The design a run follows: its frozen copy, or its workflow's design for runs started before copies existed."""
    if run.design_snapshot:
        return run.design_snapshot
    if run.workflow is not None and run.workflow.design_json:
        return run.workflow.design_json
    return {}


def step_before(db: Session, run: WorkflowRun, event_id: int | None, valid: set[str]) -> str | None:
    """Step announced by the last Maestro message before `event_id` (or overall when None)."""
    query = select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "agent.message")
    if event_id is not None:
        # Never compare ids with huge sentinels: PostgreSQL ids are 32-bit integers.
        query = query.where(RunEvent.id < event_id)
    rows = db.scalars(query.order_by(RunEvent.id.desc()))
    for row in rows:
        announced = step_marker(agent_text(row.payload_json))
        if announced and (not valid or announced in valid):
            return announced
    return None


def current_step(db: Session, run: WorkflowRun) -> str | None:
    return step_before(db, run, None, design_step_ids(run))


def actions_context(db: Session, run: WorkflowRun) -> dict[str, Any]:
    pending = tool_uses(db, run, list(run.pending_event_ids or []))
    return {
        "run": run,
        "pending_tools": [pending[i] for i in run.pending_event_ids or [] if i in pending],
        "can_continue": service.can_continue(run),
        "waiting_step": current_step(db, run) if service.can_continue(run) else None,
    }


def _partial(request: Request, name: str, ctx: dict[str, Any]) -> str:
    from app.security import get_or_create_csrf_token

    ctx = {"request": request, "csrf_token": get_or_create_csrf_token(request.session), **ctx}
    return templates.get_template(name).render(ctx)


@router.get("/runs/{run_id}")
def run_detail(
    request: Request, run_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    run = get_owned_run(db, run_id, user)
    events = list(run.events)
    design = run_design(run)
    has_steps = bool(design.get("steps"))
    return render(
        request,
        "run_detail.html",
        {
            "run": run,
            "events": event_views(db, run, events),
            "last_event_id": events[-1].id if events else 0,
            "map_svg": render_design_svg(design) if has_steps else None,
            "map_steps": [step_info(st) for st in design.get("steps") or []],
            "current_step": current_step(db, run) if has_steps else None,
            "blocked_reason": blocked_reason(db, run),
            **actions_context(db, run),
        },
    )


@router.get("/runs/{run_id}/actions")
def run_actions(
    request: Request, run_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    run = get_owned_run(db, run_id, user)
    return HTMLResponse(_partial(request, "partials/run_actions.html", actions_context(db, run)))


def _sse(event: str, data: str, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines += [f"data: {line}" for line in data.splitlines() or [""]]
    return "\n".join(lines) + "\n\n"


@router.get("/runs/{run_id}/stream")
async def run_stream(
    request: Request, run_id: int, after: int = 0, user: User = Depends(require_configured)
) -> Response:
    owner_id = user.id
    with SessionLocal() as db:
        get_owned_run(db, run_id, user)  # 404 if the run does not belong to the user
    header = request.headers.get("last-event-id")
    last_id = int(header) if header and header.isdigit() else after

    def poll(last: int, last_state: str | None) -> tuple[list[str], int, str | None, bool]:
        db = SessionLocal()
        try:
            run = db.get(WorkflowRun, run_id)
            if run is None or run.user_id != owner_id:
                return [], last, last_state, True
            new = list(
                db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.id > last)
                    .order_by(RunEvent.id)
                    .limit(200)
                )
            )
            out: list[str] = []
            for view in event_views(db, run, new):
                html = _partial(request, "partials/event.html", {"ev": view, "run": run})
                out.append(_sse("transcript", html, view["row"].id))
                last = view["row"].id
            state = json.dumps([run.status, run.pending_event_ids, run.error, run.session_id])
            if state != last_state:
                ctx = actions_context(db, run)
                out.append(_sse("actions", _partial(request, "partials/run_actions.html", ctx)))
                out.append(_sse("status", _partial(request, "partials/run_status.html",
                                                    {"run": run, "blocked_reason": blocked_reason(db, run)})))
            return out, last, state, False
        finally:
            db.close()

    async def gen() -> AsyncIterator[str]:
        nonlocal last_id
        state: str | None = None
        elapsed = 0.0
        idle = 0.0
        yield "retry: 3000\n\n"
        while elapsed < SSE_MAX_SECONDS:
            if await request.is_disconnected():
                break
            chunks, last_id, state, gone = await asyncio.to_thread(poll, last_id, state)
            if gone:
                break
            for chunk in chunks:
                yield chunk
            idle = 0.0 if chunks else idle + SSE_POLL_SECONDS
            if idle >= 15:
                yield ": ping\n\n"
                idle = 0.0
            await asyncio.sleep(SSE_POLL_SECONDS)
            elapsed += SSE_POLL_SECONDS

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _actions_response(request: Request, db: Session, run: WorkflowRun, error: str | None = None) -> Response:
    if request.headers.get("HX-Request"):
        ctx = actions_context(db, run)
        ctx["error"] = error
        return HTMLResponse(_partial(request, "partials/run_actions.html", ctx))
    if error:
        flash(request, error, "error")
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/message", dependencies=[Depends(csrf_protect)])
def run_message(
    request: Request,
    run_id: int,
    text: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    run = get_owned_run(db, run_id, user)
    error = None
    if not text.strip():
        error = "The message is empty."
    elif run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}:
        error = "This run has ended. Start a new one."
    else:
        try:
            service.send_message(db, user, run, text.strip())
        except ManagedAgentsError as exc:
            error = str(exc)
    return _actions_response(request, db, run, error)


@router.post("/runs/{run_id}/confirm", dependencies=[Depends(csrf_protect)])
def run_confirm(
    request: Request,
    run_id: int,
    event_id: str = Form(...),
    decision: str = Form(...),
    deny_message: str = Form(""),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    run = get_owned_run(db, run_id, user)
    error = None
    try:
        service.confirm_tool(db, user, run, event_id, decision == "allow", deny_message)
    except ManagedAgentsError as exc:
        error = str(exc)
    return _actions_response(request, db, run, error)


@router.post("/runs/{run_id}/continue", dependencies=[Depends(csrf_protect)])
def run_continue(
    request: Request, run_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    """Continue the project of a stopped run: a new run with the same settings picks up where it stopped."""
    run = get_owned_run(db, run_id, user)
    if run.continued_by_run_id:
        return _redirect(request, f"/runs/{run.continued_by_run_id}")
    if not service.can_continue(run):
        return _redirect(request, f"/runs/{run.id}")
    from app.maestro.service import missing_connectors

    missing = missing_connectors(db, user, run.workflow) if run.workflow else []
    if missing:
        return _actions_response(request, db, run, f"Connect {', '.join(missing)} in Settings first.")
    try:
        new = service.continue_run(db, user, run)
    except ManagedAgentsError as exc:
        return _actions_response(request, db, run, f"Could not continue the project. {exc}")
    return _redirect(request, f"/runs/{new.id}")


def _redirect(request: Request, url: str) -> Response:
    """Full-page navigation, from an htmx form or a plain one."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.post("/runs/{run_id}/cancel", dependencies=[Depends(csrf_protect)])
def run_cancel(
    request: Request, run_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    run = get_owned_run(db, run_id, user)
    error = None
    if run.status not in {RunStatus.CANCELLED, RunStatus.FAILED}:
        try:
            service.cancel_run(db, user, run)
        except ManagedAgentsError as exc:
            error = str(exc)
    return _actions_response(request, db, run, error)

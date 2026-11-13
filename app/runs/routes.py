from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.db import SessionLocal, get_db
from app.deps import csrf_protect, require_configured
from app.models import RunEvent, RunStatus, User, Workflow, WorkflowRun
from app.runs import service
from app.templating import flash, render, templates

router = APIRouter()

SSE_POLL_SECONDS = 1.0
SSE_MAX_SECONDS = 600  # le navigateur se reconnecte ensuite automatiquement


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
    """Prépare les événements pour l'affichage (résolution des noms d'outils, message initial)."""
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
    views = []
    for e in events:
        p = e.payload_json
        ref = p.get("mcp_tool_use_id") or p.get("tool_use_id")
        views.append(
            {
                "row": e,
                "type": e.type,
                "p": p,
                "is_initial": e.id == first_user,
                "tool": uses.get(str(ref)) if ref else None,
            }
        )
    return views


def actions_context(db: Session, run: WorkflowRun) -> dict[str, Any]:
    pending = tool_uses(db, run, list(run.pending_event_ids or []))
    can_save = (
        run.workflow_id is None
        and run.status in {RunStatus.COMPLETED, RunStatus.WAITING_INPUT}
        and bool(service.reference_result(db, run))
    )
    return {
        "run": run,
        "pending_tools": [pending[i] for i in run.pending_event_ids or [] if i in pending],
        "can_save": can_save,
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
    return render(
        request,
        "run_detail.html",
        {
            "run": run,
            "events": event_views(db, run, events),
            "last_event_id": events[-1].id if events else 0,
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
        get_owned_run(db, run_id, user)  # 404 si l'exécution n'appartient pas à l'utilisateur
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
                out.append(_sse("status", _partial(request, "partials/run_status.html", {"run": run})))
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
        error = "Le message est vide."
    elif run.status in {RunStatus.CANCELLED, RunStatus.FAILED}:
        error = "Cette exécution est terminée : relancez-en une nouvelle."
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


@router.post("/runs/{run_id}/save-workflow", dependencies=[Depends(csrf_protect)])
def run_save_workflow(
    request: Request,
    run_id: int,
    name: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    run = get_owned_run(db, run_id, user)
    name = name.strip()
    if run.workflow_id is not None or not name or len(name) > 128:
        flash(request, "Nom invalide (1 à 128 caractères).", "error")
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    wf = Workflow(
        user_id=user.id,
        name=name,
        content=run.content_snapshot,
        reference_result=service.reference_result(db, run),
    )
    db.add(wf)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Vous avez déjà un workflow nommé « {name} ».", "error")
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    flash(request, f"Workflow « {name} » enregistré.", "success")
    return RedirectResponse("/workflows", status_code=303)

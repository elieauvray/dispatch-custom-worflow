"""Maestro pages: describe a workflow, review the generated design, configure, schedule, launch."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.db import get_db
from app.deps import csrf_protect, require_configured
from app.maestro import service
from app.maestro.render import render_design_svg
from app.models import User, Workflow
from app.security import get_or_create_csrf_token
from app.templating import flash, render, templates
from app.workflows.routes import get_owned_workflow

router = APIRouter()
CRON_RE = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


def _maestro(db: Session, workflow_id: int, user: User) -> Workflow:
    wf = get_owned_workflow(db, workflow_id, user)
    if not wf.description:
        raise HTTPException(status_code=404)
    return wf


def _panel_ctx(request: Request, wf: Workflow) -> dict[str, Any]:
    design = wf.design_json or {}
    return {
        "request": request,
        "csrf_token": get_or_create_csrf_token(request.session),
        "wf": wf,
        "design": design,
        "svg": render_design_svg(design) if wf.design_status == "ready" and design else None,
        "counts": {a: sum(1 for s in design.get("steps", []) if s.get("actor") == a)
                   for a in ("maestro", "dispatch", "human", "upsun", "external")},
    }


@router.get("/")
@router.get("/maestro/new")
def maestro_new(request: Request, user: User = Depends(require_configured)) -> Response:
    return render(request, "maestro_new.html", {"name": "", "description": ""})


@router.post("/maestro/new", dependencies=[Depends(csrf_protect)])
def maestro_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    name, description = name.strip(), description.strip()
    error = None
    if not name or len(name) > 128:
        error = "The name must be between 1 and 128 characters."
    elif len(description) < 40:
        error = "Describe the workflow in more detail (stages, who validates, tools)."
    if not error:
        try:
            wf = service.create_maestro_workflow(db, user, name, description)
            return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
        except IntegrityError:
            db.rollback()
            error = f"You already have a workflow named {name}."
    return render(request, "maestro_new.html", {"name": name, "description": description, "error": error},
                  status_code=400)


@router.get("/workflows/{workflow_id}/design")
def design_page(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    ctx = _panel_ctx(request, wf)
    ctx.pop("request"), ctx.pop("csrf_token")
    ctx["missing"] = service.missing_connectors(db, user, wf) if wf.design_status == "ready" else []
    ctx["instructions_changed"] = service.instructions_changed(wf, service.current_instructions_hash(db))
    return render(request, "maestro_design.html", ctx)


@router.get("/workflows/{workflow_id}/design/panel")
def design_panel(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if wf.design_status == "ready":
        # Generation finished: reload the full page (config form, schedule, prompt).
        return Response(status_code=204, headers={"HX-Redirect": f"/workflows/{wf.id}/design"})
    return HTMLResponse(templates.get_template("partials/maestro_status.html").render(_panel_ctx(request, wf)))


@router.post("/workflows/{workflow_id}/design/revise", dependencies=[Depends(csrf_protect)])
def design_revise(
    request: Request,
    workflow_id: int,
    feedback: str = Form(""),
    description: str = Form(""),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if description.strip():
        wf.description = description.strip()
    service.request_design(db, wf, feedback)
    flash(request, "Maestro is regenerating the design.", "info")
    return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)


@router.post("/workflows/{workflow_id}/design/answers", dependencies=[Depends(csrf_protect)])
async def design_answers(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if wf.design_status != "needs_answers":
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    form = await request.form()
    answers: dict[str, str] = {}
    for q in (wf.clarifications or {}).get("questions") or []:
        choice = str(form.get(f"q:{q['id']}") or "")
        answers[q["id"]] = str(form.get(f"other:{q['id']}") or "").strip() if choice == "__other__" else choice
    missing = service.answer_questions(db, wf, answers)
    if missing:
        flash(request, "Answer every question (pick an option, or choose Other and write your answer).", "error")
    else:
        flash(request, "Thanks. Maestro is designing the workflow with your decisions.", "info")
    return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)


@router.post("/workflows/{workflow_id}/design/config", dependencies=[Depends(csrf_protect)])
async def design_config(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    form = await request.form()
    values = {k[4:]: str(v) for k, v in form.items() if k.startswith("cfg:")}
    service.save_config(db, wf, values)
    if wf.deployment_id:
        try:
            service.push_schedule(db, user, wf)
        except ManagedAgentsError as exc:
            flash(request, f"Config saved, but the schedule could not be updated. {exc}", "error")
            return RedirectResponse(f"/workflows/{wf.id}/design#config", status_code=303)
    flash(request, "Config saved.", "success")
    return RedirectResponse(f"/workflows/{wf.id}/design#config", status_code=303)


@router.post("/workflows/{workflow_id}/schedule", dependencies=[Depends(csrf_protect)])
def schedule_save(
    request: Request,
    workflow_id: int,
    cron: str = Form(...),
    timezone: str = Form("Europe/Paris"),
    budget: str = Form(""),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    wf = _maestro(db, workflow_id, user)
    missing = service.missing_connectors(db, user, wf)
    if missing:
        flash(request, f"Connect {', '.join(missing)} in Settings before scheduling this workflow.", "error")
        return RedirectResponse(f"/workflows/{wf.id}/design#schedule", status_code=303)
    cron = " ".join(cron.split())
    if not CRON_RE.match(cron):
        flash(request, "Invalid cron expression (5 fields, e.g. 0 * * * *).", "error")
        return RedirectResponse(f"/workflows/{wf.id}/design#schedule", status_code=303)
    try:
        wf.budget_cents = int(round(float(budget) * 100)) if budget.strip() else None
    except ValueError:
        flash(request, "The budget must be a number of dollars.", "error")
        return RedirectResponse(f"/workflows/{wf.id}/design#schedule", status_code=303)
    wf.schedule_cron, wf.schedule_timezone = cron, timezone.strip() or "Europe/Paris"
    db.commit()
    try:
        service.push_schedule(db, user, wf)
        flash(request, f"Schedule active: {cron} ({wf.schedule_timezone}).", "success")
    except ManagedAgentsError as exc:
        flash(request, f"Could not schedule Maestro. {exc}", "error")
    return RedirectResponse(f"/workflows/{wf.id}/design#schedule", status_code=303)


@router.post("/workflows/{workflow_id}/schedule/{action}", dependencies=[Depends(csrf_protect)])
def schedule_toggle(
    request: Request, workflow_id: int, action: str,
    user: User = Depends(require_configured), db: Session = Depends(get_db),
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404)
    try:
        service.set_paused(db, user, wf, action == "pause")
        flash(request, "Schedule paused." if action == "pause" else "Schedule resumed.", "success")
    except ManagedAgentsError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/workflows/{wf.id}/design#schedule", status_code=303)


@router.get("/workflows/{workflow_id}/design.svg")
def design_svg(workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)) -> Response:
    wf = _maestro(db, workflow_id, user)
    if wf.design_status != "ready" or not wf.design_json:
        raise HTTPException(status_code=404)
    slug = re.sub(r"[^a-z0-9]+", "-", wf.name.lower()).strip("-") or "workflow"
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + str(render_design_svg(wf.design_json)),
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="maestro-{slug}.svg"'},
    )

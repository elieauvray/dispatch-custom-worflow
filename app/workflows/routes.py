from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.db import get_db
from app.deps import csrf_protect, require_configured
from app.maestro import service as maestro_service
from app.maestro.diagram import DIAGRAM_ONLY_DESCRIPTION, PDF_MAX_BYTES
from app.maestro.diagram import validate as validate_diagram
from app.models import RunStatus, Skill, User, Workflow, WorkflowRun
from app.runs import service
from app.templating import flash, render

router = APIRouter()


def get_owned_workflow(db: Session, workflow_id: int, user: User) -> Workflow:
    """A workflow its owner can change (edit, delete, config, schedule)."""
    wf = db.get(Workflow, workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404)
    return wf


def get_visible_workflow(db: Session, workflow_id: int, user: User) -> Workflow:
    """A workflow the user can see and run: their own, or one another user shared."""
    wf = db.get(Workflow, workflow_id)
    if wf is None or (wf.user_id != user.id and not wf.shared):
        raise HTTPException(status_code=404)
    return wf


# --- Prompt ------------------------------------------------------------------


@router.get("/instructions")
def instructions_readonly(
    request: Request, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    """System instructions, read-only: they apply to every workflow, only admins edit them."""
    if user.is_admin:
        return RedirectResponse("/admin/instructions", status_code=303)
    skills = list(db.scalars(select(Skill).where(Skill.active.is_(True)).order_by(Skill.position, Skill.id)))
    return render(request, "instructions.html", {"skills": skills, "editing": None, "can_edit": False})


@router.get("/workflows")
def workflows_list(
    request: Request, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    workflows = list(
        db.scalars(
            select(Workflow)
            .where((Workflow.user_id == user.id) | Workflow.shared.is_(True), Workflow.description.is_not(None))
            .order_by(Workflow.name)
        )
    )
    owners = {u.id: u.login for u in db.scalars(select(User).where(User.id.in_({w.user_id for w in workflows})))}
    counts = dict(
        db.execute(
            select(WorkflowRun.workflow_id, func.count(WorkflowRun.id))
            .where(WorkflowRun.user_id == user.id, WorkflowRun.workflow_id.is_not(None))
            .group_by(WorkflowRun.workflow_id)
        ).all()
    )
    instr_hash = maestro_service.current_instructions_hash(db)
    last_runs: dict[int, WorkflowRun] = {}
    waiting: dict[int, int] = {}
    for run in db.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.user_id == user.id, WorkflowRun.workflow_id.is_not(None))
        .order_by(WorkflowRun.id.desc())
    ):
        assert run.workflow_id is not None
        last_runs.setdefault(run.workflow_id, run)
        if run.status in {RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL}:
            waiting[run.workflow_id] = waiting.get(run.workflow_id, 0) + 1
    return render(
        request,
        "workflows.html",
        {"workflows": workflows, "counts": counts, "last_runs": last_runs, "waiting": waiting, "owners": owners,
         "outdated": {wf.id for wf in workflows if maestro_service.instructions_changed(wf, instr_hash)}},
    )


def _edit_ctx(wf: Workflow, name: str, description: str, content: str, error: str | None = None) -> dict:
    return {"workflow": wf, "name": name, "description": description, "content": content, "error": error,
            "feedback": "",
            "is_maestro": bool(wf.description), "can_edit_prompt": not wf.description or wf.design_status == "ready"}


@router.get("/workflows/{workflow_id}/edit")
def workflow_edit(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    return render(request, "workflow_form.html", _edit_ctx(wf, wf.name, wf.description or "", wf.content))


@router.post("/workflows/{workflow_id}/edit", dependencies=[Depends(csrf_protect)])
async def workflow_update(
    request: Request,
    workflow_id: int,
    name: str = Form(...),
    description: str | None = Form(None),
    content: str | None = Form(None),
    feedback: str = Form(""),
    remove_diagram: str = Form(""),
    shared: str = Form(""),
    diagram: UploadFile | None = File(None),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    """Maestro workflow: a new description, diagram or change request is designed again;
    the orchestration prompt can be edited by hand once the design is ready."""
    wf = get_owned_workflow(db, workflow_id, user)
    is_maestro = bool(wf.description)
    name, feedback = name.strip(), feedback.strip()
    if description is None or not is_maestro:
        description = wf.description or ""
    else:
        description = description.strip()
    can_edit_prompt = not is_maestro or wf.design_status == "ready"
    content = wf.content if content is None or not can_edit_prompt else content.strip()

    error: str | None = None
    new_diagram: tuple[str, bytes] | None = None
    if is_maestro and diagram is not None and diagram.filename:
        data = await diagram.read(PDF_MAX_BYTES + 1)
        media_type, error = validate_diagram(data)
        if media_type:
            new_diagram = (media_type, data)
    drop_diagram = is_maestro and bool(remove_diagram) and new_diagram is None
    keeps_diagram = new_diagram is not None or (bool(wf.diagram_data) and not drop_diagram)
    if is_maestro and drop_diagram and description == DIAGRAM_ONLY_DESCRIPTION:
        description = ""  # the placeholder only made sense with a diagram
    if is_maestro and not description and keeps_diagram:
        description = DIAGRAM_ONLY_DESCRIPTION
    if error:
        pass
    elif not name or len(name) > 128:
        error = "The name must be between 1 and 128 characters."
    elif is_maestro and len(description) < 40 and not keeps_diagram:
        error = "Describe the workflow in more detail (stages, who validates, tools), or add a diagram."
    diagram_changed = new_diagram is not None or (drop_diagram and bool(wf.diagram_data))
    redesign = is_maestro and (description != wf.description or diagram_changed or bool(feedback))
    if not error and can_edit_prompt and not content and not redesign:
        error = "The orchestration prompt is required."
    if not error:
        prompt_changed = not redesign and content != wf.content
        wf.name = name
        wf.shared = bool(shared)
        if redesign:
            wf.description = description  # the new design replaces the prompt
            if new_diagram:
                wf.diagram_media_type, wf.diagram_data = new_diagram
            elif drop_diagram:
                wf.diagram_media_type, wf.diagram_data = None, None
        else:
            wf.content = content
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            error = f"You already have a workflow named {name}."
    if error:
        if new_diagram:
            error += " Attach the diagram again."
        ctx = _edit_ctx(wf, name, description, content or "", error)
        ctx["feedback"] = feedback
        return render(request, "workflow_form.html", ctx, status_code=400)
    if redesign:
        maestro_service.request_design(db, wf, feedback or None)
        flash(request, "Saved. Maestro is redesigning the workflow.", "info")
    else:
        flash(request, "Workflow updated.", "success")
        if prompt_changed and wf.deployment_id:  # scheduled Maestro runs must use the edited prompt
            try:
                maestro_service.push_schedule(db, user, wf)
            except ManagedAgentsError as exc:
                flash(request, f"The schedule could not be updated. {exc}", "error")
    return RedirectResponse(f"/workflows/{wf.id}/design" if is_maestro else "/workflows", status_code=303)


@router.post("/workflows/{workflow_id}/delete", dependencies=[Depends(csrf_protect)])
def workflow_delete(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    if wf.deployment_id:  # stop the schedule for good, or it would keep firing runs
        from app.maestro.service import archive_schedule

        try:
            archive_schedule(db, user, wf)
        except ManagedAgentsError as exc:
            flash(request, f"The workflow was not deleted: its schedule could not be stopped. {exc}", "error")
            return RedirectResponse("/workflows", status_code=303)
    db.delete(wf)
    db.commit()
    flash(request, "Workflow deleted.", "success")
    return RedirectResponse("/workflows", status_code=303)


@router.post("/workflows/{workflow_id}/run", dependencies=[Depends(csrf_protect)])
def workflow_run(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_visible_workflow(db, workflow_id, user)
    if wf.description:  # Maestro workflow: it can only run with the connectors it was designed for
        from app.maestro.service import missing_connectors

        missing = missing_connectors(db, user, wf)
        if wf.design_status != "ready" or missing:
            flash(request, f"Connect {', '.join(missing)} in Settings first." if missing
                  else "The design is not ready yet.", "error")
            return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    try:
        run = service.create_workflow_run(db, user, wf)
    except ManagedAgentsError as exc:
        flash(request, f"Could not start the workflow. {exc}", "error")
        return RedirectResponse("/workflows", status_code=303)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/workflows/{workflow_id}/history")
def workflow_history(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_visible_workflow(db, workflow_id, user)
    runs = list(
        db.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id, WorkflowRun.user_id == user.id)
            .order_by(WorkflowRun.id.desc())
        )
    )
    return render(request, "workflow_history.html", {"workflow": wf, "runs": runs})

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.db import get_db
from app.deps import csrf_protect, require_configured
from app.models import RunStatus, User, Workflow, WorkflowRun
from app.runs import service
from app.templating import flash, render

router = APIRouter()


def get_owned_workflow(db: Session, workflow_id: int, user: User) -> Workflow:
    wf = db.get(Workflow, workflow_id)
    if wf is None or wf.user_id != user.id:
        raise HTTPException(status_code=404)
    return wf


# --- Prompt ------------------------------------------------------------------


@router.get("/")
def home() -> Response:
    return RedirectResponse("/prompt", status_code=303)


@router.get("/prompt")
def prompt_page(
    request: Request, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    recent = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.user_id == user.id, WorkflowRun.workflow_id.is_(None))
            .order_by(WorkflowRun.id.desc())
            .limit(10)
        )
    )
    return render(request, "prompt.html", {"recent": recent})


@router.post("/prompt", dependencies=[Depends(csrf_protect)])
def prompt_submit(
    request: Request,
    prompt: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    prompt = prompt.strip()
    if not prompt:
        flash(request, "Le prompt est vide.", "error")
        return RedirectResponse("/prompt", status_code=303)
    try:
        run = service.create_prompt_run(db, user, prompt)
    except ManagedAgentsError as exc:
        flash(request, f"Impossible de lancer la session : {exc}", "error")
        return RedirectResponse("/prompt", status_code=303)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


# --- Custom Workflows --------------------------------------------------------


@router.get("/workflows")
def workflows_list(
    request: Request, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    workflows = list(
        db.scalars(select(Workflow).where(Workflow.user_id == user.id).order_by(Workflow.name))
    )
    counts = dict(
        db.execute(
            select(WorkflowRun.workflow_id, func.count(WorkflowRun.id))
            .where(WorkflowRun.user_id == user.id, WorkflowRun.workflow_id.is_not(None))
            .group_by(WorkflowRun.workflow_id)
        ).all()
    )
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
        {"workflows": workflows, "counts": counts, "last_runs": last_runs, "waiting": waiting},
    )


def _validate(name: str, content: str) -> str | None:
    if not name or len(name) > 128:
        return "Le nom doit contenir entre 1 et 128 caractères."
    if not content:
        return "Le contenu est obligatoire."
    return None


@router.get("/workflows/new")
def workflow_new(request: Request, user: User = Depends(require_configured)) -> Response:
    return render(request, "workflow_form.html", {"workflow": None, "name": "", "content": ""})


@router.post("/workflows/new", dependencies=[Depends(csrf_protect)])
def workflow_create(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    name, content = name.strip(), content.strip()
    error = _validate(name, content)
    if not error:
        db.add(Workflow(user_id=user.id, name=name, content=content))
        try:
            db.commit()
            flash(request, f"Workflow « {name} » créé.", "success")
            return RedirectResponse("/workflows", status_code=303)
        except IntegrityError:
            db.rollback()
            error = f"Vous avez déjà un workflow nommé « {name} »."
    return render(
        request, "workflow_form.html",
        {"workflow": None, "name": name, "content": content, "error": error}, status_code=400,
    )


@router.get("/workflows/{workflow_id}/edit")
def workflow_edit(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    return render(request, "workflow_form.html", {"workflow": wf, "name": wf.name, "content": wf.content})


@router.post("/workflows/{workflow_id}/edit", dependencies=[Depends(csrf_protect)])
def workflow_update(
    request: Request,
    workflow_id: int,
    name: str = Form(...),
    content: str = Form(...),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    name, content = name.strip(), content.strip()
    error = _validate(name, content)
    if not error:
        wf.name, wf.content = name, content
        try:
            db.commit()
            flash(request, "Workflow mis à jour.", "success")
            return RedirectResponse("/workflows", status_code=303)
        except IntegrityError:
            db.rollback()
            error = f"Vous avez déjà un workflow nommé « {name} »."
    return render(
        request, "workflow_form.html",
        {"workflow": wf, "name": name, "content": content, "error": error}, status_code=400,
    )


@router.post("/workflows/{workflow_id}/delete", dependencies=[Depends(csrf_protect)])
def workflow_delete(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    db.delete(wf)
    db.commit()
    flash(request, "Workflow supprimé.", "success")
    return RedirectResponse("/workflows", status_code=303)


@router.post("/workflows/{workflow_id}/run", dependencies=[Depends(csrf_protect)])
def workflow_run(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    try:
        run = service.create_workflow_run(db, user, wf)
    except ManagedAgentsError as exc:
        flash(request, f"Impossible de lancer le workflow : {exc}", "error")
        return RedirectResponse("/workflows", status_code=303)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/workflows/{workflow_id}/history")
def workflow_history(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = get_owned_workflow(db, workflow_id, user)
    runs = list(
        db.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id).order_by(WorkflowRun.id.desc())
        )
    )
    return render(request, "workflow_history.html", {"workflow": wf, "runs": runs})

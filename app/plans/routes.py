"""Planned workflow endpoints (HTMX fragment, regenerate, SVG download) for runs and workflows."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import csrf_protect, require_configured
from app.models import User, Workflow, WorkflowRun
from app.plans.render import render_plan_svg
from app.plans.service import PlanTarget, ensure_plan
from app.security import get_or_create_csrf_token
from app.templating import templates

router = APIRouter()


def _target(db: Session, kind: str, target_id: int, user: User) -> PlanTarget:
    model = WorkflowRun if kind == "runs" else Workflow if kind == "workflows" else None
    if model is None:
        raise HTTPException(status_code=404)
    obj = db.get(model, target_id)
    if obj is None or obj.user_id != user.id:
        raise HTTPException(status_code=404)
    return obj


def _fragment(request: Request, kind: str, target: PlanTarget) -> HTMLResponse:
    ctx: dict[str, Any] = {
        "request": request,
        "csrf_token": get_or_create_csrf_token(request.session),
        "base": f"/{kind}/{target.id}/plan",
        "target": target,
        "svg": render_plan_svg(target.plan_json) if target.plan_status == "ready" and target.plan_json else None,
    }
    return HTMLResponse(templates.get_template("partials/plan.html").render(ctx))


@router.get("/{kind}/{target_id}/plan")
def plan_fragment(
    request: Request, kind: str, target_id: int,
    user: User = Depends(require_configured), db: Session = Depends(get_db),
) -> Response:
    target = _target(db, kind, target_id, user)
    ensure_plan(db, user, target)
    return _fragment(request, kind, target)


@router.post("/{kind}/{target_id}/plan/regenerate", dependencies=[Depends(csrf_protect)])
def plan_regenerate(
    request: Request, kind: str, target_id: int,
    user: User = Depends(require_configured), db: Session = Depends(get_db),
) -> Response:
    target = _target(db, kind, target_id, user)
    ensure_plan(db, user, target, force=True)
    return _fragment(request, kind, target)


@router.get("/{kind}/{target_id}/plan.svg")
def plan_svg(
    kind: str, target_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    target = _target(db, kind, target_id, user)
    if target.plan_status != "ready" or not target.plan_json:
        raise HTTPException(status_code=404)
    name = target.name if isinstance(target, Workflow) else (target.workflow_name_snapshot or f"prompt-{target.id}")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workflow"
    svg = '<?xml version="1.0" encoding="UTF-8"?>\n' + str(render_plan_svg(target.plan_json))
    return Response(
        svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="plan-{slug}.svg"'},
    )

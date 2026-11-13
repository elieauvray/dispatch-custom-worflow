from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.auth.routes import validate_new_credentials
from app.config import get_settings
from app.db import get_db
from app.deps import csrf_protect, require_admin
from app.models import OAuthState, RunEvent, Skill, User, UserSettings, Workflow, WorkflowRun
from app.security import hash_password
from app.settings.service import client_for
from app.templating import flash, render

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")


# --- Utilisateurs ------------------------------------------------------------


@router.get("/users")
def users_page(
    request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    users = list(db.scalars(select(User).order_by(User.login)))
    return render(request, "admin_users.html", {"users": users})


@router.post("/users", dependencies=[Depends(csrf_protect)])
def create_user(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    login = login.strip()
    error = validate_new_credentials(login, password)
    if error:
        flash(request, error, "error")
        return RedirectResponse("/admin/users", status_code=303)
    user = User(login=login, password_hash=hash_password(password), is_admin=False)
    db.add(user)
    try:
        db.flush()
        db.add(UserSettings(user_id=user.id, model=get_settings().default_model))
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Le login « {login} » existe déjà.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    flash(request, f"Utilisateur « {login} » créé.", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(csrf_protect)])
def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    error = validate_new_credentials(user.login, password)
    if error:
        flash(request, error, "error")
    else:
        user.password_hash = hash_password(password)
        db.commit()
        if user.id == admin.id:
            request.session["pwd"] = user.password_hash[-16:]
        flash(request, f"Mot de passe de « {user.login} » réinitialisé.", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_user(
    request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    if user.id == admin.id:
        flash(request, "Vous ne pouvez pas supprimer votre propre compte.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    settings = user.settings
    if settings and settings.vault_id and settings.anthropic_api_key_encrypted:
        try:
            client_for(settings).archive_vault(settings.vault_id)
        except (ManagedAgentsError, RuntimeError) as exc:
            flash(request, f"Vault non archivé ({exc}) : suppression annulée.", "error")
            return RedirectResponse("/admin/users", status_code=303)
    login = user.login
    run_ids = select(WorkflowRun.id).where(WorkflowRun.user_id == user.id)
    db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
    db.execute(delete(WorkflowRun).where(WorkflowRun.user_id == user.id))
    db.execute(delete(Workflow).where(Workflow.user_id == user.id))
    db.execute(delete(OAuthState).where(OAuthState.user_id == user.id))
    db.delete(user)  # cascade ORM : settings, connecteurs
    db.commit()
    flash(request, f"Utilisateur « {login} » supprimé (vault archivé).", "success")
    return RedirectResponse("/admin/users", status_code=303)


# --- Skills ------------------------------------------------------------------


@router.get("/skills")
def skills_page(
    request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    skills = list(db.scalars(select(Skill).order_by(Skill.position, Skill.id)))
    edit_id = request.query_params.get("edit")
    editing = db.get(Skill, int(edit_id)) if edit_id and edit_id.isdigit() else None
    return render(request, "admin_skills.html", {"skills": skills, "editing": editing})


def _skill_form(name: str, content: str, position: str) -> tuple[str, str, int] | str:
    name, content = name.strip(), content.strip()
    if not name or len(name) > 128 or not content:
        return "Nom (128 caractères max) et contenu obligatoires."
    try:
        pos = int(position)
    except ValueError:
        return "L'ordre doit être un nombre entier."
    return name, content, pos


@router.post("/skills", dependencies=[Depends(csrf_protect)])
def create_skill(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
    position: str = Form("0"),
    active: str | None = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    parsed = _skill_form(name, content, position)
    if isinstance(parsed, str):
        flash(request, parsed, "error")
    else:
        db.add(Skill(name=parsed[0], content=parsed[1], position=parsed[2], active=active is not None))
        db.commit()
        flash(request, "Skill créé.", "success")
    return RedirectResponse("/admin/skills", status_code=303)


@router.post("/skills/{skill_id}", dependencies=[Depends(csrf_protect)])
def update_skill(
    request: Request,
    skill_id: int,
    name: str = Form(...),
    content: str = Form(...),
    position: str = Form("0"),
    active: str | None = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404)
    parsed = _skill_form(name, content, position)
    if isinstance(parsed, str):
        flash(request, parsed, "error")
        return RedirectResponse(f"/admin/skills?edit={skill_id}", status_code=303)
    skill.name, skill.content, skill.position = parsed
    skill.active = active is not None
    db.commit()
    flash(request, "Skill mis à jour.", "success")
    return RedirectResponse("/admin/skills", status_code=303)


@router.post("/skills/{skill_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_skill(
    request: Request, skill_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    skill = db.get(Skill, skill_id)
    if skill:
        db.delete(skill)
        db.commit()
        flash(request, "Skill supprimé.", "success")
    return RedirectResponse("/admin/skills", status_code=303)

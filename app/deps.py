"""Dépendances FastAPI : utilisateur courant, droits, CSRF."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserSettings
from app.security import csrf_matches


class RedirectError(Exception):
    """Levée par une dépendance pour rediriger (géré dans main.py)."""

    def __init__(self, location: str) -> None:
        self.location = location


def load_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    user = db.get(User, user_id)
    if user is None:
        request.session.clear()
        return None
    # Invalide la session si le mot de passe a été changé (réinitialisation admin).
    if request.session.get("pwd") != user.password_hash[-16:]:
        request.session.clear()
        return None
    request.state.user = user
    settings = user.settings
    request.state.user_configured = bool(settings and settings.is_configured)
    return user


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = load_user(request, db)
    if user is None:
        raise RedirectError("/login")
    return user


def require_configured(request: Request, user: User = Depends(require_user)) -> User:
    if not request.state.user_configured:
        raise RedirectError("/settings")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Réservé à l'administrateur.")
    return user


def get_user_settings(db: Session, user: User) -> UserSettings:
    if user.settings is None:
        user.settings = UserSettings(user_id=user.id)
        db.add(user.settings)
        db.flush()
    return user.settings


async def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    token = request.headers.get("X-CSRF-Token")
    if not token:
        form = await request.form()
        value = form.get("csrf_token")
        token = value if isinstance(value, str) else None
    if not csrf_matches(request.session, token):
        raise HTTPException(status_code=403, detail="Jeton CSRF invalide. Rechargez la page.")

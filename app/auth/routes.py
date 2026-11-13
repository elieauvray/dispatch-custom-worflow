from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import csrf_protect, load_user
from app.models import LoginAttempt, User, UserSettings, utcnow
from app.security import DUMMY_HASH, hash_password, verify_password
from app.templating import render

router = APIRouter()

MIN_PASSWORD_LENGTH = 10


def users_exist(db: Session) -> bool:
    return db.scalar(select(func.count(User.id))) != 0


def validate_new_credentials(login: str, password: str, confirm: str | None = None) -> str | None:
    if not login or len(login) > 64 or not login.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return "Invalid login (letters, digits, dot, underscore or hyphen; 64 characters max)."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    return None


def start_session(request: Request, user: User) -> None:
    request.session.clear()  # évite la fixation de session
    request.session["user_id"] = user.id
    request.session["pwd"] = user.password_hash[-16:]


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)) -> Response:
    if users_exist(db):
        raise HTTPException(status_code=404)
    return render(request, "setup.html")


@router.post("/setup", dependencies=[Depends(csrf_protect)])
def setup_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if users_exist(db):
        raise HTTPException(status_code=404)
    login = login.strip()
    error = validate_new_credentials(login, password, password_confirm)
    if error:
        return render(request, "setup.html", {"error": error, "login": login}, status_code=400)
    user = User(login=login, password_hash=hash_password(password), is_admin=True)
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id, model=get_settings().default_model))
    user.last_login_at = utcnow()
    db.commit()
    start_session(request, user)
    request.app.state.has_users = True
    return RedirectResponse("/settings", status_code=303)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)) -> Response:
    if load_user(request, db):
        return RedirectResponse("/settings", status_code=303)
    return render(request, "login.html")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    cfg = get_settings()
    login = login.strip()
    keys = [f"login:{login.lower()}", f"ip:{_client_ip(request)}"]
    since = utcnow() - timedelta(seconds=cfg.login_window_seconds)
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < since))
    def failures(key: str) -> int:
        return db.scalar(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.key == key, LoginAttempt.created_at >= since
            )
        ) or 0

    # Par login : 5 échecs / 15 min. Par IP : 4x plus (plusieurs comptes derrière un NAT).
    if failures(keys[0]) >= cfg.login_max_attempts or failures(keys[1]) >= cfg.login_max_attempts * 4:
        db.commit()
        return render(
            request,
            "login.html",
            {"error": "Too many attempts. Try again in 15 minutes.", "login": login},
            status_code=429,
        )

    user = db.scalar(select(User).where(User.login == login))
    ok = verify_password(user.password_hash if user else DUMMY_HASH, password) and user is not None
    if not ok or user is None:
        for k in keys:
            db.add(LoginAttempt(key=k))
        db.commit()
        return render(
            request,
            "login.html",
            {"error": "Incorrect login or password.", "login": login},
            status_code=401,
        )
    db.execute(delete(LoginAttempt).where(LoginAttempt.key == keys[0]))
    user.last_login_at = utcnow()
    db.commit()
    start_session(request, user)
    return RedirectResponse("/settings", status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import router as admin_router
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import SessionLocal
from app.deps import RedirectError
from app.models import User
from app.runs.routes import router as runs_router
from app.settings.routes import router as settings_router
from app.templating import render
from app.webhooks import router as webhooks_router
from app.workflows.routes import router as workflows_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Les en-têtes HTTP (dont les clés) ne doivent jamais apparaître dans les logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)

SETUP_EXEMPT = ("/setup", "/static/", "/healthz", "/webhooks/")


def create_app() -> FastAPI:
    cfg = get_settings()
    if not cfg.secret_key or len(cfg.secret_key) < 32:
        raise RuntimeError("APP_SECRET_KEY doit être défini (32 caractères minimum).")

    app = FastAPI(title="Dispatch", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.has_users = False

    @app.middleware("http")
    async def first_run_redirect(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not request.app.state.has_users and not request.url.path.startswith(SETUP_EXEMPT):
            db = SessionLocal()
            try:
                request.app.state.has_users = bool(db.scalar(select(func.count(User.id))))
            finally:
                db.close()
            if not request.app.state.has_users:
                return RedirectResponse("/setup", status_code=303)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # Ajouté après le middleware ci-dessus => exécuté avant lui (request.session disponible).
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.secret_key,
        session_cookie="dispatch_session",
        max_age=60 * 60 * 12,
        same_site="lax",
        https_only=cfg.cookie_secure,
    )

    @app.exception_handler(RedirectError)
    async def redirect_handler(request: Request, exc: RedirectError) -> Response:
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": exc.location})
        return RedirectResponse(exc.location, status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith(("/runs/", "/webhooks/")) and (
            request.headers.get("HX-Request") or request.url.path.endswith("/stream")
            or request.url.path.startswith("/webhooks/")
        ):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        messages = {404: "Page introuvable.", 403: "Accès refusé."}
        detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else None
        return render(
            request,
            "error.html",
            {"status": exc.status_code, "message": detail or messages.get(exc.status_code, "Erreur.")},
            status_code=exc.status_code,
        )

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )
    for router in (
        auth_router, settings_router, workflows_router, runs_router, admin_router, webhooks_router,
    ):
        app.include_router(router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> HTMLResponse:
        return HTMLResponse(status_code=204)

    return app


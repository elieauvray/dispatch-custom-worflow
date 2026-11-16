"""Incoming webhooks: Anthropic (verified by the SDK) and GitHub (wakes Maestro on merged PRs)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Connector, ConnectorStatus, User, UserSettings
from app.security import decrypt

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/anthropic")
async def anthropic_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    secret = get_settings().webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    body = (await request.body()).decode()
    # La vérification est locale (HMAC) : aucune clé API n'est nécessaire.
    verifier = anthropic.Anthropic(api_key="webhook-verification-only")
    try:
        event = verifier.beta.webhooks.unwrap(body, headers=dict(request.headers), key=secret)
    except Exception:  # signature invalide, horodatage trop ancien, payload illisible
        raise HTTPException(status_code=400, detail="Invalid signature.") from None
    data = event.data
    if data.type == "vault_credential.refresh_failed":
        connector = db.scalar(select(Connector).where(Connector.credential_id == data.id))
        if connector:
            connector.status = ConnectorStatus.ERROR
            connector.status_message = "Reconnect needed: the OAuth token refresh failed."
            db.commit()
            log.info("Connector %s flagged as error (refresh_failed)", connector.id)
    return Response(status_code=204)


def github_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub signs the raw body with HMAC-SHA256 in X-Hub-Signature-256: "sha256=<hex>"."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/webhooks/github/{hook_id}")
async def github_webhook(hook_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    settings = db.scalar(select(UserSettings).where(UserSettings.github_hook_id == hook_id)) if hook_id else None
    if settings is None or not settings.github_hook_secret_encrypted:
        raise HTTPException(status_code=404)
    body = await request.body()
    if not github_signature_ok(decrypt(settings.github_hook_secret_encrypted), body,
                               request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return JSONResponse({"ok": True, "message": "Maestro is listening."})
    if event != "pull_request":
        return JSONResponse({"ignored": event})
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON.") from None
    pr = payload.get("pull_request") or {}
    if payload.get("action") != "closed" or not pr.get("merged"):
        return JSONResponse({"ignored": f"pull_request.{payload.get('action')}"})
    repo = (payload.get("repository") or {}).get("full_name") or ""
    user = db.get(User, settings.user_id)
    if not repo or user is None:
        return JSONResponse({"ignored": "no repository"})
    from app.maestro.service import wake_for_repo

    woken = await run_in_threadpool(wake_for_repo, db, user, repo)
    log.info("GitHub: PR #%s merged on %s, %d workflow(s) woken", pr.get("number"), repo, len(woken))
    return JSONResponse({"repository": repo, "woken_workflows": woken}, status_code=202)

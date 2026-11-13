"""Webhook Anthropic (signature vérifiée par le SDK)."""

from __future__ import annotations

import logging

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Connector, ConnectorStatus

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
        raise HTTPException(status_code=400, detail="Signature invalide.") from None
    data = event.data
    if data.type == "vault_credential.refresh_failed":
        connector = db.scalar(select(Connector).where(Connector.credential_id == data.id))
        if connector:
            connector.status = ConnectorStatus.ERROR
            connector.status_message = "Reconnexion nécessaire : le rafraîchissement OAuth a échoué."
            db.commit()
            log.info("Connecteur %s marqué en erreur (refresh_failed)", connector.id)
    return Response(status_code=204)

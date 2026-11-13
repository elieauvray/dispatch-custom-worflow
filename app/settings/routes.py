from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError, get_client
from app.config import AVAILABLE_MODELS, get_settings
from app.db import get_db
from app.deps import csrf_protect, get_user_settings, require_admin, require_user
from app.models import Connector, ConnectorStatus, OAuthApp, OAuthState, User, utcnow
from app.security import decrypt, encrypt, last4
from app.settings import mcp_oauth
from app.settings.providers import PROVIDERS, get_provider
from app.settings.service import client_for, sync_user_resources
from app.templating import flash, render

log = logging.getLogger(__name__)
router = APIRouter()
POLICIES = {"always_ask": "Toujours demander", "always_allow": "Toujours autoriser"}


def _back() -> RedirectResponse:
    return RedirectResponse("/settings", status_code=303)


def _callback_url(provider: str) -> str:
    return f"{get_settings().public_base_url}/oauth/{provider}/callback"


def _sync(request: Request, db: Session, user: User, force: bool = False) -> None:
    settings = get_user_settings(db, user)
    try:
        changes = sync_user_resources(db, user, settings, force=force)
    except ManagedAgentsError as exc:
        flash(request, f"Synchronisation Managed Agents impossible : {exc}", "error")
        return
    if changes:
        flash(request, "Ressources Managed Agents : " + ", ".join(changes) + ".", "success")


@router.get("/settings")
def settings_page(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    settings = get_user_settings(db, user)
    db.commit()
    connectors = {
        c.provider: c for c in db.scalars(select(Connector).where(Connector.user_id == user.id))
    }
    oauth_apps = {a.provider: a for a in db.scalars(select(OAuthApp))}
    return render(
        request,
        "settings.html",
        {
            "settings": settings,
            "models": AVAILABLE_MODELS,
            "providers": PROVIDERS,
            "connectors": connectors,
            "oauth_apps": oauth_apps,
            "policies": POLICIES,
            "callback_url": _callback_url,
        },
    )


@router.post("/settings/anthropic", dependencies=[Depends(csrf_protect)])
def save_anthropic(
    request: Request,
    api_key: str = Form(""),
    model: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    settings = get_user_settings(db, user)
    if model not in {m for m, _ in AVAILABLE_MODELS}:
        flash(request, "Modèle inconnu.", "error")
        return _back()
    api_key = api_key.strip()
    if api_key:
        try:
            get_client(api_key).check_api_key()
        except ManagedAgentsError as exc:
            flash(request, f"Clé API non enregistrée : {exc}", "error")
            return _back()
        settings.anthropic_api_key_encrypted = encrypt(api_key)
        settings.api_key_last4 = last4(api_key)
    elif not settings.anthropic_api_key_encrypted:
        flash(request, "La clé API Anthropic est obligatoire.", "error")
        return _back()
    settings.model = model
    db.commit()
    flash(request, "Paramètres Anthropic enregistrés.", "success")
    _sync(request, db, user)
    return _back()


@router.post("/settings/sync", dependencies=[Depends(csrf_protect)])
def resync(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    _sync(request, db, user, force=True)
    return _back()


def _connector(db: Session, user: User, provider: str) -> Connector | None:
    return db.scalar(
        select(Connector).where(Connector.user_id == user.id, Connector.provider == provider)
    )


def _replace_credential(db: Session, user: User, connector: Connector | None) -> None:
    settings = get_user_settings(db, user)
    if connector and connector.credential_id and settings.vault_id:
        client_for(settings).archive_credential(settings.vault_id, connector.credential_id)
        connector.credential_id = None


def _ensure_vault(request: Request, db: Session, user: User) -> str | None:
    settings = get_user_settings(db, user)
    if not settings.is_configured:
        flash(request, "Enregistrez d'abord votre clé API Anthropic.", "error")
        return None
    if not settings.vault_id:
        try:
            sync_user_resources(db, user, settings)
        except ManagedAgentsError as exc:
            flash(request, f"Vault indisponible : {exc}", "error")
            return None
    return settings.vault_id


@router.post("/settings/connectors/{provider}", dependencies=[Depends(csrf_protect)])
def save_connector(
    request: Request,
    provider: str,
    mcp_server_url: str = Form(...),
    token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    prov = get_provider(provider)
    if not prov:
        raise HTTPException(404)
    url, token = mcp_server_url.strip(), token.strip()
    if not url.startswith("https://") or not token:
        flash(request, f"{prov.label} : URL HTTPS et jeton obligatoires.", "error")
        return _back()
    ok, message = mcp_oauth.probe_mcp(url, token)
    if not ok:
        flash(request, f"{prov.label} : {message} Le connecteur n'a pas été enregistré.", "error")
        return _back()
    vault_id = _ensure_vault(request, db, user)
    if not vault_id:
        return _back()
    connector = _connector(db, user, provider)
    settings = get_user_settings(db, user)
    try:
        _replace_credential(db, user, connector)
        cred_id = client_for(settings).put_static_bearer(
            vault_id, url, token, f"{prov.label} ({user.login})"
        )
    except ManagedAgentsError as exc:
        flash(request, f"{prov.label} : envoi au vault impossible : {exc}", "error")
        return _back()
    if connector is None:
        connector = Connector(user_id=user.id, provider=provider, permission_policy="always_ask")
        db.add(connector)
    connector.mcp_server_url = url
    connector.auth_type = "static_bearer"
    connector.credential_id = cred_id
    connector.last4 = last4(token)
    connector.status = ConnectorStatus.CONFIGURED
    connector.status_message = message
    connector.updated_at = utcnow()
    db.commit()
    flash(request, f"{prov.label} configuré ({message}).", "success")
    _sync(request, db, user)
    return _back()


@router.post("/settings/connectors/{provider}/test", dependencies=[Depends(csrf_protect)])
def test_connector(
    request: Request, provider: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    prov = get_provider(provider)
    connector = _connector(db, user, provider)
    if not prov or not connector or not connector.credential_id:
        flash(request, "Connecteur non configuré.", "error")
        return _back()
    settings = get_user_settings(db, user)
    try:
        msg = client_for(settings).check_credential(
            settings.vault_id or "", connector.credential_id, connector.auth_type
        )
        reachable, probe_msg = mcp_oauth.probe_mcp(connector.mcp_server_url, None)
        if not reachable:
            raise ManagedAgentsError(probe_msg)
        connector.status = ConnectorStatus.CONFIGURED
        connector.status_message = f"{msg} {probe_msg}"
        flash(request, f"{prov.label} : {msg} {probe_msg}", "success")
    except ManagedAgentsError as exc:
        connector.status = ConnectorStatus.ERROR
        connector.status_message = str(exc)
        flash(request, f"{prov.label} : {exc}", "error")
    db.commit()
    return _back()


@router.post("/settings/connectors/{provider}/delete", dependencies=[Depends(csrf_protect)])
def delete_connector(
    request: Request, provider: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    connector = _connector(db, user, provider)
    if connector:
        try:
            _replace_credential(db, user, connector)
        except ManagedAgentsError as exc:
            flash(request, f"Archivage du credential impossible : {exc}", "error")
        db.delete(connector)
        db.commit()
        flash(request, "Connecteur supprimé.", "success")
        _sync(request, db, user)
    return _back()


@router.post("/settings/connectors/{provider}/permission", dependencies=[Depends(csrf_protect)])
def set_permission(
    request: Request,
    provider: str,
    permission_policy: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    connector = _connector(db, user, provider)
    if not connector or permission_policy not in POLICIES:
        flash(request, "Connecteur ou politique invalide.", "error")
        return _back()
    connector.permission_policy = permission_policy
    db.commit()
    flash(request, f"Permissions : {POLICIES[permission_policy]}.", "success")
    _sync(request, db, user)
    return _back()


# --- Applications OAuth (admin) ---------------------------------------------


@router.post("/settings/oauth-apps/{provider}", dependencies=[Depends(csrf_protect)])
def save_oauth_app(
    request: Request,
    provider: str,
    client_id: str = Form(...),
    client_secret: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    if not get_provider(provider) or not client_id.strip():
        flash(request, "Fournisseur ou client_id invalide.", "error")
        return _back()
    app = db.get(OAuthApp, provider) or OAuthApp(provider=provider)
    app.client_id = client_id.strip()
    if client_secret.strip():
        app.client_secret_encrypted = encrypt(client_secret.strip())
    app.dynamic = False
    db.add(app)
    db.commit()
    flash(request, "Application OAuth enregistrée.", "success")
    return _back()


@router.post("/settings/oauth-apps/{provider}/register", dependencies=[Depends(csrf_protect)])
def register_oauth_app(
    request: Request,
    provider: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    prov = get_provider(provider)
    if not prov or not prov.supports_dcr:
        flash(request, "Ce fournisseur ne propose pas l'enregistrement dynamique.", "error")
        return _back()
    try:
        server = mcp_oauth.discover(prov.default_url)
        client_id, client_secret = mcp_oauth.register_client(server, _callback_url(provider))
    except mcp_oauth.OAuthError as exc:
        flash(request, f"Enregistrement dynamique impossible : {exc}", "error")
        return _back()
    app = db.get(OAuthApp, provider) or OAuthApp(provider=provider)
    app.client_id = client_id
    app.client_secret_encrypted = encrypt(client_secret) if client_secret else None
    app.dynamic = True
    db.add(app)
    db.commit()
    flash(request, f"Client OAuth {prov.label} enregistré dynamiquement.", "success")
    return _back()


@router.post("/settings/oauth-apps/{provider}/delete", dependencies=[Depends(csrf_protect)])
def delete_oauth_app(
    request: Request, provider: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    app = db.get(OAuthApp, provider)
    if app:
        db.delete(app)
        db.commit()
        flash(request, "Application OAuth supprimée.", "success")
    return _back()


# --- Flux OAuth utilisateur -------------------------------------------------


@router.post("/oauth/{provider}/start", dependencies=[Depends(csrf_protect)])
def oauth_start(
    request: Request,
    provider: str,
    mcp_server_url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    prov = get_provider(provider)
    app = db.get(OAuthApp, provider)
    if not prov or not app:
        flash(request, "OAuth n'est pas configuré pour ce fournisseur.", "error")
        return _back()
    url = mcp_server_url.strip() or prov.default_url
    if not url.startswith("https://"):
        flash(request, "URL du serveur MCP invalide.", "error")
        return _back()
    try:
        server = mcp_oauth.discover(url)
    except mcp_oauth.OAuthError as exc:
        flash(request, str(exc), "error")
        return _back()
    verifier, challenge = mcp_oauth.new_pkce()
    state = secrets.token_urlsafe(32)
    db.execute(delete(OAuthState).where(OAuthState.created_at < utcnow() - timedelta(minutes=15)))
    db.add(
        OAuthState(
            state=state,
            user_id=user.id,
            provider=provider,
            code_verifier=verifier,
            token_endpoint=server.token_endpoint,
            client_id=app.client_id,
            resource=url,
        )
    )
    db.commit()
    request.session["oauth_methods"] = server.token_auth_methods
    return RedirectResponse(
        mcp_oauth.authorization_url(
            server, app.client_id, _callback_url(provider), state, challenge, url, prov.oauth_scopes
        ),
        status_code=303,
    )


@router.get("/oauth/{provider}/callback")
def oauth_callback(
    request: Request,
    provider: str,
    state: str = "",
    code: str = "",
    error: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    prov = get_provider(provider)
    pending = db.get(OAuthState, state) if state else None
    if not prov or not pending or pending.user_id != user.id or pending.provider != provider:
        flash(request, "Retour OAuth invalide ou expiré. Recommencez.", "error")
        return _back()
    db.delete(pending)
    db.commit()
    if error or not code:
        flash(request, f"Connexion {prov.label} annulée ({error or 'pas de code'}).", "error")
        return _back()
    app = db.get(OAuthApp, provider)
    secret = decrypt(app.client_secret_encrypted) if app and app.client_secret_encrypted else None
    try:
        tokens = mcp_oauth.exchange_code(
            pending.token_endpoint, code, pending.code_verifier, pending.client_id, secret,
            _callback_url(provider), pending.resource,
        )
    except mcp_oauth.OAuthError as exc:
        flash(request, str(exc), "error")
        return _back()
    vault_id = _ensure_vault(request, db, user)
    if not vault_id:
        return _back()
    url = pending.resource or prov.default_url
    refresh = None
    if tokens.refresh_token:
        refresh = {
            "client_id": pending.client_id,
            "refresh_token": tokens.refresh_token,
            "token_endpoint": pending.token_endpoint,
            "token_endpoint_auth": mcp_oauth.token_endpoint_auth(
                secret, list(request.session.pop("oauth_methods", []))
            ),
            "resource": url,
        }
    connector = _connector(db, user, provider)
    settings = get_user_settings(db, user)
    try:
        _replace_credential(db, user, connector)
        cred_id = client_for(settings).put_mcp_oauth(
            vault_id, url, tokens.access_token, tokens.expires_at, refresh,
            f"{prov.label} OAuth ({user.login})",
        )
    except ManagedAgentsError as exc:
        flash(request, f"Envoi au vault impossible : {exc}", "error")
        return _back()
    if connector is None:
        connector = Connector(user_id=user.id, provider=provider, permission_policy="always_ask")
        db.add(connector)
    connector.mcp_server_url = url
    connector.auth_type = "mcp_oauth"
    connector.credential_id = cred_id
    connector.last4 = last4(tokens.access_token)
    connector.status = ConnectorStatus.CONFIGURED
    connector.status_message = (
        "Connecté via OAuth, rafraîchissement géré par Anthropic."
        if refresh
        else "Connecté via OAuth sans refresh token : à reconnecter à l'expiration."
    )
    db.commit()
    flash(request, f"{prov.label} connecté via OAuth.", "success")
    _sync(request, db, user)
    return _back()

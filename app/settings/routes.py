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
POLICIES = {"always_ask": "Always ask", "always_allow": "Always allow"}


def _back(section: str | None = None) -> RedirectResponse:
    """Back to Settings, scrolled to the card the message belongs to."""
    return RedirectResponse(f"/settings#{section}" if section else "/settings", status_code=303)


def _callback_url(provider: str) -> str:
    return f"{get_settings().public_base_url}/oauth/{provider}/callback"


def _sync(request: Request, db: Session, user: User, force: bool = False, section: str = "resources") -> None:
    settings = get_user_settings(db, user)
    try:
        changes = sync_user_resources(db, user, settings, force=force)
    except ManagedAgentsError as exc:
        flash(request, f"Could not sync Managed Agents resources: {exc}", "error", section=section)
        return
    if changes:
        flash(request, "Managed Agents resources: " + ", ".join(changes) + ".", "success", section=section)


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
            "sectioned": True,
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
    sec = "anthropic"
    settings = get_user_settings(db, user)
    if model not in {m for m, _ in AVAILABLE_MODELS}:
        flash(request, "Unknown model.", "error", section=sec)
        return _back(sec)
    api_key = api_key.strip()
    if api_key:
        try:
            get_client(api_key).check_api_key()
        except ManagedAgentsError as exc:
            flash(request, f"API key not saved. {exc}", "error", section=sec)
            return _back(sec)
        settings.anthropic_api_key_encrypted = encrypt(api_key)
        settings.api_key_last4 = last4(api_key)
    elif not settings.anthropic_api_key_encrypted:
        flash(request, "An Anthropic API key is required.", "error", section=sec)
        return _back(sec)
    settings.model = model
    db.commit()
    flash(request, "Anthropic settings saved.", "success", section=sec)
    _sync(request, db, user, section=sec)
    return _back(sec)


@router.post("/settings/sync", dependencies=[Depends(csrf_protect)])
def resync(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    sec = "resources"
    _sync(request, db, user, force=True, section=sec)
    return _back(sec)


def _connector(db: Session, user: User, provider: str) -> Connector | None:
    return db.scalar(
        select(Connector).where(Connector.user_id == user.id, Connector.provider == provider)
    )


def _replace_credential(db: Session, user: User, connector: Connector | None) -> None:
    settings = get_user_settings(db, user)
    if connector and connector.credential_id and settings.vault_id:
        client_for(settings).archive_credential(settings.vault_id, connector.credential_id)
        connector.credential_id = None


def _ensure_vault(request: Request, db: Session, user: User, section: str) -> str | None:
    settings = get_user_settings(db, user)
    if not settings.is_configured:
        flash(request, "Save your Anthropic API key first.", "error", section=section)
        return None
    if not settings.vault_id:
        try:
            sync_user_resources(db, user, settings)
        except ManagedAgentsError as exc:
            flash(request, f"Vault unavailable: {exc}", "error", section=section)
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
    sec = f"connector-{provider}"
    prov = get_provider(provider)
    if not prov:
        raise HTTPException(404)
    url, token = mcp_server_url.strip(), token.strip()
    if not url.startswith("https://") or not token:
        flash(request, f"{prov.label}: an HTTPS URL and a token are required.", "error", section=sec)
        return _back(sec)
    ok, message = mcp_oauth.probe_mcp(url, token)
    if not ok:
        flash(request, f"{prov.label}: {message} The connector was not saved.", "error", section=sec)
        return _back(sec)
    vault_id = _ensure_vault(request, db, user, sec)
    if not vault_id:
        return _back(sec)
    connector = _connector(db, user, provider)
    settings = get_user_settings(db, user)
    try:
        _replace_credential(db, user, connector)
        cred_id = client_for(settings).put_static_bearer(
            vault_id, url, token, f"{prov.label} ({user.login})"
        )
    except ManagedAgentsError as exc:
        flash(request, f"{prov.label}: could not store the token in the vault. {exc}", "error", section=sec)
        return _back(sec)
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
    flash(request, f"{prov.label} connected. {message}", "success", section=sec)
    _sync(request, db, user, section=sec)
    return _back(sec)


@router.post("/settings/connectors/{provider}/test", dependencies=[Depends(csrf_protect)])
def test_connector(
    request: Request, provider: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    sec = f"connector-{provider}"
    prov = get_provider(provider)
    connector = _connector(db, user, provider)
    if not prov or not connector or not connector.credential_id:
        flash(request, "This connector is not configured.", "error", section=sec)
        return _back(sec)
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
        flash(request, f"{prov.label}: {msg} {probe_msg}", "success", section=sec)
    except ManagedAgentsError as exc:
        connector.status = ConnectorStatus.ERROR
        connector.status_message = str(exc)
        flash(request, f"{prov.label}: {exc}", "error", section=sec)
    db.commit()
    return _back(sec)


@router.post("/settings/connectors/{provider}/delete", dependencies=[Depends(csrf_protect)])
def delete_connector(
    request: Request, provider: str, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> Response:
    sec = f"connector-{provider}"
    connector = _connector(db, user, provider)
    if connector:
        try:
            _replace_credential(db, user, connector)
        except ManagedAgentsError as exc:
            flash(request, f"Could not archive the credential. {exc}", "error", section=sec)
        db.delete(connector)
        db.commit()
        flash(request, "Connector removed.", "success", section=sec)
        _sync(request, db, user, section=sec)
    return _back(sec)


@router.post("/settings/connectors/{provider}/permission", dependencies=[Depends(csrf_protect)])
def set_permission(
    request: Request,
    provider: str,
    permission_policy: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    sec = f"connector-{provider}"
    connector = _connector(db, user, provider)
    if not connector or permission_policy not in POLICIES:
        flash(request, "Invalid connector or policy.", "error", section=sec)
        return _back(sec)
    connector.permission_policy = permission_policy
    db.commit()
    flash(request, f"Tool permissions: {POLICIES[permission_policy]}.", "success", section=sec)
    _sync(request, db, user, section=sec)
    return _back(sec)


# --- OAuth apps (admin) ---------------------------------------------


@router.post("/settings/oauth-apps/{provider}", dependencies=[Depends(csrf_protect)])
def save_oauth_app(
    request: Request,
    provider: str,
    client_id: str = Form(...),
    client_secret: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    sec = "oauth-apps"
    if not get_provider(provider) or not client_id.strip():
        flash(request, "Invalid provider or client_id.", "error", section=sec)
        return _back(sec)
    app = db.get(OAuthApp, provider) or OAuthApp(provider=provider)
    app.client_id = client_id.strip()
    if client_secret.strip():
        app.client_secret_encrypted = encrypt(client_secret.strip())
    app.dynamic = False
    db.add(app)
    db.commit()
    flash(request, "OAuth app saved.", "success", section=sec)
    return _back(sec)


@router.post("/settings/oauth-apps/{provider}/register", dependencies=[Depends(csrf_protect)])
def register_oauth_app(
    request: Request,
    provider: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    sec = "oauth-apps"
    prov = get_provider(provider)
    if not prov or not prov.supports_dcr:
        flash(request, "This provider does not support dynamic client registration.", "error", section=sec)
        return _back(sec)
    try:
        server = mcp_oauth.discover(prov.default_url)
        client_id, client_secret = mcp_oauth.register_client(server, _callback_url(provider))
    except mcp_oauth.OAuthError as exc:
        flash(request, f"Dynamic client registration failed. {exc}", "error", section=sec)
        return _back(sec)
    app = db.get(OAuthApp, provider) or OAuthApp(provider=provider)
    app.client_id = client_id
    app.client_secret_encrypted = encrypt(client_secret) if client_secret else None
    app.dynamic = True
    db.add(app)
    db.commit()
    flash(request, f"{prov.label} OAuth client registered automatically.", "success", section=sec)
    return _back(sec)


@router.post("/settings/oauth-apps/{provider}/delete", dependencies=[Depends(csrf_protect)])
def delete_oauth_app(
    request: Request, provider: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> Response:
    sec = "oauth-apps"
    app = db.get(OAuthApp, provider)
    if app:
        db.delete(app)
        db.commit()
        flash(request, "OAuth app removed.", "success", section=sec)
    return _back(sec)


# --- User OAuth flow -------------------------------------------------


@router.post("/oauth/{provider}/start", dependencies=[Depends(csrf_protect)])
def oauth_start(
    request: Request,
    provider: str,
    mcp_server_url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    sec = f"connector-{provider}"
    prov = get_provider(provider)
    app = db.get(OAuthApp, provider)
    if not prov or not app:
        flash(request, "OAuth is not set up for this provider.", "error", section=sec)
        return _back(sec)
    url = mcp_server_url.strip() or prov.default_url
    if not url.startswith("https://"):
        flash(request, "Invalid MCP server URL.", "error", section=sec)
        return _back(sec)
    try:
        server = mcp_oauth.discover(url)
    except mcp_oauth.OAuthError as exc:
        flash(request, str(exc), "error", section=sec)
        return _back(sec)
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
    sec = f"connector-{provider}"
    prov = get_provider(provider)
    pending = db.get(OAuthState, state) if state else None
    if not prov or not pending or pending.user_id != user.id or pending.provider != provider:
        flash(request, "Invalid or expired OAuth response. Please try again.", "error", section=sec)
        return _back(sec)
    db.delete(pending)
    db.commit()
    if error or not code:
        flash(request, f"{prov.label} sign-in canceled ({error or 'no code returned'}).", "error", section=sec)
        return _back(sec)
    app = db.get(OAuthApp, provider)
    secret = decrypt(app.client_secret_encrypted) if app and app.client_secret_encrypted else None
    try:
        tokens = mcp_oauth.exchange_code(
            pending.token_endpoint, code, pending.code_verifier, pending.client_id, secret,
            _callback_url(provider), pending.resource,
        )
    except mcp_oauth.OAuthError as exc:
        flash(request, str(exc), "error", section=sec)
        return _back(sec)
    vault_id = _ensure_vault(request, db, user, sec)
    if not vault_id:
        return _back(sec)
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
        flash(request, f"Could not store the token in the vault. {exc}", "error", section=sec)
        return _back(sec)
    if connector is None:
        connector = Connector(user_id=user.id, provider=provider, permission_policy="always_ask")
        db.add(connector)
    connector.mcp_server_url = url
    connector.auth_type = "mcp_oauth"
    connector.credential_id = cred_id
    connector.last4 = last4(tokens.access_token)
    connector.status = ConnectorStatus.CONFIGURED
    connector.status_message = (
        "Connected with OAuth. Anthropic refreshes the token automatically."
        if refresh
        else "Connected with OAuth, without a refresh token: reconnect when it expires."
    )
    db.commit()
    flash(request, f"{prov.label} connected with OAuth.", "success", section=sec)
    _sync(request, db, user, section=sec)
    return _back(sec)

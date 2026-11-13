"""Client OAuth 2.1 minimal pour les serveurs MCP distants.

Découverte (RFC 9728 + RFC 8414), enregistrement dynamique (RFC 7591), PKCE S256,
échange du code. Aucun jeton n'est conservé : il est transmis au vault Anthropic.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

TIMEOUT = httpx.Timeout(10.0)


class OAuthError(Exception):
    pass


@dataclass
class AuthServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    token_auth_methods: list[str]


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        r = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def discover(mcp_url: str, client: httpx.Client | None = None) -> AuthServer:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        parts = urlsplit(mcp_url)
        path = parts.path.rstrip("/")
        origin = _origin(mcp_url)
        as_url: str | None = None
        # 1. Protected Resource Metadata (RFC 9728)
        for candidate in (
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ):
            prm = _get_json(client, candidate)
            if prm and prm.get("authorization_servers"):
                as_url = str(prm["authorization_servers"][0]).rstrip("/")
                break
        as_url = as_url or origin
        # 2. Authorization Server Metadata (RFC 8414 / OIDC)
        as_parts = urlsplit(as_url)
        as_origin, as_path = _origin(as_url), as_parts.path.rstrip("/")
        meta = None
        for candidate in (
            f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
            f"{as_origin}/.well-known/openid-configuration{as_path}",
            f"{as_url}/.well-known/openid-configuration",
        ):
            meta = _get_json(client, candidate)
            if meta and meta.get("token_endpoint"):
                break
        if not meta or not meta.get("authorization_endpoint"):
            raise OAuthError("Could not discover the OAuth authorization server for this MCP server.")
        return AuthServer(
            issuer=str(meta.get("issuer", as_url)),
            authorization_endpoint=str(meta["authorization_endpoint"]),
            token_endpoint=str(meta["token_endpoint"]),
            registration_endpoint=meta.get("registration_endpoint"),
            token_auth_methods=list(meta.get("token_endpoint_auth_methods_supported") or []),
        )
    finally:
        if own:
            client.close()


def register_client(
    server: AuthServer, redirect_uri: str, client_name: str = "Dispatch (Claude Managed Agents)"
) -> tuple[str, str | None]:
    if not server.registration_endpoint:
        raise OAuthError("This server does not support dynamic client registration.")
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(server.registration_endpoint, json=body)
    if r.status_code not in (200, 201):
        raise OAuthError(f"Dynamic client registration was refused ({r.status_code}).")
    data = r.json()
    return str(data["client_id"]), data.get("client_secret")


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def authorization_url(
    server: AuthServer,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    resource: str,
    scopes: list[str],
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    sep = "&" if "?" in server.authorization_endpoint else "?"
    return f"{server.authorization_endpoint}{sep}{urlencode(params)}"


def token_endpoint_auth(client_secret: str | None, methods: list[str]) -> dict[str, str]:
    if not client_secret:
        return {"type": "none"}
    if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
        return {"type": "client_secret_post", "client_secret": client_secret}
    return {"type": "client_secret_basic", "client_secret": client_secret}


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None


def exchange_code(
    token_endpoint: str,
    code: str,
    verifier: str,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    resource: str | None,
) -> TokenSet:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if resource:
        data["resource"] = resource
    auth = None
    if client_secret:
        auth = (client_id, client_secret)
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(token_endpoint, data=data, auth=auth, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"The OAuth code exchange was refused ({r.status_code}).")
    payload = r.json()
    if "access_token" not in payload:
        raise OAuthError("The OAuth response has no access_token.")
    expires_at = None
    if payload.get("expires_in"):
        expires_at = datetime.now(UTC) + timedelta(seconds=int(payload["expires_in"]))
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
        scope=payload.get("scope"),
    )


def probe_mcp(url: str, token: str | None) -> tuple[bool, str]:
    """Handshake MCP `initialize`. Retourne (ok, message). Le jeton n'est jamais journalisé."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "dispatch", "version": "1.0"},
        },
    }
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            r = c.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Server unreachable ({exc.__class__.__name__})."
    if r.status_code == 200:
        return True, "MCP handshake succeeded."
    if r.status_code in (401, 403):
        if token:
            return False, f"The MCP server rejected the token ({r.status_code})."
        return True, "MCP server reachable (authentication required, as expected)."
    return False, f"Unexpected response from the MCP server ({r.status_code})."

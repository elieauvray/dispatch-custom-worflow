"""Toute la logique Claude Managed Agents est isolée ici.

Le reste de l'application ne manipule que des `dict` (événements sérialisés) et
des identifiants. Les tests remplacent `client_factory` par un faux client.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import anthropic

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es l'assistant « Dispatch » d'une équipe produit et technique.
Tu as accès, via des serveurs MCP, aux outils de l'utilisateur (Linear, GitHub, Jira/Confluence selon sa configuration).

Règles :
- Réponds dans la langue de l'utilisateur (français par défaut).
- Utilise les outils MCP disponibles pour lire ou modifier les données plutôt que d'inventer.
- Quand une information te manque pour continuer, pose UNE question claire et termine ton message par cette question.
- Quand tu as terminé, donne un résultat final structuré et concis (Markdown).
- Si un outil est refusé par l'utilisateur, tiens compte du motif et propose une alternative.
"""

# Nom des serveurs MCP dans l'agent (référencés par mcp_toolset.mcp_server_name).
MCP_SERVER_NAMES = {"linear": "linear", "github": "github", "atlassian": "atlassian"}


class ManagedAgentsError(Exception):
    """Erreur côté API Anthropic, avec un message affichable (sans secret)."""


@dataclass
class ConnectorSpec:
    provider: str
    url: str
    permission_policy: str  # always_ask | always_allow


@dataclass
class ResourceState:
    agent_id: str | None = None
    agent_version: int | None = None
    agent_config_hash: str | None = None
    environment_id: str | None = None
    vault_id: str | None = None


@dataclass
class SyncResult:
    state: ResourceState
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    vault_recreated: bool = False


def build_agent_config(model: str, connectors: list[ConnectorSpec], login: str) -> dict[str, Any]:
    """Configuration de l'agent : modèle + system prompt + serveurs MCP + toolsets. Aucun secret."""
    mcp_servers: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "auto"}},
            "configs": [
                {"name": "web_fetch", "enabled": False},
                {"name": "web_search", "enabled": False},
            ],
        }
    ]
    for spec in sorted(connectors, key=lambda c: c.provider):
        name = MCP_SERVER_NAMES.get(spec.provider, spec.provider)
        mcp_servers.append({"type": "url", "name": name, "url": spec.url})
        policy = "always_allow" if spec.permission_policy == "always_allow" else "always_ask"
        tools.append(
            {
                "type": "mcp_toolset",
                "mcp_server_name": name,
                "default_config": {"permission_policy": {"type": policy}},
            }
        )
    return {
        "name": f"Dispatch – {login}",
        "model": model,
        "system": SYSTEM_PROMPT,
        "mcp_servers": mcp_servers,
        "tools": tools,
        "metadata": {"app": "dispatch", "user": login},
    }


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def event_to_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]
    raise TypeError(f"Événement non sérialisable : {type(event)!r}")


def _api_error_message(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Clé API Anthropic refusée (401). Vérifiez la clé dans Settings."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Accès refusé par l'API Anthropic (403) : Managed Agents est-il activé sur ce workspace ?"
    if isinstance(exc, anthropic.NotFoundError):
        return "Ressource introuvable côté Anthropic (404)."
    if isinstance(exc, anthropic.RateLimitError):
        return "Limite de débit Anthropic atteinte (429). Réessayez dans un instant."
    if isinstance(exc, anthropic.APIStatusError):
        try:
            detail = exc.body.get("error", {}).get("message")  # type: ignore[union-attr]
        except AttributeError:
            detail = None
        return f"Erreur API Anthropic ({exc.status_code}) : {detail or exc.__class__.__name__}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Impossible de joindre l'API Anthropic (réseau)."
    return f"Erreur inattendue : {exc.__class__.__name__}"


class ManagedAgentsAPI(Protocol):
    """Interface utilisée par l'application (implémentée par le vrai client et par les faux)."""

    def check_api_key(self) -> None: ...
    def sync_resources(
        self, state: ResourceState, model: str, connectors: list[ConnectorSpec], login: str,
        force: bool = False,
    ) -> SyncResult: ...
    def archive_vault(self, vault_id: str) -> None: ...
    def put_static_bearer(self, vault_id: str, url: str, token: str, display_name: str) -> str: ...
    def put_mcp_oauth(
        self, vault_id: str, url: str, access_token: str, expires_at: datetime | None,
        refresh: dict[str, Any] | None, display_name: str,
    ) -> str: ...
    def archive_credential(self, vault_id: str, credential_id: str) -> None: ...
    def check_credential(self, vault_id: str, credential_id: str, auth_type: str) -> str: ...
    def create_session(
        self, agent_id: str, agent_version: int | None, environment_id: str,
        vault_ids: list[str], title: str,
    ) -> str: ...
    def send_user_message(self, session_id: str, text: str) -> None: ...
    def send_tool_confirmations(
        self, session_id: str, decisions: list[tuple[str, bool, str | None]]
    ) -> None: ...
    def interrupt(self, session_id: str) -> None: ...
    def list_events(self, session_id: str) -> list[dict[str, Any]]: ...
    def open_stream(self, session_id: str) -> Any: ...
    def session_status(self, session_id: str) -> str: ...


class ManagedAgentsClient:
    """Implémentation réelle au-dessus du SDK officiel `anthropic` (API beta)."""

    def __init__(self, api_key: str, *, stream_read_timeout: float = 120.0) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self._stream_timeout = stream_read_timeout

    # -- utilitaires --------------------------------------------------------
    @contextmanager
    def _wrap(self) -> Iterator[None]:
        try:
            yield
        except anthropic.APIError as exc:
            raise ManagedAgentsError(_api_error_message(exc)) from exc

    def _exists(self, fn: Callable[[], Any]) -> bool:
        try:
            obj = fn()
        except anthropic.NotFoundError:
            return False
        return getattr(obj, "archived_at", None) is None

    # -- clé et ressources ---------------------------------------------------
    def check_api_key(self) -> None:
        with self._wrap():
            self._client.models.list(limit=1)

    def sync_resources(
        self,
        state: ResourceState,
        model: str,
        connectors: list[ConnectorSpec],
        login: str,
        force: bool = False,
    ) -> SyncResult:
        """Crée ou met à jour agent, environnement et vault. Idempotent."""
        beta = self._client.beta
        result = SyncResult(state=ResourceState(**state.__dict__))
        st = result.state
        with self._wrap():
            # Environnement
            if not st.environment_id or not self._exists(
                lambda: beta.environments.retrieve(st.environment_id)  # type: ignore[arg-type]
            ):
                env = beta.environments.create(
                    name=f"dispatch-{login}",
                    config={
                        "type": "cloud",
                        "networking": {
                            "type": "limited",
                            "allow_mcp_servers": True,
                            "allow_package_managers": True,
                        },
                    },
                    metadata={"app": "dispatch", "user": login},
                )
                st.environment_id = env.id
                result.created.append("environnement")

            # Vault (un par utilisateur)
            if not st.vault_id or not self._exists(
                lambda: beta.vaults.retrieve(st.vault_id)  # type: ignore[arg-type]
            ):
                if st.vault_id:
                    result.vault_recreated = True
                vault = beta.vaults.create(
                    display_name=f"Dispatch – {login}",
                    metadata={"app": "dispatch", "user": login},
                )
                st.vault_id = vault.id
                result.created.append("vault")

            # Agent
            config = build_agent_config(model, connectors, login)
            digest = config_hash(config)
            agent_ok = bool(st.agent_id) and self._exists(
                lambda: beta.agents.retrieve(st.agent_id)  # type: ignore[arg-type]
            )
            if not agent_ok:
                agent = beta.agents.create(**config)
                st.agent_id, st.agent_version = agent.id, agent.version
                st.agent_config_hash = digest
                result.created.append("agent")
            elif force or digest != st.agent_config_hash:
                agent = beta.agents.update(st.agent_id, **config)  # type: ignore[arg-type]
                st.agent_version = agent.version
                st.agent_config_hash = digest
                result.updated.append("agent")
        return result

    def archive_vault(self, vault_id: str) -> None:
        with self._wrap():
            try:
                self._client.beta.vaults.archive(vault_id)
            except anthropic.NotFoundError:
                pass

    # -- credentials ---------------------------------------------------------
    def put_static_bearer(self, vault_id: str, url: str, token: str, display_name: str) -> str:
        with self._wrap():
            cred = self._client.beta.vaults.credentials.create(
                vault_id,
                display_name=display_name,
                auth={"type": "static_bearer", "mcp_server_url": url, "token": token},
            )
        return cred.id

    def put_mcp_oauth(
        self,
        vault_id: str,
        url: str,
        access_token: str,
        expires_at: datetime | None,
        refresh: dict[str, Any] | None,
        display_name: str,
    ) -> str:
        auth: dict[str, Any] = {
            "type": "mcp_oauth",
            "mcp_server_url": url,
            "access_token": access_token,
        }
        if expires_at:
            auth["expires_at"] = expires_at
        if refresh:
            auth["refresh"] = refresh
        with self._wrap():
            cred = self._client.beta.vaults.credentials.create(
                vault_id, display_name=display_name, auth=auth  # type: ignore[arg-type]
            )
        return cred.id

    def archive_credential(self, vault_id: str, credential_id: str) -> None:
        with self._wrap():
            try:
                self._client.beta.vaults.credentials.archive(credential_id, vault_id=vault_id)
            except anthropic.NotFoundError:
                pass

    def check_credential(self, vault_id: str, credential_id: str, auth_type: str) -> str:
        """Retourne un message lisible ; lève ManagedAgentsError si le credential est invalide."""
        with self._wrap():
            cred = self._client.beta.vaults.credentials.retrieve(credential_id, vault_id=vault_id)
            if getattr(cred, "archived_at", None):
                raise ManagedAgentsError("Le credential a été archivé dans le vault.")
            if auth_type == "mcp_oauth":
                res = self._client.beta.vaults.credentials.mcp_oauth_validate(
                    credential_id, vault_id=vault_id
                )
                data = event_to_dict(res)
                status = data.get("status") or data.get("result") or "ok"
                if str(status).lower() not in {"ok", "valid", "success", "succeeded"}:
                    raise ManagedAgentsError(f"Validation OAuth : {status}")
                return "Jeton OAuth validé par Anthropic."
        return "Credential présent dans le vault."

    # -- sessions ------------------------------------------------------------
    def create_session(
        self,
        agent_id: str,
        agent_version: int | None,
        environment_id: str,
        vault_ids: list[str],
        title: str,
    ) -> str:
        agent: Any = (
            {"type": "agent", "id": agent_id, "version": agent_version}
            if agent_version
            else agent_id
        )
        with self._wrap():
            session = self._client.beta.sessions.create(
                agent=agent,
                environment_id=environment_id,
                vault_ids=vault_ids,
                title=title[:200],
            )
        return session.id

    def send_user_message(self, session_id: str, text: str) -> None:
        with self._wrap():
            self._client.beta.sessions.events.send(
                session_id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
            )

    def send_tool_confirmations(
        self, session_id: str, decisions: list[tuple[str, bool, str | None]]
    ) -> None:
        events: list[dict[str, Any]] = []
        for event_id, allow, deny_message in decisions:
            ev: dict[str, Any] = {
                "type": "user.tool_confirmation",
                "tool_use_id": event_id,
                "result": "allow" if allow else "deny",
            }
            if not allow and deny_message:
                ev["deny_message"] = deny_message
            events.append(ev)
        with self._wrap():
            self._client.beta.sessions.events.send(session_id, events=events)  # type: ignore[arg-type]

    def interrupt(self, session_id: str) -> None:
        with self._wrap():
            self._client.beta.sessions.events.send(
                session_id, events=[{"type": "user.interrupt"}]
            )

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._wrap():
            page = self._client.beta.sessions.events.list(session_id, limit=1000, order="asc")
            return [event_to_dict(e) for e in page]

    @contextmanager
    def open_stream(self, session_id: str) -> Iterator[Iterator[dict[str, Any]]]:
        with self._wrap():
            stream = self._client.beta.sessions.events.stream(
                session_id, timeout=self._stream_timeout
            )
        try:
            yield (event_to_dict(e) for e in stream)
        finally:
            stream.close()

    def session_status(self, session_id: str) -> str:
        with self._wrap():
            return str(self._client.beta.sessions.retrieve(session_id).status)


def _default_factory(api_key: str) -> ManagedAgentsAPI:
    return ManagedAgentsClient(api_key)


# Remplaçable dans les tests.
client_factory: Callable[[str], ManagedAgentsAPI] = _default_factory


def get_client(api_key: str) -> ManagedAgentsAPI:
    return client_factory(api_key)

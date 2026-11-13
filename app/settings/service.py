"""Synchronisation des ressources Managed Agents d'un utilisateur."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.anthropic_client import (
    ConnectorSpec,
    ManagedAgentsAPI,
    ManagedAgentsError,
    ResourceState,
    get_client,
)
from app.models import Connector, ConnectorStatus, User, UserSettings, utcnow
from app.security import decrypt

log = logging.getLogger(__name__)


def client_for(settings: UserSettings) -> ManagedAgentsAPI:
    if not settings.anthropic_api_key_encrypted:
        raise ManagedAgentsError("No Anthropic API key configured.")
    return get_client(decrypt(settings.anthropic_api_key_encrypted))


def active_connectors(db: Session, user: User) -> list[Connector]:
    return list(
        db.scalars(
            select(Connector).where(
                Connector.user_id == user.id, Connector.credential_id.is_not(None)
            ).order_by(Connector.provider)
        )
    )


def _specs(connectors: list[Connector]) -> list[ConnectorSpec]:
    return [ConnectorSpec(c.provider, c.mcp_server_url, c.permission_policy) for c in connectors]


def sync_user_resources(db: Session, user: User, settings: UserSettings, force: bool = False) -> list[str]:
    """Crée/met à jour agent, environnement, vault. Retourne un résumé des changements.

    Lève ManagedAgentsError (message affichable) en cas d'échec.
    """
    client = client_for(settings)
    state = ResourceState(
        agent_id=settings.agent_id,
        agent_version=settings.agent_version,
        agent_config_hash=None if force else settings.agent_config_hash,
        environment_id=settings.environment_id,
        vault_id=settings.vault_id,
    )
    try:
        connectors = active_connectors(db, user)
        result = client.sync_resources(state, settings.model, _specs(connectors), user.login, force)
        if result.vault_recreated:
            # Les credentials de l'ancien vault sont perdus : on le signale et on retire les serveurs.
            for c in connectors:
                c.credential_id = None
                c.status = ConnectorStatus.ERROR
                c.status_message = "The vault was recreated: set up this connector again."
            db.flush()
            result = client.sync_resources(result.state, settings.model, [], user.login, False)
    except ManagedAgentsError as exc:
        settings.last_sync_error = str(exc)
        db.commit()
        raise
    st = result.state
    settings.agent_id = st.agent_id
    settings.agent_version = st.agent_version
    settings.agent_config_hash = st.agent_config_hash
    settings.environment_id = st.environment_id
    settings.vault_id = st.vault_id
    settings.last_sync_at = utcnow()
    settings.last_sync_error = None
    db.commit()
    changes = [f"{x} created" for x in result.created] + [f"{x} updated" for x in result.updated]
    return changes


def resources_ready(settings: UserSettings | None) -> bool:
    return bool(
        settings
        and settings.is_configured
        and settings.agent_id
        and settings.environment_id
        and settings.vault_id
    )

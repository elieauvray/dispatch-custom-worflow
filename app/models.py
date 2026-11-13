from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_STATUS_LABELS: dict[str, str] = {
    RunStatus.RUNNING: "en cours",
    RunStatus.WAITING_INPUT: "en attente de réponse",
    RunStatus.WAITING_APPROVAL: "en attente de validation",
    RunStatus.COMPLETED: "terminé",
    RunStatus.FAILED: "échoué",
    RunStatus.CANCELLED: "annulé",
}

# Statuts où le worker doit piloter activement la session.
DRIVEN_STATUSES = {RunStatus.RUNNING}
# Statuts où l'utilisateur a encore la main sur la session.
OPEN_STATUSES = {
    RunStatus.RUNNING,
    RunStatus.WAITING_INPUT,
    RunStatus.WAITING_APPROVAL,
    RunStatus.COMPLETED,
}


class ConnectorStatus(enum.StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    ERROR = "error"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    settings: Mapped[UserSettings | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    connectors: Mapped[list[Connector]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    anthropic_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_last4: Mapped[str | None] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(String(64), default="claude-opus-5-5")
    agent_id: Mapped[str | None] = mapped_column(String(128))
    agent_version: Mapped[int | None] = mapped_column(Integer)
    agent_config_hash: Mapped[str | None] = mapped_column(String(64))
    environment_id: Mapped[str | None] = mapped_column(String(128))
    vault_id: Mapped[str | None] = mapped_column(String(128))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="settings")

    @property
    def is_configured(self) -> bool:
        return bool(self.anthropic_api_key_encrypted)


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_connector_user_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    mcp_server_url: Mapped[str] = mapped_column(String(512))
    auth_type: Mapped[str] = mapped_column(String(32))  # static_bearer | mcp_oauth
    credential_id: Mapped[str | None] = mapped_column(String(128))
    last4: Mapped[str | None] = mapped_column(String(8))
    permission_policy: Mapped[str] = mapped_column(String(32), default="always_ask")
    status: Mapped[str] = mapped_column(String(32), default=ConnectorStatus.CONFIGURED)
    status_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="connectors")


class OAuthApp(Base):
    """Client OAuth configuré par l'admin pour un fournisseur MCP."""

    __tablename__ = "oauth_apps"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # Rempli par l'enregistrement dynamique de client (RFC 7591) si utilisé.
    dynamic: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    code_verifier: Mapped[str] = mapped_column(String(128))
    token_endpoint: Mapped[str] = mapped_column(String(512))
    client_id: Mapped[str] = mapped_column(String(255))
    resource: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_workflow_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    reference_result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship()
    runs: Mapped[list[WorkflowRun]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )


class WorkflowRun(Base):
    """Une exécution : workflow (workflow_id renseigné) ou prompt libre (workflow_id NULL)."""

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.RUNNING, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    pending_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    skills_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    content_snapshot: Mapped[str] = mapped_column(Text)
    workflow_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    initial_message: Mapped[str] = mapped_column(Text)
    initial_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    # Réservation par le worker (reprise après redémarrage)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[Workflow | None] = relationship(back_populates="runs")
    user: Mapped[User] = relationship()
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunEvent.id",
        passive_deletes=True,
    )

    @property
    def status_label(self) -> str:
        return RUN_STATUS_LABELS.get(self.status, self.status)

    @property
    def duration_seconds(self) -> int | None:
        if not self.started_at:
            return None
        end = self.finished_at or utcnow()
        start = self.started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return int((end - start).total_seconds())


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "anthropic_event_id", name="uq_run_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    anthropic_event_id: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[WorkflowRun] = relationship(back_populates="events")


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), index=True)  # login ou IP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

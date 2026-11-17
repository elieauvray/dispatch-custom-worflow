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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

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
    RunStatus.RUNNING: "running",
    RunStatus.WAITING_INPUT: "waiting for your reply",
    RunStatus.WAITING_APPROVAL: "waiting for approval",
    RunStatus.COMPLETED: "completed",
    RunStatus.FAILED: "failed",
    RunStatus.CANCELLED: "canceled",
}

# Workflow progress shown instead of "completed" when Maestro's session ended but the workflow did not.
PROGRESS_LABELS: dict[str, str] = {
    "ongoing": "ongoing",
    "blocked": "workflow blocked",
    "done": "completed",
    "continued": "continued",
}

# Statuses in which the worker must actively drive the session.
DRIVEN_STATUSES = {RunStatus.RUNNING}
# Statuses in which the user still has control of the session.
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
    # GitHub webhook that wakes Maestro when a pull request is merged
    github_hook_id: Mapped[str | None] = mapped_column(String(48), index=True, unique=True)
    github_hook_secret_encrypted: Mapped[str | None] = mapped_column(Text)

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
    """OAuth client configured by the admin for an MCP provider."""

    __tablename__ = "oauth_apps"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # Filled in by dynamic client registration (RFC 7591) if used.
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
    # Maestro: the user's description, the generated design (steps per actor) and the schedule
    description: Mapped[str | None] = mapped_column(Text)
    design_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    design_status: Mapped[str | None] = mapped_column(String(16))  # queued|generating|needs_answers|blocked|ready|failed
    design_error: Mapped[str | None] = mapped_column(Text)
    design_feedback: Mapped[str | None] = mapped_column(Text)
    # Clarification questions asked before the design and the user's answers
    clarifications: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    design_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Private to its owner (default) or shared: every user can see it and run it with their own connectors.
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Live progress of the design generation (stage, characters written) and the timings of the last one.
    design_progress: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    schedule_cron: Mapped[str | None] = mapped_column(String(64))
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    budget_cents: Mapped[int | None] = mapped_column(Integer)
    deployment_id: Mapped[str | None] = mapped_column(String(128))
    deployment_status: Mapped[str | None] = mapped_column(String(16))  # active | paused
    last_woken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fingerprint of the system instructions the current design was generated with (stale when they change).
    instructions_hash: Mapped[str | None] = mapped_column(String(64))
    # Diagram of the process uploaded or pasted on the describe page (image or PDF), read by Claude.
    diagram_media_type: Mapped[str | None] = mapped_column(String(64))
    diagram_data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    # Predicted plan (diagram) generated by Claude from the request
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    plan_status: Mapped[str | None] = mapped_column(String(16))  # generating | ready | failed
    plan_error: Mapped[str | None] = mapped_column(Text)
    plan_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def empty_config(self) -> list[str]:
        """Config keys of the design that have no value yet (Maestro never guesses them)."""
        return [str(c.get("key")) for c in (self.design_json or {}).get("config") or []
                if not str(c.get("value") or "").strip()]

    user: Mapped[User] = relationship()
    runs: Mapped[list[WorkflowRun]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True
    )


class WorkflowRun(Base):
    """A run: workflow (workflow_id set) or free-form prompt (workflow_id NULL)."""

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
    # Claim by the worker (resume after restart)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | schedule
    deployment_run_id: Mapped[str | None] = mapped_column(String(128), index=True, unique=True)
    # Frozen copy of the Maestro design used by this run (diagram + step ids)
    design_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Workflow progress when the session ended (see app/runs/progress.py): ongoing | blocked | done | continued
    progress: Mapped[str | None] = mapped_column(String(16))
    continued_by_run_id: Mapped[int | None] = mapped_column(Integer)
    # Predicted plan (diagram) generated by Claude from the request
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    plan_status: Mapped[str | None] = mapped_column(String(16))  # generating | ready | failed
    plan_error: Mapped[str | None] = mapped_column(Text)
    plan_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    def display_status(self) -> str:
        """Badge class: the workflow's progress once Maestro's session has ended, else the session status."""
        if self.status == RunStatus.COMPLETED and self.progress in PROGRESS_LABELS:
            return self.progress
        return str(self.status)

    @property
    def display_label(self) -> str:
        if self.status == RunStatus.COMPLETED and self.progress in PROGRESS_LABELS:
            if self.progress == "continued" and self.continued_by_run_id:
                return f"continued in #{self.continued_by_run_id}"
            return PROGRESS_LABELS[self.progress]
        return self.status_label

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
    key: Mapped[str] = mapped_column(String(255), index=True)  # login or IP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
